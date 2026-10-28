import json
import os
import pathlib
import sqlite3
import threading
import types
import unicodedata

import pytest

from app import db, dictionary, folders, ledger, marks, records, watcher
from app.ledger import Ledger
from tests.test_folders import case_blind

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
    # обратная проверка: файл без строки в журнале сканер берёт — значит, выше молчал не случайно. Другой:
    # тот же файл был бы копией записи, а копию сервис сам не распознаёт (test_watcher)
    (cfg.inbox / "Снабжение" / "новая.mp4").write_bytes(b"other video")
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


def test_failure_on_the_files_after_the_database_leaves_nothing_half_done(tmp_path, monkeypatch):
    """Маркер рядом с результатами или опись корзины не записались (диск кончился). Действие тогда не
    выполнено целиком: база прежняя, файлы на месте. Иначе база уже записала бы действие, а человек
    получил бы отказ 500 — и, повторив, «записи нет» или «имя занято»."""
    cfg, service = setup(tmp_path)
    marker = cfg.outbox / "Проекты отдела" / "план" / f"план{ledger.MARKER_SUFFIX}"
    marker_before = marker.read_bytes()
    real = pathlib.Path.write_text

    def full_disk(self, *args, **kwargs):
        if self.name.endswith(ledger.MARKER_SUFFIX) or self.name == "удалено.json":
            raise OSError(28, "No space left on device")
        return real(self, *args, **kwargs)

    def state():
        files = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for root in (cfg.inbox, cfg.outbox)
                 for p in root.rglob("*") if p.is_file()}
        return files, dict(service.ledger.records), Ledger(db.Database(cfg.outbox.parent / "state")).records

    before = state()
    monkeypatch.setattr(pathlib.Path, "write_text", full_disk)
    for action, args in ((records.rename, ("Планёрка 06.10",)), (records.move, ("Снабжение",)), (records.remove, ("ivanov",))):
        with pytest.raises(OSError):
            action(cfg, service, FILE, *args)
        assert state() == before, action.__name__
    assert not (cfg.outbox / folders.TRASH).exists() or not list((cfg.outbox / folders.TRASH).rglob("*"))
    monkeypatch.undo()
    new = records.rename(cfg, service, FILE, "Планёрка 06.10")
    moved = cfg.outbox / "Проекты отдела" / "Планёрка 06.10" / f"Планёрка 06.10{ledger.MARKER_SUFFIX}"
    assert json.loads(moved.read_text(encoding="utf-8"))["file"] == new and marker_before != moved.read_bytes()


def test_rename_that_changes_only_the_case_is_done_on_a_case_blind_volume(tmp_path, monkeypatch):
    """«план.mp4» → «План.mp4» на томе без различия регистра: «План.mp4» уже «есть» — это она сама.
    Раньше — отказ «запись с таким именем в этой папке уже есть»."""
    cfg, service = setup(tmp_path)
    case_blind(monkeypatch, tmp_path)
    new = records.rename(cfg, service, FILE, "План")
    assert new == "Проекты отдела/План.mp4" and list(service.ledger.records) == [new]
    assert sorted(os.listdir(cfg.inbox / "Проекты отдела")) == ["План.mp4"]
    out = cfg.outbox / "Проекты отдела"
    assert os.listdir(out) == ["План"] and sorted(os.listdir(out / "План" / "transcript")) == [
        "raw", "План.json", "План.names.json", "План.txt"]
    assert json.loads((out / "План" / f"План{ledger.MARKER_SUFFIX}").read_text(encoding="utf-8"))["file"] == new
    assert service.ledger.get(new)["stem"] == "План" and service.ledger.get(new)["output"] == str(out / "План")
    (cfg.inbox / "Проекты отдела" / "итоги.mp4").write_bytes(b"other")
    with pytest.raises(records.RecordError) as e:
        records.rename(cfg, service, new, "ИТОГИ")                   # а другая запись с тем же именем — занята
    assert e.value.status == 409


