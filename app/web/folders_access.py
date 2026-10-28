"""Права на папки и раздел «Папки и доступ»."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import folders, users
from . import core
from .access import _os_fault, admin_guard, jot, page_admin, steward, viewer
from .browse import _dir
from .common import _parent_of
from .core import cfg, log, people, trail, words
from .record_actions import scan_paused

router = APIRouter()


@router.get("/api/access")
def access_list(dir: str, who: users.Access = Depends(steward)):
    """Кому открыта папка — для администратора и для локального администратора этой папки."""
    folder = _dir(dir)
    if not who.admin and (not folder or not who.rules(folder)):
        raise HTTPException(403, "раздавать доступ к этой папке вы не можете")
    return {"path": folder, "can_manage": who.admin,
            "users": [u for u in people.holders(folder) if u["login"] != who.login]}


class AccessRequest(BaseModel):
    dir: str
    login: str
    read: str | None = None       # none | only | tree; не указано — не менять
    upload: str | None = None
    manage: str | None = None


@router.post("/api/access")
async def access_set(req: AccessRequest, who: users.Access = Depends(steward)):
    """Права человека на папку. Чтение и загрузку раздаёт и администратор папки,
    администрирование — только администратор сервиса: иначе администратор папки плодил бы
    себе равных."""
    folder = _dir(req.dir)
    if not who.admin and (not folder or not who.rules(folder)):
        raise HTTPException(403, "раздавать доступ к этой папке вы не можете")
    if req.manage is not None and not who.admin:
        raise HTTPException(403, "администраторов папок назначает администратор сервиса")
    try:
        await asyncio.to_thread(people.set_access, req.login, folder, req.read, req.manage, req.upload)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    log(f"доступ: «{folder or 'все папки'}» — {req.login}: "
        + ", ".join(f"{k} {v}" for k, v in (("чтение", req.read), ("загрузка", req.upload),
                                             ("администрирование", req.manage)) if v)
        + f" ({who.login})")
    jot("access", who, folder=folder, login=req.login, read=req.read, upload=req.upload, manage=req.manage)
    return {"path": folder, "can_manage": who.admin,
            "users": [u for u in people.holders(folder) if u["login"] != who.login]}


FOLDER_DEPTH = 8      # сколько уровней вложенности видно в разделе «Папки и доступ»


def _known_folders() -> list[str]:
    """Папки, которые можно открыть человеку: всё, где есть записи, и папки на диске
    до FOLDER_DEPTH уровней — входная папка может быть сетевой, и обходить её без предела нельзя."""
    assert core.service is not None
    found: set[str] = set()
    for key in list(core.service.ledger.records):
        parent = PurePosixPath(_parent_of(key)) if _parent_of(key) else None
        while parent is not None and parent.as_posix() != ".":
            found.add(parent.as_posix())
            parent = parent.parent
    for root, dirs, _ in os.walk(cfg.inbox):
        rel = Path(root).relative_to(cfg.inbox).as_posix()
        depth = 0 if rel == "." else rel.count("/") + 1
        # личные папки и служебные не раздают — в них и не заходим
        dirs[:] = [d for d in dirs if not d.startswith(".") and not (depth == 0 and d == users.HOME_ROOT)
                   and depth < FOLDER_DEPTH]
        found.update(d if rel == "." else f"{rel}/{d}" for d in dirs)
    for user in people.public():
        found.update(f for f in list(user["read"]) + list(user["upload"]) + list(user["manage"]) if f)
    home = users.HOME_ROOT
    return sorted(f for f in found if f != home and not f.startswith(home + "/")
                  and not core.service._under_outbox(f))


@router.get("/api/admin/folders", dependencies=[Depends(admin_guard)])
async def admin_folders():
    """Папки для раздела «Папки и доступ». Общие — кто администратор, кому открыта, сколько
    записей. Личные — чья и сколько записей: раздавать их нельзя, но видеть администратор должен."""
    assert core.service is not None
    folders = await asyncio.to_thread(_known_folders)
    parents = [_parent_of(key) for key in list(core.service.ledger.records)]
    everyone = people.public()

    def records(f: str) -> int:
        return sum(1 for p in parents if p == f or p.startswith(f + "/"))

    def named(kind: str, f: str) -> list[str]:
        return [u["name"] + ("" if u[kind][f] == users.TREE else " · только эта папка")
                for u in everyone if f in u[kind]]

    return {"folders": [{
        "path": f,
        "records": records(f),
        "managers": named("manage", f),
        "viewers": named("read", f),
        "uploaders": named("upload", f),
    } for f in folders],
        "everything": named("read", ""),
        "personal": [{"path": u["home"], "login": u["login"], "name": u["name"], "records": records(u["home"]),
                      "upload": u["admin"] or u["home_upload"]}
                     for u in everyone],
        "can_create": core.upload_ready}


class FolderRename(BaseModel):
    dir: str
    name: str


@router.post("/api/admin/folders/rename", dependencies=[Depends(admin_guard)])
async def admin_folder_rename(req: FolderRename, who: users.Access = Depends(viewer)):
    """Новое имя папки: вместе с ней переезжают результаты, журнал обработки и права."""
    assert core.service is not None
    if not core.upload_ready:
        raise HTTPException(503, "входная папка доступна только для чтения")
    old = _dir(req.dir)

    def also(new: str) -> None:
        words.move_folder(old, new)
        jot("folder_rename", who, folder=old, to=new)

    async with scan_paused():
        try:
            new = folders.rename(cfg, core.service, people, old, req.name, also=also)
        except folders.FolderError as e:
            raise HTTPException(e.status, e.detail)
        except OSError as e:
            raise _os_fault("не удалось переименовать", e)
    if new != old:
        log(f"папка переименована: {old} → {new} ({who.login})")
    return {"ok": True, "path": new}


class FolderDelete(BaseModel):
    dir: str


@router.post("/api/admin/folders/delete", dependencies=[Depends(page_admin)])
async def admin_folder_delete(req: FolderDelete, who: users.Access = Depends(viewer)):
    """Папка со всем содержимым уходит в корзину на сервере; через месяц стирается."""
    assert core.service is not None
    if not core.upload_ready:
        raise HTTPException(503, "входная папка доступна только для чтения")
    folder = _dir(req.dir)

    def also(done: dict) -> dict:
        # словари и авторы загрузок уходят из базы — в опись корзины, чтобы вернуться вместе с папкой
        kept = {"dictionary": words.taken(folder=folder), "uploads": trail.kept(folder=folder)}
        words.drop_folder(folder)
        jot("folder_delete", who, folder=folder, records=done["records"], trash=done["trash"])
        return kept

    async with scan_paused():
        try:
            done = folders.remove(cfg, core.service, people, folder, who.login, also=also)
        except folders.FolderError as e:
            raise HTTPException(e.status, e.detail)
        except OSError as e:
            raise _os_fault("не удалось удалить", e)
    log(f"папка удалена в корзину: {folder}, записей {done['records']}, "
        f"лежит в {done['trash']} ({who.login})")
    return {"ok": True, **done}
