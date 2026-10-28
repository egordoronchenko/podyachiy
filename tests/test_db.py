"""База сервиса: схема и её шаги, транзакции, одновременная запись, отказы при запуске,
файловая система папки состояния, снимок на ходу."""
import sqlite3
import subprocess
import sys
import threading

import pytest

from app import db, journal

EXTRA = (("CREATE TABLE extra (id INTEGER PRIMARY KEY, note TEXT) STRICT", "INSERT INTO extra (note) VALUES ('шаг 2')"),)


def test_new_base_gets_the_whole_schema_and_the_safe_modes(tmp_path):
    base = db.Database(tmp_path / "state")
    assert base.created and base.path == tmp_path / "state" / "podyachiy.db" and base.path.is_file()
    assert base.version == db.SCHEMA_VERSION == len(db.MIGRATIONS)
    tables = {r["name"] for r in base.rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"users", "grants", "keys", "dictionaries", "settings", "template_notes", "requests", "records", "marks",
            "events", "runs", "run_money", "run_stages", "run_providers", "uploads", "meta", "series_off"} <= tables
    assert base.value("PRAGMA journal_mode") == "wal" and base.value("PRAGMA foreign_keys") == 1
    assert base.value("PRAGMA synchronous") == 2                    # FULL: закрытая транзакция переживает и отключение питания
    base.check()
    again = db.Database(tmp_path / "state")
    assert not again.created and again.version == db.SCHEMA_VERSION


def test_strict_tables_and_links_refuse_what_the_code_must_not_write(tmp_path):
    base = db.Database(tmp_path)
    for bad in ("INSERT INTO grants (login, kind, folder, scope) VALUES ('нет такого', 'read', 'П', 'tree')",    # права без человека
                "INSERT INTO marks (file, key, task) VALUES ('нет такой записи', 'k', 'т')",                        # отметка без записи
                "INSERT INTO users (login, name, home, admin) VALUES ('a', 'А', 'П/a', 'да')",                      # не тот тип
                "INSERT INTO requests (id, at, name, email, login, comment, status) VALUES ('1', '', '', '', '', '', 'странный')"):
        with pytest.raises(sqlite3.IntegrityError):
            with base.write() as con:
                con.execute(bad)


def test_schema_steps_are_applied_in_order_and_a_failed_step_changes_nothing(tmp_path):
    lines = []
    base = db.Database(tmp_path, lines.append)
    with base.write() as con:
        con.execute("INSERT INTO meta (key, value) VALUES ('память', 'цела')")
    base.close()
    assert lines == []                                              # новая база — не «обновление схемы»

    # шаг с ошибкой: первая его команда уже выполнилась, но шаг откатывается целиком
    n = len(db.MIGRATIONS)
    broken = db.MIGRATIONS + (("CREATE TABLE half (id INTEGER PRIMARY KEY)", "CREATE TABLE users (x INTEGER)"),)
    with pytest.raises(db.Refusal, match=f"до версии {n + 1} не удалось.*осталась версии {n}"):
        db.Database(tmp_path, lines.append, steps=broken)
    same = db.Database(tmp_path, lines.append)
    assert same.version == n and same.row("SELECT name FROM sqlite_master WHERE name = 'half'") is None
    same.close()

    newer = db.Database(tmp_path, lines.append, steps=db.MIGRATIONS + EXTRA + (("ALTER TABLE extra ADD COLUMN more TEXT",),))
    assert newer.version == n + 2 and newer.noted("память") == "цела"
    assert [dict(r) for r in newer.rows("SELECT * FROM extra")] == [{"id": 1, "note": "шаг 2", "more": None}]
    assert lines == [f"схема базы обновлена: версия {n} → {n + 1}", f"схема базы обновлена: версия {n + 1} → {n + 2}"]
    newer.close()
    # база от более новой версии сервиса — этой версией не открывается и не трогается
    with pytest.raises(db.Refusal, match="от более новой версии сервиса"):
        db.Database(tmp_path)
    assert db.Database(tmp_path, steps=db.MIGRATIONS + EXTRA + (("SELECT 1",),)).version == n + 2


