"""Данные для внешних систем: 1С, трекер задач, свой скрипт.

Страница сервиса читает протокол глазами, а программе нужны поля: список записей,
готовых после такого-то момента, и по каждой — протокол и задачи с исполнителем,
сроком и местом в записи. Здесь только чтение, по тем же правилам, что у страницы:
отдаётся лишь то, что есть в журнале обработки.
"""
from __future__ import annotations

import datetime
import re
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode

from . import mindmap

NO_ONE = "ответственный не назван"
ABSENT = "не участвовали в совещании"
EMPTY = ("", "—", "-", "–")


def parse_since(value: str) -> datetime.datetime | None:
    """Момент «готовы после». Время без пояса считается местным временем сервиса."""
    if not value:
        return None
    # «+03:00» в адресе без кодирования приходит пробелом — возвращаем плюс на место
    value = re.sub(r"(T[\d:.]+) (\d\d:\d\d)$", r"\1+\2", value.strip())
    moment = datetime.datetime.fromisoformat(value)
    return moment.astimezone() if moment.tzinfo is None else moment


def finished(rec: dict) -> datetime.datetime | None:
    try:
        return parse_since(rec.get("finished_at") or "")
    except ValueError:
        return None


def seconds(clock: str) -> int:
    h, m, s = (int(x) for x in clock.split(":"))
    return h * 3600 + m * 60 + s


def tasks(md: str) -> list[dict]:
    """Задачи из раздела «Задачи» протокола совещания.

    В таблице жирная строка с пустыми ячейками — имя ответственного, под ней его
    задачи. Блок «Ответственный не назван» даёт задачи без исполнителя, блок
    «Не участвовали в совещании» — задачи отсутствовавших: имя там стоит в самом
    тексте, до двоеточия. В протоколах старого образца ответственный — колонкой.
    """
    return [task for _, _, task in tasks_at(md)]


def tasks_at(md: str) -> list[tuple[int, dict[str, int | None], dict]]:
    """Те же задачи с их местом в тексте: номер строки протокола (с нуля), колонки таблицы
    («задача», «статус» → номер ячейки) и сама задача. Место нужно, чтобы поставить отметку
    «выполнено» у нужной строки — на странице и в выгружаемом файле."""
    out: list[tuple[int, dict[str, int | None], dict]] = []
    in_section, block = False, []
    for n, ln in enumerate(md.replace("\r\n", "\n").split("\n") + ["## конец"]):
        if ln.startswith("#"):
            if in_section:
                break
            in_section = bool(re.match(r"#+\s*(\d+\.\s*)?Задачи\s*$", ln))
        elif in_section and ln.startswith("|"):
            block.append((n, mindmap.cells(ln)))
    if len(block) < 2:
        return out
    header = block[0][1]
    rows = [(n, r) for n, r in block[1:] if not all(mindmap.TABLE_RULE.fullmatch(c or "-") for c in r)]
    low = [h.lower() for h in header]
    # заголовки менялись: «Задача (что сделать)», «Приоритет/статус (если назван)»
    col = {key: next((i for i, h in enumerate(low) if key in h), None)
           for key in ("задача", "ответственный", "срок", "статус")}
    group, absent = None, False

    def cell(r: list[str], name: str) -> str | None:
        i = col[name]
        v = mindmap.no_ts(mindmap.plain(r[i])) if i is not None and i < len(r) else ""
        return None if v in EMPTY else v

    for n, r in rows:
        if re.fullmatch(r"\*\*.+\*\*", r[0]) and not any(c.strip() for c in r[1:]):
            name = mindmap.plain(r[0])
            absent = name.lower() == ABSENT
            group = None if absent or name.lower() == NO_ONE else name
            continue
        text = cell(r, "задача")
        if not text:
            continue
        who = group or cell(r, "ответственный")
        if absent and ": " in text:
            who, text = (part.strip() for part in text.split(": ", 1))
        marks = mindmap.TS.findall(" ".join(r))
        out.append((n, col, {"who": who, "absent": absent, "task": text,
                             "due": cell(r, "срок"), "status": cell(r, "статус"),
                             "at": marks[0] if marks else None,
                             "at_sec": seconds(marks[0]) if marks else None}))
    return out


