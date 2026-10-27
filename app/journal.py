"""Журнал событий: в базе сервиса (db.py: events и таблицы прогонов), только дозапись.

Журнал обработки (ledger) помнит по записи лишь последний прогон и сумму, а при
удалении папки забывает и это. Отчёту о расходах нужно другое: каждый прогон
отдельно — когда, сколько, по чьей загрузке и по чьей команде. Поэтому прогоны
пишутся сюда и никогда не переписываются: переименование и удаление папок
оставляют потраченное на месте.

Сюда же идут действия людей — вход, загрузка, папки, права, пользователи. Это
и есть журнал действий: отдельного механизма для него нет.

Событие — строка таблицы events: когда, вид, кто, о какой записи, папке и человеке (по этим
столбцам есть индексы) и остальные поля события в столбце data. У прогона ещё строка в runs
и суммы в run_money, run_stages и run_providers: отчёт о расходах выбирает прогоны периода
запросом, а не читает журнал целиком.

Расходы записи не должны распадаться на два названия, когда её или её папку переименовали.
Поэтому у прогона, кроме пути, под которым он шёл (events.file — не меняется никогда), есть
путь записи на сегодня (runs.path): событие о переименовании или переносе той же транзакцией
переводит на новый путь прогоны, которые к этой записи относятся. Прогоны удалённой записи —
и всех записей удалённой папки — остаются под последним путём и дальше не переезжают
(runs.settled): по тому же пути позже может лечь другая запись или папка, и её переименование
не должно утащить за собой чужие расходы.
Так же ведётся таблица uploads — кто загрузил запись, лежащую сейчас по этому пути.
"""
from __future__ import annotations

import datetime
import json
import sqlite3

from . import db, usage

BUCKETS = ("day", "week", "month", "year")
EARLIER = "earlier"          # этап для расходов, набежавших до появления журнала
RECORD_MOVES = ("record_rename", "record_move")     # запись сменила путь: file → to
# поля прогона, у которых в базе свои столбцы и таблицы; прочие поля события лежат в events.data
RUN_FIELDS = ("at", "kind", "by", "file", "uploaded_by", "reason", "status", "started_at", "audio_sec",
              "cost", "stages", "providers", "runs")
STAGE_FIELDS = ("stage", "model", "provider", "currency", "cost")
PROVIDER_FIELDS = ("provider", "currency", "cost")


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _moved(path: str, old: str, new: str) -> str:
    return new + path[len(old):] if path == old or path.startswith(old + "/") else path


def _text(value) -> str | None:
    return value if isinstance(value, str) else None


