"""Полная резервная копия: архив на ходу, опись, проверка, хранение последних копий, восстановление
в пустую установку."""
import datetime
import json
import os
import sqlite3
import tarfile
import threading
import types

import pytest

from app import backup, db, dictionary, journal, ledger, marks, settings, users
from tests import legacy
from tests.test_migrate import old_install

kept = lambda path: True                                            # в тестах папки «подключены с сервера»
quiet = lambda line: None


def install(tmp_path):
    """Установка с базой, в которой есть всё, и с файлами во всех четырёх папках."""
    cfg = old_install(tmp_path)
    legacy.start(cfg).close()
    cfg.prompts_dir, cfg.backup_dir = tmp_path / "prompts", tmp_path / "backup"
    for folder in (cfg.prompts_dir, cfg.backup_dir):
        folder.mkdir()
    (cfg.prompts_dir / "meeting.md").write_text("# шаблон с нашей правкой", encoding="utf-8")
    (cfg.prompts_dir / "prev").mkdir()
    (cfg.prompts_dir / "prev" / "meeting.20261001.md").write_text("# прежний", encoding="utf-8")
    (cfg.inbox / "Проекты").mkdir()
    (cfg.inbox / "Проекты" / "план.mp4").write_bytes(b"video" * 1000)
    (cfg.inbox / "Пустая папка").mkdir()                            # папка без записей — тоже папка на странице
    (cfg.inbox / ".uploads").mkdir()
    (cfg.inbox / ".uploads" / "недогрузка.part").write_bytes(b"half")
    (cfg.inbox / ".trash" / "20261001-100000").mkdir(parents=True)
    (cfg.inbox / ".trash" / "20261001-100000" / "удалённая.mp3").write_bytes(b"gone")
    (cfg.outbox / "Проекты" / "план" / "план.audio.mp3").write_bytes(b"sound" * 500)
    (cfg.outbox / "_logs").mkdir()
    (cfg.outbox / "_logs" / "podyachiy.log").write_text("2026-10-10 10:00:00 запущен\n", encoding="utf-8")
    return cfg


def members(archive) -> dict:
    with tarfile.open(archive) as tar:
        return {m.name: (tar.extractfile(m).read() if m.isfile() else None) for m in tar}


def test_archive_holds_everything_needed_and_the_manifest_describes_it(tmp_path):
    cfg = install(tmp_path)
    said = []
    made = backup.create(cfg, kept=kept, say=said.append, now=datetime.datetime(2026, 10, 10, 23, 30, 5).astimezone())
    archive = cfg.backup_dir / "podyachiy-backup-20261010-233005.tar"
    assert made["archive"] == str(archive) and archive.is_file()
    assert sorted(p.name for p in cfg.backup_dir.iterdir()) == [archive.name, archive.name + ".manifest.json"]
    found = members(archive)
    files = {name for name, body in found.items() if body is not None}
    keep = next(cfg.state_dir.glob("до-базы-*")).name
    assert {"state/podyachiy.db", "state/session.key", "state/ldap-ca.pem", "state/brand/logo.png",
            f"state/{keep}/users.json", f"state/{keep}/outbox/_state/processed.json",
            "prompts/meeting.md", "prompts/prev/meeting.20261001.md",
            "outbox/Проекты/план/summary/план.md", "outbox/Проекты/план/план.audio.mp3", "outbox/_logs/podyachiy.log",
            "outbox/.trash/x/итоги/итоги.tasks.json", "inbox/Проекты/план.mp4",
            "inbox/.trash/20261001-100000/удалённая.mp3", "MANIFEST.json"} <= files
    # спутники базы, недогрузки и сама папка копий в архив не идут; пустая папка — идёт
    assert not [n for n in found if "podyachiy.db-" in n or ".uploads" in n or n.startswith("backup")]
    assert found["inbox/Пустая папка"] is None and list(found)[-1] == "MANIFEST.json"

    manifest = json.loads(found["MANIFEST.json"])
    assert json.loads((cfg.backup_dir / (archive.name + ".manifest.json")).read_text(encoding="utf-8")) == manifest
    assert (manifest["kind"], manifest["format"], manifest["schema"], manifest["inbox"]) == (
        "podyachiy-backup", 1, db.SCHEMA_VERSION, True)
    assert manifest["created"].startswith("2026-10-10T23:30:05") and manifest["service"] == backup.about.version()
    assert {f["path"] for f in manifest["files"]} == files - {"MANIFEST.json"}
    listed = {f["path"]: f for f in manifest["files"]}
    video = listed["inbox/Проекты/план.mp4"]
    assert video["bytes"] == 5000 and video["sha256"] == backup.hashlib.sha256(b"video" * 1000).hexdigest()
    assert manifest["parts"]["inbox"] == {"files": 2, "bytes": 5004} and manifest["parts"]["prompts"]["files"] == 2
    assert manifest["bytes"] == sum(f["bytes"] for f in manifest["files"])
    assert manifest["base"]["counts"] == {"users": 3, "grants": 4, "keys": 2, "dictionaries": 2, "requests": 2, "records": 3,
                                          "marks": 2, "events": 9, "runs": 3, "spent": {"RUB": 25.0, "USD": 0.332468}}
    assert manifest["skipped"] == [] and any(".env" in line for line in manifest["not_included"])
    # база в архиве — один файл в обычном режиме журнала, открывается сама по себе
    (tmp_path / "copy.db").write_bytes(found["state/podyachiy.db"])
    copy = sqlite3.connect(tmp_path / "copy.db")
    assert copy.execute("PRAGMA quick_check").fetchone()[0] == "ok" and copy.execute("SELECT count(*) FROM users").fetchone()[0] == 3

    # след: строка в журнале работы, событие в журнале действий, «последняя копия» для страницы
    assert said[0].startswith("резервная копия сделана: podyachiy-backup-20261010-233005.tar") and "файлов" in said[0]
    assert said[0] in (cfg.outbox / "_logs" / "podyachiy.log").read_text(encoding="utf-8")
    base = db.Database(cfg.state_dir)
    event = journal.Journal(base).actions(kind="backup")[0]
    assert (event["file"], event["inbox"], event["skipped"], event["by"]) == (archive.name, True, 0, "")
    noted = backup.last(base)
    assert noted["file"] == archive.name and noted["bytes"] == archive.stat().st_size and noted["at"].startswith("20")


