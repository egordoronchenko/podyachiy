"""База сервиса: один файл SQLite в папке состояния — STATE/podyachiy.db.

В базе лежит всё, что сервис помнит между запусками и что не является документом: пользователи
и права, ключи доступа, словари, настройки, заявки, журнал событий и расходов, журнал обработки
записей, отметки и сроки поручений. Документы — записи, расшифровки, протоколы, шаблоны,
логотип, сертификаты, ключ подписи входов, лог — остаются файлами.

Файл лежит в папке состояния на диске сервера, а не в контейнере: пересоздание контейнера и
обновление образа его не затрагивают. Сервера базы нет — SQLite работает внутри процесса.

Соединения. Сервис многопоточный: обработчики и часть страниц идут в событийном цикле, вход,
проверка ключа, сканер и отчёты — в потоках. У каждого потока своё соединение: у SQLite одно
соединение — одна транзакция, и общее на всех пришлось бы целиком закрывать замком, так что
отчёт о расходах на секунду останавливал бы вход. Режим журнала — WAL: читатели не ждут
писателя, писатель не ждёт читателей; писатель в каждый момент один, остальные ждут своей
очереди не дольше BUSY_SEC. Запись идёт только через write(): BEGIN IMMEDIATE сразу берёт право
записи — «прочитал, проверил, записал» внутри него никто не перебьёт, и замки в памяти для этого
не нужны. Вложенный write() в том же потоке входит в уже открытую транзакцию: так действие,
которое меняет несколько таблиц (переименование папки — права, словари, журнал обработки,
отметки, журнал событий), записывается целиком или не записывается вовсе.

Схема. Номер версии схемы хранится в самой базе (PRAGMA user_version). При открытии недостающие
шаги из MIGRATIONS применяются по порядку, каждый в своей транзакции. База с номером больше
известного этому коду — от более новой версии сервиса: с ней сервис не запускается.

Отказ открыть базу (Refusal) — причина словами для администратора. Сервис при нём не
запускается: чинить базу сам он не берётся, восстановление — из резервной копии.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

FILE = "podyachiy.db"
BUSY_SEC = 15               # сколько писатель ждёт своей очереди, прежде чем отказать
SQLITE_MIN = (3, 37, 0)     # строгие таблицы (STRICT); в образе — 3.46

# Файловые системы, на которых блокировки файлов и общая память журнала WAL работают не всегда:
# сетевые папки. База на них может повредиться, когда связь рвётся посреди записи.
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smb2", "smbfs", "ncpfs", "afs", "ceph", "lustre", "glusterfs",
              "fuse.glusterfs", "fuse.sshfs", "fuse.davfs", "davfs", "fuse.rclone", "fuse.s3fs", "beegfs"}
# Папка компьютера, подключённая в контейнер Docker Desktop (Windows, macOS): не настоящий
# локальный диск, а прослойка. Какой диск под ней — локальный или сетевой, — из контейнера видно
# только по пути (у Windows: буква диска или \\сервер\папка).
DESKTOP_FS = {"9p", "virtiofs", "fuse.grpcfuse", "fuse.osxfs", "fakeowner"}
LOCAL, DESKTOP, NETWORK = "local", "desktop", "network"

# Шаги схемы по порядку: шаг N приводит базу к версии N. Прежние шаги не правятся — новое
# дописывается следующим шагом.
MIGRATIONS: tuple[tuple[str, ...], ...] = ((
    # сведения о самой базе: когда и какой версией сервиса создана, итог переноса с файлов
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT, WITHOUT ROWID",

    # ---------- пользователи и права ----------
    """CREATE TABLE users (
        login TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT NOT NULL DEFAULT '',
        password TEXT NOT NULL DEFAULT '',          -- хеш scrypt; пусто — пароля нет (приглашён, из домена)
        home TEXT NOT NULL,                         -- личная папка
        admin INTEGER NOT NULL DEFAULT 0,
        home_upload INTEGER NOT NULL DEFAULT 0,     -- загрузка в личную папку
        notify_ready INTEGER NOT NULL DEFAULT 1,    -- письмо «запись готова»
        source TEXT NOT NULL DEFAULT 'local' CHECK (source IN ('local', 'domain')),
        checked INTEGER,                            -- доменная запись: когда домен её подтвердил
        link_hash TEXT,                             -- одноразовая ссылка «задать пароль»: отпечаток
        link_expires INTEGER                        -- и срок
    ) STRICT, WITHOUT ROWID""",
    "CREATE INDEX users_email ON users (email) WHERE email <> ''",
    "CREATE INDEX users_link ON users (link_hash) WHERE link_hash IS NOT NULL",
    "CREATE INDEX users_home ON users (home)",
    """CREATE TABLE grants (
        login TEXT NOT NULL REFERENCES users (login) ON DELETE CASCADE ON UPDATE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('read', 'upload', 'manage')),
        folder TEXT NOT NULL,                       -- '' — все папки
        scope TEXT NOT NULL CHECK (scope IN ('only', 'tree')),
        PRIMARY KEY (login, kind, folder)
    ) STRICT, WITHOUT ROWID""",
    "CREATE INDEX grants_folder ON grants (folder)",

    # ---------- ключи доступа ----------
    """CREATE TABLE keys (
        n INTEGER PRIMARY KEY,                      -- порядок выпуска
        id TEXT NOT NULL,
        hash TEXT NOT NULL UNIQUE,                  -- sha256 ключа; сам ключ не хранится
        prefix TEXT NOT NULL,
        owner TEXT NOT NULL,                        -- логин; у admin из .env строки в users нет
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('plain', 'manage')),
        created INTEGER NOT NULL,
        expires INTEGER,
        used INTEGER
    ) STRICT""",
    "CREATE INDEX keys_owner ON keys (owner)",
    "CREATE INDEX keys_id ON keys (id)",

    # ---------- словари папок и записей ----------
    """CREATE TABLE dictionaries (
        scope TEXT NOT NULL CHECK (scope IN ('folders', 'records')),
        path TEXT NOT NULL,
        people TEXT NOT NULL,                       -- JSON: [{name, role, aka}]
        terms TEXT NOT NULL,                        -- JSON: [{term, meaning}]
        author TEXT NOT NULL DEFAULT '',
        at TEXT NOT NULL DEFAULT '',
        changed REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (scope, path)
    ) STRICT, WITHOUT ROWID""",

    # ---------- настройки со страницы ----------
    """CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,                        -- JSON
        author TEXT NOT NULL DEFAULT '',
        at TEXT NOT NULL DEFAULT ''
    ) STRICT, WITHOUT ROWID""",
    """CREATE TABLE template_notes (
        item TEXT PRIMARY KEY,
        author TEXT NOT NULL,
        at TEXT NOT NULL,
        digest TEXT NOT NULL
    ) STRICT, WITHOUT ROWID""",

    # ---------- заявки на доступ ----------
    """CREATE TABLE requests (
        n INTEGER PRIMARY KEY,
        id TEXT NOT NULL UNIQUE,
        at TEXT NOT NULL,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        login TEXT NOT NULL,
        comment TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
        decided_at TEXT,
        decided_by TEXT
    ) STRICT""",
    "CREATE INDEX requests_status ON requests (status, n)",

    # ---------- журнал обработки записей ----------
    """CREATE TABLE records (
        file TEXT PRIMARY KEY,                      -- путь записи во входной папке
        status TEXT,
        finished_at TEXT,
        data TEXT NOT NULL,                         -- JSON: строка журнала целиком
        marks_at REAL NOT NULL DEFAULT 0            -- когда последний раз меняли отметки поручений
    ) STRICT, WITHOUT ROWID""",
    "CREATE INDEX records_finished ON records (finished_at)",
    "CREATE INDEX records_status ON records (status)",

    # ---------- отметки «выполнено» и сроки поручений ----------
    """CREATE TABLE marks (
        file TEXT NOT NULL REFERENCES records (file) ON DELETE CASCADE ON UPDATE CASCADE,
        key TEXT NOT NULL,                          -- отпечаток исполнителя и текста поручения
        who TEXT,
        task TEXT NOT NULL,
        status TEXT,
        done_by TEXT,
        done_at TEXT,
        comment TEXT,
        due TEXT,
        due_by TEXT,
        due_at TEXT,
        PRIMARY KEY (file, key)
    ) STRICT, WITHOUT ROWID""",
    "CREATE INDEX marks_due ON marks (due) WHERE due IS NOT NULL",

    # ---------- журнал событий: действия людей и прогоны записей ----------
    """CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT '',             -- кто сделал; пусто — сервис сам
        file TEXT,                                  -- о какой записи
        folder TEXT,                                -- о какой папке (у события о записи — её папка)
        login TEXT,                                 -- о каком пользователе
        data TEXT NOT NULL DEFAULT '{}'             -- JSON: остальные поля события
    ) STRICT""",
    "CREATE INDEX events_at ON events (at)",
    "CREATE INDEX events_kind ON events (kind, id)",
    "CREATE INDEX events_actor ON events (actor, id)",
    "CREATE INDEX events_file ON events (file, id) WHERE file IS NOT NULL",
    "CREATE INDEX events_folder ON events (folder, id) WHERE folder IS NOT NULL",
    "CREATE INDEX events_login ON events (login, id) WHERE login IS NOT NULL",
    "CREATE INDEX events_actions ON events (id) WHERE kind <> 'run'",
    # Прогон записи — событие вида run и строка здесь. path — путь записи, каким он стал после
    # переименований и переносов: расходы одной записи не распадаются на два названия. У удалённой
    # записи путь застывает (settled): по нему позже может лечь другая запись.
    """CREATE TABLE runs (
        id INTEGER PRIMARY KEY REFERENCES events (id) ON DELETE CASCADE,
        at TEXT NOT NULL,
        day TEXT NOT NULL,                          -- ГГГГ-ММ-ДД: по нему выбирается период отчёта
        path TEXT NOT NULL,
        settled INTEGER NOT NULL DEFAULT 0,
        actor TEXT NOT NULL DEFAULT '',             -- кто запустил прогон
        uploaded_by TEXT NOT NULL DEFAULT '',       -- кто загрузил запись
        reason TEXT,
        status TEXT,
        started_at TEXT,
        audio_sec REAL,
        runs INTEGER                                -- сколько прогонов в строке (расходы «до журнала»)
    ) STRICT""",
    "CREATE INDEX runs_day ON runs (day)",
    "CREATE INDEX runs_path ON runs (path)",
    "CREATE INDEX runs_actor ON runs (actor)",
    """CREATE TABLE run_money (                     -- итог прогона по валютам
        id INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        currency TEXT NOT NULL,
        cost REAL NOT NULL,
        PRIMARY KEY (id, currency)
    ) STRICT, WITHOUT ROWID""",
    """CREATE TABLE run_stages (                    -- то же по этапам и моделям
        id INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        n INTEGER NOT NULL,
        stage TEXT NOT NULL,
        model TEXT NOT NULL,
        provider TEXT NOT NULL,
        currency TEXT NOT NULL,
        cost REAL NOT NULL,
        PRIMARY KEY (id, n)
    ) STRICT, WITHOUT ROWID""",
    """CREATE TABLE run_providers (                 -- то же по поставщикам: кому платили
        id INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        n INTEGER NOT NULL,
        provider TEXT NOT NULL,
        currency TEXT NOT NULL,
        cost REAL NOT NULL,
        PRIMARY KEY (id, n)
    ) STRICT, WITHOUT ROWID""",
    # кто загрузил запись, лежащую сейчас по этому пути
    "CREATE TABLE uploads (path TEXT PRIMARY KEY, actor TEXT NOT NULL) STRICT, WITHOUT ROWID",
),)
SCHEMA_VERSION = len(MIGRATIONS)


class Refusal(RuntimeError):
    """База не открыта или не годится для работы; текст — причина для администратора."""


def below(column: str) -> str:
    """Условие SQL «путь равен папке или лежит в ней» — с тремя подстановками: папка, папка + «/»,
    папка + «0». «0» — знак, следующий за «/»: всё, что начинается с «папка/», лежит между ними,
    и поиск идёт по индексу. LIKE здесь не годится: в именах папок бывают % и _."""
    return f"({column} = ? OR ({column} >= ? AND {column} < ?))"


def span(folder: str) -> tuple[str, str, str]:
    """Подстановки для below."""
    return folder, folder + "/", folder + "0"


class _Thread(threading.local):
    con: sqlite3.Connection | None = None
    depth = 0
    undo: list


class Database:
    def __init__(self, state_dir: Path, log: Callable[[str], None] = lambda s: None,
                 steps: tuple[tuple[str, ...], ...] | None = None):
        if sqlite3.sqlite_version_info < SQLITE_MIN:
            raise Refusal(f"SQLite {sqlite3.sqlite_version} слишком старый: нужен "
                          f"{'.'.join(map(str, SQLITE_MIN))} или новее")
        self.dir = Path(state_dir)
        self.path = self.dir / FILE
        self.log = log
        self._mine = _Thread()
        self._all: list[sqlite3.Connection] = []
        self._guard = threading.Lock()          # только список соединений — чтобы закрыть их разом
        self.created = not self.path.exists()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            con = self.con
            mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode != "wal":
                raise Refusal(f"база {self.path}: журнал WAL не включился (режим {mode}) — файловая система "
                              "папки состояния его не поддерживает. Держите папку состояния на локальном диске")
            self._upgrade(con, MIGRATIONS if steps is None else steps)
        except Refusal:
            self.close()
            raise
        except (sqlite3.Error, OSError) as e:
            self.close()
            raise Refusal(_why(self.path, e)) from e

    # ---------- соединения ----------

    @property
    def con(self) -> sqlite3.Connection:
        """Соединение этого потока. Читать через него можно всегда; писать — внутри write()."""
        mine = self._mine
        if mine.con is None:
            # isolation_level=None: транзакции открывает и закрывает write(), а не модуль sqlite3
            con = sqlite3.connect(self.path, timeout=BUSY_SEC, isolation_level=None, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            # FULL: закрытая транзакция пережидает и отключение питания; записей в секунду у
            # сервиса единицы, цена — одна синхронизация диска на транзакцию
            con.execute("PRAGMA synchronous=FULL")
            mine.con, mine.depth, mine.undo = con, 0, []
            with self._guard:
                self._all.append(con)
        return mine.con

    def close(self) -> None:
        """Закрывает соединения всех потоков: конец работы инструмента или теста."""
        with self._guard:
            for con in self._all:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
        self._mine = _Thread()

    # ---------- чтение ----------

    def rows(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.con.execute(sql, args).fetchall()

    def row(self, sql: str, *args) -> sqlite3.Row | None:
        return self.con.execute(sql, args).fetchone()

    def value(self, sql: str, *args):
        found = self.con.execute(sql, args).fetchone()
        return found[0] if found else None

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """Несколько чтений подряд с одного состояния базы: что бы ни записали другие потоки
        между запросами, все они видят базу такой, какой она была при первом. Писателей не
        задерживает."""
        con, mine = self.con, self._mine
        if mine.depth or con.in_transaction:
            yield con
            return
        con.execute("BEGIN")
        try:
            yield con
        finally:
            con.execute("COMMIT")

    # ---------- запись ----------

    @property
    def writing(self) -> bool:
        """Идёт ли в этом потоке транзакция записи."""
        return self._mine.depth > 0

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Транзакция записи: всё внутри записывается целиком или не записывается вовсе.
        Вложенный вызов в том же потоке входит в уже открытую транзакцию."""
        con, mine = self.con, self._mine
        if mine.depth:
            mine.depth += 1
            try:
                yield con
            finally:
                mine.depth -= 1
            return
        con.execute("BEGIN IMMEDIATE")
        mine.depth, mine.undo = 1, []
        try:
            yield con
            con.execute("COMMIT")
        except BaseException:
            self._rollback(con)
            raise
        finally:
            mine.depth = 0

    def _rollback(self, con: sqlite3.Connection) -> None:
        try:
            if con.in_transaction:
                con.execute("ROLLBACK")
        except sqlite3.Error as e:
            self.log(f"ВНИМАНИЕ: откат транзакции базы не удался: {e}")
        undo, self._mine.undo = self._mine.undo, []
        for back in reversed(undo):
            try:
                back()
            except Exception as e:      # возврат одного не должен помешать вернуть остальное
                self.log(f"ВНИМАНИЕ: после отката транзакции не удалось вернуть прежнее состояние: {e!r}")

    def undo(self, back: Callable[[], None]) -> None:
        """Что сделать, если идущая транзакция откатится: вернуть на место то, что база не
        откатывает сама, — копию в памяти, переименованный файл. Зовётся внутри write()."""
        assert self._mine.depth, "undo — только внутри транзакции записи"
        self._mine.undo.append(back)

    # ---------- схема ----------

    @property
    def version(self) -> int:
        return self.con.execute("PRAGMA user_version").fetchone()[0]

    def _upgrade(self, con: sqlite3.Connection, steps: tuple[tuple[str, ...], ...]) -> None:
        have = con.execute("PRAGMA user_version").fetchone()[0]
        if have > len(steps):
            raise Refusal(f"база {self.path} — от более новой версии сервиса (схема {have}, эта версия знает "
                          f"{len(steps)}). Верните образ той версии, которой база записана, либо восстановите "
                          "копию, сделанную до обновления")
        for number in range(have + 1, len(steps) + 1):
            # шаг целиком или никак: оборванный шаг оставляет базу прежней версии
            try:
                with self.write():
                    for statement in steps[number - 1]:
                        con.execute(statement)
                    con.execute(f"PRAGMA user_version={number}")
            except sqlite3.Error as e:
                raise Refusal(f"обновление схемы базы {self.path} до версии {number} не удалось: {e}. База осталась "
                              f"версии {number - 1} и не изменена; верните образ прежней версии сервиса и сообщите "
                              "автору") from e
            if have:
                self.log(f"схема базы обновлена: версия {number - 1} → {number}")

    # ---------- проверки ----------

    def check(self) -> None:
        """Годится ли база для работы: принимает запись и цела. Нет — Refusal. Зовётся при запуске."""
        try:
            with self.write() as con:
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('checked', ?)",
                            (time.strftime("%Y-%m-%dT%H:%M:%S%z"),))
        except sqlite3.Error as e:
            raise Refusal(f"база {self.path} недоступна на запись: {e}. Проверьте права на папку состояния, "
                          "свободное место на диске и что папка не подключена только для чтения") from e
        try:
            found = [r[0] for r in self.con.execute("PRAGMA quick_check")]
        except sqlite3.Error as e:
            raise Refusal(_why(self.path, e)) from e
        if found != ["ok"]:
            raise Refusal(f"база {self.path} повреждена: {'; '.join(found[:3])}. Сервис её не чинит — восстановите "
                          "базу из резервной копии (install/README.md, раздел 12)")

    def note(self, key: str, value: str) -> None:
        with self.write() as con:
            con.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    def noted(self, key: str) -> str | None:
        return self.value("SELECT value FROM meta WHERE key = ?", key)

    def size(self) -> int:
        """Сколько база занимает на диске вместе с журналом WAL."""
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.path.getsize(str(self.path) + suffix)
            except OSError:
                pass
        return total

    def snapshot(self, target: Path) -> None:
        """Согласованный снимок базы в файл target — на ходу, пока сервис пишет: средствами
        самого SQLite (страницы копируются под его же блокировками, а не чтением файла).
        Снимок — один файл в обычном режиме журнала, без спутников -wal и -shm."""
        source = sqlite3.connect(self.path, timeout=BUSY_SEC)
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
            copy.execute("PRAGMA journal_mode=DELETE")
        finally:
            copy.close()
            source.close()


