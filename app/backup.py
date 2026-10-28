"""Полная резервная копия установки одним архивом, её проверка и восстановление.

    python -m app.backup create [--to ПАПКА] [--no-inbox] [--keep N]
    python -m app.backup verify [АРХИВ]
    python -m app.backup restore АРХИВ
    python -m app.backup record АРХИВ "Папка/запись.mp4"

Запускается в контейнере сервиса (docker compose exec — рядом с работающим сервисом,
docker compose run — при остановленном): папки берутся из тех же настроек, что у сервиса.

Что в архиве (podyachiy-backup-ГГГГММДД-ЧЧММСС.tar):
  state/podyachiy.db — снимок базы, снятый средствами самого SQLite: согласованный, хотя
      сервис в это время пишет (db.Database.snapshot). Файл базы «на ходу» копировать нельзя —
      получится половина транзакции; снимок — можно;
  state/…  — остальное из папки состояния: ключ подписи входов, логотип, сертификаты, папка
      «до-базы-…»;
  prompts/… — шаблоны и словарь; outbox/… — результаты, корзина, лог; inbox/… — исходные записи
      (их можно не брать: --no-inbox);
  MANIFEST.json — опись: версия сервиса и схемы базы, состав, размеры, контрольные суммы
      (sha256) каждого файла, числа из базы (пользователи, записи, события, расходы по валютам).
Опись лежит последней в архиве и ещё раз — файлом рядом с ним.

Файлов настроек (.env, compose.yml, Caddyfile, сертификаты прокси) в архиве нет: они лежат на
сервере вне контейнера, сервис их не видит — их копируют отдельно (install/README.md, раздел 12).

Сначала снимается база, потом копируются файлы. Запись, обработка которой закончилась, пока
шла копия, попадёт в архив результатами, но не строкой базы: после восстановления сервис
возьмёт её снова — расшифровку переиспользует с диска и заново сделает только протокол.
Обратный порядок оставлял бы записи, которые в базе готовы, а на диске без результатов.

Копию делает и сам сервис: по расписанию со страницы «Обработка» и кнопкой «Сделать копию сейчас»
(web.py). Расписание — одно время суток по часовому поясу сервиса (TZ), каждый день или раз в неделю;
когда копия положена, решает due: пропущенное время (сервис был выключен) не догоняется пачкой —
одна копия, как только сервис снова работает.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

from . import about, db, journal, logbook, migrate, upload, usage
from .config import Settings

PREFIX = "podyachiy-backup-"
SUFFIX = ".tar"
PARTIAL = ".partial"                    # архив, который ещё пишется: готовый получает имя одним переименованием
MANIFEST = "MANIFEST.json"
SIDE = ".manifest.json"                 # опись рядом с архивом: <архив>.manifest.json
FORMAT = 1
BASE = f"state/{db.FILE}"
PARTS = ("state", "prompts", "outbox", "inbox")
CHUNK = 1 << 20
LAST = "last_backup"                    # под этим ключом в базе лежат сведения о последней копии
FAILED = "backup_failed"                # а здесь — о последней несостоявшейся, пока после неё не было удачной
# кто сделал копию: команда в контейнере, сервис по расписанию, человек кнопкой на странице (или ключом)
COMMAND, SCHEDULE, PAGE = "command", "schedule", "page"
# таблицы, строки которых считаются в описи: число строк до копии и после восстановления должно совпасть
COUNTED = ("users", "grants", "keys", "dictionaries", "requests", "records", "marks", "events", "runs")


class BackupError(Exception):
    """Копия не сделана, не прошла проверку или не восстановлена; текст — причина для администратора."""


def human(size: int) -> str:
    for unit, step in (("ГБ", 2 ** 30), ("МБ", 2 ** 20), ("КБ", 2 ** 10)):
        if size >= step:
            return f"{size / step:.1f} {unit}"
    return f"{size} Б"


def roots(cfg) -> dict[str, Path]:
    return {"state": Path(cfg.state_dir), "prompts": Path(cfg.prompts_dir), "outbox": Path(cfg.outbox),
            "inbox": Path(cfg.inbox)}


def inventory(con: sqlite3.Connection) -> dict:
    """Числа из базы для описи: строк в таблицах и расходы по валютам — по ним сверяют копию
    с установкой и восстановленную установку с копией."""
    found = {table: con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in COUNTED}
    spent: dict[str, float] = {}            # тем же сложением, что и отчёт о расходах: по порядку прогонов
    for currency, cost in con.execute("SELECT currency, cost FROM run_money ORDER BY id, currency"):
        spent = usage.plus(spent, {currency: cost})
    return {**found, "spent": spent}


class _Counted:
    """Читает файл для архива ровно объявленного размера и считает его контрольную сумму.
    Файл, который укоротился, пока его копировали, дополняется нулями — иначе архив бы сломался."""

    def __init__(self, file, size: int):
        self.file, self.left, self.digest, self.short = file, size, hashlib.sha256(), False
        self.failed = ""                    # файл перестал читаться посреди копирования — почему

    def read(self, size: int = -1) -> bytes:
        size = self.left if size < 0 else min(size, self.left)
        try:
            data = b"" if self.failed else self.file.read(size)
        except OSError as e:
            data, self.failed = b"", e.strerror or str(e)
        if len(data) < size:
            data += b"\0" * (size - len(data))
            self.short = True
        self.left -= len(data)
        self.digest.update(data)
        return data


def _walk(root: Path, skip: list[Path]):
    """Папки и файлы внутри root — по порядку имён; skip — что обходить стороной."""
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        path = Path(entry.path)
        if any(path == other or other in path.parents for other in skip):
            continue
        yield path, entry
        if entry.is_dir(follow_symlinks=False):
            yield from _walk(path, skip)


def _latest(folder: Path) -> list[Path]:
    """Готовые архивы в папке копий, свежие первыми."""
    return sorted((p for p in folder.glob(f"{PREFIX}*{SUFFIX}") if p.is_file()), reverse=True)


def create(cfg, to: Path | None = None, with_inbox: bool = True, keep: int = 0, say=print,
           kept=db.kept, now: datetime.datetime | None = None, *, by: str = "", how: str = COMMAND,
           log=None, base: db.Database | None = None) -> dict:
    """Делает копию и отдаёт её опись (с полем archive — путь архива). Сбой — BackupError.

    Из сервиса: by — кто нажал кнопку (пусто — сервис сам, по расписанию), how — как сделана (COMMAND,
    SCHEDULE, PAGE), log — журнал работы сервиса (итог — строкой в него, а не дописью в файл мимо него),
    base — его база."""
    started = time.perf_counter()
    target = Path(to or cfg.backup_dir)
    places = roots(cfg)
    if not (places["state"] / db.FILE).is_file():
        raise BackupError(f"базы {places['state'] / db.FILE} нет: копировать нечего. Сервис ещё ни разу не "
                          "запускался в этой установке либо папка состояния не та")
    if not target.is_dir():
        raise BackupError(f"папки для копий {target} нет. Подключите её с сервера: в compose.yml — строка "
                          "с HOST_BACKUP (install/README.md, раздел 12), затем docker compose up -d")
    if not kept(target):
        raise BackupError(f"папка для копий {target} не подключена с сервера: архив остался бы внутри контейнера и "
                          "пропал при его пересоздании. В compose.yml нужна строка с HOST_BACKUP "
                          "(install/README.md, раздел 12)")
    moment = now or datetime.datetime.now().astimezone()
    name = f"{PREFIX}{moment:%Y%m%d-%H%M%S}{SUFFIX}"
    final, partial = target / name, target / (name + PARTIAL)
    if final.exists():
        raise BackupError(f"архив {final} уже есть — копия с этой секундой уже сделана")
    for stale in target.glob(f"{PREFIX}*{PARTIAL}"):         # след копии, оборванной в прошлый раз
        stale.unlink()
    for stale in target.glob(f".{PREFIX}*.db*"):
        stale.unlink()

    manifest = {"kind": "podyachiy-backup", "format": FORMAT, "created": moment.isoformat(timespec="seconds"),
                "service": about.version(), "schema": None, "inbox": with_inbox, "base": {}, "parts": {},
                "files": [], "dirs": 0, "changed": [], "skipped": [],
                "not_included": [".env и compose.yml (настройки, ключи поставщиков, пароль admin)",
                                 "Caddyfile, certs и папка caddy (HTTPS)", "недогруженные файлы (inbox/.uploads)"]
                                + ([] if with_inbox else ["исходные записи (inbox): копия сделана без них"])}
    snapshot = target / f".{name}.db"
    # не копируются: сама база и её спутники (вместо них — снимок), пробные файлы, папка копий,
    # если она лежит внутри копируемых, недогрузки
    skip = [target.resolve(), places["state"] / migrate.WORK, places["inbox"] / upload.PARTS_DIR]
    skip += [places["state"] / (db.FILE + tail) for tail in ("", "-wal", "-shm", "-journal")]
    try:
        if base is not None:
            base.snapshot(snapshot)
        else:
            own = db.Database(places["state"])
            try:
                own.snapshot(snapshot)
            finally:
                own.close()
        con = sqlite3.connect(snapshot)
        try:
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BackupError("снимок базы не прошёл проверку целостности — база повреждена; копия не сделана")
            manifest["schema"] = con.execute("PRAGMA user_version").fetchone()[0]
            manifest["base"] = {"path": BASE, "counts": inventory(con)}
        finally:
            con.close()

        with tarfile.open(partial, "w", format=tarfile.PAX_FORMAT) as tar:
            def add(path: Path, member: str) -> dict | None:
                try:
                    file = open(path, "rb")
                except OSError as e:
                    manifest["skipped"].append({"path": member, "why": e.strerror or str(e)})
                    return None
                with file:
                    before = os.fstat(file.fileno())
                    info = tarfile.TarInfo(member)
                    info.size, info.mtime, info.mode = before.st_size, int(before.st_mtime), 0o644
                    reader = _Counted(file, before.st_size)
                    tar.addfile(info, reader)       # сбой здесь — уже сбой записи архива: копия не состоялась
                if reader.failed:
                    manifest["skipped"].append({"path": member, "why": reader.failed})
                try:
                    after = path.stat()
                    same = (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
                except OSError:
                    same = False
                if reader.short or not same:
                    manifest["changed"].append(member)
                return {"path": member, "bytes": before.st_size, "sha256": reader.digest.hexdigest()}

            for part in PARTS:
                if part == "inbox" and not with_inbox:
                    continue
                count = size = 0
                if part == "state":
                    made = add(snapshot, BASE)
                    if made is None:
                        raise BackupError("снимок базы не прочитался — копия не сделана")
                    manifest["files"].append(made)
                    manifest["base"].update(bytes=made["bytes"], sha256=made["sha256"])
                    count, size = 1, made["bytes"]
                # папка результатов внутри входной — её содержимое уже скопировано как outbox
                inside = [places["outbox"]] if part == "inbox" else []
                for path, entry in _walk(places[part], skip + inside):
                    member = f"{part}/{path.relative_to(places[part]).as_posix()}"
                    if entry.is_symlink():
                        manifest["skipped"].append({"path": member, "why": "ссылка на другой файл — не копируется"})
                    elif entry.is_dir(follow_symlinks=False):
                        info = tarfile.TarInfo(member)
                        info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, int(entry.stat().st_mtime)
                        tar.addfile(info)                       # папки — тоже: пустая папка на странице — папка
                        manifest["dirs"] += 1
                    elif entry.is_file(follow_symlinks=False):
                        made = add(path, member)
                        if made:
                            manifest["files"].append(made)
                            count, size = count + 1, size + made["bytes"]
                manifest["parts"][part] = {"files": count, "bytes": size}
            manifest["bytes"] = sum(part["bytes"] for part in manifest["parts"].values())
            body = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
            info = tarfile.TarInfo(MANIFEST)
            info.size, info.mtime, info.mode = len(body), int(moment.timestamp()), 0o644
            tar.addfile(info, _Counted(_Bytes(body), len(body)))
        with open(partial, "rb") as written:
            os.fsync(written.fileno())                         # архив на диске целиком — только после этого он «готов»
        (target / (name + SIDE)).write_bytes(body)
        os.replace(partial, final)
    except BackupError:
        raise
    except (OSError, sqlite3.Error, tarfile.TarError, db.Refusal) as e:
        raise BackupError(f"копия не сделана: {e}") from e
    finally:
        orphan = [] if final.exists() else [target / (name + SIDE)]     # опись без архива не нужна
        for leftover in (snapshot, partial, Path(str(snapshot) + "-journal"), *orphan):
            try:
                leftover.unlink()
            except OSError:
                pass

    manifest["archive"] = str(final)
    manifest["archive_bytes"] = final.stat().st_size
    removed = _thin(target, keep, say) if keep > 0 else []
    spent = time.perf_counter() - started
    line = (f"резервная копия сделана: {final.name}, {human(manifest['archive_bytes'])}, файлов {len(manifest['files'])}"
            + ("" if with_inbox else ", без исходных записей")
            + f", за {spent:.0f} с" + (f"; изменились во время копии — {len(manifest['changed'])}" if manifest["changed"] else "")
            + (f"; НЕ ПРОЧИТАНО файлов — {len(manifest['skipped'])}: копия неполная" if manifest["skipped"] else "")
            + (f"; старых копий удалено — {len(removed)}" if removed else ""))
    _note(cfg, line, {"file": final.name, "bytes": manifest["archive_bytes"], "files": len(manifest["files"]),
                      "inbox": with_inbox, "skipped": len(manifest["skipped"]), "how": how}, by, log, base)
    say(line)
    return manifest


class _Bytes:
    """Опись как файл для архива."""

    def __init__(self, body: bytes):
        self.body, self.at = body, 0

    def read(self, size: int = -1) -> bytes:
        size = len(self.body) - self.at if size < 0 else size
        data = self.body[self.at:self.at + size]
        self.at += len(data)
        return data


def _thin(target: Path, keep: int, say) -> list[str]:
    """Оставляет keep последних копий, остальные стирает — вместе с их описями."""
    removed = []
    for old in _latest(target)[keep:]:
        old.unlink()
        Path(str(old) + SIDE).unlink(missing_ok=True)
        removed.append(old.name)
    return removed


def _line(cfg, line: str, log) -> None:
    """Строка в журнал работы: в журнал сервиса, когда копию делает он, иначе — дописью в файл журнала."""
    if log is not None:
        log(line)
        return
    try:
        path = Path(cfg.outbox) / logbook.LOG_DIR / logbook.LOG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as out:
            out.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def _noted(cfg, base: db.Database | None, write) -> None:
    """write(база) — в базу сервиса: его собственную или открытую на время. Базы нет — ничего: заводить
    пустую ради отметки нельзя. Сбой здесь копию не отменяет и не подменяет её отказ."""
    try:
        if base is not None:
            write(base)
            return
        if not (Path(cfg.state_dir) / db.FILE).is_file():
            return
        own = db.Database(cfg.state_dir)
        try:
            write(own)
        finally:
            own.close()
    except (sqlite3.Error, db.Refusal):
        pass


def _note(cfg, line: str, event: dict, by: str = "", log=None, base: db.Database | None = None) -> None:
    """Итог копии — строкой в журнал работы, событием в журнал действий и отметкой «последняя копия»
    для страницы администратора; отметка о прежнем сбое снимается. Сбой здесь копию не отменяет: архив
    уже готов."""
    _line(cfg, line, log)

    def write(into: db.Database) -> None:
        with into.write() as con:
            journal.Journal(into).write("backup", by, **event)
            into.note(LAST, json.dumps({"at": journal.now_iso(), **event}, ensure_ascii=False))
            con.execute("DELETE FROM meta WHERE key = ?", (FAILED,))
    _noted(cfg, base, write)


def failed(cfg, why: str, *, by: str = "", how: str = COMMAND, log=None, base: db.Database | None = None) -> None:
    """Несостоявшаяся копия — строкой «ВНИМАНИЕ» в журнал работы (вывода копии по расписанию никто не
    видит), событием в журнал действий и отметкой для предупреждения на странице «Обработка»: она висит,
    пока следующая копия не удастся."""
    _line(cfg, f"ВНИМАНИЕ: резервная копия НЕ сделана: {why}", log)

    def write(into: db.Database) -> None:
        with into.write():
            journal.Journal(into).write("backup_failed", by, why=why, how=how)
            into.note(FAILED, json.dumps({"at": journal.now_iso(), "why": why, "how": how, "by": by},
                                         ensure_ascii=False))
    _noted(cfg, base, write)


def last(base: db.Database) -> dict | None:
    """Последняя копия — командой или из сервиса: когда, архив, размер, с записями ли, как сделана (how)."""
    noted = base.noted(LAST)
    return json.loads(noted) if noted else None


def failure(base: db.Database) -> dict | None:
    """Последняя несостоявшаяся копия, если удачной после неё не было: когда, почему, как сделана, кем."""
    noted = base.noted(FAILED)
    return json.loads(noted) if noted else None


# ---------- расписание ----------

# Расписание, пока его не сохраняли на странице, — выключено. keep — сколько последних копий держать.
PLAN = {"on": False, "time": "03:00", "every": "day", "weekday": 0, "inbox": True, "keep": 7}
EVERY = ("day", "week")
KEEP_MAX = 365
_CLOCK = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def plan_of(on: bool, at: str, every: str, weekday: int, inbox: bool, keep: int) -> dict:
    """Расписание со страницы, проверенное. Не годится — BackupError: что поправить."""
    if not _CLOCK.fullmatch(at or ""):
        raise BackupError("время копии — ЧЧ:ММ по часовому поясу сервиса, например 03:00")
    if every not in EVERY:
        raise BackupError("частота копии: day — каждый день или week — раз в неделю")
    if not 0 <= weekday <= 6:
        raise BackupError("день недели: от 0 (понедельник) до 6 (воскресенье)")
    if not 1 <= keep <= KEEP_MAX:
        raise BackupError(f"сколько копий держать: от 1 до {KEEP_MAX}; копии старше стираются")
    return {"on": bool(on), "time": at, "every": every, "weekday": weekday if every == "week" else 0,
            "inbox": bool(inbox), "keep": keep}


WEEKDAYS = ("понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям")


def described(plan: dict) -> str:
    """Расписание словами — для журнала работы."""
    if not plan["on"]:
        return "выключено"
    when = "каждый день" if plan["every"] == "day" else f"по {WEEKDAYS[plan['weekday']]}"
    return (f"{when} в {plan['time']}" + ("" if plan["inbox"] else ", без исходных записей")
            + f", держать копий: {plan['keep']}")


def slot(plan: dict, now: datetime.datetime) -> datetime.datetime:
    """Последнее время копии по расписанию, не позже now — времени сервиса, с его часовым поясом."""
    hour, minute = map(int, plan["time"].split(":"))
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment > now:
        moment -= datetime.timedelta(days=1)
    if plan["every"] == "week":
        moment -= datetime.timedelta(days=(moment.weekday() - plan["weekday"]) % 7)
    return moment


def upcoming(plan: dict, now: datetime.datetime) -> datetime.datetime:
    """Следующее время копии по расписанию — позже now."""
    return slot(plan, now) + datetime.timedelta(days=7 if plan["every"] == "week" else 1)


def due(plan: dict, now: datetime.datetime, since: list[str | None]) -> bool:
    """Положена ли копия сейчас: расписание включено, и его последнее время наступило позже всего, что
    в since (ISO с часовым поясом): сохранения самого расписания, последней копии, последней неудачи.

    Пропущенное не догоняется пачкой: сервис проспал три назначенных времени — одна копия, как только он
    снова работает, потому что «последнее время» одно. Неудача тоже в since: следующая попытка — снова по
    расписанию, а не каждую минуту. Расписание, сохранённое после назначенного времени, ждёт следующего:
    включённое в 10 утра не делает тут же копию «за сегодняшние 3 часа ночи»."""
    if not plan.get("on"):
        return False
    latest = max((datetime.datetime.fromisoformat(at) for at in since if at), default=None)
    return latest is None or slot(plan, now) > latest


# ---------- проверка ----------

def _safe(member: str) -> tuple[str, PurePosixPath]:
    """Часть установки и путь внутри неё по имени в архиве. Имя, ведущее наружу, — отказ."""
    part, _, rest = member.partition("/")
    path = PurePosixPath(rest)
    if part not in PARTS or not rest or path.is_absolute() or ".." in path.parts or "\\" in rest or "\0" in rest:
        raise BackupError(f"в архиве файл с недопустимым именем: {member!r} — архив не от этой команды или испорчен")
    return part, path


def verify(archive: Path, say=print, into: dict[str, Path] | None = None) -> dict:
    """Проверяет архив, не разворачивая установку: читается ли он целиком, все ли файлы описи на
    месте с теми же размерами и контрольными суммами, открывается ли база и цела ли она, сходятся
    ли её числа с описью. Отдаёт опись. into — куда заодно разложить файлы (восстановление)."""
    archive = Path(archive)
    if not archive.is_file():
        raise BackupError(f"архива {archive} нет")
    seen: dict[str, tuple[int, str]] = {}
    manifest = None
    dirs: list[tuple[Path, int]] = []
    work = Path(tempfile.mkdtemp(prefix="podyachiy-verify-"))
    try:
        try:
            with tarfile.open(archive, "r|") as tar:
                for member in tar:
                    if member.name == MANIFEST:
                        manifest = json.loads(tar.extractfile(member).read().decode("utf-8"))
                        continue
                    part, rel = _safe(member.name)
                    if member.isdir():
                        if into:
                            (into[part] / rel).mkdir(parents=True, exist_ok=True)
                            dirs.append((into[part] / rel, member.mtime))
                        continue
                    if not member.isfile():
                        raise BackupError(f"в архиве не файл и не папка: {member.name} — архив не от этой команды")
                    source = tar.extractfile(member)
                    # база нужна на диске, чтобы открыть её и проверить; остальное — только прочитать и сосчитать
                    target = (into[part] / rel) if into else (work / db.FILE if member.name == BASE else None)
                    digest, size = hashlib.sha256(), 0
                    if target:
                        target.parent.mkdir(parents=True, exist_ok=True)
                    out = open(target, "wb") if target else None
                    try:
                        while chunk := source.read(CHUNK):
                            digest.update(chunk)
                            size += len(chunk)
                            if out:
                                out.write(chunk)
                    finally:
                        if out:
                            out.close()
                    if target:
                        os.utime(target, (member.mtime, member.mtime))
                    seen[member.name] = (size, digest.hexdigest())
        except (tarfile.TarError, OSError, EOFError, ValueError) as e:
            raise BackupError(f"архив {archive.name} не читается: {e} — он оборван или испорчен") from e
        if not isinstance(manifest, dict) or manifest.get("kind") != "podyachiy-backup":
            raise BackupError(f"в архиве {archive.name} нет описи ({MANIFEST}): он оборван или сделан не этой командой")
        if manifest.get("format") != FORMAT:
            raise BackupError(f"архив {archive.name} — другого вида (format {manifest.get('format')}), эта версия "
                              f"сервиса понимает {FORMAT}")
        listed = {f["path"]: (f["bytes"], f["sha256"]) for f in manifest["files"]}
        missing = sorted(set(listed) - set(seen))
        extra = sorted(set(seen) - set(listed))
        wrong = sorted(name for name in set(listed) & set(seen) if listed[name] != seen[name])
        if missing or extra or wrong:
            raise BackupError(
                f"архив {archive.name} не сходится с описью: "
                + "; ".join(f"{what} — {len(names)} ({', '.join(names[:3])}{' …' if len(names) > 3 else ''})"
                            for what, names in (("нет файлов", missing), ("лишние файлы", extra),
                                                ("другой размер или контрольная сумма", wrong)) if names))
        side = Path(str(archive) + SIDE)
        if side.is_file():
            try:
                same = json.loads(side.read_text(encoding="utf-8")) == manifest
            except (OSError, ValueError):
                same = False
            if not same:
                raise BackupError(f"опись рядом с архивом ({side.name}) не совпадает с описью внутри него: "
                                  "файл описи от другого архива или изменён")
        base = (into["state"] / db.FILE) if into else work / db.FILE
        if BASE not in seen:
            raise BackupError(f"в архиве {archive.name} нет базы ({BASE})")
        try:
            con = sqlite3.connect(f"file:{base.as_posix()}?mode=ro", uri=True)
            try:
                check = [row[0] for row in con.execute("PRAGMA quick_check")]
                schema = con.execute("PRAGMA user_version").fetchone()[0]
                counts = inventory(con)
            finally:
                con.close()
        except sqlite3.Error as e:
            raise BackupError(f"база из архива {archive.name} не открывается: {e}") from e
        if check != ["ok"]:
            raise BackupError(f"база из архива {archive.name} повреждена: {'; '.join(check[:3])}")
        if schema != manifest["schema"] or counts != manifest["base"]["counts"]:
            raise BackupError(f"база из архива {archive.name} не сходится с описью: в описи схема {manifest['schema']} и "
                              f"{manifest['base']['counts']}, в базе схема {schema} и {counts}")
        for path, mtime in reversed(dirs):                      # время папок — после файлов: запись в папку его меняет
            os.utime(path, (mtime, mtime))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    counts = manifest["base"]["counts"]
    say(f"архив {archive.name} цел: сделан {manifest['created']} сервисом версии {manifest['service'] or 'без номера'}, "
        f"схема базы {manifest['schema']}; файлов {len(listed)}, {human(manifest['bytes'])}"
        + "".join(f"; {part} — {info['files']} ({human(info['bytes'])})" for part, info in manifest["parts"].items())
        + ("" if manifest["inbox"] else "; БЕЗ исходных записей")
        + f". База читается и цела: пользователей {counts['users']}, ключей {counts['keys']}, записей {counts['records']}, "
          f"событий {counts['events']}, расходы {usage.text(counts['spent']) or '0'}"
        + (f". Не прочитано при копировании: {len(manifest['skipped'])} — копия неполная" if manifest["skipped"] else ""))
    return manifest


# ---------- восстановление ----------

# Спутники файла базы — журналы SQLite. Без своей базы они чужие любой другой: SQLite применил бы их к
# восстановленной при первом же открытии (журнал WAL дописал бы в неё страницы прежней базы,
# незакрытый журнал отката «вернул» бы их) — и испортил бы её.
COMPANIONS = ("-wal", "-shm", "-journal")


def _stopped(state: Path) -> None:
    """Сервис над этой базой не работает — или BackupError. Признак — блокировка самого SQLite: каждое
    соединение, читавшее базу в режиме WAL, держит на её файле разделяемую блокировку, пока открыто, а
    сервис держит свои соединения открытыми всё время работы. Исключительную блокировку тогда не взять.
    Тот же признак работает между контейнерами: папку состояния, где блокировки между процессами не
    держатся, сервис не принимает при запуске (db.probe). Остановленный сервис блокировок не держит,
    в том числе упавший: их снимает система вместе с процессом."""
    try:
        con = sqlite3.connect(state / db.FILE, timeout=0, isolation_level=None)
    except sqlite3.Error as e:
        raise BackupError(f"база {state / db.FILE} не открывается: {e}") from e
    try:
        con.execute("PRAGMA locking_mode=EXCLUSIVE")
        con.execute("BEGIN EXCLUSIVE")
        con.execute("ROLLBACK")
    except sqlite3.OperationalError as e:
        if "locked" not in str(e):
            raise BackupError(f"база {state / db.FILE} не открывается: {e}") from e
        raise BackupError(f"сервис работает: база {state / db.FILE} открыта им. Запись в базу у работающего сервиса за "
                          "спиной он не увидит — журнал обработки он держит ещё и в памяти — и следующей своей записью "
                          "затрёт. Остановите сервис (docker compose stop) и запустите команду через docker compose run") from e
    finally:
        con.close()


def restore(cfg, archive: Path, say=print, kept=db.kept) -> dict:
    """Разворачивает архив в пустую установку: папки берутся из настроек. Сначала архив проверяется
    целиком, потом раскладывается и сверяется ещё раз — уже разложенным. Отдаёт опись."""
    places = roots(cfg)
    if not kept(places["state"]):
        raise BackupError(f"папка состояния {places['state']} не подключена с сервера: восстановленное осталось бы "
                          "внутри контейнера. Запускайте команду в установке с её compose.yml")
    busy = []
    if (places["state"] / db.FILE).exists():
        busy.append(f"в папке состояния уже есть база ({places['state'] / db.FILE})")
    else:
        left = [db.FILE + tail for tail in COMPANIONS if (places["state"] / (db.FILE + tail)).exists()]
        if left:
            busy.append(f"в папке состояния остались журналы прежней базы ({', '.join(left)}): SQLite применил бы их к "
                        "восстановленной и испортил бы её — уберите их вместе с прежней базой")
    for part in ("outbox", "inbox"):
        held = [p.name for p in places[part].iterdir() if p.name not in (logbook.LOG_DIR, upload.PARTS_DIR)] \
            if places[part].is_dir() else []
        if held:
            busy.append(f"папка {places[part]} не пуста ({', '.join(sorted(held)[:3])}{' …' if len(held) > 3 else ''})")
    if busy:
        raise BackupError("восстанавливают только в пустую установку, а здесь " + "; ".join(busy) + ". Поверх работающей "
                          "установки архив не разворачивается: остановите сервис и уберите (переименуйте) папки "
                          "state, outbox и inbox — или восстановите в новую папку")
    manifest = verify(archive, lambda line: None)               # сначала — цел ли архив; до этого ничего не трогаем
    if manifest["schema"] > db.SCHEMA_VERSION:
        raise BackupError(f"архив сделан более новой версией сервиса (схема базы {manifest['schema']}, эта версия знает "
                          f"{db.SCHEMA_VERSION}): восстановите его образом версии {manifest['service'] or 'той же'} или новее")
    try:
        for place in places.values():
            place.mkdir(parents=True, exist_ok=True)
        verify(archive, lambda line: None, into=places)         # разложить и сверить каждый файл с описью
    except OSError as e:
        raise BackupError(f"архив не развернулся: {e}. Освободите место или дайте права и повторите в пустую папку") from e
    counts = manifest["base"]["counts"]
    line = (f"установка восстановлена из архива {Path(archive).name} (сделан {manifest['created']}, версия сервиса "
            f"{manifest['service'] or 'без номера'}): файлов {len(manifest['files'])}, {human(manifest['bytes'])}; "
            f"пользователей {counts['users']}, записей {counts['records']}, событий {counts['events']}, "
            f"расходы {usage.text(counts['spent']) or '0'}")
    try:
        path = places["outbox"] / logbook.LOG_DIR / logbook.LOG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass
    say(line)
    if manifest["schema"] < db.SCHEMA_VERSION:
        say(f"схема базы в архиве старее ({manifest['schema']}), чем у этой версии сервиса ({db.SCHEMA_VERSION}): "
            "сервис обновит её сам при запуске")
    if not manifest["inbox"]:
        say("архив сделан без исходных записей: протоколы, расшифровки и звук на месте, но обработать запись "
            "заново с распознаванием нельзя, пока исходник не положат обратно в inbox")
    say("дальше: верните на место .env и compose.yml (их в архиве нет) и запустите сервис — docker compose up -d")
    return manifest


def record(cfg, archive: Path, file: str, say=print) -> dict:
    """Возвращает из архива одну запись, удалённую по ошибке: исходник и папку результатов — на диск,
    строку журнала обработки, отметки поручений и словарь записи — в базу. Сервис на это время
    остановлен: журнал обработки он держит ещё и в памяти. Расходы записи остались в журнале
    событий и при удалении — их команда не трогает."""
    places = roots(cfg)
    if not (places["state"] / db.FILE).is_file():
        raise BackupError(f"базы {places['state'] / db.FILE} нет: возвращать запись некуда")
    _stopped(places["state"])
    manifest = verify(archive, lambda line: None)
    work = Path(tempfile.mkdtemp(prefix="podyachiy-record-"))
    written: list[Path] = []
    try:
        with tarfile.open(archive, "r|") as tar:                # база из архива — во временную папку
            for member in tar:
                if member.name == BASE:
                    with open(work / db.FILE, "wb") as out:
                        shutil.copyfileobj(tar.extractfile(member), out)
                    break
        old = sqlite3.connect(f"file:{(work / db.FILE).as_posix()}?mode=ro", uri=True)
        old.row_factory = sqlite3.Row
        try:
            row = old.execute("SELECT * FROM records WHERE file = ?", (file,)).fetchone()
            if row is None:
                raise BackupError(f"записи «{file}» в архиве {Path(archive).name} нет: проверьте путь (папка/имя с расширением) "
                                  "или возьмите архив, сделанный, когда запись ещё была")
            noted = old.execute("SELECT * FROM marks WHERE file = ?", (file,)).fetchall()
            words = old.execute("SELECT * FROM dictionaries WHERE scope = 'records' AND path = ?", (file,)).fetchone()
            loader = old.execute("SELECT actor FROM uploads WHERE path = ?", (file,)).fetchone()
        finally:
            old.close()
        stem = json.loads(row["data"]).get("stem") or PurePosixPath(file).stem
        folder = PurePosixPath(file).parent.as_posix()
        results = "outbox/" + (f"{folder}/" if folder != "." else "") + stem
        wanted = {f["path"] for f in manifest["files"] if f["path"] == f"inbox/{file}" or f["path"].startswith(results + "/")}
        base = db.Database(places["state"])
        try:
            if base.row("SELECT 1 FROM records WHERE file = ?", file):
                raise BackupError(f"запись «{file}» в установке уже есть — возвращать нечего")
            taken = [name for name in sorted(wanted) if (places[_safe(name)[0]] / _safe(name)[1]).exists()]
            if taken or (places["outbox"] / PurePosixPath(results[len("outbox/"):])).exists():
                raise BackupError(f"на месте записи «{file}» уже лежат файлы ({', '.join(taken[:3]) or results}): "
                                  "уберите их или верните запись под другим именем вручную")
            with tarfile.open(archive, "r|") as tar:
                for member in tar:
                    if member.name in wanted:
                        part, rel = _safe(member.name)
                        target = places[part] / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        written.append(target)
                        with open(target, "wb") as out:
                            shutil.copyfileobj(tar.extractfile(member), out)
                        os.utime(target, (member.mtime, member.mtime))
            with base.write() as con:
                con.execute("INSERT INTO records (file, status, finished_at, data, marks_at) VALUES (?, ?, ?, ?, ?)",
                            tuple(row[k] for k in ("file", "status", "finished_at", "data", "marks_at")))
                columns = "file, key, who, task, status, done_by, done_at, comment, due, due_by, due_at"
                con.executemany(f"INSERT INTO marks ({columns}) VALUES ({', '.join('?' * 11)})",
                                [tuple(mark[k] for k in columns.split(", ")) for mark in noted])
                if words:
                    con.execute("INSERT OR REPLACE INTO dictionaries (scope, path, people, terms, author, at, changed) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(words))
                if loader:
                    con.execute("INSERT OR REPLACE INTO uploads (path, actor) VALUES (?, ?)", (file, loader["actor"]))
                journal.Journal(base).write("record_restore", "", file=file, archive=Path(archive).name)
        finally:
            base.close()
    except BaseException:
        for path in written:                                    # не записалась база — файлы обратно не оставляем
            path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    line = (f"запись «{file}» возвращена из архива {Path(archive).name}: файлов {len(written)}"
            + ("" if f"inbox/{file}" in wanted else " (исходной записи в архиве нет — только результаты)")
            + f", отметок поручений {len(noted)}" + (", словарь записи" if words else ""))
    try:
        with open(places["outbox"] / logbook.LOG_DIR / logbook.LOG_FILE, "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass
    say(line)
    return {"file": file, "files": len(written), "marks": len(noted)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backup", description="Резервная копия установки «Подьячий»")
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("create", help="сделать копию; сервис при этом может работать")
    make.add_argument("--to", metavar="ПАПКА", help="куда положить архив; по умолчанию — папка копий (BACKUP_DIR)")
    make.add_argument("--no-inbox", action="store_true", help="без исходных записей (папка inbox)")
    make.add_argument("--keep", type=int, default=0, metavar="N", help="оставить N последних копий, старые стереть")
    check = commands.add_parser("verify", help="проверить архив, не разворачивая его")
    check.add_argument("archive", nargs="?", help="архив; не указан — последний в папке копий")
    back = commands.add_parser("restore", help="развернуть архив в пустую установку (сервис остановлен)")
    back.add_argument("archive")
    one = commands.add_parser("record", help="вернуть из архива одну удалённую запись (сервис остановлен)")
    one.add_argument("archive")
    one.add_argument("file", help="путь записи, каким он был: папка/имя с расширением")
    args = parser.parse_args(argv)
    cfg = Settings.from_env()
    try:
        if args.command == "create":
            if args.keep < 0:
                raise BackupError("--keep: число копий не бывает отрицательным")
            made = create(cfg, Path(args.to) if args.to else None, not args.no_inbox, args.keep)
            if made["changed"]:
                print(f"изменились, пока копировались (в архиве — как успели прочитаться): {', '.join(made['changed'][:5])}"
                      + (" …" if len(made["changed"]) > 5 else ""))
            for lost in made["skipped"][:20]:
                print(f"НЕ ПРОЧИТАН: {lost['path']} — {lost['why']}")
            print(f"архив: {made['archive']}")
            print(f"опись: {made['archive']}{SIDE}")
            return 2 if made["skipped"] else 0
        if args.command == "verify":
            archive = Path(args.archive) if args.archive else next(iter(_latest(Path(cfg.backup_dir))), None)
            if archive is None:
                raise BackupError(f"в папке копий {cfg.backup_dir} архивов нет")
            verify(archive)
            return 0
        if args.command == "record":
            record(cfg, Path(args.archive), args.file)
            return 0
        restore(cfg, Path(args.archive))
        return 0
    except BackupError as e:
        print(f"ОТКАЗ: {e}", file=sys.stderr)
        if args.command == "create":
            failed(cfg, str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
