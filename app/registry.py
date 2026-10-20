"""Сквозной реестр поручений: задачи из протоколов всех записей одной таблицей.

Поручения живут в тексте протокола — разделе «Задачи» (разбор — external.tasks). Реестр
ничего не хранит: он читает протоколы записей, которые человеку открыты, добавляет отметки
и сроки из базы и отдаёт строки с отбором и сортировкой. Протоколов сотни и тысячи, поэтому
разбор запоминается (Cache): файл читается заново, только если изменился. Протокол остаётся
документом на диске и единственным источником поручений: его можно поправить руками, и
реестр это увидит.

Здесь же блок «Поручения прошлого совещания», который сервис дописывает в конец нового
протокола: незакрытые поручения предыдущей записи той же папки (серии совещаний лежат по
папкам). Блок собирается кодом, без вызова модели, и в разбор поручений самой записи не
попадает: external.tasks читает только раздел «Задачи», а блок — отдельный раздел со списком.

Закрыто поручение или нет, складывается из двух источников: слово в колонке «Статус»
протокола — что сказали на совещании, и отметка «выполнено», которую человек поставил
потом (marks.py; лежит в базе). Отметка человека главнее. В строке они видны
порознь: status — слова протокола, done — отметка человека, open — итог.

Со сроком так же: due_text — слова протокола («после отпуска»), due — дата, которую задал
человек (там же, в marks.py), due_date — дата, по которой строку ставят в ряд: заданная
человеком, а без неё вычисленная из слов протокола. Просроченным (overdue) поручение считается
только по дате человека: вычисленная — догадка от дня обработки записи, по ней не тревожат.
"""
from __future__ import annotations

import datetime
import re
import threading
from pathlib import Path, PurePosixPath

from . import external, marks

BLOCK_TITLE = "Поручения прошлого совещания"
SORTS = ("date", "due")
# исполнитель — метка говорящего, которого в записи не подписали: «S2», «S13 (технический специалист)»
UNSIGNED = re.compile(r"S\d+\b")

# «выполнено», «сделано», «закрыта», «готово», «снято», «отменили», «решено»; но не
# «не выполнено» и не «частично выполнена»
CLOSED = re.compile(r"\b(выполнен|сделан|закрыт|готов|снят|отмен|решен)")
NOT_CLOSED = re.compile(r"\b(не|частично|почти)\b")

WEEKDAYS = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресен")
MONTHS = ("январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр")
DAY_MONTH = re.compile(r"\b(\d{1,2})\s+(%s)[а-я]*" % "|".join(MONTHS))
NUMERIC = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?\b")
NO_DUE = ("не назван", "не определ", "без срока")


def fold(text: str | None) -> str:
    return (text or "").lower().replace("ё", "е")


def is_open(status: str | None) -> bool:
    """Не закрыто ли поручение — по тому, что о нём записано в колонке «Статус»."""
    text = fold(status)
    return not CLOSED.search(text) or bool(NOT_CLOSED.search(text))


def has_due(due: str | None) -> bool:
    """Назван ли срок: «не назван» в колонке — тоже отсутствие срока."""
    return bool(due) and not fold(due).startswith(NO_DUE)


def due_day(due: str | None, meeting: str) -> str | None:
    """Срок датой ГГГГ-ММ-ДД, если его можно вычислить от дня совещания, иначе None.

    Срок в протоколе — как прозвучал: «сегодня», «в пятницу», «до конца недели», «15.10».
    Понимаются: сегодня и завтра, день недели (ближайший после совещания), эта и следующая
    неделя (её воскресенье), число с месяцем. Остальное («после отпуска») датой не становится.
    """
    if not has_due(due):
        return None
    try:
        base = datetime.date.fromisoformat(meeting)
    except ValueError:
        return None
    text = fold(due)

    def near(day: int, month: int, year: int | None) -> str | None:
        try:
            found = datetime.date(year or base.year, month, day)
            if year is None and found < base - datetime.timedelta(days=31):     # «до 15 января», сказанное в декабре
                found = found.replace(year=base.year + 1)
        except ValueError:
            return None
        return found.isoformat()

    if m := NUMERIC.search(text):
        year = int(m.group(3)) if m.group(3) else None
        return near(int(m.group(1)), int(m.group(2)), year + 2000 if year is not None and year < 100 else year)
    if m := DAY_MONTH.search(text):
        return near(int(m.group(1)), MONTHS.index(m.group(2)) + 1, None)
    if "послезавтра" in text:
        return (base + datetime.timedelta(days=2)).isoformat()
    if "завтра" in text:
        return (base + datetime.timedelta(days=1)).isoformat()
    if re.search(r"\b(сегодня|сейчас)\b", text):
        return base.isoformat()
    sunday = base + datetime.timedelta(days=6 - base.weekday())
    if re.search(r"следующ\w+ недел", text):
        return (sunday + datetime.timedelta(days=7)).isoformat()
    for number, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}", text):
            ahead = (number - base.weekday()) % 7 or 7          # «в пятницу», сказанное в пятницу, — следующая
            return (base + datetime.timedelta(days=ahead)).isoformat()
    if re.search(r"\b(эт\w+|конц\w+|течение) недел", text):
        return sunday.isoformat()
    return None