def _why(path: Path, e: Exception) -> str:
    text = str(e)
    if isinstance(e, sqlite3.DatabaseError) and ("not a database" in text or "malformed" in text):
        return (f"база {path} повреждена ({text}). Сервис её не чинит — восстановите базу из резервной копии "
                "(install/README.md, раздел 12)")
    return (f"база {path} не открывается: {text}. Проверьте, что папка состояния подключена, доступна на запись "
            "и на диске есть место")


# ---------- файловая система папки состояния ----------

def _plain(text: str) -> str:
    """Имя из /proc/self/mountinfo: пробел, табуляция, перевод строки и обратная черта записаны там кодами."""
    for code, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        text = text.replace(code, char)
    return text


def filesystem(path: Path, table: str | None = None) -> dict:
    """На какой файловой системе лежит папка: {"type", "source", "kind"}. kind — local (обычный
    диск), desktop (папка компьютера в Docker Desktop) или network (сетевая папка).

    Узнаётся по таблице подключений ядра (/proc/self/mountinfo): из контейнера видно только
    её. Это надёжно для папок, подключённых по сети на самом сервере (NFS, SMB), и для Docker
    Desktop; но сетевой диск, который Windows показывает Docker Desktop под буквой, отсюда
    неотличим от локального — тип тогда desktop, и отвечает за диск администратор.
    table — содержимое таблицы (для проверок); None — прочитать у системы."""
    if table is None:
        try:
            table = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"type": "", "source": "", "kind": LOCAL}
    where = os.path.realpath(path)
    best: tuple[int, str, str, str] | None = None
    for line in table.splitlines():
        head, _, tail = line.partition(" - ")
        fields, rest = head.split(), tail.split()
        if len(fields) < 5 or len(rest) < 2:
            continue
        point = _plain(fields[4])
        if where == point or where.startswith(point.rstrip("/") + "/"):
            if best is None or len(point) >= best[0]:
                best = (len(point), rest[0], _plain(rest[1]), _plain(rest[2]) if len(rest) > 2 else "")
    if best is None:
        return {"type": "", "source": "", "kind": LOCAL}
    _, kind, source, options = best
    if kind in DESKTOP_FS:
        # Docker Desktop под Windows: путь на компьютере записан в параметрах — path=C:\ или path=UNC\сервер\папка
        host = next((part[5:] for part in options.replace(",", ";").split(";") if part.startswith("path=")), source)
        shared = host.upper().startswith(("UNC", "\\\\", "//"))
        return {"type": kind, "source": host, "kind": NETWORK if shared else DESKTOP}
    return {"type": kind, "source": source, "kind": NETWORK if kind in NETWORK_FS else LOCAL}


