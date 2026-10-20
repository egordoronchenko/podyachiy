"""Отметки «выполнено» и сроки поручений: то, что человек добавил к поручению после совещания.

Протокол говорит, каким был статус на совещании; закрыли поручение потом — этого в нём нет
и быть не может. Так же и срок: в протоколе он словами, как прозвучал («после отпуска»), а
дату назначает человек. Одна запись на поручение: отметка (status, by, at, comment) и срок
(due — дата ГГГГ-ММ-ДД, due_by, due_at); любой из двух частей может не быть.
Ставит их человек, и хранятся они не в протоколе, а в базе сервиса (db.py: таблица marks),
по пути записи. Строки привязаны к строке записи в журнале обработки: переименовали или
перенесли запись либо её папку — отметки переезжают той же транзакцией; удалили — уходят
вместе с ней (в корзине они остаются в файле «удалено.json»). Сам протокол на диске не
переписывается.

Ключ поручения — хеш от исполнителя и текста поручения, приведённых к одному виду (строчные
буквы, «ё» как «е», без знаков препинания и лишних пробелов): номер строки не годится —
пересборка протокола переставляет строки. Пересборка может и переписать формулировку. Тогда
отметка ищет своё поручение мягко: тот же исполнитель и почти те же слова (ALIKE), и только
если такое поручение одно. Не нашла — остаётся в базе без пары и нигде не показывается.
Два поручения одного исполнителя с одинаковым текстом — для отметки одно и то же поручение.
"""
from __future__ import annotations

import hashlib
import re
import time

from . import db, external, journal, mindmap

COMMENT_MAX = 300
ALIKE = 0.7             # доля общих слов, при которой переписанное поручение считается тем же
STEM = 4                # слова сравниваются по первым буквам: «смету» и «сметы» — одно слово
BLANK = {"done": False, "done_by": None, "done_at": None, "comment": None}
DONE_FIELDS = ("status", "by", "at", "comment")
DUE_FIELDS = ("due", "due_by", "due_at")

def norm(text: str | None) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", (text or "").lower().replace("ё", "е")).split())


def key(who: str | None, task: str) -> str:
    return hashlib.sha1(f"{norm(who)}|{norm(task)}".encode("utf-8")).hexdigest()[:16]


def _words(text: str | None) -> set[str]:
    return {w[:STEM] for w in norm(text).split() if len(w) > 2}


def alike(a: str | None, b: str | None) -> bool:
    one, two = _words(a), _words(b)
    return bool(one and two) and len(one & two) / len(one | two) >= ALIKE


def resolve(tasks: list[dict], saved: dict[str, dict]) -> dict[str, str]:
    """Ключ поручения протокола → ключ его отметки в базе. Обычно это один и тот же ключ;
    разные они, когда протокол пересобрали и формулировка изменилась."""
    keys = [key(t["who"], t["task"]) for t in tasks]
    found = {k: k for k in keys if k in saved}
    free = {k: t for k, t in zip(keys, tasks) if k not in found}
    claims: dict[str, list[str]] = {}
    for old, mark in saved.items():
        if old in found:
            continue
        near = [k for k, t in free.items()
                if norm(t["who"]) == norm(mark.get("who")) and alike(t["task"], mark.get("task"))]
        if len(near) == 1:
            claims.setdefault(near[0], []).append(old)
    found.update({k: olds[0] for k, olds in claims.items() if len(olds) == 1})
    return found


def merged(tasks: list[dict], saved: dict[str, dict]) -> list[dict]:
    """Поручения протокола с тем, что добавил человек: key — ключ поручения; done — отметил ли
    он его выполненным, done_by, done_at и comment — кто, когда и с каким пояснением; due — срок
    датой ГГГГ-ММ-ДД, если человек его задал (иначе None), due_by и due_at — кто и когда.
    Срок словами, как он записан в протоколе, здесь называется due_text."""
    pairs = resolve(tasks, saved)
    out = []
    for task in tasks:
        k = key(task["who"], task["task"])
        mark = saved.get(pairs.get(k, "")) or {}
        out.append({**task, "key": k, "due_text": task["due"],
                    "due": mark.get("due"), "due_by": mark.get("due_by"), "due_at": mark.get("due_at"),
                    **({"done": True, "done_by": mark["by"], "done_at": mark["at"], "comment": mark.get("comment") or None}
                       if mark.get("status") == "done" else BLANK)})
    return out


# поле отметки → столбец таблицы marks
COLUMNS = {"status": "status", "by": "done_by", "at": "done_at", "comment": "comment",
           "due": "due", "due_by": "due_by", "due_at": "due_at"}