class Cache:
    """Разобранные поручения по файлу протокола, уже с отметками «выполнено», — в памяти процесса.

    Ключ — путь файла, проверка свежести — время изменения и размер протокола и время последней
    правки отметок записи: пересобранный протокол записывается заново, отметка обновляет своё
    время в базе, переименованная и перенесённая запись получает другой путь, удалённая уходит
    из журнала обработки и больше не запрашивается. Запросы страницы идут из потоков, поэтому словарь под
    замком; сами файлы читаются без замка — два одновременных чтения дадут одно и то же.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, tuple[tuple, list[dict]]] = {}
        self._version = -1
        self.reads = 0          # сколько раз файл пришлось читать и разбирать

    def tasks(self, path: Path, noted_at: float = 0.0, noted=lambda: {}) -> list[dict] | None:
        """Поручения протокола; None — файла нет. Список общий для всех запросов: не менять.
        noted_at — когда у записи последний раз меняли отметки (0 — никогда), noted — функция,
        отдающая сами отметки: зовётся, только когда разбор приходится делать заново."""
        key = str(path)
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size, noted_at)
            with self._lock:
                hit = self._seen.get(key)
            if hit and hit[0] == stamp:
                return hit[1]
            parsed = marks.merged(external.tasks(path.read_text(encoding="utf-8")), noted() if noted_at else {})
        except OSError:
            with self._lock:
                self._seen.pop(key, None)
            return None
        with self._lock:
            self._seen[key] = (stamp, parsed)
            self.reads += 1
        return parsed

    def prune(self, version: int, paths) -> None:
        """Журнал обработки изменился (version — Ledger.version): забыть файлы, которых в нём
        больше нет. paths — функция, отдающая пути протоколов всех записей журнала."""
        with self._lock:
            if version == self._version:
                return
            live = {str(p) for p in paths()}
            self._seen = {k: v for k, v in self._seen.items() if k in live}
            self._version = version

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)


def today() -> str:
    return datetime.date.today().isoformat()


def state(task: dict, meeting: str, now: str) -> dict:
    """То, что о поручении (с отметками — marks.merged) выводится, а не хранится: open — не
    закрыто ли, due_date — срок датой (человека или из слов протокола), overdue — просрочено ли
    на день now. meeting — день совещания."""
    still = is_open(task["status"]) and not task["done"]
    return {"open": still, "due_date": task["due"] or due_day(task["due_text"], meeting),
            "overdue": bool(still and task["due"] and task["due"] < now)}


def rows(records, path_of, cache: Cache, base: str = "", now: str = "",
         marked: marks.Store | None = None) -> tuple[list[dict], int]:
    """Строки реестра по записям и число записей, у которых протокол нашёлся.
    path_of(запись) — файл её протокола; base — адрес сервиса для ссылок; now — сегодняшний день;
    marked — отметки и сроки поручений (без них строки — как в протоколе)."""
    out, scanned = [], 0
    now = now or today()
    stamps = marked.stamps() if marked else {}      # одним запросом на весь реестр
    for rec in records:
        tasks = cache.tasks(path_of(rec), stamps.get(rec["file"], 0.0), lambda: marked.load(rec["file"]))
        if tasks is None:
            continue
        scanned += 1
        file, day = rec["file"], external.day_of(rec)
        parent = PurePosixPath(file).parent.as_posix()
        for task in tasks:
            out.append({**task, **state(task, day, now),
                        "unsigned": bool(task["who"] and UNSIGNED.match(task["who"])),
                        "file": file, "dir": "" if parent == "." else parent,
                        "title": rec.get("title"), "date": day,
                        "link": external.page_url(file, base, task["at_sec"])})
    return out, scanned


def pick(found: list[dict], who: str = "", due: bool = False, only_open: bool = False, q: str = "",
         overdue: bool = False) -> list[dict]:
    """Отбор строк: who — часть имени исполнителя (поручения без исполнителя не подходят),
    due — только со сроком (датой или названным в протоколе), only_open — только незакрытые,
    overdue — только просроченные, q — все слова запроса должны встретиться в поручении,
    исполнителе, сроке, статусе или названии совещания."""
    wanted, words = fold(who).strip(), fold(q).split()
    out = []
    for row in found:
        if wanted and wanted not in fold(row["who"]):
            continue
        if (due and not (row["due"] or has_due(row["due_text"]))) or (only_open and not row["open"]):
            continue
        if overdue and not row["overdue"]:
            continue
        if words:
            text = fold(" ".join(row[k] or "" for k in ("task", "who", "due_text", "status", "title"))
                        + (" " + marks.day(row["due"]) if row["due"] else ""))
            if not all(word in text for word in words):
                continue
        out.append(row)
    return out


def order(found: list[dict], sort: str) -> list[dict]:
    """date — свежие совещания первыми, внутри совещания порядок протокола. due — по сроку:
    сначала сроки, заданные человеком (ближайшие первыми), потом вычисленные из слов протокола,
    потом срок словами, потом без срока."""
    if sort == "due":
        return sorted(found, key=lambda r: (0 if r["due"] else 1, r["due_date"]) if r["due_date"]
                      else (2 if has_due(r["due_text"]) else 3, ""))
    return sorted(found, key=lambda r: r["date"], reverse=True)


def people(found: list[dict]) -> list[str]:
    """Исполнители, которые встречаются в строках, — для подсказки в поле отбора."""
    return sorted({row["who"] for row in found if row["who"]}, key=fold)


# ---------- «что поручали в прошлый раз» ----------

def order_key(rec: dict) -> tuple[str, str]:
    """Место записи в ряду совещаний папки: дата совещания, а при равных датах (и когда дату
    разбор не назвал — она тогда равна дню первой обработки) — момент первой обработки.
    Первой, а не последней: пересборка старого протокола не должна делать запись «новейшей»."""
    return external.day_of(rec), rec.get("first_done_at") or rec.get("finished_at") or ""


def previous(records, file: str, key: tuple[str, str]) -> dict | None:
    """Предыдущая запись той же папки: готовая (с протоколом), стоящая в ряду прямо перед key.
    Подпапки — другие серии совещаний, в расчёт не идут."""
    folder = PurePosixPath(file).parent
    earlier = [r for r in records
               if r["file"] != file and PurePosixPath(r["file"]).parent == folder
               and r.get("status") == "ok" and order_key(r) < key]
    return max(earlier, key=order_key, default=None)


def block(prev: dict, tasks: list[dict], base: str = "") -> str:
    """Раздел для конца протокола: незакрытые поручения предыдущей записи — те, что не закрыты
    ни словом в её протоколе, ни отметкой человека (tasks — с отметками, marks.merged). Пусто —
    нечего напоминать. Метки времени — ссылками на ту запись, а не в виде [чч:мм:сс]: такая
    метка в протоколе вела бы на момент этой записи, а не прошлой."""
    pending = [t for t in tasks if is_open(t["status"]) and not t["done"]]
    if not pending:
        return ""
    file = prev["file"]
    day = external.day_of(prev)
    when = f" от {day[8:10]}.{day[5:7]}.{day[:4]}" if day else ""
    name = (prev.get("title") or PurePosixPath(file).name).replace("[", "(").replace("]", ")")
    lines = [f"## {BLOCK_TITLE}", "",
             f"Незакрытые поручения из протокола записи [«{name}»{when}]({external.page_url(file, base)}). "
             f"Раздел добавлен сервисом; статус — каким он был на том совещании.", ""]
    for t in pending:
        who = (t["who"] or "Ответственный не назван") + (" (не участвовал в совещании)" if t["absent"] else "")
        parts = [t["task"].rstrip(".")]
        if t["due"]:                                    # срок, который человек задал датой, главнее слов
            parts.append(f"срок: {marks.day(t['due'])}")
        elif has_due(t["due_text"]):
            parts.append(f"срок: {t['due_text']}")
        if t["status"]:
            parts.append(f"статус: {t['status']}")
        if t["at"]:
            parts.append(f"[в записи {t['at']}]({external.page_url(file, base, t['at_sec'])})")
        lines.append(f"- **{who}:** " + "; ".join(parts))
    return "\n".join(lines) + "\n"