def test_verify_passes_a_whole_archive_and_names_what_is_wrong_with_a_damaged_one(tmp_path):
    cfg = install(tmp_path)
    archive = backup.create(cfg, kept=kept, say=quiet)["archive"]
    said = []
    manifest = backup.verify(archive, said.append)
    assert manifest["base"]["counts"]["events"] == 9
    for part in ("цел", "схема базы 1", "База читается и цела", "пользователей 3", "записей 3", "25.00 ₽ + 0.33 $"):
        assert part in said[0], part
    before = sorted(p.name for p in tmp_path.rglob("*"))
    backup.verify(archive, quiet)
    assert sorted(p.name for p in tmp_path.rglob("*")) == before        # проверка ничего не разворачивает и не оставляет

    data = bytearray(open(archive, "rb").read())
    broken = tmp_path / "broken.tar"

    def refused(body, match, side=None):
        broken.write_bytes(bytes(body))
        if side is not None:
            (tmp_path / "broken.tar.manifest.json").write_text(side, encoding="utf-8")
        with pytest.raises(backup.BackupError, match=match):
            backup.verify(broken, quiet)

    at = data.index(b"video" * 1000)
    flipped = bytearray(data)
    flipped[at + 2500] ^= 0xFF                                          # один испорченный байт в записи
    refused(flipped, "не сходится с описью: другой размер или контрольная сумма — 1 \\(inbox/Проекты/план.mp4\\)")
    refused(data[:len(data) // 2], "не читается|нет описи")              # архив оборван на середине
    refused(b"not a tar at all" * 100, "не читается")
    at = data.index(b"SQLite format 3")
    spoiled = bytearray(data)
    spoiled[at + 5000:at + 9000] = b"\xff" * 4000                       # испорчена сама база
    refused(spoiled, "не сходится с описью|повреждена")
    refused(data, "опись рядом с архивом .* не совпадает", side=json.dumps({"kind": "podyachiy-backup"}))
    with pytest.raises(backup.BackupError, match="архива .* нет"):
        backup.verify(tmp_path / "нет такого.tar", quiet)
    foreign = tmp_path / "foreign.tar"
    with tarfile.open(foreign, "w") as tar:                             # чужой архив с файлом, ведущим наружу
        info = tarfile.TarInfo("state/../../etc/passwd")
        info.size = 0
        tar.addfile(info)
    with pytest.raises(backup.BackupError, match="недопустимым именем"):
        backup.verify(foreign, quiet)


def test_copy_taken_while_the_service_writes_is_consistent(tmp_path):
    """Копия без остановки: пока идёт архив, сервис пишет в базу составные транзакции и кладёт файлы.
    В архиве — целый снимок базы: ни одного события без своего прогона; архив проходит проверку."""
    cfg = install(tmp_path)
    for n in range(300):                                                # чтобы копия шла не мгновенно
        (cfg.outbox / "Проекты" / f"файл {n}.txt").write_bytes(os.urandom(20_000))
    base = db.Database(cfg.state_dir)
    trail, stop, failures, done = journal.Journal(base), threading.Event(), [], []

    def service():
        try:
            n = 0
            while not stop.is_set():
                with base.write():
                    trail.write("upload", "ivanov", file=f"Проекты/на ходу {n}.mp3")
                    trail.write("run", "", file=f"Проекты/на ходу {n}.mp3", cost={"RUB": 1.0})
                (cfg.outbox / "Проекты" / f"на ходу {n}.txt").write_text("результат", encoding="utf-8")
                n += 1
            done.append(n)
        except Exception as e:
            failures.append(e)

    thread = threading.Thread(target=service)
    thread.start()
    try:
        archives = [backup.create(cfg, kept=kept, say=quiet, now=datetime.datetime(2026, 10, 10, 12, 0, n).astimezone())
                    for n in range(3)]
    finally:
        stop.set()
        thread.join()
    assert failures == [] and done[0] > 0
    for n, made in enumerate(archives):
        manifest = backup.verify(made["archive"], quiet)
        counts = manifest["base"]["counts"]
        # сверх исходных девяти событий — пары «загрузка и прогон» и по событию на каждую прежнюю копию
        assert counts["events"] - 9 - n == 2 * (counts["runs"] - 3), counts
        assert counts["spent"]["RUB"] == 25.0 + (counts["runs"] - 3)
    assert archives[0]["base"]["counts"]["events"] < archives[2]["base"]["counts"]["events"] or done[0] < 3
    # после копий сервис работает дальше, и события о копиях легли в ту же базу
    assert len(trail.actions(kind="backup")) == 3


def test_copy_without_the_source_recordings_says_so(tmp_path):
    cfg = install(tmp_path)
    said = []
    made = backup.create(cfg, with_inbox=False, kept=kept, say=said.append)
    assert not [f for f in made["files"] if f["path"].startswith("inbox/")] and "inbox" not in made["parts"]
    assert made["inbox"] is False and "без исходных записей" in said[0]
    assert any("исходные записи" in line for line in made["not_included"])
    assert "БЕЗ исходных записей" in (said.clear() or backup.verify(made["archive"], said.append) and said[0])
    assert journal.Journal(db.Database(cfg.state_dir)).actions(kind="backup")[0]["inbox"] is False


def test_only_the_last_copies_are_kept_when_asked(tmp_path):
    cfg = install(tmp_path)
    at = lambda day: datetime.datetime(2026, 10, day, 3, 0, 0).astimezone()
    for day in (1, 2, 3):
        backup.create(cfg, kept=kept, say=quiet, now=at(day))
    (cfg.backup_dir / "чужой файл.txt").write_text("не копия", encoding="utf-8")
    assert len(list(cfg.backup_dir.glob("*.tar"))) == 3                 # без --keep ничего не стирается
    said = []
    backup.create(cfg, keep=2, kept=kept, say=said.append, now=at(4))
    assert sorted(p.name for p in cfg.backup_dir.iterdir()) == [
        "podyachiy-backup-20261003-030000.tar", "podyachiy-backup-20261003-030000.tar.manifest.json",
        "podyachiy-backup-20261004-030000.tar", "podyachiy-backup-20261004-030000.tar.manifest.json", "чужой файл.txt"]
    assert "старых копий удалено — 2" in said[0]
    with pytest.raises(backup.BackupError, match="уже есть"):
        backup.create(cfg, kept=kept, say=quiet, now=at(4))


def test_copy_is_refused_with_a_reason_and_leaves_no_half_archive(tmp_path, monkeypatch):
    cfg = install(tmp_path)
    with pytest.raises(backup.BackupError, match="не подключена с сервера.*HOST_BACKUP"):
        backup.create(cfg, kept=lambda path: False, say=quiet)
    with pytest.raises(backup.BackupError, match="папки для копий .* нет.*HOST_BACKUP"):
        backup.create(cfg, to=tmp_path / "нет такой", kept=kept, say=quiet)
    empty = types.SimpleNamespace(**{**vars(cfg), "state_dir": tmp_path / "пустая"})
    with pytest.raises(backup.BackupError, match="базы .* нет: копировать нечего"):
        backup.create(empty, kept=kept, say=quiet)
    assert not (tmp_path / "пустая").exists()                           # и пустую базу команда не завела

    def full_disk(self, info, fileobj=None):                            # диск с копиями кончился посреди архива
        if info.name.startswith("outbox/"):
            raise OSError(28, "No space left on device")
        real(self, info, fileobj)

    real = tarfile.TarFile.addfile
    monkeypatch.setattr(tarfile.TarFile, "addfile", full_disk)
    with pytest.raises(backup.BackupError, match="копия не сделана.*No space left"):
        backup.create(cfg, kept=kept, say=quiet)
    assert list(cfg.backup_dir.iterdir()) == []                         # ни архива, ни описи, ни снимка базы
    monkeypatch.undo()
    assert journal.Journal(db.Database(cfg.state_dir)).actions(kind="backup") == []


def test_file_that_cannot_be_read_is_named_and_the_copy_is_marked_incomplete(tmp_path, monkeypatch):
    cfg = install(tmp_path)
    real_open = open

    def guarded(path, mode="r", *args, **kw):
        if str(path).endswith("план.audio.mp3") and "b" in mode:
            raise PermissionError(13, "Permission denied")
        return real_open(path, mode, *args, **kw)

    monkeypatch.setattr("builtins.open", guarded)
    said = []
    made = backup.create(cfg, kept=kept, say=said.append)
    monkeypatch.undo()
    assert made["skipped"] == [{"path": "outbox/Проекты/план/план.audio.mp3", "why": "Permission denied"}]
    assert "НЕ ПРОЧИТАНО файлов — 1: копия неполная" in said[0]
    assert "копия неполная" in (said.clear() or backup.verify(made["archive"], said.append) and said[0])


def tree(cfg) -> dict:
    """Файлы установки с содержимым и временем изменения (без базы и её спутников) и папки."""
    found = {}
    for part, root in backup.roots(cfg).items():
        for path in sorted(root.rglob("*")):
            name = f"{part}/{path.relative_to(root).as_posix()}"
            if path.is_file() and "podyachiy.db" not in path.name and ".uploads" not in name:
                found[name] = (path.read_bytes(), int(path.stat().st_mtime))
            elif path.is_dir() and ".uploads" not in name:
                found[name] = None
    return found


def state_of(cfg) -> dict:
    """Всё, что сервис знает из базы, — теми же классами, которыми он работает."""
    base = db.Database(cfg.state_dir)
    people, trail = users.Users(base, "секрет"), journal.Journal(base)
    try:
        return {"users": people.users, "keys": people.all_keys(now=0), "words": dictionary.Store(base).get("folders", "Проекты"),
                "speech": settings.Store(base).speech_model(), "records": ledger.Ledger(base).records,
                "marks": marks.Store(base).everything(), "report": trail.report(bucket="month"),
                "actions": [e for e in trail.actions(2000) if e["kind"] != "backup"], "uploaded": trail.uploaded}
    finally:
        base.close()


def test_restore_into_an_empty_installation_brings_everything_back(tmp_path):
    cfg = install(tmp_path)
    old_time = 1_700_000_000
    os.utime(cfg.inbox / "Проекты" / "план.mp4", (old_time, old_time))
    os.utime(cfg.outbox / ".trash" / "x", (old_time, old_time))         # по времени папки корзина отсчитывает 30 дней
    before_files, before_state = tree(cfg), state_of(cfg)
    made = backup.create(cfg, kept=kept, say=quiet)

    new = types.SimpleNamespace(state_dir=tmp_path / "новый" / "state", inbox=tmp_path / "новый" / "inbox",
                                outbox=tmp_path / "новый" / "outbox", prompts_dir=tmp_path / "новый" / "prompts",
                                backup_dir=cfg.backup_dir)
    said = []
    manifest = backup.restore(new, made["archive"], said.append, kept=kept)
    assert manifest["base"]["counts"] == made["base"]["counts"]
    after = tree(new)
    # журнал работы дописан строкой о восстановлении; остальное — байт в байт и с прежним временем
    log = "outbox/_logs/podyachiy.log"
    assert after[log][0].startswith(before_files[log][0]) and "установка восстановлена из архива".encode() in after[log][0]
    assert {k: v for k, v in after.items() if k != log} == {k: v for k, v in before_files.items() if k != log}
    assert after["inbox/Пустая папка"] is None and after["inbox/Проекты/план.mp4"][1] == old_time
    assert int((new.outbox / ".trash" / "x").stat().st_mtime) == old_time
    assert not (new.inbox / ".uploads").exists()
    # база: то же самое до последней отметки и копейки; пользователи входят прежними паролями
    assert state_of(new) == before_state
    people = users.Users(db.Database(new.state_dir), "секрет")
    assert people.verify("ivanov", "пароль-1234") and people.access("ivanov").uploads("Снабжение/Заявки")
    assert (new.state_dir / "session.key").read_bytes() == b"k" * 32   # входы в браузерах остаются в силе
    assert "установка восстановлена из архива" in said[0] and "25.00 ₽ + 0.33 $" in said[0]
    assert any(".env и compose.yml" in line and "docker compose up -d" in line for line in said)
    # сервис над восстановленной установкой запускается как обычно, без переноса и отказов
    lines = []
    legacy.start(new, lines).close()
    assert not any("перенос" in line or "ВНИМАНИЕ" in line for line in lines)


def test_restore_refuses_an_installation_that_is_not_empty_and_a_damaged_or_newer_archive(tmp_path):
    cfg = install(tmp_path)
    made = backup.create(cfg, kept=kept, say=quiet)
    with pytest.raises(backup.BackupError, match="только в пустую установку.*уже есть база.*не пуста"):
        backup.restore(cfg, made["archive"], quiet, kept=kept)             # поверх работающей — никогда
    target = lambda name: types.SimpleNamespace(
        state_dir=tmp_path / name / "state", inbox=tmp_path / name / "inbox", outbox=tmp_path / name / "outbox",
        prompts_dir=tmp_path / name / "prompts", backup_dir=cfg.backup_dir)
    with pytest.raises(backup.BackupError, match="не подключена с сервера"):
        backup.restore(target("а"), made["archive"], quiet, kept=lambda path: False)
    occupied = target("б")
    (occupied.outbox / "Чужое").mkdir(parents=True)
    with pytest.raises(backup.BackupError, match="outbox не пуста \\(Чужое\\)"):
        backup.restore(occupied, made["archive"], quiet, kept=kept)
    # испорченный архив не разворачивается вовсе: сначала проверка целиком
    data = bytearray(open(made["archive"], "rb").read())
    data[data.index(b"video" * 1000) + 10] ^= 0xFF
    (tmp_path / "broken.tar").write_bytes(bytes(data))
    with pytest.raises(backup.BackupError, match="не сходится с описью"):
        backup.restore(target("в"), tmp_path / "broken.tar", quiet, kept=kept)
    assert not (tmp_path / "в").exists()


def test_archive_of_an_older_schema_is_restored_and_the_service_updates_it_at_start(tmp_path, monkeypatch):
    """Архив старее сервиса: база в нём прежней схемы. Он разворачивается, а схему сервис доводит
    сам при запуске — теми же шагами, что и при обычном обновлении."""
    cfg = install(tmp_path)
    made = backup.create(cfg, kept=kept, say=quiet)
    steps = db.MIGRATIONS + (("CREATE TABLE later (id INTEGER PRIMARY KEY) STRICT",),)
    monkeypatch.setattr(db, "MIGRATIONS", steps)
    monkeypatch.setattr(db, "SCHEMA_VERSION", len(steps))
    new = types.SimpleNamespace(state_dir=tmp_path / "н" / "state", inbox=tmp_path / "н" / "inbox",
                                outbox=tmp_path / "н" / "outbox", prompts_dir=tmp_path / "н" / "prompts",
                                backup_dir=cfg.backup_dir)
    said = []
    backup.restore(new, made["archive"], said.append, kept=kept)
    assert any("схема базы в архиве старее (1)" in line and "обновит её сам при запуске" in line for line in said)
    lines = []
    base = db.Database(new.state_dir, lines.append)
    assert base.version == 2 and lines == ["схема базы обновлена: версия 1 → 2"] and users.Users(base).count() == 3
    base.close()

    # и наоборот: архив сделан более новой версией сервиса (схема 2), а разворачивает его прежняя
    newer = backup.create(new, kept=kept, say=quiet, now=datetime.datetime(2030, 1, 1).astimezone())
    assert newer["schema"] == 2
    monkeypatch.undo()
    older = types.SimpleNamespace(state_dir=tmp_path / "с" / "state", inbox=tmp_path / "с" / "inbox",
                                  outbox=tmp_path / "с" / "outbox", prompts_dir=tmp_path / "с" / "prompts",
                                  backup_dir=cfg.backup_dir)
    with pytest.raises(backup.BackupError, match="архив сделан более новой версией сервиса \(схема базы 2, эта версия знает 1\)"):
        backup.restore(older, newer["archive"], quiet, kept=kept)
    assert not (tmp_path / "с").exists()                               # ничего не развёрнуто
    backup.verify(newer["archive"], quiet)                             # а проверить такой архив можно и прежней версией


def test_command_line_returns_codes_and_writes_a_failed_copy_to_the_log(tmp_path, monkeypatch, capsys):
    cfg = install(tmp_path)
    monkeypatch.setattr(backup.Settings, "from_env", classmethod(lambda cls: cfg))
    monkeypatch.setattr(db, "kept", lambda path: True)
    monkeypatch.setattr(backup.create, "__kwdefaults__", None, raising=False)
    monkeypatch.setattr(backup.create, "__defaults__", (None, True, 0, print, db.kept, None))
    monkeypatch.setattr(backup.restore, "__defaults__", (print, db.kept))
    assert backup.main(["create", "--keep", "5"]) == 0
    out = capsys.readouterr().out
    assert "резервная копия сделана" in out and "архив: " in out and "опись: " in out
    assert backup.main(["verify"]) == 0 and "цел" in capsys.readouterr().out      # без имени — последний архив
    assert backup.main(["verify", str(tmp_path / "нет.tar")]) == 1 and "ОТКАЗ" in capsys.readouterr().err
    assert backup.main(["create", "--to", str(tmp_path / "нет такой")]) == 1
    assert "ОТКАЗ: папки для копий" in capsys.readouterr().err
    assert "ВНИМАНИЕ: резервная копия НЕ сделана: папки для копий" in (cfg.outbox / "_logs" / "podyachiy.log").read_text(encoding="utf-8")
    assert backup.main(["restore", str(next(cfg.backup_dir.glob("*.tar")))]) == 1   # поверх работающей установки
    assert "только в пустую установку" in capsys.readouterr().err


def test_one_deleted_record_comes_back_from_the_archive_with_its_marks_and_words(tmp_path):
    """Запись удалили по ошибке, корзина уже пуста: из вчерашнего архива возвращаются её файлы,
    строка журнала обработки, отметки поручений и словарь; расходы и остальное не трогаются."""
    cfg = install(tmp_path)
    file = "Проекты/план.mp4"
    made = backup.create(cfg, kept=kept, say=quiet)
    base = db.Database(cfg.state_dir)
    book, trail = ledger.Ledger(base), journal.Journal(base)
    before = {"record": book.get(file), "marks": marks.Store(base).load(file), "report": trail.report(),
              "words": dictionary.Store(base).get("records", file), "files": tree(cfg)}
    # удаление, как его делает сервис, и чистка корзины
    with base.write():
        book.forget(file)
        dictionary.Store(base).drop_record(file)
        trail.write("record_delete", "ivanov", file=file, trash=".trash/x")
    (cfg.inbox / file).unlink()
    __import__("shutil").rmtree(cfg.outbox / "Проекты" / "план")
    base.close()
    assert marks.Store(db.Database(cfg.state_dir)).everything() == {}

    with pytest.raises(backup.BackupError, match="в архиве .* нет"):
        backup.record(cfg, made["archive"], "Проекты/нет такой.mp4", quiet)
    said = []
    done = backup.record(cfg, made["archive"], file, said.append)
    assert done == {"file": file, "files": 3, "marks": 2} and "возвращена из архива" in said[0]
    base = db.Database(cfg.state_dir)
    assert ledger.Ledger(base).get(file) == before["record"] and marks.Store(base).load(file) == before["marks"]
    assert dictionary.Store(base).get("records", file) == before["words"]
    assert journal.Journal(base).report() == before["report"] and journal.Journal(base).uploader(file) == "ivanov"
    assert journal.Journal(base).actions(kind="record_restore")[0]["archive"] == made["archive"].rsplit("/", 1)[-1]
    after = tree(cfg)
    log = "outbox/_logs/podyachiy.log"
    assert {k: v for k, v in after.items() if k != log} == {k: v for k, v in before["files"].items() if k != log}
    with pytest.raises(backup.BackupError, match="уже есть"):
        backup.record(cfg, made["archive"], file, quiet)               # второй раз возвращать нечего
