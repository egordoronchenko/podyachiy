import json
import os
import sqlite3
import types

import pytest

from app import db, dictionary, folders, journal, marks, users, watcher
from app.ledger import Ledger


def setup(tmp_path):
    cfg = types.SimpleNamespace(inbox=tmp_path / "in", outbox=tmp_path / "out", outbox_inside_inbox=None)
    for rel in ("Проекты отдела/Сметы/запись.mp4", "Проекты отдела/план.mp4", "Снабжение/вкс.mp4"):
        (cfg.inbox / rel).parent.mkdir(parents=True, exist_ok=True)
        (cfg.inbox / rel).write_bytes(b"video")
        out = cfg.outbox / rel.rsplit(".", 1)[0] / "summary"
        out.mkdir(parents=True)
        (out / "протокол.md").write_text("# Протокол", encoding="utf-8")
    ledger = Ledger(db.Database(cfg.outbox.parent / "state"))
    for rel in ("Проекты отдела/Сметы/запись.mp4", "Проекты отдела/план.mp4", "Снабжение/вкс.mp4"):
        ledger.put({"file": rel, "status": "ok", "output": str(cfg.outbox / rel.rsplit(".", 1)[0])})
    service = types.SimpleNamespace(ledger=ledger, queue=[], pending={}, running=[])
    people = users.Users(ledger.db, "секрет")              # одна база на всё, как в сервисе
    people.put("ivanov", "Иванов", "пароль-1234")
    people.set_access("ivanov", "Проекты отдела", read="only")
    people.set_access("ivanov", "Проекты отдела/Сметы", read="tree", manage="only")
    people.set_access("ivanov", "Снабжение", read="tree")
    return cfg, service, people


def test_rename_moves_files_results_ledger_and_rights(tmp_path):
    cfg, service, people = setup(tmp_path)
    assert folders.rename(cfg, service, people, "Проекты отдела", "Проекты") == "Проекты"
    assert (cfg.inbox / "Проекты/Сметы/запись.mp4").is_file() and not (cfg.inbox / "Проекты отдела").exists()
    assert (cfg.outbox / "Проекты/план/summary/протокол.md").is_file()
    assert sorted(service.ledger.records) == ["Проекты/Сметы/запись.mp4", "Проекты/план.mp4", "Снабжение/вкс.mp4"]
    rec = service.ledger.get("Проекты/план.mp4")
    assert rec["file"] == "Проекты/план.mp4" and rec["output"] == str(cfg.outbox / "Проекты" / "план")
    who = people.access("ivanov")
    assert who.sees("Проекты") and who.rules("Проекты/Сметы") and not who.sees("Проекты отдела")
    assert who.sees("Снабжение")                                 # чужую папку переименование не задело
    assert Ledger(db.Database(cfg.outbox.parent / "state")).get("Проекты/план.mp4")            # журнал записан на диск


def test_rename_of_a_subfolder_keeps_the_parent(tmp_path):
    cfg, service, people = setup(tmp_path)
    assert folders.rename(cfg, service, people, "Проекты отдела/Сметы", "Сметы 2026") == "Проекты отдела/Сметы 2026"
    assert service.ledger.get("Проекты отдела/Сметы 2026/запись.mp4") and service.ledger.get("Проекты отдела/план.mp4")
    assert people.access("ivanov").rules("Проекты отдела/Сметы 2026")


def test_rename_refuses_taken_and_bad_names(tmp_path):
    cfg, service, people = setup(tmp_path)
    for name in ("Снабжение", "..", "а/б", ".скрытая", ""):
        with pytest.raises(folders.FolderError):
            folders.rename(cfg, service, people, "Проекты отдела", name)
    assert (cfg.inbox / "Проекты отдела/план.mp4").is_file()


def test_delete_moves_everything_to_trash_and_forgets_the_folder(tmp_path):
    cfg, service, people = setup(tmp_path)
    done = folders.remove(cfg, service, people, "Проекты отдела", "admin")
    assert done["records"] == 2 and done["keep_days"] == folders.TRASH_DAYS
    assert not (cfg.inbox / "Проекты отдела").exists() and not (cfg.outbox / "Проекты отдела").exists()
    assert sorted(service.ledger.records) == ["Снабжение/вкс.mp4"]
    who = people.access("ivanov")
    assert not who.sees("Проекты отдела") and not who.rules("Проекты отдела/Сметы") and who.sees("Снабжение")
    # всё лежит в корзине: и исходники, и результаты, и сведения для возврата
    assert (cfg.inbox / done["trash"] / "Проекты отдела/Сметы/запись.mp4").is_file()
    assert (cfg.outbox / done["trash"] / "Проекты отдела/план/summary/протокол.md").is_file()
    note = json.loads((cfg.outbox / done["trash"] / "удалено.json").read_text(encoding="utf-8"))
    assert note["folder"] == "Проекты отдела" and note["by"] == "admin" and len(note["records"]) == 2