def test_write_is_all_or_nothing_and_nested_writes_join_the_outer_one(tmp_path):
    base = db.Database(tmp_path)
    put = lambda con, key: con.execute("INSERT INTO meta (key, value) VALUES (?, '')", (key,))
    keys = lambda: [r["key"] for r in base.rows("SELECT key FROM meta ORDER BY key")]
    undone = []
    with pytest.raises(RuntimeError):
        with base.write() as con:
            put(con, "а")
            base.undo(lambda: undone.append("первое"))
            with base.write() as inner:                             # вложенная — та же транзакция
                assert inner is con and base.writing
                put(inner, "б")
                base.undo(lambda: undone.append("второе"))
            assert keys() == ["а", "б"]                             # своё соединение видит незакрытое
            raise RuntimeError("сбой в конце действия")
    assert keys() == [] and undone == ["второе", "первое"] and not base.writing    # возврат — в обратном порядке
    with base.write() as con:
        put(con, "в")
        base.undo(lambda: undone.append("не зовётся"))
    assert keys() == ["в"] and undone == ["второе", "первое"]
    with pytest.raises(sqlite3.IntegrityError):                     # сбой самой базы посреди транзакции
        with base.write() as con:
            put(con, "г")
            put(con, "в")
    assert keys() == ["в"]


