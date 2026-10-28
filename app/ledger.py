"""Журнал обработки: в базе сервиса (db.py: таблица records), строка на запись.

Единственный источник ответа на вопрос «этот файл уже обрабатывали?».
Перезапуск контейнера не должен приводить к повторной обработке и повторной
оплате, поэтому запись в журнал появляется для любого исхода — и успеха,
и «нет звука», и ошибки.

Файл с ошибкой или без протокола автоматика берёт снова, но редко и
ограниченно: через полчаса после первого сбоя, через час после второго,
а после третьего подряд — уже только администратор. Сбой провайдера на
час иначе оставил бы без результата все записи, пришедшие за этот час,
а повтор каждые десять секунд жёг бы деньги по кругу.

Строки журнала сервис держит ещё и в памяти: список записей, поиск, реестр поручений и
сканер спрашивают журнал на каждый запрос и на каждый обход, тысячи строк разом. Правда —
в базе: память читается из неё при запуске и меняется только вместе с ней, тем же вызовом;
если транзакция, в которую вошла правка, откатилась, память перечитывается из базы. Пишет
журнал обработки один процесс — сам сервис.

Рядом с результатами пишется маркер <stem>.done.json — копия строки журнала: её видно в
файловом менеджере без сервиса.
"""
from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path

from . import db, usage

MARKER_SUFFIX = ".done.json"

RETRY_STATUSES = ("error", "no_summary")   # исходы, после которых стоит повторить
MAX_FAILURES = 3                           # сбоев подряд, после которых — только вручную
RETRY_AFTER_SEC = 1800                     # пауза после первого сбоя, дальше растёт
# раздел «Поручения прошлого совещания» у записи убран вручную: пересборка протокола его не
# возвращает (pipeline.previous_tasks). Ставит и снимает человек кнопкой у раздела (web.py)
REMOVED = "prev_tasks_removed"
# Исходник готовой записи с тех пор изменился (докопировали, перезаписали): протокол и расшифровка сделаны
# по прежнему файлу. Ставит сканер (watcher), снимает полный прогон заново или возврат прежнего файла.
# Исходник, которого нет на диске, сюда не пишется: это видно по последнему обходу (Service.missing).
SOURCE_STATE = "source_state"
SOURCE_CHANGED = "changed"
SOURCE_MISSING = "missing"
# Последний прогон записи отменил администратор (watcher.Service.cancel): кто и когда. По расписанию такая
# запись не повторяется — следующий прогон только вручную; он же отметку и снимает.
CANCELLED = "cancelled"


def said(record: dict | None) -> str | None:
    """О чём по записи уже сообщено письмом автору загрузки: "ready", "failed" или ни о чём
    (правило — в notify.py). Готовность записи, которая сейчас готова, считается сообщённой
    и без отметки: так обстоит дело у записей, обработанных до появления писем, и у доведённых
    до конца ручным запуском. Иначе повтор после неудачной пересборки прислал бы автору
    «запись готова» через месяц после того, как её прочли."""
    if not record:
        return None
    return "ready" if record.get("status") == "ok" else record.get("announced")


