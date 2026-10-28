"""Ключи доступа."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import keys, users
from .access import admin_guard, jot, page_admin, person, viewer
from .core import cfg, log, people

router = APIRouter()


# ---------- ключи доступа ----------

NO_KEYS_HERE = "сервис работает без входа — ключи доступа здесь не нужны"


def _keys_on() -> None:
    if cfg.personal:
        raise HTTPException(404, NO_KEYS_HERE)


class KeyRequest(BaseModel):
    name: str
    days: int = 365         # 0 — без срока (только у обычного ключа)
    manage: bool = False    # ключ «с управлением» — только администратору сервиса


def _own_keys(who: users.Access) -> dict:
    return {"keys": people.keys(who.login), "can_manage": who.admin,
            "days": {"plain": keys.DAYS[keys.PLAIN], "manage": keys.DAYS[keys.MANAGE]}}


@router.get("/api/keys")
def keys_list(who: users.Access = Depends(person)):
    """Ключи человека: имя, вид, начало ключа, когда выпущен, когда им пользовались, когда истечёт."""
    _keys_on()
    return _own_keys(who)


@router.post("/api/keys")
async def key_issue(req: KeyRequest, who: users.Access = Depends(person)):
    """Новый ключ. Сама строка есть только в этом ответе — второй раз сервис её не покажет."""
    _keys_on()
    try:
        token, key = await asyncio.to_thread(people.issue_key, who.login, req.name, req.days, req.manage)
    except users.UserError as e:
        raise HTTPException(403 if req.manage and not who.admin else 400, str(e))
    log(f"ключ доступа выпущен: «{key['name']}»" + (", с управлением" if req.manage else "") + f" ({who.login})")
    jot("key_issue", who, key=key["name"], key_kind=key["kind"], expires=key["expires"])
    return {"key": token, "issued": key, **_own_keys(who)}


class KeyRevoke(BaseModel):
    id: str


@router.post("/api/keys/revoke")
async def key_revoke(req: KeyRevoke, who: users.Access = Depends(person)):
    _keys_on()
    try:
        key = await asyncio.to_thread(people.revoke_key, req.id, who.login)
    except users.UserError as e:
        raise HTTPException(404, str(e))
    log(f"ключ доступа отозван: «{key['name']}» ({who.login})")
    jot("key_revoke", who, key=key["name"], key_kind=key["kind"])
    return {"ok": True, **_own_keys(who)}


@router.get("/api/admin/keys", dependencies=[Depends(admin_guard)])
async def admin_keys():
    """Все ключи доступа — администратору сервиса: чей, имя, вид, создан, последний раз
    использован, истекает; mark — expired (срок вышел) или stale (давно не использовался).
    Значений ключей нет: сервис их не хранит."""
    return {"keys": people.all_keys(), "stale_days": keys.STALE_DAYS}


@router.post("/api/admin/keys/revoke", dependencies=[Depends(page_admin)])
async def admin_key_revoke(req: KeyRevoke, who: users.Access = Depends(viewer)):
    """Администратор сервиса отзывает любой ключ — только сам, на странице."""
    try:
        key = await asyncio.to_thread(people.revoke_key, req.id)
    except users.UserError as e:
        raise HTTPException(404, str(e))
    log(f"ключ доступа отозван администратором: «{key['name']}» пользователя {key['owner']} ({who.login})")
    jot("key_revoke", who, key=key["name"], key_kind=key["kind"], owner=key["owner"])
    return {"ok": True, "keys": people.all_keys()}