def test_many_threads_write_at_once_and_readers_never_see_half_of_a_transaction(tmp_path):
    """Шестнадцать потоков пишут составные транзакции (событие и прогон с суммами), четыре читают.
    Ни одна запись не теряется, а читатель ни разу не видит событие без его прогона."""
    base = db.Database(tmp_path)
    trail = journal.Journal(base)
    failures, stop = [], threading.Event()

    def writer(n):
        try:
            for i in range(40):
                with base.write():
                    trail.write("upload", f"user{n}", file=f"П{n}/з{i}.mp4", size=i)
                    trail.write("run", f"user{n}", file=f"П{n}/з{i}.mp4", cost={"RUB": 1.0},
                                stages=[{"stage": "speech", "model": "m", "provider": "p", "currency": "RUB", "cost": 1.0}])
        except Exception as e:
            failures.append(e)

    def reader():
        try:
            while not stop.is_set():
                with base.reading() as con:
                    events = con.execute("SELECT count(*) FROM events").fetchone()[0]
                    runs = con.execute("SELECT count(*) FROM runs").fetchone()[0]
                    money = con.execute("SELECT count(*) FROM run_money").fetchone()[0]
                    uploads = con.execute("SELECT count(*) FROM uploads").fetchone()[0]
                assert events == 2 * runs == 2 * money == 2 * uploads, (events, runs, money, uploads)
        except Exception as e:
            failures.append(e)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    writers = [threading.Thread(target=writer, args=(n,)) for n in range(16)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join()
    assert failures == []
    again = journal.Journal(db.Database(tmp_path))
    assert again.count() == 16 * 40 * 2 and again.report()["total"]["cost"] == {"RUB": 640.0}
    assert len(again.uploaded) == 640


def test_reading_holds_one_state_of_the_base_while_others_write(tmp_path):
    base = db.Database(tmp_path)
    count = lambda con: con.execute("SELECT count(*) FROM meta").fetchone()[0]

    def other():
        with base.write() as con:
            con.execute("INSERT INTO meta (key, value) VALUES ('позже', '')")

    with base.reading() as con:
        before = count(con)
        thread = threading.Thread(target=other)
        thread.start()
        thread.join()                                               # писатель читателя не ждал
        assert count(con) == before
    assert count(base.con) == before + 1


WRITER = """
import sys
sys.path.insert(0, sys.argv[2])
from app import db, journal
trail = journal.Journal(db.Database(sys.argv[1]))
for i in range(150):
    trail.write("upload", "второй процесс", file=f"В/{i}.mp4")
"""


def test_second_process_writes_to_the_same_base_like_the_backup_command_does(tmp_path):
    """Копия на ходу и инструменты — отдельные процессы рядом с сервисом: обе стороны пишут в одну
    базу, и ничего не теряется."""
    base = db.Database(tmp_path)
    trail = journal.Journal(base, lambda line: pytest.fail(line))
    root = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
    other = subprocess.Popen([sys.executable, "-c", WRITER, str(tmp_path), root])
    for i in range(150):
        trail.write("upload", "сервис", file=f"С/{i}.mp4")
    assert other.wait(120) == 0
    assert trail.count() == 300 and len(trail.uploaded) == 300
    base.check()


def test_snapshot_taken_while_the_service_writes_is_whole(tmp_path):
    base = db.Database(tmp_path / "state")
    trail = journal.Journal(base)
    stop, failures = threading.Event(), []

    def writer():
        try:
            n = 0
            while not stop.is_set():
                with base.write():
                    trail.write("upload", "ivanov", file=f"П/{n}.mp4")
                    trail.write("run", "", file=f"П/{n}.mp4", cost={"RUB": 2.0})
                n += 1
        except Exception as e:
            failures.append(e)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for n in range(5):
            base.snapshot(tmp_path / f"снимок-{n}.db")
    finally:
        stop.set()
        thread.join()
    assert failures == []
    for n in range(5):
        copy = sqlite3.connect(tmp_path / f"снимок-{n}.db")
        assert copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"       # один файл, без спутников
        events, runs = (copy.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("events", "runs"))
        assert events == 2 * runs                                   # ни одной половины транзакции
        copy.close()
        assert not (tmp_path / f"снимок-{n}.db-wal").exists()


# ---------- отказы при запуске ----------

def test_file_that_is_not_a_base_is_refused_and_left_alone(tmp_path):
    (tmp_path / db.FILE).write_bytes(b"this is not a database, just bytes " * 100)
    before = (tmp_path / db.FILE).read_bytes()
    with pytest.raises(db.Refusal, match="повреждена.*восстановите базу из резервной копии"):
        db.Database(tmp_path)
    assert (tmp_path / db.FILE).read_bytes() == before              # сервис её не «чинит»


def test_damaged_base_is_found_by_the_check_at_start(tmp_path):
    base = db.Database(tmp_path)
    trail = journal.Journal(base)
    for n in range(300):
        trail.write("upload", "ivanov", file=f"Проекты/запись {n}.mp4", size=n)
    base.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    base.check()
    base.close()
    data = bytearray((tmp_path / db.FILE).read_bytes())
    page = base_page_size = 4096
    for start in range(page * 3, len(data) - page, page * 2):       # испортить страницы в середине файла
        data[start:start + base_page_size] = b"\xff" * base_page_size
    (tmp_path / db.FILE).write_bytes(bytes(data))
    with pytest.raises(db.Refusal, match="повреждена"):
        db.Database(tmp_path).check()


def test_base_that_does_not_take_writes_is_refused(tmp_path):
    base = db.Database(tmp_path)
    base.con.execute("PRAGMA query_only=ON")                        # как папка состояния, подключённая только для чтения
    with pytest.raises(db.Refusal, match="недоступна на запись"):
        base.check()


def test_state_folder_that_cannot_be_used_is_refused(tmp_path):
    (tmp_path / "state").write_text("это файл, а не папка", encoding="utf-8")
    with pytest.raises(db.Refusal, match="не открывается"):
        db.Database(tmp_path / "state")


# ---------- на чём лежит папка состояния ----------

MOUNTS = """\
1 0 0:50 / / rw,relatime - overlay overlay rw,lowerdir=/var/lib/docker/overlay2/l/A
2 1 0:51 / /proc rw,nosuid - proc proc rw
3 1 0:74 /1C/podyachiy/state /data/state rw,noatime - 9p C:\\134 rw,aname=drvfs;path=C:\\;uid=0;gid=0;metadata;symlinkroot=/mnt/host/,cache=0x5
4 1 0:75 /share /data/unc rw,noatime - 9p drvfs rw,aname=drvfs;path=UNC\\fileserver\\meetings;uid=0;gid=0
5 1 0:76 / /data/nfs rw,relatime - nfs4 nas.corp.local:/export/podyachiy rw,vers=4.2,addr=10.0.0.5
6 1 0:77 / /data/smb rw,relatime - cifs //fileserver/meetings rw,vers=3.0,username=svc
7 1 8:1 /opt/podyachiy/state /data/disk rw,relatime - ext4 /dev/sda1 rw
8 1 0:78 /Users/me/podyachiy/state /data/mac rw,relatime - fuse.grpcfuse grpcfuse rw,user_id=0
9 1 0:79 / /data/with\\040space rw - ext4 /dev/sdb1 rw
10 1 0:80 / /data/virtiofs rw - virtiofs podyachiy rw
"""


@pytest.mark.parametrize("path, kind, fstype, source", [
    ("/data/state", db.DESKTOP, "9p", "C:\\"),                      # Docker Desktop, папка на диске C: пробная установка
    ("/data/unc/state", db.NETWORK, "9p", "UNC\\fileserver\\meetings"),     # Docker Desktop, сетевая папка Windows
    ("/data/nfs", db.NETWORK, "nfs4", "nas.corp.local:/export/podyachiy"),
    ("/data/smb/sub/dir", db.NETWORK, "cifs", "//fileserver/meetings"),
    ("/data/disk", db.LOCAL, "ext4", "/dev/sda1"),
    ("/data/mac", db.DESKTOP, "fuse.grpcfuse", "grpcfuse"),
    ("/data/virtiofs", db.DESKTOP, "virtiofs", "podyachiy"),
    ("/data/with space/state", db.LOCAL, "ext4", "/dev/sdb1"),
    ("/data/inside-the-container", db.LOCAL, "overlay", "overlay"),
])
def test_filesystem_of_the_state_folder_is_read_from_the_mount_table(path, kind, fstype, source):
    assert db.filesystem(path, MOUNTS) == {"type": fstype, "source": source, "kind": kind}
    assert db.kept(path, MOUNTS) is (path != "/data/inside-the-container")


def test_lock_probe_passes_on_a_local_disk_and_refuses_a_folder_that_does_not_hold_locks(tmp_path, monkeypatch):
    db.probe(tmp_path)                                              # настоящая проверка: второй процесс, WAL, блокировка
    assert list(tmp_path.iterdir()) == []                           # пробные файлы убраны
    answers = {"got 1": "блокировка файла между процессами на ней не держится",
               "held 0": "второй процесс не смог прочитать базу",
               "failed -1 OperationalError('disk I/O error')": "disk I/O error",
               "": "второй процесс не смог прочитать базу"}
    for said, why in answers.items():
        monkeypatch.setattr(db.subprocess, "run", lambda *a, said=said, **kw: subprocess.CompletedProcess(a, 0, said, ""))
        with pytest.raises(db.Refusal, match="не годится для базы") as refused:
            db.probe(tmp_path)
        assert why in str(refused.value) and "локальном диске" in str(refused.value)
        assert list(tmp_path.iterdir()) == []


def test_base_of_version_1_1_1_takes_the_step_of_1_2_0(tmp_path):
    """Обновление с 1.1.1 (схема 2): шаг 3 проходит, люди и их входы на месте, у каждого счётчик
    сессий с нуля — метки входа, выданные этой версией до «Выйти везде», действуют."""
    from app import users
    old = db.Database(tmp_path, steps=db.MIGRATIONS[:2])
    with old.write() as con:
        con.execute("INSERT INTO users (login, name, password, home) VALUES ('ivanov', 'Иванов', '', 'Пользовательские/ivanov')")
    assert old.version == 2
    old.close()
    lines = []
    base = db.Database(tmp_path, lines.append)
    assert base.version == db.SCHEMA_VERSION == 3 and lines == ["схема базы обновлена: версия 2 → 3"]
    assert dict(base.row("SELECT login, sessions FROM users")) == {"login": "ivanov", "sessions": 0}
    people = users.Users(base, "секрет-админа")
    people.put("ivanov", "Иванов", "пароль-1234")
    token = people.token("ivanov")
    people.end_sessions("ivanov")
    assert base.value("SELECT sessions FROM users WHERE login = 'ivanov'") == 1 and people.by_token(token) is None
