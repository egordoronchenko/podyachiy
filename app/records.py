"""Действия с отдельной записью: переименовать, перенести в другую папку, удалить.

Запись, как и папка, живёт в нескольких местах: исходный файл во входной папке, папка
результатов в выходной (outbox/<папка>/<stem>/, а в ней файлы, названные по тому же stem),
а в базе — строка в журнале обработки, отметки поручений, словарь записи и пути в журнале
расходов. Действие проходит по всем, и повторной обработки не бывает: журнал обработки
ведётся по пути, и запись переезжает в нём вместе с файлом. Всё, что в базе, меняется одной
транзакцией: сначала переезжают файлы, потом записывается база; не записалась — файлы
возвращаются на место.

Гонки со сканером и обработчиком нет, и вот почему. Тот, кто зовёт эти функции (web.py),
держит Service.scan_lock — сканер под тем же замком делает весь обход, значит, середину
переезда («файл уже под новым именем, журнал ещё под старым») он не видит и за новую
запись её не примет. Сами функции выполняются в событийном цикле, без await: там же живут
обработчики и ручной запуск, поэтому между проверкой «запись не в очереди» и переездом
в очередь её никто не поставит и в работу не возьмёт.

Удаление — перенос в ту же корзину, что и у папок (.trash/<метка>/), со сведениями для
возврата. Расходы в журнале событий остаются: деньги потрачены, удалена запись или нет.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from . import folders, ledger, marks, media, pipeline, upload, users

ERROR_SUFFIX = ".error.json"


class RecordError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _parent(rel: str) -> str:
    return rel.rpartition("/")[0]


def _known(service, file: str) -> dict:
    """Запись из журнала, с которой сейчас можно что-то делать."""
    record = service.ledger.get(file)
    if record is None:
        raise RecordError(404, "файла нет в журнале обработки")
    busy = [i.path for i in service.queue] + list(service.pending) + [j["file"] for j in service.running]
    if file in busy:
        raise RecordError(409, "запись сейчас в очереди или обрабатывается — дождитесь окончания")
    return record


def _swap(name: str, old: str, new: str) -> str:
    """Имя файла результата под новым stem: «<stem>.txt», «<stem>.names.json» и так далее."""
    return new + name[len(old):] if name == old or name.startswith(old + ".") else name


def _relocate(cfg, service, file: str, target: str, also=None) -> str:
    record = _known(service, file)
    if target == file:
        return file
    folder, name = _parent(target), PurePosixPath(target).name
    stem, new_stem = pipeline.stem_of(cfg.inbox, file, record), Path(name).stem
    old_out = cfg.outbox / _parent(file) / stem
    new_out = cfg.outbox / folder / new_stem
    # Занято не только точное имя: «запись.mp3» рядом с «запись.mp4» — две записи с одним
    # названием, и папку результатов они поделили бы между собой (см. pipeline.stem_for).
    here = cfg.inbox / folder         # при переименовании её может и не быть: исходники убрали
    twins = [p for p in (here.iterdir() if here.is_dir() else ())
             if p.is_file() and p.suffix.lower() in media.MEDIA_EXTS
             and p.stem.lower() == new_stem.lower() and p != cfg.inbox / file]
    if (twins or (cfg.inbox / target).exists() or service.ledger.get(target) is not None
            or (new_out.exists() and new_out != old_out)):
        raise RecordError(409, "запись с таким именем в этой папке уже есть")

    moved = dict(record, file=target, stem=new_stem)
    for field, value in record.items():
        if not isinstance(value, str):
            continue
        if value.startswith(str(old_out)):                    # output и другие полные пути
            moved[field] = str(new_out) + value[len(str(old_out)):]
        elif field in ("transcript", "summary"):              # пути внутри папки результатов
            sub = PurePosixPath(value)
            moved[field] = (sub.parent / _swap(sub.name, stem, new_stem)).as_posix()

    renamed: list[tuple[Path, Path]] = []                     # что переехало на диске: откуда → куда

    def shift(old: Path, new: Path) -> None:
        old.rename(new)
        renamed.append((old, new))

    src = cfg.inbox / file
    try:
        if src.is_file():             # исходник могли убрать руками — результаты переедут и без него
            shift(src, cfg.inbox / target)
        if old_out.is_dir() and new_out != old_out:
            new_out.parent.mkdir(parents=True, exist_ok=True)
            shift(old_out, new_out)
        if new_stem != stem and new_out.is_dir():
            for p in [p for p in new_out.rglob("*") if p.is_file()]:
                if p.name != _swap(p.name, stem, new_stem):
                    shift(p, p.with_name(_swap(p.name, stem, new_stem)))
        with service.ledger.db.write():
            service.ledger.rekey(file, moved)                 # отметки поручений переезжают вместе со строкой
            if also:
                also(target)
    except BaseException:
        for old, new in reversed(renamed):
            new.rename(old)
        raise
    # маркер рядом с результатами — копия строки журнала: путь в нём тоже новый
    for suffix in (ledger.MARKER_SUFFIX, ERROR_SUFFIX):
        marker = new_out / f"{new_stem}{suffix}"
        if marker.is_file():
            marker.write_text(json.dumps(moved, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def rename(cfg, service, file: str, name: str, also=None) -> str:
    """Новое имя записи в той же папке; расширение остаётся прежним. Возвращает новый путь.
    also(новый путь) — что ещё записать в базу той же транзакцией: словарь записи, событие журнала."""
    ext = PurePosixPath(file).suffix
    name = name.strip()
    if ext and name.lower().endswith(ext.lower()):            # имя вписали вместе с расширением
        name = name[:-len(ext)].rstrip()
    try:
        upload._check_part(name + ext, "имя записи")
        upload._check_part(name, "имя записи")
    except upload.UploadError as e:
        raise RecordError(e.status, e.detail)
    folder = _parent(file)
    return _relocate(cfg, service, file, f"{folder}/{name}{ext}" if folder else name + ext, also)


def move(cfg, service, file: str, folder: str, also=None) -> str:
    """Запись под тем же именем в другой папке. Возвращает новый путь. also — как у rename."""
    folder = users.norm_folder(folder)
    hidden = any(part.startswith(".") for part in folder.split("/"))
    inside = cfg.outbox_inside_inbox
    if hidden or folder == users.HOME_ROOT or (inside is not None and Path(folder).is_relative_to(inside)):
        raise RecordError(400, "в эту папку записи не кладут")
    if not (cfg.inbox / folder).is_dir():
        raise RecordError(404, folders.NO_FOLDER)
    name = PurePosixPath(file).name
    return _relocate(cfg, service, file, f"{folder}/{name}" if folder else name, also)


def remove(cfg, service, file: str, by: str, also=None) -> dict:
    """Исходник и результаты уходят в корзину, строка — из журнала обработки, вместе с ней —
    отметки поручений. also(что убрано) — что ещё записать в базу той же транзакцией."""
    record = _known(service, file)
    stem = pipeline.stem_of(cfg.inbox, file, record)
    stamp = folders.trash_stamp(cfg)          # две записи удалили в одну секунду — папки корзины разные
    done = {"trash": f"{folders.TRASH}/{stamp}", "keep_days": folders.TRASH_DAYS}
    moved = []
    try:
        for src, root in ((cfg.inbox / file, cfg.inbox), (cfg.outbox / _parent(file) / stem, cfg.outbox)):
            if src.exists():
                bin_dir = root / folders.TRASH / stamp
                bin_dir.mkdir(parents=True)
                src.rename(bin_dir / src.name)
                moved.append((src, bin_dir / src.name))
        with service.ledger.db.write():
            noted = marks.Store(service.ledger.db).load(file)
            service.ledger.forget(file)
            if also:
                also(done)
    except BaseException:
        for place, binned in reversed(moved):
            binned.rename(place)
            folders._tidy(binned.parent)
        raise
    # что это было и откуда — рядом с результатами, чтобы запись можно было вернуть руками
    note = cfg.outbox / folders.TRASH / stamp
    note.mkdir(parents=True, exist_ok=True)
    (note / "удалено.json").write_text(json.dumps(
        {"file": file, "by": by, "at": stamp, "moved": [str(binned) for _, binned in moved],
         "records": [record], "marks": {file: noted} if noted else {}},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return done
