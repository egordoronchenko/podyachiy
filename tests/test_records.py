import json
import sqlite3
import threading
import types

import pytest

from app import db, dictionary, folders, ledger, marks, records, watcher
from app.ledger import Ledger

FILE = "Проекты отдела/план.mp4"


def setup(tmp_path):
    """Одна готовая запись со всеми файлами результата и пустая папка рядом."""
    cfg = types.SimpleNamespace(inbox=tmp_path / "in", outbox=tmp_path / "out", outbox_inside_inbox=None,
                                stable_sec=0)
    (cfg.inbox / "Проекты отдела").mkdir(parents=True)
    (cfg.inbox / "Снабжение").mkdir()
    (cfg.inbox / FILE).write_bytes(b"video")
    out = cfg.outbox / "Проекты отдела" / "план"
    for rel in ("план.audio.mp3", "summary/план.md", "summary/план.map.html", "summary/prev/план.20261001-120000.md",
                "transcript/план.txt", "transcript/план.json", "transcript/план.names.json",
                "transcript/raw/план.part01.json"):
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text("{}", encoding="utf-8")
    book = Ledger(db.Database(cfg.outbox.parent / "state"))
    book.put({"file": FILE, "stem": "план", "status": "ok", "output": str(out), "cost": 0.5,
              "transcript": "transcript/план.txt", "summary": "summary/план.md", "source_size": 5})
    ledger.write_marker(out, "план", book.get(FILE))
    service = types.SimpleNamespace(cfg=cfg, ledger=book, queue=[], pending={}, running=[])
    return cfg, service


def scanner(cfg, service):
    """Настоящий сканер над теми же папками и тем же журналом."""
    return watcher.Service(cfg=cfg, log=lambda s: None, ledger=service.ledger, journal=None,
                           speech_model=lambda: "elevenlabs/scribe-v2", tell=lambda record, kind, login, by_hand: None,
                           words=lambda rel: dictionary.merge([]), series=lambda rel: True)


def test_rename_moves_source_results_ledger_and_marker(tmp_path):
    cfg, service = setup(tmp_path)
    new = records.rename(cfg, service, FILE, " Планёрка 06.10 ")
    assert new == "Проекты отдела/Планёрка 06.10.mp4"                   # расширение прежнее
    assert (cfg.inbox / new).read_bytes() == b"video" and not (cfg.inbox / FILE).exists()
    out = cfg.outbox / "Проекты отдела" / "Планёрка 06.10"
    assert not (cfg.outbox / "Проекты отдела" / "план").exists()
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "summary/prev/Планёрка 06.10.20261001-120000.md", "summary/Планёрка 06.10.map.html",
        "summary/Планёрка 06.10.md", "transcript/raw/Планёрка 06.10.part01.json",
        "transcript/Планёрка 06.10.json", "transcript/Планёрка 06.10.names.json",
        "transcript/Планёрка 06.10.txt", "Планёрка 06.10.audio.mp3", "Планёрка 06.10.done.json"]
    rec = service.ledger.get(new)
    assert service.ledger.get(FILE) is None and list(service.ledger.records) == [new]
    assert rec["file"] == new and rec["stem"] == "Планёрка 06.10" and rec["output"] == str(out)
    assert rec["transcript"] == "transcript/Планёрка 06.10.txt" and rec["summary"] == "summary/Планёрка 06.10.md"
    assert rec["runs"] == 1 and rec["cost_total"] == {"USD": 0.5}             # прогона не было — счётчики те же
    assert json.loads((out / "Планёрка 06.10.done.json").read_text(encoding="utf-8"))["file"] == new
    assert Ledger(db.Database(cfg.outbox.parent / "state")).get(new)["stem"] == "Планёрка 06.10"   # журнал записан на диск


def test_name_typed_with_the_extension_does_not_double_it(tmp_path):
    cfg, service = setup(tmp_path)
    assert records.rename(cfg, service, FILE, "Итоги.MP4") == "Проекты отдела/Итоги.mp4"
    assert records.rename(cfg, service, "Проекты отдела/Итоги.mp4", "Итоги") == "Проекты отдела/Итоги.mp4"   # то же имя


def test_move_keeps_the_name_and_changes_the_folder(tmp_path):
    cfg, service = setup(tmp_path)
    new = records.move(cfg, service, FILE, "Снабжение")
    assert new == "Снабжение/план.mp4" and (cfg.inbox / new).is_file() and not (cfg.inbox / FILE).exists()
    assert (cfg.outbox / "Снабжение/план/summary/план.md").is_file()
    assert (cfg.outbox / "Снабжение/план/transcript/план.names.json").is_file()      # подписи говорящих
    rec = service.ledger.get(new)
    assert rec["output"] == str(cfg.outbox / "Снабжение" / "план") and rec["stem"] == "план"
    assert rec["summary"] == "summary/план.md"
    assert records.move(cfg, service, new, "") == "план.mp4" and (cfg.outbox / "план/план.audio.mp3").is_file()


