"""Признак папки «Серия совещаний»: записи папки — одна череда встреч, и в конец протокола новой
записи сервис дописывает незакрытые поручения предыдущей записи этой папки (registry.block).

Признак есть у каждой папки, кроме корня, и включён, пока его не выключили: подпапки обычно и
заводят под одно повторяющееся совещание. Поэтому база помнит только исключения — папки, где он
выключен (таблица series_off); новая папка и папки, бывшие до появления признака, — серии без
единой строки в базе. Корень — не серия никогда: туда кладут разрозненные записи, и
переключателя у него нет. Подпапки признак не наследуют: каждая папка — своя серия, как и
предыдущая запись ищется только в самой папке (registry.previous).

Папку переименовали — строки её и её подпапок переезжают, удалили — уходят: folders.py делает
это той же транзакцией, что права и словари.
"""
from __future__ import annotations

from . import db


class Store:
    def __init__(self, base: db.Database):
        self.db = base

    def on(self, folder: str) -> bool:
        """Серия ли папка. Корень — нет."""
        return bool(folder) and self.db.row("SELECT 1 FROM series_off WHERE folder = ?", folder) is None

    def off(self) -> set[str]:
        """Все папки, где признак выключен, — одним запросом на дерево папок."""
        return {row["folder"] for row in self.db.rows("SELECT folder FROM series_off")}

    def put(self, folder: str, on: bool) -> bool:
        """Включает или выключает признак папки. Возвращает, изменилось ли что-то."""
        assert folder, "у корня признака нет"
        with self.db.write() as con:
            if on:
                return con.execute("DELETE FROM series_off WHERE folder = ?", (folder,)).rowcount > 0
            return con.execute("INSERT OR IGNORE INTO series_off (folder) VALUES (?)", (folder,)).rowcount > 0

    def move_folder(self, old: str, new: str) -> None:
        """Папку переименовали — признак её самой и её подпапок переезжает следом."""
        with self.db.write() as con:
            con.execute(f"UPDATE OR REPLACE series_off SET folder = ? || substr(folder, ?) WHERE {db.below('folder')}",
                        (new, len(old) + 1, *db.span(old)))

    def drop_folder(self, folder: str) -> None:
        """Папку удалили — её признак и признаки подпапок больше ни к чему."""
        with self.db.write() as con:
            con.execute(f"DELETE FROM series_off WHERE {db.below('folder')}", db.span(folder))