def test_new_record_name_from_a_mac_is_stored_in_one_form(tmp_path):
    cfg, service = setup(tmp_path)
    new = records.rename(cfg, service, FILE, unicodedata.normalize("NFD", "Итоги ё"))
    assert new == "Проекты отдела/Итоги ё.mp4" and (cfg.inbox / new).is_file() and list(service.ledger.records) == [new]


def test_adopt_moves_results_to_a_file_already_renamed_on_disk_and_refuses_otherwise(tmp_path):
    """Исходник переименовали мимо сервиса: файл уже по новому пути, переезжают результаты и строка журнала.
    Прежний файл на месте или нового нет — это не переименование, и ничего не трогается."""
    cfg, service = setup(tmp_path)
    with pytest.raises(records.RecordError):
        records.adopt(cfg, service, FILE, "Снабжение/план.mp4")              # нового файла нет
    (cfg.inbox / "Снабжение/план.mp4").write_bytes(b"video")
    with pytest.raises(records.RecordError):
        records.adopt(cfg, service, FILE, "Снабжение/план.mp4")              # а прежний на месте — копия
    assert service.ledger.get(FILE) and (cfg.outbox / "Проекты отдела/план/summary/план.md").is_file()
    (cfg.inbox / FILE).unlink()
    assert records.adopt(cfg, service, FILE, "Снабжение/план.mp4") == "Снабжение/план.mp4"
    assert (cfg.outbox / "Снабжение/план/summary/план.md").is_file() and service.ledger.get(FILE) is None
    assert (cfg.inbox / "Снабжение/план.mp4").read_bytes() == b"video"


# ---------- возврат из корзины ----------

def test_deleted_record_comes_back_from_the_trash_without_processing(tmp_path):
    cfg, service = setup(tmp_path)
    marks.Store(service.ledger.db).restore(FILE, {"k1": {"who": "Иванов", "task": "Смета", "status": "done",
                                                         "by": "ivanov", "at": "2026-10-10T10:00:00+03:00"}})
    before = dict(service.ledger.get(FILE))
    stamp = records.remove(cfg, service, FILE, "ivanov", also=lambda done: {"uploads": {FILE: "ivanov"}})["trash"]
    stamp = stamp.rpartition("/")[2]
    [item] = folders.binned(cfg)
    assert (item["id"], item["kind"], item["path"], item["by"], item["partial"]) == (stamp, "record", FILE, "ivanov", False)
    seen = []
    assert records.restore(cfg, service, stamp, also=seen.append) == FILE
    assert seen[0]["kept"] == {"uploads": {FILE: "ivanov"}}
    assert (cfg.inbox / FILE).read_bytes() == b"video"
    assert (cfg.outbox / "Проекты отдела/план/summary/план.md").is_file()
    assert service.ledger.get(FILE) == before                          # та же строка: прогонов и расходов не прибавилось
    assert Ledger(db.Database(cfg.outbox.parent / "state")).get(FILE)["runs"] == 1
    assert marks.Store(service.ledger.db).load(FILE)["k1"]["status"] == "done"
    assert not (cfg.inbox / folders.TRASH / stamp).exists() and not (cfg.outbox / folders.TRASH / stamp).exists()
    assert folders.binned(cfg) == []
    scan = scanner(cfg, service)
    scan.scan_once()
    scan.scan_once()
    assert scan.queue == [] and scan.pending == {}                  # вернувшаяся запись в обработку не идёт
    with pytest.raises(records.RecordError) as e:
        records.restore(cfg, service, stamp)                        # второй раз вернуть нечего
    assert e.value.status == 404