def test_scanner_does_not_take_a_renamed_or_moved_record_for_a_new_one(tmp_path):
    cfg, service = setup(tmp_path)
    scan = scanner(cfg, service)
    renamed = records.rename(cfg, service, FILE, "Итоги")
    scan.scan_once()
    moved = records.move(cfg, service, renamed, "Снабжение")
    scan.scan_once()
    scan.scan_once()
    assert scan.queue == [] and scan.pending == {} and list(service.ledger.records) == [moved]
    # обратная проверка: файл без строки в журнале сканер берёт — значит, выше молчал не случайно
    (cfg.inbox / "Снабжение" / "новая.mp4").write_bytes(b"video")
    scan.scan_once()
    scan.scan_once()
    assert [i.path for i in scan.queue] == ["Снабжение/новая.mp4"]


def test_scanner_waits_while_a_record_is_being_moved(tmp_path):
    cfg, service = setup(tmp_path)
    scan = scanner(cfg, service)
    scan.scan_lock.acquire()                  # так действие с записью держит замок в web.py
    walk = threading.Thread(target=scan.scan_once)
    walk.start()
    walk.join(0.3)
    assert walk.is_alive() and scan.scans == 0
    scan.scan_lock.release()
    walk.join(5)
    assert scan.scans == 1


def test_taken_and_bad_names_are_refused(tmp_path):
    cfg, service = setup(tmp_path)
    (cfg.inbox / "Проекты отдела" / "смета.mp4").write_bytes(b"video")
    (cfg.inbox / "Проекты отдела" / "Отчёт.MP3").write_bytes(b"audio")
    (cfg.outbox / "Проекты отдела" / "занято").mkdir()
    for name in ("смета", "отчёт", "занято"):         # то же имя, то же имя с другим расширением, чужие результаты
        with pytest.raises(records.RecordError) as e:
            records.rename(cfg, service, FILE, name)
        assert e.value.status == 409, name
    for name in ("", "  ", "..", "а/б", "а\\б", ".скрытая", "точка.", "во?прос"):
        with pytest.raises(records.RecordError) as e:
            records.rename(cfg, service, FILE, name)
        assert e.value.status == 400, name
    assert (cfg.inbox / FILE).is_file() and list(service.ledger.records) == [FILE]


def test_move_refuses_missing_hidden_and_occupied_folders(tmp_path):
    cfg, service = setup(tmp_path)
    (cfg.inbox / ".trash").mkdir()
    (cfg.inbox / "Снабжение" / "план.mp3").write_bytes(b"audio")
    for folder, status in (("Нет такой", 404), (".trash", 400), ("Снабжение/../.trash", 400),
                           ("Пользовательские", 400), ("Снабжение", 409)):
        with pytest.raises(records.RecordError) as e:
            records.move(cfg, service, FILE, folder)
        assert e.value.status == status, folder
    assert records.move(cfg, service, FILE, "Проекты отдела") == FILE          # в ту же папку — ничего не делает
    assert (cfg.inbox / FILE).is_file()


def test_busy_and_unknown_records_are_refused(tmp_path):
    cfg, service = setup(tmp_path)
    act = (lambda: records.rename(cfg, service, FILE, "Итоги"), lambda: records.move(cfg, service, FILE, "Снабжение"),
           lambda: records.remove(cfg, service, FILE, "admin"))
    service.queue = [types.SimpleNamespace(path=FILE)]
    for step in act:
        with pytest.raises(records.RecordError) as e:
            step()
        assert e.value.status == 409
    service.queue, service.running = [], [{"file": FILE}]
    for step in act:
        with pytest.raises(records.RecordError) as e:
            step()
        assert e.value.status == 409
    service.running = [{"file": "Снабжение/другая.mp4"}]           # чужая обработка не мешает
    with pytest.raises(records.RecordError) as e:
        records.remove(cfg, service, "Проекты отдела/нет.mp4", "admin")
    assert e.value.status == 404
    assert (cfg.inbox / FILE).is_file() and records.rename(cfg, service, FILE, "Итоги")