def test_deleted_recordings_are_not_picked_up_again(tmp_path):
    cfg, service, people = setup(tmp_path)
    done = folders.remove(cfg, service, people, "Снабжение", "admin")
    assert watcher.hidden(f"{done['trash']}/Снабжение/вкс.mp4")
    assert watcher.hidden(".uploads/abc.part") and not watcher.hidden("Снабжение/вкс.mp4")


def test_busy_protected_and_missing_folders_are_refused(tmp_path):
    cfg, service, people = setup(tmp_path)
    service.queue = [types.SimpleNamespace(path="Проекты отдела/Сметы/запись.mp4")]
    with pytest.raises(folders.FolderError) as e:
        folders.remove(cfg, service, people, "Проекты отдела", "admin")
    assert e.value.status == 409
    service.queue = []
    service.running = [{"file": "Снабжение/вкс.mp4"}]
    with pytest.raises(folders.FolderError):
        folders.rename(cfg, service, people, "Снабжение", "Склад")
    service.running = []
    for folder in ("", users.HOME_ROOT, f"{users.HOME_ROOT}/ivanov", "Нет такой"):
        with pytest.raises(folders.FolderError):
            folders.remove(cfg, service, people, folder, "admin")


def test_dots_and_hidden_names_in_the_path_are_refused_before_anything_moves(tmp_path):
    """Путь папки проверяется так же, как у новой папки: «..» не уводит переименование за пределы
    входной папки и не обходит защиту папки личных папок, служебные папки не трогаются."""
    cfg, service, people = setup(tmp_path)
    (cfg.inbox / users.HOME_ROOT / "ivanov").mkdir(parents=True)
    (cfg.inbox / ".trash").mkdir()
    for folder in (f"Снабжение/../{users.HOME_ROOT}", "../out", "Снабжение/..", "Снабжение/./", ".trash"):
        for act in (lambda: folders.rename(cfg, service, people, folder, "Другое"),
                    lambda: folders.remove(cfg, service, people, folder, "admin")):
            with pytest.raises(folders.FolderError) as e:
                act()
            assert e.value.status == 400, folder
    assert (cfg.inbox / users.HOME_ROOT / "ivanov").is_dir() and (cfg.outbox / "Снабжение" / "вкс").is_dir()
    assert not (tmp_path / "Другое").exists() and not (cfg.inbox / "Другое").exists()
    assert sorted(service.ledger.records) == ["Проекты отдела/Сметы/запись.mp4", "Проекты отдела/план.mp4",
                                              "Снабжение/вкс.mp4"]


def test_trash_is_emptied_after_the_keep_period_only(tmp_path):
    cfg, service, people = setup(tmp_path)
    old = folders.remove(cfg, service, people, "Проекты отдела", "admin")["trash"]
    for root in (cfg.inbox, cfg.outbox):
        past = (root / old).stat().st_mtime - folders.TRASH_DAYS * 86400 - 60
        os.utime(root / old, (past, past))
    service.ledger.put({"file": "Снабжение/вкс.mp4", "status": "ok"})
    fresh_dir = cfg.inbox / folders.TRASH / "свежая"
    fresh_dir.mkdir()
    assert folders.purge(cfg, lambda s: None) == (2, {})
    assert not (cfg.inbox / old).exists() and not (cfg.outbox / old).exists() and fresh_dir.is_dir()