class Ledger:
    def __init__(self, base: db.Database, log=lambda s: None):
        self.db = base
        self.log = log
        self.records: dict[str, dict] = {}
        # растёт при каждом изменении: страница по нему понимает, что историю
        # можно не перекачивать
        self.version = 0
        # Копию в памяти меняют и читают с разных сторон: обработчики и действия с записями — из
        # событийного цикла, сканер и страница статуса — из потоков. Замок — только для неё:
        # запись в базу защищает транзакция. Порядок всегда один — сначала транзакция, потом
        # замок: иначе поток с замком ждал бы транзакцию, а поток с транзакцией — замок.
        self._lock = threading.RLock()
        self._load()
        if self.records:
            self.log(f"записей в журнале обработки: {len(self.records)}")

    def _load(self) -> None:
        with self._lock:
            self.records = {row["file"]: json.loads(row["data"])
                            for row in self.db.rows("SELECT file, data FROM records")}
            self.version += 1

    def _store(self, con, record: dict) -> None:
        # не REPLACE: он стёр бы строку и вместе с ней отметки поручений записи
        con.execute("INSERT INTO records (file, status, finished_at, data) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (file) DO UPDATE SET status = excluded.status, finished_at = excluded.finished_at, "
                    "data = excluded.data",
                    (record["file"], record.get("status"), record.get("finished_at"),
                     json.dumps(record, ensure_ascii=False)))

    def get(self, rel: str) -> dict | None:
        return self.records.get(rel)

    def is_processed(self, rel: str) -> bool:
        """Есть запись любого исхода — автоматика сама этот файл не возьмёт,
        разве что пришло время повтора после сбоя (retry_due)."""
        return rel in self.records

    def retry_due(self, rel: str) -> bool:
        """Пора ли автоматике повторить файл после сбоя.

        Пауза растёт с каждым сбоем подряд: 30 минут, потом час. После
        MAX_FAILURES сбоев подряд автоматика сдаётся — дальше повторяет
        администратор, посмотрев на ошибку. Прогон, отменённый администратором, не повторяется вовсе.
        """
        rec = self.records.get(rel)
        if rec is None or rec.get("status") not in RETRY_STATUSES or rec.get(CANCELLED):
            return False
        failures = int(rec.get("failures") or 1)
        if failures >= MAX_FAILURES:
            return False
        try:
            finished = datetime.datetime.fromisoformat(rec.get("finished_at") or "")
        except ValueError:
            return False
        waited = (datetime.datetime.now().astimezone() - finished).total_seconds()
        return waited >= RETRY_AFTER_SEC * failures

    def put(self, record: dict) -> None:
        """Запись по файлу заменяется, но потраченное накапливается.

        Администратор может прогнать файл повторно с force, и каждый такой
        прогон стоит денег. Если бы сводка показывала стоимость только
        последнего прогона, расход в отчёте оказался бы занижен.
        """
        with self.db.write() as con, self._lock:
            prev = self.records.get(record["file"]) or {}
            record["runs"] = int(prev.get("runs") or 0) + 1
            # Когда запись обработана впервые: по этому моменту она стоит в ряду совещаний
            # папки, если разбор не назвал дату. Пересборка его не меняет.
            record["first_done_at"] = (prev.get("first_done_at") or prev.get("finished_at")
                                       or record.get("finished_at"))
            record["failures"] = (int(prev.get("failures") or 0) + 1
                                  if record.get("status") in RETRY_STATUSES else 0)
            # о чём автору уже написано — переживает повторы и пересборки
            record["announced"] = said(prev)
            # и раздел, убранный человеком, тоже: пересборка его не вернула, не вернёт и следующая
            if prev.get(REMOVED):
                record[REMOVED] = True
            # суммы — по валютам; у строк, записанных до этого, одно число в долларах
            record["cost_total"] = usage.plus(usage.amounts(prev.get("cost_total")),
                                              usage.amounts(record.get("cost")))
            self._store(con, record)
            self._touch()
            self.records[record["file"]] = record

    def announce(self, rel: str, kind: str) -> None:
        """Отметка: по записи наступило событие, о котором автору положено письмо."""
        self.amend(rel, announced=kind)

    def amend(self, rel: str, **fields) -> dict:
        """Поля строки записи, которые меняет не прогон, а событие или человек: письмо автору,
        убранный раздел. Поле, переданное со значением None, из строки убирается."""
        with self.db.write() as con, self._lock:
            record = dict(self.records[rel], **fields)
            for name in [k for k, v in fields.items() if v is None]:
                del record[name]
            self._store(con, record)
            self._touch()
            self.records[rel] = record
            return record

    def move(self, old: str, new: str, old_out: str, new_out: str) -> int:
        """Папку переименовали: её записи теперь живут по новому пути. Отметки поручений
        переезжают вместе со строками (в базе они привязаны к ним)."""
        with self.db.write() as con, self._lock:
            fresh = {}
            for key in [k for k in self.records if k.startswith(old + "/")]:
                rec = dict(self.records[key], file=new + key[len(old):])
                for field, value in rec.items():
                    # пути результатов внутри выходной папки записаны строками
                    if isinstance(value, str) and value.startswith(old_out):
                        rec[field] = new_out + value[len(old_out):]
                con.execute("UPDATE records SET file = ?, data = ? WHERE file = ?",
                            (rec["file"], json.dumps(rec, ensure_ascii=False), key))
                fresh[key] = rec
            if fresh:
                self._touch()
                for key, rec in fresh.items():
                    self.records.pop(key)
                for rec in fresh.values():
                    self.records[rec["file"]] = rec
            return len(fresh)

    def drop(self, folder: str) -> list[dict]:
        """Папку удалили: её записи уходят из журнала. Возвращает убранное."""
        with self.db.write() as con, self._lock:
            keys = [k for k in self.records if k.startswith(folder + "/")]
            con.executemany("DELETE FROM records WHERE file = ?", [(k,) for k in keys])
            if keys:
                self._touch()
            return [self.records.pop(k) for k in keys]

    def rekey(self, old: str, record: dict) -> None:
        """Запись переименовали или перенесли: та же запись под новым путём.
        Не put: прогона не было, счётчики прогонов и потраченное остаются как есть."""
        with self.db.write() as con, self._lock:
            if old not in self.records:
                raise KeyError(old)
            con.execute("UPDATE records SET file = ?, status = ?, finished_at = ?, data = ? WHERE file = ?",
                        (record["file"], record.get("status"), record.get("finished_at"),
                         json.dumps(record, ensure_ascii=False), old))
            self._touch()
            self.records.pop(old)
            self.records[record["file"]] = record

    def forget(self, rel: str) -> dict:
        """Запись удалили: её строка уходит из журнала. Возвращает убранное."""
        with self.db.write() as con, self._lock:
            if rel not in self.records:
                raise KeyError(rel)
            con.execute("DELETE FROM records WHERE file = ?", (rel,))
            self._touch()
            return self.records.pop(rel)

    def restore(self, records: list[dict]) -> None:
        """Записи вернули из корзины: их строки — назад в журнал такими, какими они были при удалении.
        Не put: прогона не было, счётчики прогонов и потраченное остаются как есть."""
        with self.db.write() as con, self._lock:
            for record in records:
                if record["file"] in self.records:
                    raise KeyError(record["file"])
                self._store(con, record)
            if records:
                self._touch()
            for record in records:
                self.records[record["file"]] = record

    def _touch(self) -> None:
        """Память сейчас изменится вслед за базой: если транзакция откатится, память
        перечитывается из базы."""
        self.version += 1
        self.db.undo(self._load)

    def totals(self) -> dict:
        with self._lock:
            by_status: dict[str, int] = {}
            for r in self.records.values():
                by_status[r.get("status", "?")] = by_status.get(r.get("status", "?"), 0) + 1
            cost: dict[str, float] = {}
            for r in self.records.values():
                cost = usage.plus(cost, usage.amounts(r.get("cost_total") or r.get("cost")))
            return {
                "files": len(self.records),
                "by_status": by_status,
                "cost": cost,
                "runs": sum(int(r.get("runs") or 1) for r in self.records.values()),
                "audio_sec": round(sum(float(r.get("audio_duration_sec") or 0)
                                       for r in self.records.values()), 1),
            }

    def history(self, limit: int = 200) -> list[dict]:
        with self._lock:
            return sorted(self.records.values(),
                          key=lambda r: r.get("finished_at") or "", reverse=True)[:limit]



def write_marker(out_dir: Path, stem: str, record: dict, log=lambda s: None) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{stem}{MARKER_SUFFIX}").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log(f"не удалось записать маркер обработки: {e}")
