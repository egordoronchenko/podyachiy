"""Загрузка записи через страницу — частями, с продолжением после обрыва.

Браузер режет файл на части и шлёт их по очереди. Принятое копится во временном
файле внутри входной папки, и только целиком собранная запись переименованием
становится на своё место — сканер недогруженного файла не увидит. Если связь
оборвалась или вкладку закрыли, тот же файл, выбранный снова, продолжит
грузиться с принятого места: временный файл находится по папке, имени и размеру.

Программа (ИИ-клиент с curl) шлёт запись одним запросом, без частей: тело пишется
потоком в такой же временный файл и тем же переименованием встаёт на место. Продолжения
после обрыва у такой загрузки нет — оборванное стирается сразу, а если сервис упал
посреди приёма, временный файл уберёт общая чистка недогрузок.

Загрузка только добавляет новые файлы: существующую запись она не заменит.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import time
from pathlib import Path, PurePosixPath

from . import media

PARTS_DIR = ".uploads"          # сканер эти файлы пропускает по расширению .part
CHUNK_BYTES = 8 * 1024 * 1024   # размер части, который сервер просит у страницы
STALE_SEC = 7 * 86400           # брошенная недогрузка живёт неделю
FORBIDDEN = set('<>:"|?*\\/')   # входная папка обычно лежит на Windows
NAME_MAX = 200


class UploadError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _check_part(part: str, what: str) -> None:
    if (not part or part != part.strip() or part.startswith((".", "~$")) or part.endswith(".")
            or len(part) > NAME_MAX or any(c in FORBIDDEN or ord(c) < 32 for c in part)):
        raise UploadError(400, f"недопустимое {what}: {part!r}")


def target_rel(folder: str, name: str, outbox_inside: Path | None) -> str:
    """Путь записи относительно входной папки — или отказ с объяснением."""
    parts = [p for p in folder.replace("\\", "/").split("/") if p]
    for p in parts:
        _check_part(p, "имя папки")
    _check_part(name, "имя файла")
    if Path(name).suffix.lower() not in media.MEDIA_EXTS:
        raise UploadError(400, "это не запись: принимаются " + ", ".join(
            e.lstrip(".") for e in sorted(media.MEDIA_EXTS)))
    rel = PurePosixPath(*parts, name).as_posix()
    if outbox_inside is not None and Path(rel).is_relative_to(outbox_inside):
        raise UploadError(400, "это папка результатов — записи сюда не кладут")
    return rel


def folder_rel(folder: str, outbox_inside: Path | None) -> str:
    """Путь новой папки относительно входной — или отказ с объяснением."""
    parts = [p for p in folder.replace("\\", "/").split("/") if p]
    if not parts:
        raise UploadError(400, "у папки должно быть имя")
    for p in parts:
        _check_part(p, "имя папки")
    rel = PurePosixPath(*parts).as_posix()
    if outbox_inside is not None and Path(rel).is_relative_to(outbox_inside):
        raise UploadError(400, "это папка результатов — записи сюда не кладут")
    return rel


def folder_arg(folder: str, outbox_inside: Path | None) -> str:
    """Папка из запроса: пусто — корень записей, иначе — проверка folder_rel. Одна на все пути папок,
    что приходят снаружи: «..», скрытые и служебные имена и папка результатов не проходят — иначе
    путь вывел бы из открытой человеку папки или за пределы входной."""
    if not folder.replace("\\", "/").strip("/ "):
        return ""
    return folder_rel(folder, outbox_inside)


def part_path(inbox: Path, rel: str, size: int) -> Path:
    key = hashlib.sha1(f"{rel}\0{size}".encode("utf-8")).hexdigest()[:20]
    return inbox / PARTS_DIR / f"{key}.part"


def writable(inbox: Path) -> bool:
    """Входную папку часто монтируют только для чтения — тогда загрузки нет."""
    try:
        (inbox / PARTS_DIR).mkdir(exist_ok=True)
        probe = inbox / PARTS_DIR / ".probe"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def sweep(inbox: Path, now: float | None = None) -> list[str]:
    """Убирает недогрузки, к которым неделю никто не возвращался."""
    now = time.time() if now is None else now
    removed = []
    try:
        parts = list((inbox / PARTS_DIR).glob("*.part"))
    except OSError:
        return removed
    for p in parts:
        try:
            if now - p.stat().st_mtime > STALE_SEC:
                p.unlink()
                removed.append(p.name)
        except OSError:
            continue
    return removed


def received(inbox: Path, rel: str, size: int, max_bytes: int) -> int:
    """Сколько байт этой записи уже принято. Заодно все проверки до начала загрузки."""
    if size <= 0:
        raise UploadError(400, "файл пустой")
    if size > max_bytes:
        raise UploadError(413, f"файл больше предела {max_bytes / 2**30:.0f} ГБ")
    if (inbox / rel).exists():
        raise UploadError(409, "запись с таким именем в этой папке уже есть")
    part = part_path(inbox, rel, size)
    have = part.stat().st_size if part.is_file() else 0
    free = shutil.disk_usage(inbox).free
    if free < size - have:
        raise UploadError(507, f"на диске не хватает места: свободно {free / 2**30:.1f} ГБ")
    return have


def append(inbox: Path, rel: str, size: int, offset: int, data: bytes) -> tuple[int, bool]:
    """Дописывает часть. Возвращает, сколько принято всего и собрана ли запись.

    Часть принимается только встык к уже принятому: при расхождении страница
    получает отказ, спрашивает настоящее место и продолжает с него.
    """
    target = inbox / rel
    if target.exists():
        raise UploadError(409, "запись с таким именем в этой папке уже есть")
    part = part_path(inbox, rel, size)
    have = part.stat().st_size if part.is_file() else 0
    if offset != have:
        raise UploadError(409, f"часть не встык: принято {have}, пришло с {offset}")
    if not data or have + len(data) > size:
        raise UploadError(400, "часть не подходит по размеру")
    part.parent.mkdir(exist_ok=True)
    with open(part, "ab") as f:
        f.write(data)
    have += len(data)
    if have < size:
        return have, False
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(part, target)     # в пределах одной папки-тома: запись появляется сразу целой
    return have, True


def cancel(inbox: Path, rel: str, size: int) -> None:
    part_path(inbox, rel, size).unlink(missing_ok=True)


# ---------- загрузка одним запросом ----------

def stream_start(inbox: Path, rel: str, expected: int | None, max_bytes: int) -> Path:
    """Проверки до приёма тела и временный файл для него. expected — размер из заголовка
    Content-Length, если клиент его назвал."""
    if (inbox / rel).exists():
        raise UploadError(409, "запись с таким именем в этой папке уже есть")
    if expected is not None:
        if expected > max_bytes:
            raise UploadError(413, f"файл больше предела {max_bytes / 2**30:.0f} ГБ")
        free = shutil.disk_usage(inbox).free
        if free < expected:
            raise UploadError(507, f"на диске не хватает места: свободно {free / 2**30:.1f} ГБ")
    (inbox / PARTS_DIR).mkdir(exist_ok=True)
    return inbox / PARTS_DIR / f"stream-{secrets.token_hex(10)}.part"


def stream_finish(inbox: Path, rel: str, part: Path) -> int:
    """Принятое целиком тело становится записью. Отдаёт размер."""
    size = part.stat().st_size
    if size == 0:
        raise UploadError(400, "файл пустой")
    target = inbox / rel
    if target.exists():
        raise UploadError(409, "запись с таким именем в этой папке уже есть")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(part, target)
    return size