def test_two_folders_with_one_name_deleted_in_one_second_do_not_collide(tmp_path, monkeypatch):
    """Метка корзины — время до секунды: две одноимённые папки, удалённые в одну секунду, легли бы
    в одну папку корзины, и «удалено.json» второй затёр бы первый."""
    cfg, service, people = setup(tmp_path)
    for root in (cfg.inbox, cfg.outbox):
        (root / "Снабжение" / "Сметы").mkdir(parents=True)
    (cfg.inbox / "Снабжение" / "Сметы" / "другая.mp4").write_bytes(b"other")
    monkeypatch.setattr(folders.time, "strftime", lambda fmt: "20261010-120000")
    first = folders.remove(cfg, service, people, "Проекты отдела/Сметы", "admin")["trash"]
    second = folders.remove(cfg, service, people, "Снабжение/Сметы", "admin")["trash"]
    assert (first, second) == (f"{folders.TRASH}/20261010-120000", f"{folders.TRASH}/20261010-120000-2")
    assert (cfg.inbox / first / "Сметы" / "запись.mp4").read_bytes() == b"video"
    assert (cfg.inbox / second / "Сметы" / "другая.mp4").read_bytes() == b"other"
    notes = [json.loads((cfg.outbox / t / "удалено.json").read_text(encoding="utf-8")) for t in (first, second)]
    assert [n["folder"] for n in notes] == ["Проекты отдела/Сметы", "Снабжение/Сметы"]
    assert len(notes[0]["records"]) == 1 and notes[1]["records"] == []


def test_each_trash_is_emptied_on_its_own_and_the_reason_of_a_skip_is_returned(tmp_path, monkeypatch):
    """Корзина входной папки не стирается (папка только для чтения) — корзина результатов
    чистится всё равно, а причина пропуска возвращается вызвавшему, а не сыплется в журнал."""
    cfg, service, people = setup(tmp_path)
    old = folders.remove(cfg, service, people, "Проекты отдела", "admin")["trash"]
    for root in (cfg.inbox, cfg.outbox):
        past = (root / old).stat().st_mtime - folders.TRASH_DAYS * 86400 - 60
        os.utime(root / old, (past, past))
    real = folders.shutil.rmtree

    def rmtree(path):
        if cfg.inbox in path.parents:
            raise OSError(30, "Read-only file system")
        real(path)

    monkeypatch.setattr(folders.shutil, "rmtree", rmtree)
    said = []
    assert folders.purge(cfg, said.append) == (1, {"входной папки": "Read-only file system"})
    assert (cfg.inbox / old).is_dir() and not (cfg.outbox / old).exists() and said == []


# ---------- всё или ничего: папка на диске и таблицы базы ----------

def state_of(cfg, service, people, words, trail):
    """Всё, что переименование и удаление папки меняют: диск, журнал обработки (в памяти и в базе),
    права, словари, отметки поручений, журнал событий."""
    files = sorted(p.relative_to(cfg.inbox.parent).as_posix() for root in (cfg.inbox, cfg.outbox)
                   for p in root.rglob("*") if p.is_file())
    on_disk = db.Database(cfg.outbox.parent / "state")              # другим соединением: что записано, а не что в памяти
    return {"files": files, "ledger": dict(service.ledger.records), "ledger on disk": Ledger(on_disk).records,
            "rights": people.users["ivanov"], "words": words.get(dictionary.FOLDERS, "Проекты отдела"),
            "marks": marks.Store(on_disk).everything(), "events": trail.read(), "report": trail.report(),
            "uploaded": trail.uploaded}


def full(tmp_path):
    """Папка, о которой знают все таблицы: запись с отметкой поручения, права, словарь, расходы."""
    cfg, service, people = setup(tmp_path)
    base = service.ledger.db
    words, trail, noted = dictionary.Store(base), journal.Journal(base), marks.Store(base)
    words.put(dictionary.FOLDERS, "Проекты отдела", dictionary.parse_people("Ершова Лидия Марковна"), [], "ivanov")
    words.put(dictionary.RECORDS, "Проекты отдела/план.mp4", [], dictionary.parse_terms("КС-2 — акт"), "ivanov")
    noted.restore("Проекты отдела/план.mp4", {"0123456789abcdef": {
        "status": "done", "by": "ivanov", "at": "2026-10-10T10:00:00+03:00", "who": "Иванов", "task": "Согласовать смету"}})
    trail.write("upload", "ivanov", file="Проекты отдела/план.mp4", size=5)
    trail.run({"file": "Проекты отдела/план.mp4", "status": "ok", "cost": {"RUB": 12.0}, "audio_duration_sec": 600.0,
               "cost_models": [{"stage": "speech", "model": "scribe", "provider": "RouterAI", "currency": "RUB", "cost": 12.0}],
               "cost_providers": [{"provider": "RouterAI", "currency": "RUB", "cost": 12.0}]}, "", "new")
    return cfg, service, people, words, trail


def refusing(error):
    def refuse(*args, **kw):
        raise error
    return refuse