def kept(path: Path, table: str | None = None) -> bool:
    """Подключена ли папка с хоста, а не лежит в самом контейнере: у неё (или у папки выше)
    своя точка подключения, отличная от корня контейнера. table — как у filesystem."""
    if table is None:
        try:
            table = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return os.path.ismount(path)
    where = os.path.realpath(path)
    points = [_plain(line.split()[4]) for line in table.splitlines() if len(line.split()) > 4]
    return any(point != "/" and (where == point or where.startswith(point.rstrip("/") + "/")) for point in points)


_PROBE = """
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
try:
    seen = con.execute("SELECT count(*) FROM probe").fetchone()[0]
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("ROLLBACK")
        print("got", seen)
    except sqlite3.OperationalError as e:
        print("held" if "locked" in str(e) else "failed", seen, e)
except Exception as e:
    print("failed", -1, repr(e))
"""


def probe(state_dir: Path) -> None:
    """Проверка делом: держит ли папка состояния блокировку файла между процессами и работает
    ли на ней журнал WAL. На пробном файле рядом с базой один процесс открывает запись, второй
    должен получить отказ на запись и при этом прочитать уже записанное. Не так — Refusal:
    копия базы на ходу (вторым процессом) и сама база на такой папке ненадёжны."""
    path = Path(state_dir) / f".probe-{os.getpid()}.db"
    bad = (f"папка состояния {state_dir} не годится для базы: {{}}. Держите папку состояния (HOST_STATE) на "
           "локальном диске сервера, не на сетевой папке")
    con = None
    try:
        con = sqlite3.connect(path, timeout=BUSY_SEC, isolation_level=None)
        if con.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise Refusal(bad.format("журнал WAL на ней не включается"))
        con.execute("CREATE TABLE probe (v INTEGER)")
        con.execute("INSERT INTO probe VALUES (1)")
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO probe VALUES (2)")
        answer = subprocess.run([sys.executable, "-I", "-c", _PROBE, str(path)], capture_output=True, text=True,
                                timeout=60).stdout.split(None, 2)
        con.execute("ROLLBACK")
        verdict, seen = (answer + ["", ""])[:2]
        if verdict == "got":
            raise Refusal(bad.format("блокировка файла между процессами на ней не держится"))
        if verdict != "held" or seen != "1":
            raise Refusal(bad.format("второй процесс не смог прочитать базу при открытой записи"
                                     + (f" ({' '.join(answer[2:])})" if answer[2:] else "")))
    except (sqlite3.Error, OSError, subprocess.SubprocessError) as e:
        raise Refusal(bad.format(f"пробная база на ней не работает ({e})")) from e
    finally:
        if con is not None:
            con.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(str(path) + suffix)
            except OSError:
                pass