# Состояние записи одним словом — одинаково в списке, дереве и ответе о ходе обработки.
STATES = {"ok": "ready", "no_summary": "no_protocol", "no_audio": "no_audio", "error": "error"}
STATE_TEXT = {
    "uploaded": "файл принят, сервис ещё не заметил его — заметит при следующем обходе папки",
    "waiting": "сервис ждёт, пока файл перестанет меняться, потом поставит в очередь",
    "queued": "в очереди на обработку",
    "processing": "обрабатывается",
    "ready": "готово: есть протокол и расшифровка",
    "no_protocol": "речь распознана, протокол собрать не удалось — сервис попробует ещё раз",
    "no_audio": "в файле нет звука — обрабатывать нечего",
    "error": "ошибка обработки",
}


def state_of(rec: dict) -> str:
    return STATES.get(rec.get("status"), "error")


def links(file: str, ready: bool, base: str = "") -> dict:
    """Где взять остальное. base — адрес сервиса; без него ссылки от корня сайта."""
    def url(address: str, **query) -> str:
        return base + address + "?" + urlencode(query, quote_via=quote)

    return {"record": url("/api/v1/record", file=file),
            "page": page_url(file, base),
            "html": url("/api/v1/record.html", file=file) if ready else None}


def page_url(file: str, base: str = "", at_sec: int | None = None) -> str:
    """Запись на странице сервиса; at_sec — открыть с перемоткой на этот момент."""
    parent = PurePosixPath(file).parent.as_posix()
    query = {"path": "" if parent == "." else parent, "open": file}
    if at_sec is not None:
        query["t"] = at_sec
    return base + "/?" + urlencode(query, quote_via=quote)


def brief(rec: dict, base: str = "") -> dict:
    """Запись одной строкой списка: что это, когда готово, где взять остальное."""
    file = rec["file"]
    parent = PurePosixPath(file).parent.as_posix()
    return {
        "file": file,
        "dir": "" if parent == "." else parent,
        "name": PurePosixPath(file).name,
        "state": state_of(rec),
        "error": rec.get("error"),
        "finished_at": rec.get("finished_at"),
        "date": day_of(rec) or None,
        "title": rec.get("title"),
        "gist": rec.get("gist"),
        "template": rec.get("template"),
        "duration_sec": rec.get("audio_duration_sec"),
        "speakers": rec.get("speakers"),
        "has_protocol": rec.get("status") == "ok",
        "links": links(file, rec.get("status") == "ok", base),
    }


def short(rec: dict) -> dict:
    """Запись строкой дерева папок: только то, по чему её выбирают."""
    return {"file": rec["file"], "title": rec.get("title"), "date": day_of(rec) or None,
            "duration_sec": rec.get("audio_duration_sec"), "state": state_of(rec),
            "has_protocol": rec.get("status") == "ok"}


def inside(folder: str, root: str) -> bool:
    """Лежит ли папка в root: сама она или любая её подпапка. Пустой root — всё."""
    return not root or folder == root or folder.startswith(root + "/")


def day_of(rec: dict) -> str:
    """Дата совещания ГГГГ-ММ-ДД. Её называет разбор записи, и чаще всего не называет вовсе —
    тогда это день, когда запись обработана впервые (пересборка протокола дату не сдвигает)."""
    named = rec.get("record_date") or ""
    if re.fullmatch(r"\d{4}-\d\d-\d\d", named):
        return named
    return (rec.get("first_done_at") or rec.get("finished_at") or "")[:10]


def listing(records, since: datetime.datetime | None, folder: str, base: str = "") -> list[dict]:
    """Записи, обработка которых закончилась после since, от старых к новым:
    время последней в ответе — готовое значение since для следующего запроса."""
    folder = folder.strip("/")
    picked = []
    for rec in records:
        done = finished(rec)
        if done is None or (since is not None and done <= since):
            continue
        item = brief(rec, base)
        if not inside(item["dir"], folder):
            continue
        picked.append((done, item))
    return [item for _, item in sorted(picked, key=lambda p: p[0])]