@pytest.mark.parametrize("broken", ["ledger", "rights", "words", "event", "commit"])
def test_rename_that_fails_midway_leaves_nothing_half_done(tmp_path, monkeypatch, broken):
    """Сбой на любом шаге переименования — папки на диске, журнал обработки, права, словари, отметки
    и журнал расходов остаются такими, какими были: ни в базе, ни в памяти, ни на диске нет половины."""
    cfg, service, people, words, trail = full(tmp_path)
    before = state_of(cfg, service, people, words, trail)

    def whole(new):
        words.move_folder("Проекты отдела", new)
        trail.write("folder_rename", "admin", folder="Проекты отдела", to=new)

    def also(new):
        words.move_folder("Проекты отдела", new)
        if broken == "words":
            raise OSError("диск отвалился на словарях")
        trail.write("folder_rename", "admin", folder="Проекты отдела", to=new)
        if broken == "event":
            raise RuntimeError("сбой после записи события")
        if broken == "commit":
            # сама фиксация транзакции не удастся: отложенная проверка связей найдёт строку без пары
            service.ledger.db.con.execute("PRAGMA defer_foreign_keys=ON")
            service.ledger.db.con.execute("INSERT INTO marks (file, key, task) VALUES ('нет такой записи', 'k', 'т')")

    if broken == "ledger":
        monkeypatch.setattr(service.ledger, "move", refusing(sqlite3.OperationalError("disk I/O error")))
    if broken == "rights":
        monkeypatch.setattr(people, "rename_folder", refusing(sqlite3.OperationalError("database is locked")))
    with pytest.raises((OSError, RuntimeError, sqlite3.Error)):
        folders.rename(cfg, service, people, "Проекты отдела", "Проекты", also=also)
    assert state_of(cfg, service, people, words, trail) == before
    # и после неудачи то же действие проходит целиком
    monkeypatch.undo()
    assert folders.rename(cfg, service, people, "Проекты отдела", "Проекты", also=whole) == "Проекты"
    after = state_of(cfg, service, people, words, trail)
    assert sorted(after["ledger"]) == sorted(after["ledger on disk"]) == [
        "Проекты/Сметы/запись.mp4", "Проекты/план.mp4", "Снабжение/вкс.mp4"]
    assert list(after["marks"]) == ["Проекты/план.mp4"] and after["uploaded"] == {"Проекты/план.mp4": "ivanov"}
    assert after["rights"]["read"] == {"Проекты": "only", "Проекты/Сметы": "tree", "Снабжение": "tree"}
    assert words.get(dictionary.FOLDERS, "Проекты")["people"] and words.get(dictionary.RECORDS, "Проекты/план.mp4")["terms"]
    assert [r["file"] for r in after["report"]["records"]] == ["Проекты/план.mp4"]


@pytest.mark.parametrize("broken", ["rights", "words"])
def test_delete_that_fails_midway_returns_the_folder_from_the_trash(tmp_path, monkeypatch, broken):
    cfg, service, people, words, trail = full(tmp_path)
    before = state_of(cfg, service, people, words, trail)

    def also(done):
        words.drop_folder("Проекты отдела")
        if broken == "words" and not also.mended:
            raise OSError("диск отвалился на словарях")
        trail.write("folder_delete", "admin", folder="Проекты отдела", records=done["records"], trash=done["trash"])

    also.mended = False
    if broken == "rights":
        monkeypatch.setattr(people, "drop_folder", refusing(sqlite3.OperationalError("database is locked")))
    with pytest.raises((OSError, sqlite3.Error)):
        folders.remove(cfg, service, people, "Проекты отдела", "admin", also=also)
    assert state_of(cfg, service, people, words, trail) == before
    assert not list((cfg.outbox / folders.TRASH).rglob("*.json"))           # в корзине о несостоявшемся удалении ничего
    monkeypatch.undo()
    also.mended = True
    done = folders.remove(cfg, service, people, "Проекты отдела", "admin", also=also)
    note = json.loads((cfg.outbox / done["trash"] / "удалено.json").read_text(encoding="utf-8"))
    assert done["records"] == 2 and list(note["marks"]) == ["Проекты отдела/план.mp4"]
    assert marks.Store(service.ledger.db).everything() == {} and sorted(service.ledger.records) == ["Снабжение/вкс.mp4"]
    assert people.users["ivanov"]["read"] == {"Снабжение": "tree"}
    assert words.get(dictionary.FOLDERS, "Проекты отдела")["people"] == []
    assert trail.report()["total"]["cost"] == {"RUB": 12.0}                # расходы удаление не трогает