class Journal:
    def __init__(self, base: db.Database, log=lambda s: None):
        self.db = base
        self.log = log

    # ---------- запись ----------

    def write(self, kind: str, by: str = "", **fields) -> dict:
        """by — кто это сделал; пусто — сервис сам.

        Внутри идущей транзакции (действие, которое меняет несколько таблиц) сбой записи события
        отменяет всё действие. Событие само по себе (вход, отправка письма) действие не срывает:
        сбой уходит строкой в журнал работы."""
        event = {"at": now_iso(), "kind": kind, "by": by, **fields}
        try:
            with self.db.write() as con:
                self._insert(con, event)
        except sqlite3.Error as e:
            if self.db.writing:
                raise
            self.log(f"ВНИМАНИЕ: журнал событий не пишется: {e}")
        return event

    def _insert(self, con, event: dict) -> None:
        if event["kind"] == "run":
            self._insert_run(con, event)
            return
        fields = {k: v for k, v in event.items() if k not in ("at", "kind", "by")}
        file, folder = _text(fields.get("file")), _text(fields.get("folder"))
        if folder is None and file is not None:
            folder = file.rpartition("/")[0]
        con.execute("INSERT INTO events (at, kind, actor, file, folder, login, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event["at"], event["kind"], event.get("by") or "", file, folder, _text(fields.get("login")),
                     json.dumps(fields, ensure_ascii=False)))
        self._follow(con, event)

    def _insert_run(self, con, event: dict) -> None:
        run = _with_money(event)
        rest = {k: v for k, v in run.items() if k not in RUN_FIELDS}
        file = run["file"]
        number = con.execute(
            "INSERT INTO events (at, kind, actor, file, folder, data) VALUES (?, 'run', ?, ?, ?, ?)",
            (run["at"], run.get("by") or "", file, file.rpartition("/")[0],
             json.dumps(rest, ensure_ascii=False))).lastrowid
        con.execute(
            "INSERT INTO runs (id, at, day, path, actor, uploaded_by, reason, status, started_at, audio_sec, runs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (number, run["at"], run["at"][:10], file, run.get("by") or "", run.get("uploaded_by") or "",
             run.get("reason"), run.get("status"), run.get("started_at"),
             None if run.get("audio_sec") is None else float(run["audio_sec"]),
             None if run.get("runs") is None else int(run["runs"])))
        con.executemany("INSERT INTO run_money (id, currency, cost) VALUES (?, ?, ?)",
                        [(number, cur, float(v)) for cur, v in run["cost"].items()])
        con.executemany("INSERT INTO run_stages (id, n, stage, model, provider, currency, cost) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(number, n, s["stage"], s["model"], s["provider"], s["currency"], float(s["cost"]))
                         for n, s in enumerate(run["stages"])])
        con.executemany("INSERT INTO run_providers (id, n, provider, currency, cost) VALUES (?, ?, ?, ?, ?)",
                        [(number, n, s["provider"], s["currency"], float(s["cost"]))
                         for n, s in enumerate(run["providers"])])

    def _follow(self, con, event: dict) -> None:
        """Что событие меняет в путях: кто загрузил запись и под каким путём числятся её прогоны."""
        kind, file, to = event["kind"], _text(event.get("file")), _text(event.get("to"))
        if kind == "upload" and file is not None:
            con.execute("INSERT INTO uploads (path, actor) VALUES (?, ?) ON CONFLICT (path) DO UPDATE SET "
                        "actor = excluded.actor", (file, event.get("by") or ""))
        elif kind == "folder_rename" and _text(event.get("folder")) is not None and to is not None:
            old = event["folder"]
            for table, live in (("uploads", ""), ("runs", "settled = 0 AND ")):
                con.execute(f"UPDATE OR REPLACE {table} SET path = ? || substr(path, ?) WHERE {live}{db.below('path')}",
                            (to, len(old) + 1, *db.span(old)))
        elif kind in RECORD_MOVES and file is not None and to is not None:
            con.execute("UPDATE OR REPLACE uploads SET path = ? WHERE path = ?", (to, file))
            con.execute("UPDATE runs SET path = ? WHERE settled = 0 AND path = ?", (to, file))
        elif kind == "record_delete" and file is not None:
            # по этому пути потом может лечь другая запись — прежний автор и прежние расходы к ней не относятся
            con.execute("DELETE FROM uploads WHERE path = ?", (file,))
            con.execute("UPDATE runs SET settled = 1 WHERE path = ?", (file,))
        elif kind == "folder_delete" and _text(event.get("folder")) is not None:
            # то же для всех записей папки: потом может появиться новая папка с тем же именем
            folder = event["folder"]
            con.execute(f"DELETE FROM uploads WHERE {db.below('path')}", db.span(folder))
            con.execute(f"UPDATE runs SET settled = 1 WHERE {db.below('path')}", db.span(folder))

    def run(self, record: dict, by: str, reason: str) -> dict:
        """Прогон записи закончился — сколько он стоил и кому его отнести."""
        return self.write(
            "run", by, file=record["file"], uploaded_by=self.uploader(record["file"]),
            reason=reason, status=record.get("status"), started_at=record.get("started_at"),
            audio_sec=record.get("audio_duration_sec"), cost=usage.amounts(record.get("cost")),
            stages=record.get("cost_models") or [], providers=record.get("cost_providers") or [])

    def replace(self, events: list[dict]) -> None:
        """Журнал заново из готовых событий, в этом порядке: пути прогонов и авторы загрузок
        выводятся из них же. Для инструментов (перенос с файлов, демонстрационные данные)."""
        with self.db.write() as con:
            for table in ("uploads", "events"):         # прогоны и их суммы уходят вместе с событиями
                con.execute(f"DELETE FROM {table}")
            for event in events:
                self._insert(con, event)

    # ---------- чтение ----------

    def uploader(self, file: str) -> str:
        """Кто загрузил запись; пусто — неизвестно: файл положили мимо страницы."""
        return self.db.value("SELECT actor FROM uploads WHERE path = ?", file) or ""

    @property
    def uploaded(self) -> dict[str, str]:
        """Запись → кто её загрузил."""
        return {row["path"]: row["actor"] for row in self.db.rows("SELECT path, actor FROM uploads")}

    def _runs(self, where: str, args: tuple, path: str) -> dict[int, dict]:
        """Прогоны под условием where — номер события → прогон в том виде, в каком его ждут отчёты.
        path — какой путь отдать в поле file: events.file (под каким шёл) или runs.path (где запись сейчас)."""
        con = self.db.con
        picked = f"SELECT runs.id FROM runs WHERE {where}"
        found: dict[int, dict] = {}
        # одним чтением: между запросами другой поток может дописать прогон, и суммы разошлись бы со списком
        with self.db.reading():
            for row in con.execute(
                    f"SELECT runs.*, events.file AS first_path, events.data FROM runs JOIN events ON events.id = runs.id "
                    f"WHERE {where} ORDER BY runs.id", args):
                run = {"at": row["at"], "kind": "run", "by": row["actor"],
                       "file": row["path"] if path == "path" else row["first_path"],
                       "uploaded_by": row["uploaded_by"], "reason": row["reason"], "status": row["status"],
                       "started_at": row["started_at"], "audio_sec": row["audio_sec"], "cost": {}, "stages": [],
                       "providers": []}
                if row["runs"] is not None:
                    run["runs"] = row["runs"]
                run.update(json.loads(row["data"]))
                found[row["id"]] = run
            for row in con.execute(f"SELECT * FROM run_money WHERE id IN ({picked}) ORDER BY id, currency", args):
                found[row["id"]]["cost"][row["currency"]] = row["cost"]
            for row in con.execute(f"SELECT * FROM run_stages WHERE id IN ({picked}) ORDER BY id, n", args):
                found[row["id"]]["stages"].append({k: row[k] for k in STAGE_FIELDS})
            for row in con.execute(f"SELECT * FROM run_providers WHERE id IN ({picked}) ORDER BY id, n", args):
                found[row["id"]]["providers"].append({k: row[k] for k in PROVIDER_FIELDS})
        return found

    def read(self) -> list[dict]:
        """Все события по порядку, как они были записаны: прогон — под путём, под которым он шёл."""
        runs = self._runs("1", (), "first")
        return [runs[row["id"]] if row["kind"] == "run"
                else {"at": row["at"], "kind": row["kind"], "by": row["actor"], **json.loads(row["data"])}
                for row in self.db.rows("SELECT id, at, kind, actor, data FROM events ORDER BY id")]

    def within(self, start: str = "", end: str = "") -> list[dict]:
        """Прогоны за период — с путями, какими они стали после всех переименований и переносов.
        start и end — даты ГГГГ-ММ-ДД включительно, пусто — без границы. Период считается по
        времени прогона: деньги тратятся при обработке, а не при загрузке."""
        where, args = ["1"], []
        if start:
            where.append("runs.day >= ?")
            args.append(start)
        if end:
            where.append("runs.day <= ?")
            args.append(end)
        return list(self._runs(" AND ".join(where), tuple(args), "path").values())

    def report(self, start: str = "", end: str = "", bucket: str = "day") -> dict:
        """Расходы за период — см. summary."""
        return summary(self.within(start, end), start, end, bucket)

    def actions(self, limit: int = 200, kind: str = "", by: str = "", file: str = "", folder: str = "",
                login: str = "", start: str = "", end: str = "") -> list[dict]:
        """Последние действия людей, свежие первыми; прогонов записей здесь нет — они в отчёте о
        расходах. Отбор: kind — вид события, by — кто сделал, file — о какой записи, folder — о
        какой папке (с подпапками), login — о каком пользователе, start и end — дни включительно."""
        where, args = ["kind <> 'run'"], []
        for column, value in (("kind", kind), ("actor", by), ("file", file), ("login", login)):
            if value:
                where.append(f"{column} = ?")
                args.append(value)
        if folder:
            where.append(db.below("folder"))
            args += db.span(folder)
        if start:
            where.append("at >= ?")
            args.append(start)
        if end:
            where.append("at < ?")
            args.append(end + "U")           # всё, что начинается с этой даты: после неё в строке идёт «T»
        rows = self.db.rows(f"SELECT at, kind, actor, data FROM events WHERE {' AND '.join(where)} "
                            f"ORDER BY id DESC LIMIT ?", *args, limit)
        return [{"at": row["at"], "kind": row["kind"], "by": row["actor"], **json.loads(row["data"])} for row in rows]

    def count(self) -> int:
        return self.db.value("SELECT count(*) FROM events")


