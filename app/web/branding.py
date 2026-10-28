"""Оформление: логотип и название организации."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import brand, users
from .access import _os_fault, admin_guard, jot, page_admin, viewer
from .core import cfg, log, look, state_kept
from .pages import _brand_public

router = APIRouter()


# ---------- оформление: логотип и название организации ----------
# Только корпоративный режим: в личном режиме раздела нет, и адреса отвечают 404.
# Посмотреть может администратор сервиса и его ключ «с управлением»; менять — только сам
# администратор на странице: логотип видят все, включая страницу входа.

def _brand_on() -> None:
    if cfg.personal:
        raise HTTPException(404, "в личном режиме оформление не меняется")


async def _body(request: Request, limit: int, too_big: str) -> bytes:
    """Тело запроса целиком, но не больше limit: лишнее не читается и в память не попадает."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise HTTPException(413, too_big)
    data = bytearray()
    async for piece in request.stream():
        data += piece
        if len(data) > limit:
            raise HTTPException(413, too_big)
    return bytes(data)


def _brand_view() -> dict:
    return {**look.view(),
            "public": _brand_public(), "requirements": brand.REQUIREMENTS, "svg": brand.SVG_REFUSED,
            "state_kept": state_kept}


@router.get("/api/admin/brand", dependencies=[Depends(admin_guard)])
def admin_brand():
    _brand_on()
    return _brand_view()


@router.put("/api/admin/brand/logo", dependencies=[Depends(page_admin)])
async def admin_brand_logo(request: Request, who: users.Access = Depends(viewer)):
    """Логотип: тело запроса — сам файл. Формат определяется по содержимому, размеры и
    целость проверяются, наружу отдаются только копии, собранные сервисом (brand.py)."""
    _brand_on()
    data = await _body(request, brand.MAX_BYTES, "файл больше 2 МБ — уменьшите картинку")
    try:
        info = await asyncio.to_thread(look.set_logo, data, who.login)
    except brand.BrandError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise _os_fault("не удалось записать логотип в папку состояния", e)
    log(f"оформление: логотип заменён ({who.login}) — {info['format']}, {info['width']}×{info['height']}, "
        f"{info['bytes'] / 1024:.0f} КБ")
    jot("brand_logo", who, format=info["format"], width=info["width"], height=info["height"], size=info["bytes"])
    return {"ok": True, "advice": info["advice"], **_brand_view()}


class BrandName(BaseModel):
    name: str = ""


@router.post("/api/admin/brand/name", dependencies=[Depends(page_admin)])
async def admin_brand_name(req: BrandName, who: users.Access = Depends(viewer)):
    """Короткое название организации: рядом с логотипом вместо надписи, в заголовке вкладки,
    в письмах и в колонтитуле скачиваемых файлов. Пусто — убрать."""
    _brand_on()
    was = look.name()
    try:
        name = await asyncio.to_thread(look.set_name, req.name, who.login)
    except brand.BrandError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise _os_fault("не удалось записать оформление в папку состояния", e)
    log(f"оформление: название организации — «{name}» ({who.login})" if name
        else f"оформление: название организации убрано ({who.login})")
    jot("brand_name", who, name=name, was=was)
    return {"ok": True, **_brand_view()}


@router.post("/api/admin/brand/reset", dependencies=[Depends(page_admin)])
async def admin_brand_reset(who: users.Access = Depends(viewer)):
    """Вернуть стандартное оформление: логотип и надпись сервиса, без названия организации."""
    _brand_on()
    try:
        await asyncio.to_thread(look.reset, who.login)
    except OSError as e:
        raise _os_fault("не удалось записать оформление в папку состояния", e)
    log(f"оформление: возвращено стандартное ({who.login})")
    jot("brand_reset", who)
    return {"ok": True, **_brand_view()}