class Store:
    """Отметки и сроки поручений в базе: по пути записи — {ключ поручения: отметка}."""

    def __init__(self, base: db.Database):
        self.db = base

    @staticmethod
    def _mark(row) -> dict:
        mark = {field: row[column] for field, column in COLUMNS.items() if row[column] is not None}
        return {**mark, "who": row["who"], "task": row["task"]}

    def load(self, file: str) -> dict[str, dict]:
        """Отметки записи: ключ поручения → отметка (status, by, at, comment), срок (due, due_by,
        due_at) и само поручение, каким оно было при отметке (who, task)."""
        return {row["key"]: self._mark(row)
                for row in self.db.rows("SELECT * FROM marks WHERE file = ? ORDER BY key", file)}

    def everything(self, folder: str = "") -> dict[str, dict[str, dict]]:
        """Отметки всех записей (или записей папки folder с подпапками): путь записи → её отметки."""
        where, args = ("WHERE " + db.below("file"), db.span(folder)) if folder else ("", ())
        found: dict[str, dict[str, dict]] = {}
        for row in self.db.rows(f"SELECT * FROM marks {where} ORDER BY file, key", *args):
            found.setdefault(row["file"], {})[row["key"]] = self._mark(row)
        return found

    def stamps(self) -> dict[str, float]:
        """Путь записи → когда у неё последний раз меняли отметки. Записей, у которых отметок
        не было никогда, здесь нет."""
        return {row["file"]: row["marks_at"]
                for row in self.db.rows("SELECT file, marks_at FROM records WHERE marks_at > 0")}

    def changed(self, file: str) -> float:
        """Когда у записи последний раз меняли отметки; 0 — никогда."""
        return self.db.value("SELECT marks_at FROM records WHERE file = ?", file) or 0.0

    def restore(self, file: str, saved: dict[str, dict]) -> None:
        """Отметки записи целиком, как они были сохранены, — для переноса и возврата из корзины."""
        with self.db.write() as con:
            self._write(con, file, saved)

    def _write(self, con, file: str, saved: dict[str, dict]) -> None:
        con.execute("DELETE FROM marks WHERE file = ?", (file,))
        con.executemany(
            f"INSERT INTO marks (file, key, who, task, {', '.join(COLUMNS.values())}) "
            f"VALUES (?, ?, ?, ?, {', '.join('?' * len(COLUMNS))})",
            [(file, k, mark.get("who"), mark["task"], *(mark.get(field) for field in COLUMNS))
             for k, mark in saved.items()])
        # время правки остаётся и тогда, когда отметок не осталось: по нему реестр и карта узнают, что отметку сняли
        con.execute("UPDATE records SET marks_at = ? WHERE file = ?", (time.time(), file))

    def _edit(self, file: str, tasks: list[dict], k: str, change) -> tuple[dict, bool, dict]:
        """Правит запись о поручении с ключом k: change(запись) меняет её на месте и отвечает,
        изменилось ли что-то. Возвращает поручение с отметками, этот признак и запись, какой она
        была до правки. KeyError — такого поручения в протоколе нет."""
        task = next((t for t in tasks if key(t["who"], t["task"]) == k), None)
        if task is None:
            raise KeyError(k)
        # прочитать, поправить и записать — одной транзакцией: две отметки разом друг друга не затрут
        with self.db.write() as con:
            saved = self.load(file)
            old = resolve(tasks, saved).get(k)
            mark = saved.pop(old) if old is not None else {}
            before = dict(mark)
            changed = change(mark)
            if "status" in mark or "due" in mark:           # ни отметки, ни срока — записи нет
                saved[k] = {**mark, "who": task["who"], "task": task["task"]}
            if changed:
                self._write(con, file, saved)
        return next(t for t in merged(tasks, saved) if t["key"] == k), changed, before

    def put(self, file: str, tasks: list[dict], k: str, done: bool, by: str, comment: str = "") -> tuple[dict, bool]:
        """Поставить или снять отметку «выполнено» у поручения с ключом k. Возвращает поручение
        с отметками и признак, что отметка изменилась. Срок, заданный человеком, отметка не трогает."""
        def change(mark: dict) -> bool:
            was = mark.get("status") == "done"
            for field in DONE_FIELDS:
                mark.pop(field, None)
            if done:
                mark.update(status="done", by=by, at=journal.now_iso(), **({"comment": comment} if comment else {}))
            return done or was

        task, changed, _ = self._edit(file, tasks, k, change)
        return task, changed

    def put_due(self, file: str, tasks: list[dict], k: str, due: str | None, by: str) -> tuple[dict, bool, str | None]:
        """Задать поручению срок датой (ГГГГ-ММ-ДД) или снять его (None) — тогда сроком снова служат
        слова протокола. Возвращает поручение, признак, что срок изменился, и прежний срок-дату."""
        def change(mark: dict) -> bool:
            if mark.get("due") == due:
                return False
            for field in DUE_FIELDS:
                mark.pop(field, None)
            if due:
                mark.update(due=due, due_by=by, due_at=journal.now_iso())
            return True

        task, changed, before = self._edit(file, tasks, k, change)
        return task, changed, before.get("due")


def day(moment: str) -> str:
    return f"{moment[8:10]}.{moment[5:7]}.{moment[:4]}"


def stamp(md: str, saved: dict[str, dict]) -> str:
    """Текст протокола с тем, что добавил человек: «выполнено, 10.10.2026» в колонке статуса
    и срок датой вместо слов в колонке срока — для файлов, которые уходят из сервиса (DOCX,
    PDF, HTML). Протокол на диске остаётся как был."""
    if not saved:
        return md
    found = external.tasks_at(md)
    noted = merged([task for _, _, task in found], saved)
    lines = md.replace("\r\n", "\n").split("\n")

    def write(cells: list[str], at: int | None, text: str, keep: bool) -> None:
        if at is None:                                  # старый протокол без такой колонки
            cells[col["задача"]] += f" ({text})"
            return
        cells += [""] * (at + 1 - len(cells))
        cells[at] = f"{cells[at]}; {text}" if keep and cells[at] not in external.EMPTY else text

    for (n, col, _), task in zip(found, noted):
        if not task["done"] and not task["due"]:
            continue
        cells = mindmap.cells(lines[n])
        if task["due"]:
            at = col["срок"]
            write(cells, at, day(task["due"]) if at is not None else f"срок: {day(task['due'])}", False)
        if task["done"]:
            write(cells, col["статус"], f"выполнено, {day(task['done_at'])}", True)
        lines[n] = "| " + " | ".join(cells) + " |"
    return "\n".join(lines)