def _with_money(run: dict) -> dict:
    """Прогон с суммами по валютам и поставщикам — в каком бы виде он ни был записан.

    Прогоны, записанные до появления второго поставщика, несут одно число без валюты и
    этапы без поставщика: тогда сервис работал только через OpenRouter, в долларах.
    В базу прогон ложится уже в полном виде."""
    cost = usage.amounts(run.get("cost"))
    stages = [{"provider": usage.LEGACY_PROVIDER, "currency": usage.LEGACY_CURRENCY, **s}
              for s in run.get("stages") or []]
    spent = run.get("providers")
    if spent is None:
        spent = [{"provider": usage.LEGACY_PROVIDER, "currency": cur, "cost": v} for cur, v in cost.items()]
    return {**run, "cost": cost, "stages": stages, "providers": spent}


def runs(events: list[dict]) -> list[dict]:
    """Прогоны с путями, какими они стали после всех переименований и переносов папок
    и записей: иначе расходы одной папки или записи распались бы на два названия.
    То же самое, что база делает при записи событий (Journal._follow), но над готовым
    списком событий.

    Прогоны удалённой записи и записей удалённой папки остаются под последним путём и дальше
    не переезжают: по тому же пути позже может лечь другая запись, и её переименование не
    должно утащить за собой чужие расходы."""
    out: list[dict] = []
    settled: set[int] = set()       # номера прогонов удалённых записей и папок
    for event in events:
        kind = event["kind"]
        if kind == "run":
            out.append(_with_money(event))
        elif kind == "folder_rename":
            for run in (run for n, run in enumerate(out) if n not in settled):
                run["file"] = _moved(run["file"], event["folder"], event["to"])
        elif kind in RECORD_MOVES:
            for n, run in enumerate(out):
                if n not in settled and run["file"] == event["file"]:
                    run["file"] = event["to"]
        elif kind == "record_delete":
            settled.update(n for n, run in enumerate(out) if run["file"] == event["file"])
        elif kind == "folder_delete":
            settled.update(n for n, run in enumerate(out) if run["file"].startswith(event["folder"] + "/"))
    return out


