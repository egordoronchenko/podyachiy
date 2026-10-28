"""Переименование и удаление папок администратором.

Папка живёт сразу в нескольких местах: записи во входной папке, результаты в выходной,
а в базе — строки журнала обработки, отметки поручений, права пользователей, словари, признак
«серия совещаний» и пути в журнале расходов. Переименование и удаление проходят по всем, иначе
записи «потеряются» на странице или пойдут в обработку заново. Всё, что в базе, меняется одной
транзакцией: сначала переезжают папки на диске, потом записывается база; не записалась — папки
возвращаются на место, и половины переезда не остаётся.

Гонки со сканером и обработчиками нет по той же причине, что и у действий с записью
(подробно — в records.py): тот, кто зовёт rename и remove (app/web), держит
Service.scan_lock, а сами функции идут в событийном цикле без await — сканер не застанет
середину переезда и не примет переехавшие файлы за новые записи, а обработчик не возьмёт
запись из папки между проверкой «папка не занята» и переездом.

Удаление — это перенос в корзину (.trash во входной и выходной папках), а не стирание:
папку с записями удаляют одним нажатием, и ошибку должно быть можно исправить. Корзина
чистится сама через TRASH_DAYS дней. Рядом с удалённым — опись (NOTE): что это было, кто удалил,
строки журнала обработки, отметки поручений и то, что удаление убрало из базы (kept: права, признак
серии, словари, авторы загрузок). По ней удалённое возвращается кнопкой (restore, records.restore):
файлы — на прежнее место, строки — в базу, тем же порядком, что у переезда, — сначала файлы, потом база,
опись убирается последней перед фиксацией; сбой — файлы обратно в корзину, опись на месте.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path, PurePosixPath

from . import marks, series, upload, users

TRASH = ".trash"
TRASH_DAYS = 30
NOTE = "удалено.json"           # опись удалённого рядом с ним в корзине
STAMP = re.compile(r"\d{8}-\d{6}(?:-\d+)?")      # имя папки корзины (trash_stamp)
NOT_IN_TRASH = "в корзине такого нет: его уже вернули или срок хранения вышел"
BROKEN_NOTE = "опись удалённого в корзине не читается — вернуть его можно только вручную (раздел о корзине в инструкции)"
# прежнее место занято: вернуть рядом под другим именем сервис не берётся — сначала освободить место
FOLDER_TAKEN = ("на прежнем месте уже есть папка с таким именем — переименуйте или удалите её, "
                "потом верните удалённую")
PARENT_GONE = ("папки, в которой это лежало, больше нет: верните из корзины сначала её "
               "или создайте её заново под тем же именем")
EMPTY_BIN = "в корзине не осталось файлов этого удаления — вернуть нечего"
# один текст на несуществующую и на закрытую человеку папку: существование чужой не раскрывается
NO_FOLDER = "такой папки нет или она вам не открыта"
# папка результатов записи «вкс.mp3» — «вкс»: подпапка «вкс» положила бы свои результаты туда же
RESULTS_TAKEN = ("это имя занято среди результатов: так называется запись в этой папке (имя без расширения) "
                 "или папка, от которой остались одни результаты, — у новой папки результаты легли бы туда же; "
                 "выберите другое имя")


class FolderError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def same_place(old: Path, new: Path) -> bool:
    """Ведёт ли другое имя к тому же файлу или папке. Так бывает на томах без различия регистра
    (Windows, macOS, папка Windows в Docker Desktop): «Папка» и «папка» там — одна папка, и новое
    имя, отличное только регистром, «уже занято» ею самой."""
    try:
        return old != new and os.path.samefile(old, new)
    except OSError:
        return False


def move(old: Path, new: Path) -> None:
    """Переименование файла или папки. Смена одного регистра (same_place) — через промежуточное
    имя рядом: не всякий такой том переименовывает «Папку» в «папку» напрямую — иной молча оставляет
    прежнее имя. Промежуточное имя начинается с точки — сканер такое не смотрит."""
    if same_place(old, new):
        step = old.with_name(f".{old.name}.{secrets.token_hex(4)}")
        old.rename(step)
        step.rename(new)
    else:
        old.rename(new)


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + "/")


def _path(cfg, folder: str) -> str:
    """Путь папки из запроса — той же проверкой, что у новой папки (upload.folder_arg): «..» увёл бы
    переезд за пределы входной папки, а «Проекты/../Пользовательские» обошёл бы защиту ниже."""
    try:
        return upload.folder_arg(folder, cfg.outbox_inside_inbox)
    except upload.UploadError as e:
        raise FolderError(e.status, e.detail)


def _check(folder: str, service, people: users.Users) -> None:
    if not folder:
        raise FolderError(400, "корень не переименовать и не удалить")
    if folder == users.HOME_ROOT:
        raise FolderError(400, "это папка личных папок пользователей — она нужна сервису")
    owner = people.owner_of(folder)
    if owner:
        raise FolderError(400, f"это личная папка пользователя {owner}: она привязана к его учётной записи")
    paths = [i.path for i in service.queue] + list(service.pending) + [j["file"] for j in service.running]
    if any(_inside(p, folder) for p in paths):
        raise FolderError(409, "в этой папке сейчас копируется или обрабатывается запись — дождитесь окончания")


def rename(cfg, service, people: users.Users, folder: str, name: str, also=None) -> str:
    """Новое имя папки на том же месте. Возвращает новый путь.
    also(новый путь) — что ещё записать в базу той же транзакцией: словари, событие журнала."""
    return _rename(cfg, service, people, _path(cfg, folder), name, also)


def to_nfc(cfg, service, people: users.Users, folder: str, also=None) -> str:
    """Папку, известную сервису под именем в разложенной форме Unicode (так пишет macOS), — в составную
    (upload.nfc): тем же переездом, что rename. Путь — как он записан на диске и в базе: проверка пути
    приводит его к составной форме, и он вёл бы уже к другой папке. Папки выше — уже в составной."""
    parent, _, name = folder.rpartition("/")
    if _path(cfg, folder) != upload.nfc(folder) or upload.nfc(parent) != parent:
        raise FolderError(400, "папки выше ещё в разложенной форме — сначала они")
    return _rename(cfg, service, people, folder, name, also)


def _rename(cfg, service, people: users.Users, folder: str, name: str, also) -> str:
    _check(folder, service, people)
    name = upload.nfc(name.strip())
    try:
        upload._check_part(name, "имя папки")
    except upload.UploadError as e:
        raise FolderError(e.status, e.detail)
    parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
    target = f"{parent}/{name}" if parent else name
    if target == folder:
        return folder
    if (cfg.inbox / target).exists() and not same_place(cfg.inbox / folder, cfg.inbox / target):
        raise FolderError(409, "папка с таким именем здесь уже есть")
    if (cfg.outbox / target).exists() and not same_place(cfg.outbox / folder, cfg.outbox / target):
        raise FolderError(409, RESULTS_TAKEN)
    if not (cfg.inbox / folder).is_dir() and not (cfg.outbox / folder).is_dir():
        raise FolderError(404, NO_FOLDER)
    moved = []
    try:
        for root in (cfg.inbox, cfg.outbox):
            if (root / folder).is_dir():
                move(root / folder, root / target)
                moved.append(root)
        with service.ledger.db.write():
            service.ledger.move(folder, target, str(cfg.outbox / folder), str(cfg.outbox / target))
            people.rename_folder(folder, target)
            series.Store(service.ledger.db).move_folder(folder, target)
            if also:
                also(target)
    except BaseException:
        for root in reversed(moved):
            move(root / target, root / folder)
        raise
    return target


def trash_stamp(cfg) -> str:
    """Метка новой папки в корзине: время до секунды, а занята — с номером. Иначе две папки или
    записи, удалённые в одну секунду, легли бы в одну папку корзины, и вторая затёрла бы и
    одноимённую первую, и её «удалено.json»."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    taken = lambda s: any((root / TRASH / s).exists() for root in (cfg.inbox, cfg.outbox))
    n = 1
    while taken(stamp if n == 1 else f"{stamp}-{n}"):
        n += 1
    return stamp if n == 1 else f"{stamp}-{n}"


