"""Сведения о сервисе: название, версия, автор и как с ним связаться.

Единственное место, где записаны контакты автора и откуда берётся версия: раздел
«О сервисе», страница лицензии, справка и ответы API читают их отсюда.

Версия — файл VERSION в корне репозитория; при сборке он попадает в образ рядом с пакетом
app. Дата сборки и короткий идентификатор коммита приходят при сборке образа аргументами
(BUILD_DATE, GIT_COMMIT → переменные окружения): к git из контейнера сервис не обращается,
и без аргументов эти два поля просто пусты.
"""
from __future__ import annotations

import os
from pathlib import Path

NAME = "Подьячий"
TAGLINE = "Записи совещаний превращает в расшифровки с говорящими, протоколы и поручения."
AUTHOR = "Доронченко Егор"
EMAIL = "mazedastra@gmail.com"
TELEGRAM = "@EgorDoronchenko"
TELEGRAM_URL = "https://t.me/EgorDoronchenko"

ROOT = Path(__file__).resolve().parent.parent
# тексты лицензий, которые сервис отдаёт вошедшим: адрес → (файл в корне образа, тип содержимого).
# Перечень компонентов написан в Markdown, но отдаётся простым текстом: его браузер покажет, а не скачает.
DOCUMENTS = {"license": ("LICENSE", "text/plain"), "third-party": ("THIRD_PARTY.md", "text/plain")}


def version() -> str:
    """Версия сервиса из файла VERSION. Файла нет (код подложен в образ старой сборки) — пусто:
    страница так и скажет «не указана», выдумывать номер незачем."""
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build() -> dict:
    return {"date": (os.environ.get("APP_BUILD_DATE") or "").strip(),
            "commit": (os.environ.get("APP_COMMIT") or "").strip()[:12]}


def contacts() -> dict:
    return {"author": AUTHOR, "email": EMAIL, "telegram": TELEGRAM, "telegram_url": TELEGRAM_URL}


def document(name: str) -> tuple[str, str] | None:
    """Текст лицензии сервиса или перечня сторонних компонентов и его тип; None — файла в образе нет."""
    file, kind = DOCUMENTS[name]
    try:
        return (ROOT / file).read_text(encoding="utf-8"), kind
    except OSError:
        return None