def spender(run: dict) -> str:
    """Кому отнести расход: кто запустил прогон, а если сервис взял запись сам —
    кто её загрузил. Пусто — неизвестно."""
    return run.get("by") or run.get("uploaded_by") or ""


def _period(at: str, bucket: str) -> str:
    if bucket == "year":
        return at[:4]
    if bucket == "month":
        return at[:7]
    if bucket == "week":
        day = datetime.date.fromisoformat(at[:10])
        return (day - datetime.timedelta(days=day.weekday())).isoformat()
    return at[:10]


class _Sum:
    def __init__(self):
        self.cost: dict[str, float] = {}        # по валютам, без пересчёта
        self.runs = 0
        self.audio: dict[str, float] = {}       # по записям: пересборка не удваивает часы звука

    def add(self, run: dict, cost: dict[str, float] | None = None) -> None:
        self.cost = usage.plus(self.cost, run["cost"] if cost is None else cost)
        self.runs += int(run.get("runs") or 1)
        self.audio[run["file"]] = max(self.audio.get(run["file"], 0.0), float(run.get("audio_sec") or 0))

    def out(self) -> dict:
        return {"cost": self.cost, "runs": self.runs, "records": len(self.audio),
                "audio_sec": round(sum(self.audio.values()), 1)}


def within(events: list[dict], start: str = "", end: str = "") -> list[dict]:
    """Прогоны за период. start и end — даты ГГГГ-ММ-ДД включительно, пусто — без границы.
    Период считается по времени прогона: деньги тратятся при обработке, а не при загрузке."""
    return [r for r in runs(events)
            if (not start or r["at"][:10] >= start) and (not end or r["at"][:10] <= end)]


def report(events: list[dict], start: str = "", end: str = "", bucket: str = "day") -> dict:
    """Расходы за период по списку событий (границы — как у within)."""
    return summary(within(events, start, end), start, end, bucket)


