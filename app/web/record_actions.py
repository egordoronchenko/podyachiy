"""Действия с записью: переименование, перенос, удаление."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import folders, records, users
from . import core
from .access import _os_fault, _record, jot, person, viewer
from .browse import _dir
from .common import _parent_of
from .core import cfg, log, trail, words

router = APIRouter()


# ---------- действия с записью ----------

@asynccontextmanager
async def scan_paused():
    """Замок сканера на время действия с записью или папкой.

    Действие идёт под замком и прямо в событийном цикле, без потока: так между
    проверкой «запись не в работе» и переездом файлов не вклинится ни обход входной
    папки, ни обработчик (подробно — в records.py). Замок берётся опросом, а не в потоке:
    обход сетевой папки может идти долго, и ждать его, остановив весь сервис, нельзя.
    """
    assert core.service is not None
    while not core.service.scan_lock.acquire(blocking=False):
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        core.service.scan_lock.release()


async def _record_action(action, file: str, who: users.Access, *args, also=None):
    """Переименование, перенос и удаление записи — её хозяину. Чужая запись — 404."""
    assert core.service is not None
    _record(file, who)
    if not who.owns(_parent_of(file)):
        raise HTTPException(403, "это может владелец папки или её администратор")
    if not core.upload_ready:
        raise HTTPException(503, "входная папка доступна только для чтения")
    async with scan_paused():
        try:
            return action(cfg, core.service, file, *args, also=also)
        except records.RecordError as e:
            raise HTTPException(e.status, e.detail)
        except OSError as e:
            raise _os_fault("не получилось", e)


class RecordRename(BaseModel):
    file: str
    name: str


async def _rename(file: str, name: str, who: users.Access) -> dict:
    """Новое имя записи: переезжают исходник, результаты, строка журнала, отметки и словарь.
    Обработки не будет."""
    def also(new: str) -> None:
        words.move_record(file, new)
        jot("record_rename", who, file=file, to=new)

    new = await _record_action(records.rename, file, who, name, also=also)
    if new != file:
        log(f"запись переименована: {file} → {new} ({who.login})")
    return {"ok": True, "file": new}


async def _move(file: str, dir: str, who: users.Access) -> dict:
    """Запись в другую папку — туда, где человек тоже хозяин. Обработки не будет."""
    def also(new: str) -> None:
        words.move_record(file, new)
        jot("record_move", who, file=file, to=new)

    _record(file, who)
    target = _dir(dir)
    if not who.sees(target):
        raise HTTPException(404, folders.NO_FOLDER)         # закрытая папка неотличима от несуществующей
    if not who.owns(target):
        raise HTTPException(403, "переносить можно в папку, где вы владелец или администратор")
    new = await _record_action(records.move, file, who, dir, also=also)
    if new != file:
        log(f"запись перенесена: {file} → {new} ({who.login})")
    return {"ok": True, "file": new}


# Переименование и перенос ключу доступа открыты, удаление — нет: его делает сам человек.
@router.post("/api/record/rename")
async def record_rename(req: RecordRename, who: users.Access = Depends(viewer)):
    return await _rename(req.file, req.name, who)


@router.post("/api/v1/rename")
async def v1_rename(file: str, name: str, who: users.Access = Depends(viewer)):
    """То же для ИИ-клиентов и программ: параметры в адресе, как у остальных /api/v1."""
    return await _rename(file, name, who)


class RecordMove(BaseModel):
    file: str
    dir: str


@router.post("/api/record/move")
async def record_move(req: RecordMove, who: users.Access = Depends(viewer)):
    return await _move(req.file, req.dir, who)


@router.post("/api/v1/move")
async def v1_move(file: str, dir: str, who: users.Access = Depends(viewer)):
    return await _move(file, dir, who)


class RecordDelete(BaseModel):
    file: str


@router.post("/api/record/delete")
async def record_delete(req: RecordDelete, who: users.Access = Depends(person)):
    """Запись с результатами уходит в корзину на сервере; через месяц стирается."""
    def also(done: dict) -> dict:
        # словарь записи и автор загрузки уходят из базы — в опись корзины, чтобы вернуться вместе с ней
        kept = {"dictionary": words.taken(file=req.file), "uploads": trail.kept(file=req.file)}
        words.drop_record(req.file)
        jot("record_delete", who, file=req.file, trash=done["trash"])
        return kept

    done = await _record_action(records.remove, req.file, who, who.login, also=also)
    log(f"запись удалена в корзину: {req.file}, лежит в {done['trash']} ({who.login})")
    return {"ok": True, **done}
