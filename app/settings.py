"""Настройки, которые администратор меняет на странице: в базе сервиса (db.py: settings, template_notes).

Всё остальное задаётся в .env и читается при запуске. Здесь — то, что должно меняться
без перезапуска и без доступа к серверу: модель распознавания для новых записей.
Значение из .env остаётся значением по умолчанию, пока на странице не выбрано другое.

Здесь же отметки о шаблонах: кто и когда последний раз сохранил файл шаблона на странице
и каким он тогда был. Сами шаблоны лежат в папке prompts, и править их можно в обход
страницы — отметка с отпечатком текста позволяет это заметить.
"""
from __future__ import annotations

import datetime
import json

from . import db, speech


SPEECH_MODEL = "speech_model"


class Store:
    def __init__(self, base: db.Database, log=lambda s: None):
        self.db = base
        self.log = log
        chosen = self._chosen()
        if chosen and chosen["value"] not in speech.ENGINES:
            log(f"ВНИМАНИЕ: модель распознавания {chosen['value']}, выбранную на странице, сервис больше "
                f"не поддерживает — действует значение из .env")

    def _chosen(self) -> dict | None:
        row = self.db.row("SELECT value, author, at FROM settings WHERE key = ?", SPEECH_MODEL)
        return row and {"value": json.loads(row["value"]), "by": row["author"], "at": row["at"]}

    def speech_model(self) -> dict | None:
        """Что выбрано на странице: {"value", "by", "at"}; None — ничего, действует .env.
        Модель, которую сервис больше не поддерживает, — тоже ничего."""
        chosen = self._chosen()
        return chosen if chosen and chosen["value"] in speech.ENGINES else None

    def set_speech_model(self, model: str | None, by: str) -> None:
        """model=None — вернуть к значению из .env."""
        if model is not None:
            speech.engine(model)
        with self.db.write() as con:
            if model is None:
                con.execute("DELETE FROM settings WHERE key = ?", (SPEECH_MODEL,))
            else:
                con.execute("INSERT INTO settings (key, value, author, at) VALUES (?, ?, ?, ?) ON CONFLICT (key) "
                            "DO UPDATE SET value = excluded.value, author = excluded.author, at = excluded.at",
                            (SPEECH_MODEL, json.dumps(model, ensure_ascii=False), by, _now()))

    def template_notes(self) -> dict:
        """Кто и когда сохранял шаблоны на странице: {идентификатор: {"by", "at", "digest"}}."""
        return {row["item"]: {"by": row["author"], "at": row["at"], "digest": row["digest"]}
                for row in self.db.rows("SELECT * FROM template_notes ORDER BY item")}

    def note_template(self, item: str, by: str, digest: str | None) -> None:
        """digest — отпечаток сохранённого текста; None — отметку убрать (тип удалён)."""
        with self.db.write() as con:
            if digest is None:
                con.execute("DELETE FROM template_notes WHERE item = ?", (item,))
            else:
                con.execute("INSERT INTO template_notes (item, author, at, digest) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT (item) DO UPDATE SET author = excluded.author, at = excluded.at, "
                            "digest = excluded.digest", (item, by, _now(), digest))


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")