def summary(picked: list[dict], start: str = "", end: str = "", bucket: str = "day") -> dict:
    """Расходы по прогонам picked — их уже выбрали за период с start по end.

    Каждая сумма — разбивка по валютам ({"RUB": 35.0, "USD": 1.2}): где запись обработана,
    в той валюте и записано, и между собой валюты не складываются."""
    total = _Sum()
    own: dict[str, dict[str, float]] = {}
    split: dict[str, dict] = {"folders": {}, "users": {}, "stages": {}}     # разрез → период → ключ → сумма
    heard: dict[str, dict] = {}                 # период ("" — весь отчёт) → валюта → запись → секунды звука
    groups: dict[str, dict] = {"folders": {}, "users": {}, "records": {}, "stages": {},
                               "providers": {}, "series": {}}

    def put(group: str, key, run: dict, cost: dict[str, float] | None = None) -> None:
        groups[group].setdefault(key, _Sum()).add(run, cost)

    def part(group: str, period: str, key: str, cost: dict[str, float]) -> None:
        cell = split[group].setdefault(period, {})
        cell[key] = usage.plus(cell.get(key, {}), cost)

    for run in picked:
        total.add(run)
        folder = run["file"].rpartition("/")[0]
        own[folder] = usage.plus(own.get(folder, {}), run["cost"])
        parts = folder.split("/") if folder else []
        for depth in range(len(parts) + 1):                 # папка и все, что над ней
            put("folders", "/".join(parts[:depth]), run)
        put("users", spender(run), run)
        put("records", run["file"], run)
        period = _period(run["at"], bucket)
        put("series", period, run)
        part("folders", period, parts[0] if parts else "", run["cost"])
        part("users", period, spender(run), run["cost"])
        for cur in run["cost"]:
            for span in ("", period):
                files = heard.setdefault(span, {}).setdefault(cur, {})
                files[run["file"]] = max(files.get(run["file"], 0.0), float(run.get("audio_sec") or 0))
        for spent in run["stages"]:
            put("stages", (spent["stage"], spent["model"]), run, {spent["currency"]: spent["cost"]})
            part("stages", period, spent["stage"], {spent["currency"]: spent["cost"]})
        for spent in run["providers"]:
            put("providers", spent["provider"], run, {spent["currency"]: spent["cost"]})

    def seconds(by_cur: dict[str, dict]) -> dict[str, float]:
        return {cur: round(sum(files.values()), 1) for cur, files in sorted(by_cur.items())}

    def rows(group: str, name: str) -> list[dict]:
        return [{name: key, **s.out()} for key, s in groups[group].items()]

    def weight(row: dict) -> float:
        """Порядок строк «от дорогих»: валюты не сравнить, поэтому по доле строки в итоге
        своей валюты — у строки с двумя валютами доли складываются."""
        return -sum(v / total.cost[cur] for cur, v in row["cost"].items() if total.cost.get(cur))

    return {
        "start": start, "end": end, "bucket": bucket, "total": total.out(),
        # валюты, в которых за период что-то потрачено: столько итогов и колонок у отчёта
        "currencies": list(total.cost),
        # корень показан итогом; own — потрачено на записи самой папки, без подпапок
        "folders": sorted(({**r, "own": own.get(r["folder"], {})}
                           for r in rows("folders", "folder") if r["folder"]),
                          key=lambda r: r["folder"]),
        "users": sorted(rows("users", "login"), key=weight),
        "records": sorted(rows("records", "file"), key=weight),
        "stages": sorted(({"stage": k[0], "model": k[1], "cost": s.cost, "runs": s.runs}
                          for k, s in groups["stages"].items()), key=weight),
        # кому платили
        "providers": sorted(rows("providers", "provider"), key=weight),
        "series": sorted(rows("series", "period"), key=lambda r: r["period"]),
        # тот же ряд, разложенный внутри периода: по папкам верхнего уровня ("" — вне папок),
        # по людям и по этапам — {"folders": {"2026-10": {"Проекты": {"RUB": 35.0}}}, …}
        "split": split,
        # секунды звука записей, за которые платили в этой валюте, — за весь отчёт и по периодам:
        # цену часа считают внутри валюты, а audio_sec — весь звук разом, в чём бы ни платили
        "paid_audio": {"total": seconds(heard.pop("", {})),
                       "series": {period: seconds(by_cur) for period, by_cur in sorted(heard.items())}},
    }
