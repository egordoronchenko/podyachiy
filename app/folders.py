"""Переименование и удаление папок администратором.

Папка живёт сразу в нескольких местах: записи во входной папке, результаты в выходной,
а в базе — строки журнала обработки, отметки поручений, права пользователей, словари, признак
«серия совещаний» и пути в журнале расходов. Переименование и удаление проходят по всем, иначе
записи «потеряются» на странице или пойдут в обработку заново. Всё, что в базе, меняется одной
транзакцией: сначала переезжают папки на диске, потом записывается база; не записалась — папки
возвращаются на место, и половины переезда не остаётся.

Гонки со сканером и обработчиками нет по той же причине, что и у действий с записью
(подробно — в records.py): тот, кто зовёт rename и remove (web.py), держит
Service.scan_lock, а сами функции идут в событийном цикле без await — сканер не застанет
середину переезда и не примет переехавшие файлы за новые записи, а обработчик не возьмёт
запись из папки между проверкой «папка не занята» и переездом.

Удаление — это перенос в корзину (.trash во входной и выходной папках), а не стирание:
папку с записями удаляют одним нажатием, и ошибку должно быть можно исправить. Корзина
чистится сама через TRASH_DAYS дней.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import marks, series, upload, users

TRASH = ".trash"
TRASH_DAYS = 30
# один текст на несуществующую и на закрытую человеку папку: существование чужой не раскрывается
NO_FOLDER = "такой папки нет или она вам не открыта"


class FolderError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + "/")


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
    folder = users.norm_folder(folder)
    _check(folder, service, people)
    try:
        upload._check_part(name.strip(), "имя папки")
    except upload.UploadError as e:
        raise FolderError(e.status, e.detail)
    parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
    target = f"{parent}/{name.strip()}" if parent else name.strip()
    if target == folder:
        return folder
    if (cfg.inbox / target).exists() or (cfg.outbox / target).exists():
        raise FolderError(409, "папка с таким именем здесь уже есть")
    if not (cfg.inbox / folder).is_dir() and not (cfg.outbox / folder).is_dir():
        raise FolderError(404, NO_FOLDER)
    moved = []
    try:
        for root in (cfg.inbox, cfg.outbox):
            if (root / folder).is_dir():
                (root / folder).rename(root / target)
                moved.append(root)
        with service.ledger.db.write():
            service.ledger.move(folder, target, str(cfg.outbox / folder), str(cfg.outbox / target))
            people.rename_folder(folder, target)
            series.Store(service.ledger.db).move_folder(folder, target)
            if also:
                also(target)
    except BaseException:
        for root in reversed(moved):
            (root / target).rename(root / folder)
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
    also(что убрано) — что ещё записать в базу той же транзакцией: словари, событие журнала."""
    folder = users.norm_folder(folder)
    _check(folder, service, people)
    if not (cfg.inbox / folder).is_dir() and not (cfg.outbox / folder).is_dir():
        raise FolderError(404, NO_FOLDER)
    stamp = trash_stamp(cfg)
    name = folder.rsplit("/", 1)[-1]
    moved = []
    done = {"records": 0, "trash": f"{TRASH}/{stamp}", "keep_days": TRASH_DAYS}
    try:
        for root in (cfg.inbox, cfg.outbox):
            if (root / folder).is_dir():
                bin_dir = root / TRASH / stamp
                bin_dir.mkdir(parents=True)
                (root / folder).rename(bin_dir / name)
                moved.append((root / folder, bin_dir / name))
        with service.ledger.db.write():
            # отметки поручений уходят из базы вместе со строками записей — сначала запомнить их
            noted = marks.Store(service.ledger.db).everything(folder)
            records = service.ledger.drop(folder)
            people.drop_folder(folder)
            series.Store(service.ledger.db).drop_folder(folder)
            done["records"] = len(records)
            if also:
                also(done)
    except BaseException:
        for place, binned in reversed(moved):
            binned.rename(place)
            _tidy(binned.parent)
        raise
    # что это было и откуда — рядом с результатами, чтобы папку можно было вернуть руками
    note = cfg.outbox / TRASH / stamp
    note.mkdir(parents=True, exist_ok=True)
    (note / "удалено.json").write_text(json.dumps(
        {"folder": folder, "by": by, "at": stamp, "moved": [str(binned) for _, binned in moved],
         "records": records, "marks": noted},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return done


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
