"""Оформление корпоративного режима: логотип организации и её короткое название.

Администратор загружает картинку на странице; сервис проверяет её, пересобирает из точек
заново и хранит в папке состояния (state/brand) три своих файла PNG: логотип для страницы
входа, малый для меню и значок вкладки. Название организации и сведения о логотипе (метка
версии, кто и когда загрузил) лежат в базе сервиса, в настройках (db.py: settings). Исходный файл не хранится и не отдаётся: наружу
уходит только то, что сервис нарисовал сам, без метаданных и без чужих байтов.

Разбор картинок — Pillow. Отдельно ставить его не пришлось: он уже в образе, им пользуется
reportlab (выгрузка PDF). Свой разбор заголовков PNG, JPEG и WebP дал бы только размеры:
уменьшенные копии, сброс метаданных и проверку «файл целый» всё равно пришлось бы делать
библиотекой.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps

from . import db

DIR = "brand"
KEY = "brand"             # под этим ключом сведения об оформлении лежат в настройках базы
NAME_MAX = 40
MAX_BYTES = 2 * 1024 * 1024
MIN_SIDE, MAX_SIDE = 256, 4096
FORMATS = {"PNG": "PNG", "JPEG": "JPEG", "WEBP": "WebP"}
# файлы, которые сервис делает из загруженной картинки: имя → наибольшие ширина и высота
FILES = {"logo.png": (640, 512), "logo-small.png": (432, 108), "favicon.png": (64, 64)}
SVG_REFUSED = ("SVG не принимается: это не картинка из точек, а документ, в котором может быть сценарий, — "
               "он выполнился бы в браузере каждого, кто открыл сервис. Сохраните логотип как PNG")
REQUIREMENTS = {"formats": list(FORMATS.values()), "max_bytes": MAX_BYTES, "min_side": MIN_SIDE,
                "max_side": MAX_SIDE, "name_max": NAME_MAX}


class BrandError(ValueError):
    """Файл или название не приняты; текст — для администратора на странице."""


def sniff(data: bytes) -> str:
    """Формат по первым байтам файла: расширению и заявленному типу сервис не верит."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<svg") or (head.startswith((b"<?xml", b"<!doctype")) and b"<svg" in head):
        raise BrandError(SVG_REFUSED)
    if data.startswith((b"GIF87a", b"GIF89a")):
        raise BrandError("GIF не принимается: нужен PNG, JPEG или WebP. Сохраните логотип как PNG")
    raise BrandError("это не картинка PNG, JPEG или WebP. Сервис смотрит на содержимое файла, а не на "
                     "расширение: переименованный файл другого вида не подойдёт")


def read_logo(data: bytes) -> tuple[Image.Image, dict]:
    """Проверяет файл и отдаёт картинку и сведения о ней. Отказ — BrandError с объяснением."""
    if not data:
        raise BrandError("файл пустой")
    if len(data) > MAX_BYTES:
        raise BrandError(f"файл больше 2 МБ ({len(data) / 1048576:.1f} МБ) — уменьшите картинку")
    fmt = sniff(data)
    broken = BrandError("файл не читается как картинка: он повреждён или обрезан. Сохраните логотип "
                        "заново из графического редактора")
    try:
        img = Image.open(io.BytesIO(data), formats=[fmt])
        width, height = img.size                      # из заголовка, до разбора точек
    except Image.DecompressionBombError:
        raise BrandError(f"картинка слишком велика: сторона — не больше {MAX_SIDE} точек. Уменьшите её "
                         "в графическом редакторе")
    except Exception:                                 # Pillow бросает разное: OSError, SyntaxError, ValueError
        raise broken
    if min(width, height) < MIN_SIDE:
        raise BrandError(f"картинка слишком мала: {width}×{height} точек, а меньшая сторона нужна не меньше "
                         f"{MIN_SIDE}. Возьмите исходник логотипа покрупнее")
    if max(width, height) > MAX_SIDE:
        raise BrandError(f"картинка слишком велика: {width}×{height} точек, а сторона — не больше {MAX_SIDE}. "
                         "Уменьшите её в графическом редакторе")
    try:
        img.load()
        img = ImageOps.exif_transpose(img)            # снимок с телефона: поворот записан в метаданных
        opaque = img.mode not in ("RGBA", "LA", "PA") and "transparency" not in img.info
        img = img.convert("RGBA")
        if not opaque:
            opaque = img.getchannel("A").getextrema()[0] == 255
    except Exception:
        raise broken
    img.info = {}                                     # метаданные исходного файла дальше не идут
    width, height = img.size
    advice = []
    if max(width, height) / min(width, height) > 1.5:
        advice.append(f"Логотип не квадратный ({width}×{height}): в меню он впишется в высоту 36 точек, "
                      "и мелкие детали пропадут. Лучше квадратный знак без надписи.")
    if opaque:
        advice.append("У картинки непрозрачный фон: в тёмной теме вокруг логотипа будет виден прямоугольник. "
                      "Лучше PNG или WebP с прозрачным фоном.")
    return img, {"format": FORMATS[fmt], "width": width, "height": height, "bytes": len(data), "advice": advice}