def remove(cfg, service, people: users.Users, folder: str, by: str, also=None) -> dict:
    """Папка со всем содержимым уходит в корзину. Возвращает, сколько чего убрано.
    also(что убрано) — что ещё записать в базу той же транзакцией: словари, событие журнала; что оно
    вернёт (словарь), ложится в опись (kept) — для возврата из корзины."""
    folder = _path(cfg, folder)
    _check(folder, service, people)
    if not (cfg.inbox / folder).is_dir() and not (cfg.outbox / folder).is_dir():
        raise FolderError(404, NO_FOLDER)
    stamp = trash_stamp(cfg)
    name = folder.rsplit("/", 1)[-1]
    moved = []
    done = {"records": 0, "trash": f"{TRASH}/{stamp}", "keep_days": TRASH_DAYS}
    note = cfg.outbox / TRASH / stamp
    try:
        for root in (cfg.inbox, cfg.outbox):
            if (root / folder).is_dir():
                bin_dir = root / TRASH / stamp
                bin_dir.mkdir(parents=True)
                (root / folder).rename(bin_dir / name)
                moved.append((root / folder, bin_dir / name))
        with service.ledger.db.write():
            # отметки поручений, права и признак серии уходят из базы — сначала запомнить их для возврата
            noted = marks.Store(service.ledger.db).everything(folder)
            kept = {"grants": people.grants_below(folder),
                    "series_off": sorted(f for f in series.Store(service.ledger.db).off() if _inside(f, folder))}
            records = service.ledger.drop(folder)
            people.drop_folder(folder)
            series.Store(service.ledger.db).drop_folder(folder)
            done["records"] = len(records)
            if also:
                kept.update(also(done) or {})
            # что это было и откуда — рядом с результатами, чтобы папку можно было вернуть кнопкой или руками.
            # До записи базы: не записалась опись — удаления нет, а не «удалено, но отказ 500»
            trash_note(note, {"folder": folder, "by": by, "at": stamp, "moved": [str(binned) for _, binned in moved],
                              "records": records, "marks": noted, "kept": kept})
    except BaseException:
        (note / NOTE).unlink(missing_ok=True)
        for place, binned in reversed(moved):
            binned.rename(place)
            _tidy(binned.parent)
        _tidy(note)
        raise
    return done


