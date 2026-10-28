"""Признак папки «Серия совещаний» в базе: включён, пока его не выключили, у корня его нет,
подпапки его не наследуют, переименование и удаление папки его переносят и убирают; база
прежней схемы обновляется, и у всех папок, что в ней были, признак включён. Здесь же отметка
записи «раздел убран вручную» в журнале обработки: пересборка её не стирает."""
from app import db, ledger, series, users


def test_every_folder_is_a_series_until_switched_off_and_the_root_never_is(tmp_path):
    store = series.Store(db.Database(tmp_path))
    assert store.on("Планёрки") and store.on("Планёрки/Архив") and not store.on("")
    assert store.put("Планёрки", False) and not store.put("Планёрки", False)       # второй раз — ничего не изменилось
    assert not store.on("Планёрки") and store.on("Планёрки/Архив")                  # подпапка — своя серия
    assert store.on("Планёрки 2") and store.off() == {"Планёрки"}
    assert store.put("Планёрки", True) and store.on("Планёрки") and store.off() == set()


def test_rename_and_removal_of_a_folder_carry_and_drop_the_switch(tmp_path):
    store = series.Store(db.Database(tmp_path))
    for folder in ("Отдел", "Отдел/Разное", "Отдел 2", "Отдел_x"):
        store.put(folder, False)
    store.move_folder("Отдел", "Склад")
    assert store.off() == {"Склад", "Склад/Разное", "Отдел 2", "Отдел_x"}           # соседи с похожим именем на месте
    assert store.on("Отдел")                                                         # новая папка под старым именем — серия
    store.drop_folder("Склад")
    assert store.off() == {"Отдел 2", "Отдел_x"}


def test_base_of_the_previous_schema_is_updated_and_its_folders_stay_series(tmp_path):
    """Обновление: база версии 1 — с правами и записями папок — открывается этой версией, шаг
    схемы проходит, и у всех прежних папок признак включён: работает так же, как до обновления."""
    old = db.Database(tmp_path, steps=db.MIGRATIONS[:1])
    users.Users(old).put("ivanov", "Иванов", "пароль-1234")
    users.Users(old).set_access("ivanov", "Снабжение/Планёрки", "tree", None, None)
    ledger.Ledger(old).put({"file": "Проекты/Склад/статус.mp4", "status": "ok", "finished_at": "2026-10-08T10:00:00+03:00"})
    assert old.version == 1
    old.close()

    lines = []
    base = db.Database(tmp_path, lines.append)
    assert base.version == db.SCHEMA_VERSION and lines[0] == "схема базы обновлена: версия 1 → 2"
    store = series.Store(base)
    assert store.off() == set() and store.on("Снабжение/Планёрки") and store.on("Проекты/Склад")
    assert ledger.Ledger(base).get("Проекты/Склад/статус.mp4")["status"] == "ok"      # прежнее на месте


def test_removed_block_mark_of_a_record_survives_its_rebuild(tmp_path):
    book = ledger.Ledger(db.Database(tmp_path))
    file = "Планёрки/вкс.mp4"
    book.put({"file": file, "status": "ok", "finished_at": "2026-10-08T10:00:00+03:00"})
    book.amend(file, **{ledger.REMOVED: True})
    book.put({"file": file, "status": "ok", "finished_at": "2026-10-09T10:00:00+03:00"})     # пересборка
    assert book.get(file)[ledger.REMOVED] is True and book.get(file)["runs"] == 2
    book.amend(file, **{ledger.REMOVED: None})
    book.put({"file": file, "status": "ok", "finished_at": "2026-10-10T10:00:00+03:00"})
    assert ledger.REMOVED not in book.get(file)
    assert ledger.REMOVED not in ledger.Ledger(book.db).get(file)                               # и в базе так же