def render(img: Image.Image, box: tuple[int, int], square: bool) -> bytes:
    """Картинка, вписанная в box, файлом PNG. square — значок вкладки: на прозрачном квадрате по центру."""
    small = img.copy()
    small.thumbnail(box, Image.LANCZOS)
    if square:
        canvas = Image.new("RGBA", box, (0, 0, 0, 0))
        canvas.paste(small, ((box[0] - small.width) // 2, (box[1] - small.height) // 2))
        small = canvas
    out = io.BytesIO()
    small.save(out, "PNG", optimize=True)
    return out.getvalue()


def clean_name(name: str) -> str:
    name = " ".join(str(name).split())
    if len(name) > NAME_MAX:
        raise BrandError(f"название организации — до {NAME_MAX} знаков, сейчас {len(name)}")
    if any(ch < " " or ch in "<>" for ch in name):
        raise BrandError("в названии организации не должно быть знаков < и >")
    return name


class Brand:
    """Что выбрано в оформлении. Читают страницы (каждая, включая страницу входа), пишет
    администратор — из разных потоков."""

    def __init__(self, base: db.Database, log: Callable[[str], None] = lambda s: None):
        self.db = base
        self.dir = base.dir / DIR
        self.log = log
        self._lock = threading.Lock()       # файлы логотипа меняются тройкой: две загрузки разом их бы перемешали
        # файлов логотипа на диске нет — до новой загрузки показываем стандартный
        self._lost = False
        self._lost = bool(self._data().get("logo")) and not all((self.dir / name).is_file() for name in FILES)
        if self._lost:
            log(f"ВНИМАНИЕ: файлов логотипа в {self.dir} нет — показываю стандартный логотип")

    def _data(self) -> dict:
        """Сведения об оформлении из базы: name, logo (метка версии), source — и кто и когда менял."""
        row = self.db.row("SELECT value, author, at FROM settings WHERE key = ?", KEY)
        data = {**json.loads(row["value"]), "by": row["author"], "at": row["at"]} if row else {}
        if self._lost:
            data.pop("logo", None)
        return data

    def view(self) -> dict:
        data = self._data()
        return {"name": data.get("name") or "", "logo": data.get("logo") or "",
                "by": data.get("by") or "", "at": data.get("at") or "",
                "source": dict(data.get("source") or {})}

    def name(self) -> str:
        return self._data().get("name") or ""

    def file(self, version: str, name: str) -> Path | None:
        """Файл логотипа по адресу с меткой версии; None — такой версии или файла нет."""
        if name not in FILES or not version or version != self._data().get("logo"):
            return None
        return self.dir / name

    def _save(self, data: dict, by: str) -> None:
        value = {k: v for k, v in data.items() if k not in ("by", "at")}
        with self.db.write() as con:
            con.execute("INSERT INTO settings (key, value, author, at) VALUES (?, ?, ?, ?) ON CONFLICT (key) "
                        "DO UPDATE SET value = excluded.value, author = excluded.author, at = excluded.at",
                        (KEY, json.dumps(value, ensure_ascii=False), by,
                         datetime.datetime.now().astimezone().isoformat(timespec="seconds")))

    def set_logo(self, data: bytes, by: str) -> dict:
        """Проверяет и ставит логотип. Отдаёт сведения об исходном файле и рекомендации."""
        img, info = read_logo(data)
        files = {name: render(img, box, square=name == "favicon.png") for name, box in FILES.items()}
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            for name, body in files.items():
                tmp = self.dir / (name + ".tmp")
                tmp.write_bytes(body)
                os.replace(tmp, self.dir / name)
            # метка версии стоит в адресе картинки: новый логотип — новый адрес, и браузеры
            # берут его сразу, хотя старый им разрешено хранить год
            self._lost = False
            self._save({**self._data(), "logo": secrets.token_hex(6),
                        "source": {k: info[k] for k in ("format", "width", "height", "bytes")}}, by)
        return info

    def set_name(self, name: str, by: str) -> str:
        name = clean_name(name)
        self._save({**self._data(), "name": name}, by)
        return name

    def reset(self, by: str) -> None:
        """Стандартное оформление: наш логотип и надпись, названия организации нет."""
        with self._lock:
            self._save({}, by)
            for name in FILES:
                (self.dir / name).unlink(missing_ok=True)