def trash_note(where: Path, what: dict) -> None:
    """Опись удалённого в папке корзины: что это было, кто и когда удалил, где оно теперь."""
    where.mkdir(parents=True, exist_ok=True)
    (where / NOTE).write_text(json.dumps(what, ensure_ascii=False, indent=1), encoding="utf-8")


def read_note(cfg, stamp: str) -> dict:
    """Опись удалённого по имени папки корзины — проверенная: путь удалённого и строки журнала в ней
    те же, что проходят проверку пути у новой папки и записи (upload). Файл описи лежит на диске,
    его могли поправить руками, а по нему файлы переезжают во входную папку."""
    if not isinstance(stamp, str) or not STAMP.fullmatch(stamp):
        raise FolderError(404, NOT_IN_TRASH)
    try:
        note = json.loads((cfg.outbox / TRASH / stamp / NOTE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FolderError(404, NOT_IN_TRASH)
    except (OSError, ValueError):
        raise FolderError(409, BROKEN_NOTE)
    try:
        if "folder" in note:
            where = _path(cfg, note["folder"])
            assert where and where == note["folder"]
            assert all(r["file"].startswith(where + "/") for r in note["records"])
        else:
            file = note["file"]
            parent, _, name = file.rpartition("/")
            assert _path(cfg, parent) == parent
            upload._check_part(name, "имя записи")
            assert [r["file"] for r in note["records"]] == [file]
            upload._check_part(note["records"][0].get("stem") or PurePosixPath(name).stem, "имя записи")
        assert isinstance(note.get("marks", {}), dict) and isinstance(note.get("kept", {}), dict)
        assert set(note.get("marks", {})) <= {r["file"] for r in note["records"]}
    except (AssertionError, FolderError, upload.UploadError, KeyError, TypeError, AttributeError):
        raise FolderError(409, BROKEN_NOTE)
    return note


def entry(stamp: str, note: dict) -> dict:
    """Строка списка корзины: что удалено (kind — folder или record, path — путь), кто и когда, до какого
    дня хранится, сколько в нём записей и вернётся ли оно целиком (partial — опись до версии 1.2.0: словари,
    права и авторы загрузок в ней не записаны и не вернутся)."""
    at = datetime.datetime.strptime(stamp[:15], "%Y%m%d-%H%M%S").astimezone()
    path = note["folder"] if "folder" in note else note["file"]
    found = {"id": stamp, "kind": "folder" if "folder" in note else "record", "path": path,
             "dir": path.rpartition("/")[0], "name": path.rpartition("/")[2], "by": note.get("by") or "",
             "at": at.isoformat(timespec="seconds"),
             "until": (at + datetime.timedelta(days=TRASH_DAYS)).date().isoformat(),
             "records": len(note["records"]), "partial": "kept" not in note}
    if found["kind"] == "record":
        found["title"] = (note["records"][0].get("title") or "").strip() or None
    return found


def binned(cfg) -> list[dict]:
    """Что лежит в корзине — по описям, свежее первым. Папка корзины без описи или с негодной описью
    в список не идёт: вернуть её можно только руками."""
    try:
        stamps = [p.name for p in (cfg.outbox / TRASH).iterdir() if STAMP.fullmatch(p.name)]
    except OSError:
        return []
    found = []
    for stamp in stamps:
        try:
            found.append(entry(stamp, read_note(cfg, stamp)))
        except FolderError:
            continue
    return sorted(found, key=lambda x: x["id"], reverse=True)


def unbin(cfg, base, stamp: str, places: list[tuple[Path, Path]], commit) -> None:
    """Обратный перенос из корзины: places — (что в корзине, куда вернуть). Сначала файлы, потом база:
    commit() пишет её одной транзакцией, а опись убирается последней перед фиксацией — после фиксации
    сбоя на диске уже не бывает. Не вышло — файлы назад в корзину, опись на место, созданные для них
    папки убираются."""
    note = cfg.outbox / TRASH / stamp / NOTE
    saved = note.read_bytes()
    moved: list[tuple[Path, Path]] = []
    made: list[Path] = []
    try:
        for binned_at, place in places:
            for up in reversed([d for d in place.parents if not d.exists()]):
                up.mkdir()
                made.append(up)
            binned_at.rename(place)
            moved.append((binned_at, place))
        with base.write():
            commit()
            note.unlink()
    except BaseException:
        if not note.exists():
            note.write_bytes(saved)
        for binned_at, place in reversed(moved):
            place.rename(binned_at)
        for up in reversed(made):
            _tidy(up)
        raise
    for root in (cfg.inbox, cfg.outbox):
        _tidy(root / TRASH / stamp)


def restore(cfg, service, people: users.Users, stamp: str, also=None) -> dict:
    """Удалённую папку — из корзины на прежнее место: её записи и результаты, строки журнала обработки,
    отметки поручений, права и признак серии из описи. also(опись) — что ещё записать в базу той же
    транзакцией: словари, событие журнала. Прежнее место занято или папки выше больше нет — отказ 409.
    Обработки не будет: строки журнала возвращаются вместе с файлами. Возвращает путь папки и сколько
    записей вернулось."""
    note = read_note(cfg, stamp)
    if "folder" not in note:
        raise FolderError(404, NOT_IN_TRASH)
    folder = note["folder"]
    parent, _, name = folder.rpartition("/")
    if parent and not (cfg.inbox / parent).is_dir():
        raise FolderError(409, PARENT_GONE)
    if (cfg.inbox / folder).exists() or any(_inside(f, folder) for f in service.ledger.records):
        raise FolderError(409, FOLDER_TAKEN)
    if (cfg.outbox / folder).exists():
        raise FolderError(409, RESULTS_TAKEN)
    places = [(root / TRASH / stamp / name, root / folder) for root in (cfg.inbox, cfg.outbox)
              if (root / TRASH / stamp / name).exists()]
    if not places:
        raise FolderError(409, EMPTY_BIN)
    kept = note.get("kept") or {}

    def commit() -> None:
        service.ledger.restore(note["records"])
        for file, saved in (note.get("marks") or {}).items():
            marks.Store(service.ledger.db).restore(file, saved)
        people.put_grants(kept.get("grants") or [])
        for off in kept.get("series_off") or []:
            series.Store(service.ledger.db).put(off, False)
        if also:
            also(note)

    unbin(cfg, service.ledger.db, stamp, places, commit)
    return {"path": folder, "records": len(note["records"])}


def _tidy(bin_dir: Path) -> None:
    """Папка корзины, из которой всё вернули на место, не нужна."""
    try:
        bin_dir.rmdir()
    except OSError:
        pass


def purge(cfg, log, now: float | None = None) -> tuple[int, dict[str, str]]:
    """Стирает из корзины то, что лежит дольше TRASH_DAYS. Корзины входной и выходной папок
    чистятся независимо: что не удалось в одной, другой не мешает. Возвращает число стёртых
    папок и причины пропуска: чья корзина (словами, без пути на диске) → почему её отжившее
    осталось на месте."""
    now = time.time() if now is None else now
    gone = 0
    skipped: dict[str, str] = {}
    for root, whose in ((cfg.inbox, "входной папки"), (cfg.outbox, "папки результатов")):
        try:
            entries = list((root / TRASH).iterdir())
        except OSError:
            continue                    # корзины нет: удалять было нечего
        for entry in entries:
            try:
                if entry.is_dir() and now - entry.stat().st_mtime > TRASH_DAYS * 86400:
                    shutil.rmtree(entry)
                    gone += 1
            except OSError as e:
                skipped[whose] = e.strerror or str(e)
    return gone, skipped