def test_delete_moves_everything_to_trash_and_forgets_the_record(tmp_path):
    cfg, service = setup(tmp_path)
    done = records.remove(cfg, service, FILE, "ivanov")
    assert done["keep_days"] == folders.TRASH_DAYS and done["trash"].startswith(folders.TRASH + "/")
    assert not (cfg.inbox / FILE).exists() and not (cfg.outbox / "Проекты отдела" / "план").exists()
    assert (cfg.inbox / "Проекты отдела").is_dir()                      # папка остаётся
    assert service.ledger.records == {} and Ledger(db.Database(cfg.outbox.parent / "state")).records == {}
    assert (cfg.inbox / done["trash"] / "план.mp4").read_bytes() == b"video"
    assert (cfg.outbox / done["trash"] / "план" / "summary" / "план.md").is_file()
    note = json.loads((cfg.outbox / done["trash"] / "удалено.json").read_text(encoding="utf-8"))
    assert note["file"] == FILE and note["by"] == "ivanov" and note["records"][0]["stem"] == "план"
    assert len(note["moved"]) == 2
    scan = scanner(cfg, service)
    scan.scan_once()
    scan.scan_once()
    assert scan.queue == [] and scan.pending == {}                  # из корзины запись в обработку не идёт


def test_two_deletions_in_one_second_do_not_collide(tmp_path):
    cfg, service = setup(tmp_path)
    (cfg.inbox / "Снабжение" / "план.mp4").write_bytes(b"other")
    service.ledger.put({"file": "Снабжение/план.mp4", "status": "no_audio", "stem": "план"})
    first = records.remove(cfg, service, FILE, "admin")["trash"]
    second = records.remove(cfg, service, "Снабжение/план.mp4", "admin")["trash"]
    assert first != second
    assert (cfg.inbox / first / "план.mp4").read_bytes() == b"video"
    assert (cfg.inbox / second / "план.mp4").read_bytes() == b"other"
    assert folders.purge(cfg, lambda s: None) == (0, {})                  # свежее корзина не стирает


def test_record_without_the_source_file_still_moves_its_results(tmp_path):
    cfg, service = setup(tmp_path)
    (cfg.inbox / FILE).unlink()
    new = records.rename(cfg, service, FILE, "Архив")
    assert (cfg.outbox / "Проекты отдела/Архив/summary/Архив.md").is_file() and service.ledger.get(new)


# ---------- всё или ничего: файлы записи и база ----------

def test_rename_move_and_delete_that_fail_midway_put_the_files_back(tmp_path, monkeypatch):
    """База не приняла переименование, перенос или удаление — исходник, папка результатов и имена
    файлов в ней возвращаются на место, журнал обработки (в памяти и в базе) и отметки — прежние."""
    cfg, service = setup(tmp_path)
    noted = marks.Store(service.ledger.db)
    noted.restore(FILE, {"0123456789abcdef": {"due": "2026-10-20", "due_by": "ivanov", "due_at": "2026-10-10T10:00:00+03:00",
                                              "who": None, "task": "Проверить остатки"}})

    def state():
        files = sorted(p.relative_to(tmp_path).as_posix() for root in (cfg.inbox, cfg.outbox) for p in root.rglob("*")
                       if p.is_file())
        return files, dict(service.ledger.records), Ledger(db.Database(cfg.outbox.parent / "state")).records, noted.everything()

    def refuse(*args):
        raise sqlite3.OperationalError("database or disk is full")

    before = state()
    for action, args in ((records.rename, ("Планёрка 06.10",)), (records.move, ("Снабжение",)), (records.remove, ("ivanov",))):
        with pytest.raises(sqlite3.OperationalError):
            action(cfg, service, FILE, *args, also=refuse)
        assert state() == before, action.__name__
    # журнал обработки сам отказал посреди действия — то же самое
    monkeypatch.setattr(service.ledger, "rekey", refuse)
    with pytest.raises(sqlite3.OperationalError):
        records.rename(cfg, service, FILE, "Планёрка 06.10")
    assert state() == before
    monkeypatch.undo()
    new = records.rename(cfg, service, FILE, "Планёрка 06.10")
    assert list(noted.everything()) == [new] and service.ledger.get(new)["stem"] == "Планёрка 06.10"


def test_record_is_not_renamed_or_moved_onto_the_name_of_a_subfolder(tmp_path):
    """Подпапка «Сметы» ещё без результатов: запись «Сметы.mp4» рядом с ней положила бы свои результаты
    в папку «Сметы», куда лягут и результаты записей подпапки."""
    cfg, service = setup(tmp_path)
    (cfg.inbox / "Проекты отдела" / "Сметы").mkdir()
    (cfg.inbox / "Снабжение" / "план").mkdir()
    with pytest.raises(records.RecordError) as e:
        records.rename(cfg, service, FILE, "Сметы")
    assert e.value.status == 409 and "подпапка" in e.value.detail
    with pytest.raises(records.RecordError) as e:
        records.move(cfg, service, FILE, "Снабжение")
    assert e.value.status == 409 and "подпапка" in e.value.detail
    assert (cfg.inbox / FILE).is_file() and list(service.ledger.records) == [FILE]
