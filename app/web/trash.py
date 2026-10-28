"""Корзина: что удалено и возврат."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import folders, records, users
from . import core
from .access import _os_fault, jot, person
from .common import _parent_of
from .core import cfg, log, people, words
from .record_actions import scan_paused

router = APIRouter()


# ---------- корзина: что удалено и возврат ----------
# Удалённое лежит в корзине TRASH_DAYS дней (folders.py). Вернуть запись может тот, кто мог её удалить, —
# хозяин её папки; папку — администратор сервиса. Как и удаление, это делает сам человек на странице.
# Подтверждения нет: возврат обратим — вернувшееся снова удаляют. Обработки при возврате нет: результаты
# возвращаются с диска, строки журнала обработки — из описи.

def _restorable(who: users.Access, item: dict) -> bool:
    return who.admin if item["kind"] == "folder" else who.owns(_parent_of(item["path"]))


@router.get("/api/trash")
async def trash_list(who: users.Access = Depends(person)):
    """Что человек может вернуть из корзины: записи его папок, а администратору сервиса — всё."""
    found = await asyncio.to_thread(folders.binned, cfg)
    return {"items": [item for item in found if _restorable(who, item)], "keep_days": folders.TRASH_DAYS}


class TrashRestore(BaseModel):
    id: str


@router.post("/api/trash/restore")
async def trash_restore(req: TrashRestore, who: users.Access = Depends(person)):
    """Запись или папка из корзины — на прежнее место, со всем, что удаление убрало из базы."""
    assert core.service is not None
    if not core.upload_ready:
        raise HTTPException(503, "входная папка доступна только для чтения")
    trash = f"{folders.TRASH}/{req.id}"
    async with scan_paused():
        try:
            note = folders.read_note(cfg, req.id)
            item = folders.entry(req.id, note)
            if not _restorable(who, item):
                raise HTTPException(404, folders.NOT_IN_TRASH)      # чужое в корзине неотличимо от отсутствующего
            kept = note.get("kept") or {}

            def also(note: dict) -> None:
                words.put_back(kept.get("dictionary") or [])
                where = {"folder": item["path"], "records": item["records"]} if item["kind"] == "folder" \
                    else {"file": item["path"]}
                jot(f"{item['kind']}_restore", who, **where, trash=trash, uploads=kept.get("uploads") or {})

            if item["kind"] == "folder":
                folders.restore(cfg, core.service, people, req.id, also=also)
            else:
                records.restore(cfg, core.service, req.id, also=also)
        except (folders.FolderError, records.RecordError) as e:
            raise HTTPException(e.status, e.detail)
        except OSError as e:
            raise _os_fault("не удалось вернуть из корзины", e)
    log(f"{'папка' if item['kind'] == 'folder' else 'запись'} возвращена из корзины: {item['path']} ({who.login})")
    return {"ok": True, "kind": item["kind"], "path": item["path"], "records": item["records"]}