def test_record_is_not_restored_over_a_new_one_or_into_a_gone_folder(tmp_path):
    cfg, service = setup(tmp_path)
    stamp = records.remove(cfg, service, FILE, "ivanov")["trash"].rpartition("/")[2]
    (cfg.inbox / "Проекты отдела/план.mkv").write_bytes(b"new")         # новая запись с тем же названием
    with pytest.raises(records.RecordError) as e:
        records.restore(cfg, service, stamp)
    assert e.value.status == 409 and "на прежнем месте уже есть запись" in e.value.detail
    (cfg.inbox / "Проекты отдела/план.mkv").unlink()
    (cfg.inbox / "Проекты отдела").rename(cfg.inbox / "Другое")
    with pytest.raises(records.RecordError) as e:
        records.restore(cfg, service, stamp)
    assert e.value.status == 409 and "папки, в которой это лежало, больше нет" in e.value.detail
    assert (cfg.inbox / folders.TRASH / stamp / "план.mp4").is_file()       # всё осталось в корзине
    assert (cfg.outbox / folders.TRASH / stamp / folders.NOTE).is_file() and service.ledger.records == {}


def test_restore_that_fails_on_the_database_puts_the_files_back_into_the_trash(tmp_path):
    cfg, service = setup(tmp_path)
    stamp = records.remove(cfg, service, FILE, "ivanov")["trash"].rpartition("/")[2]
    note = (cfg.outbox / folders.TRASH / stamp / folders.NOTE).read_bytes()

    def broken(_):
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        records.restore(cfg, service, stamp, also=broken)
    assert not (cfg.inbox / FILE).exists() and not (cfg.outbox / "Проекты отдела/план").exists()
    assert (cfg.inbox / folders.TRASH / stamp / "план.mp4").is_file()
    assert (cfg.outbox / folders.TRASH / stamp / folders.NOTE).read_bytes() == note
    assert service.ledger.records == {} and Ledger(db.Database(cfg.outbox.parent / "state")).records == {}
    assert records.restore(cfg, service, stamp) == FILE                 # причина ушла — возвращается


def test_tampered_trash_note_does_not_move_files_outside_the_inbox(tmp_path):
    cfg, service = setup(tmp_path)
    stamp = records.remove(cfg, service, FILE, "ivanov")["trash"].rpartition("/")[2]
    path = cfg.outbox / folders.TRASH / stamp / folders.NOTE
    note = json.loads(path.read_text(encoding="utf-8"))
    for file in ("../чужое/план.mp4", ".trash/план.mp4", "Проекты отдела/../../план.mp4"):
        path.write_text(json.dumps({**note, "file": file, "records": [{**note["records"][0], "file": file}]}),
                        encoding="utf-8")
        with pytest.raises(records.RecordError) as e:
            records.restore(cfg, service, stamp)
        assert e.value.status == 409, file
    for bad in ("../x", "20261010-101010/../..", ""):
        with pytest.raises(records.RecordError) as e:
            records.restore(cfg, service, bad)
        assert e.value.status == 404


def test_runs_of_a_returned_record_follow_it_again_in_the_base_and_in_the_replay(tmp_path):
    """Прогоны удалённой записи застывают под её путём; возврат из корзины их размораживает — и в базе,
    и при проигрывании событий (journal.runs). Прогоны другой записи, удалённой с того же пути раньше,
    остаются застывшими."""
    from app import journal
    trail = journal.Journal(db.Database(tmp_path / "state"))
    run = lambda: trail.write("run", "", file="П/а.mp4", cost=1.0, status="ok")
    run()
    trail.write("record_delete", "ivanov", file="П/а.mp4", trash=".trash/20261001-100000")
    run()
    trail.write("upload", "petrov", file="П/а.mp4")
    trail.write("record_delete", "ivanov", file="П/а.mp4", trash=".trash/20261002-100000")
    trail.write("record_restore", "ivanov", file="П/а.mp4", trash=".trash/20261002-100000",
                uploads={"П/а.mp4": "petrov"})
    trail.write("record_rename", "ivanov", file="П/а.mp4", to="П/б.mp4")
    assert [r["file"] for r in trail.within()] == ["П/а.mp4", "П/б.mp4"]
    assert [r["file"] for r in journal.runs(trail.read())] == ["П/а.mp4", "П/б.mp4"]
    assert trail.uploaded == {"П/б.mp4": "petrov"}
