"""Навык для ИИ-клиентов."""
from __future__ import annotations

import io
import zipfile

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from .api_v1 import _markdown, _skill_changes, _skill_text, _skill_version
from .core import SKILL_NAME

router = APIRouter()


# ---------- навык для ИИ-клиентов ----------
# Отдаётся без входа: в нём нет ничего, кроме адреса этой установки и описания запросов.
# Ключ человек вписывает сам в файл у себя в домашней папке.

@router.get("/skill")
def skill_install(request: Request):
    """Инструкция по установке навыка — текст для модели: его выполняет сам ИИ-клиент."""
    return PlainTextResponse(_skill_text("INSTALL.md", request))


@router.get("/skill/SKILL.md")
def skill_file(request: Request):
    return _markdown(_skill_text("SKILL.md", request))


@router.get("/skill/CHANGELOG.md")
def skill_changes(request: Request, since: int = 0):
    """Что нового в навыке после версии since — коротко, чтобы клиент сказал это человеку одной строкой."""
    fresh = [text for version, text in _skill_changes() if version > since]
    text = f"# Навык «Подьячий»: версия {_skill_version()}\n\n" + ("\n".join(fresh) if fresh else "Новее ничего нет.\n")
    return _markdown(text)


@router.get(f"/skill/{SKILL_NAME}.zip")
def skill_zip(request: Request):
    """Навык архивом — для ручной установки: внутри папка podyachiy с файлом SKILL.md."""
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{SKILL_NAME}/SKILL.md", _skill_text("SKILL.md", request))
    return Response(packed.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{SKILL_NAME}.zip"'})
