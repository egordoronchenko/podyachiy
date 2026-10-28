"""Поручения: отметки и сроки, раздел «Поручения прошлого совещания», признак «серия совещаний»."""
from __future__ import annotations

import datetime
import os
import re
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import external, marks, mindmap, pipeline, registry, users
from ..ledger import REMOVED
from . import core
from .access import _record, jot, viewer
from .browse import _dir
from .common import _in_work, _parent_of, _protocol
from .core import base, cfg, log, noted, serial
from .dictionaries import _dict_folder

router = APIRouter()


class TaskMark(BaseModel):
    file: str
    key: str
    done: bool | None = None        # не указано — отметку не трогать
    comment: str = ""
    due: str | None = None          # не указано — срок не трогать; пусто — вернуть срок из протокола


async def _mark(file: str, key: str, done: bool | None, comment: str, due: str | None, who: users.Access) -> dict:
    """Отметка «выполнено» и срок датой у поручения: ставит и снимает хозяин записи. Чужая
    запись — 404. done и due, которые не указаны (None), не трогаются; пустой due снимает срок.

    Идёт прямо в событийном цикле, без потока и без await, — как действия с записью:
    переезд записи не вклинится между чтением протокола и записью отметки. Отметка и строка
    о ней в журнале действий записываются одной транзакцией."""
    record = _record(file, who)
    if not who.owns(_parent_of(file)):
        raise HTTPException(403, "отметить поручение и задать ему срок может владелец папки или её администратор")
    comment = " ".join(comment.split())
    if len(comment) > marks.COMMENT_MAX:
        raise HTTPException(400, f"комментарий длиннее {marks.COMMENT_MAX} знаков")
    if due is not None:
        due = due.strip()
    if due:
        try:
            if not re.fullmatch(r"\d{4}-\d\d-\d\d", due):
                raise ValueError(due)
            due = datetime.date.fromisoformat(due).isoformat()
        except ValueError:
            raise HTTPException(400, "срок — датой ГГГГ-ММ-ДД, например 2026-10-20; пустой срок возвращает срок из протокола")
    protocol = _protocol(file, record)
    if not protocol:
        raise HTTPException(404, "у записи нет протокола — отмечать нечего")
    tasks = external.tasks(protocol)
    try:
        with base.write():
            if done is not None:
                task, changed = noted.put(file, tasks, key, done, who.login, comment)
                if changed:
                    jot("task_done" if done else "task_reopen", who, file=file,
                        task=task["task"][:120], owner=task["who"] or "")
            if due is not None:
                task, changed, was = noted.put_due(file, tasks, key, due or None, who.login)
                if changed:
                    # было → стало: дата, а где её нет — слова протокола
                    jot("task_due", who, file=file, task=task["task"][:120], owner=task["who"] or "",
                        was=was or task["due_text"] or "", now=task["due"] or task["due_text"] or "")
    except KeyError:
        raise HTTPException(404, "такого поручения в протоколе записи нет: возьмите key из её поручений заново")
    return {"ok": True, "task": {**task, **registry.state(task, external.day_of(record), registry.today())}}


# Отметка и срок открыты и ключу доступа — в рамках тех же прав; в журнале действий они с пометкой.
@router.post("/api/record/task")
async def record_task(req: TaskMark, who: users.Access = Depends(viewer)):
    if req.done is None and req.due is None:
        raise HTTPException(400, "укажите done или due")
    return await _mark(req.file, req.key, req.done, req.comment, req.due, who)


@router.post("/api/v1/task")
async def v1_task(file: str, key: str, done: bool | None = None, comment: str = "", due: str | None = None,
                  who: users.Access = Depends(viewer)):
    """То же для ИИ-клиентов и программ: параметры в адресе. key — из поручений записи или реестра.
    due=ГГГГ-ММ-ДД задаёт срок, пустой due возвращает срок из протокола. Без done и без due
    поручение отмечается выполненным."""
    return await _mark(file, key, True if done is None and due is None else done, comment, due, who)


# ---------- раздел «Поручения прошлого совещания» и признак «серия совещаний» ----------
# Раздел сервис дописывает в конец протокола сам, кодом (registry.block), — если папка записи
# отмечена как серия совещаний (series.py). У одной записи его убирают и возвращают кнопкой у
# раздела: без пересборки протокола и без вызова модели. Права — как у подписи говорящих: хозяин
# папки; ключу доступа — в тех же правах. Признак серии виден всем, кому папка открыта (browse,
# /api/v1/tree), а включает и выключает его хозяин папки.

NO_PREV_EDIT = "убрать и вернуть раздел может владелец папки или её администратор"
NO_SERIES_EDIT = "серию совещаний у папки включает и выключает владелец папки или её администратор"
ROOT_NO_SERIES = ("у корня записей серии нет: в корень кладут разрозненные записи, и поручения прошлого "
                  "совещания там не собираются")


def _prev_view(file: str, record: dict, text: str) -> dict:
    """Раздел у записи: present — он есть в протоколе; removed — убран человеком, и пересборка его
    не вернёт; series — папка записи — серия совещаний: только тогда раздел можно вернуть."""
    return {"present": registry.has_block(text), "removed": bool(record.get(REMOVED)),
            "series": serial.on(_parent_of(file))}


async def _prev_set(file: str, on: bool, who: users.Access) -> dict:
    """Убрать раздел из протокола записи (on=False) или вернуть его (on=True).

    Убранный раздел запоминается у записи (ledger.REMOVED): пересборка протокола после подписей
    или словаря его не вернёт. Возвращается он тем же кодом, что собирает его при обработке, — из
    протокола предыдущей записи папки и отметок у её поручений, какими они стали сейчас; модель
    не вызывается. Вместе с протоколом заново собирается его карта; выгрузки DOCX и PDF, поиск и
    реестр поручений читают протокол с диска и правку видят сами.

    Время изменения файла протокола остаётся прежним: правка раздела — не пересборка, а по этому
    времени страница сверяет, не новее ли протокола подписи и словарь (_rebuild_state).

    Идёт прямо в событийном цикле, без await, — как отметка поручения: пересборка не встанет
    между проверкой «запись не в работе» и записью файла. Признак записи, строка в журнале
    действий и файл протокола записываются одной транзакцией: не записался файл — признак прежний."""
    assert core.service is not None
    record = _record(file, who)
    folder = _parent_of(file)
    if not who.owns(folder):
        raise HTTPException(403, NO_PREV_EDIT)
    path = pipeline.protocol_path(cfg, file, record)
    if record.get("status") != "ok" or not path.is_file():
        raise HTTPException(404, "у записи нет протокола — раздела в нём нет")
    if any(i.path == file for i in core.service.queue) or _in_work(file):
        raise HTTPException(409, "протокол записи сейчас собирается — дождитесь окончания")
    if on and not folder:
        raise HTTPException(409, "запись лежит в корне: в корне поручения прошлого совещания не собираются")
    if on and not serial.on(folder):
        raise HTTPException(409, "папка записи не отмечена как серия совещаний: включите у папки «Серию совещаний»")
    text = path.read_text(encoding="utf-8")
    stamp = path.stat()
    with base.write():
        flag = bool(record.get(REMOVED)) == on              # признак не тот, что нужен
        if flag:
            core.service.ledger.amend(file, **{REMOVED: None if on else True})
        if not on:
            fresh = registry.without_block(text)
        elif registry.has_block(text):
            fresh = text
        else:
            # признак уже снят: previous_tasks смотрит на него в строке журнала
            tail = pipeline.previous_tasks(
                cfg, file, core.service.ledger.history(limit=len(core.service.ledger.records)), record.get("record_date") or "",
                record.get("finished_at") or "", lambda line: log(f"[{PurePosixPath(file).name}] {line}"), noted,
                series=True)
            fresh = registry.with_block(text, tail) if tail else text
        if flag or fresh != text:
            jot("previous_tasks", who, file=file, on=on)
        if fresh != text:
            path.write_text(fresh, encoding="utf-8")
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            mindmap.write_for(path, noted.load(file))
    if flag or fresh != text:
        log(f"раздел «{registry.BLOCK_TITLE}»: {file} — "
            + ("убран вручную" if not on else "возвращён" if fresh != text else
               "снова собирается, но незакрытых поручений у предыдущей записи папки нет")
            + f" ({who.login}" + (f", ключ «{who.key}»" if who.key else "") + ")")
    return {"ok": True, "file": file, **_prev_view(file, core.service.ledger.get(file), fresh)}


class PreviousTasks(BaseModel):
    file: str
    on: bool                    # false — убрать раздел, true — вернуть


@router.post("/api/record/previous_tasks")
async def record_previous_tasks(req: PreviousTasks, who: users.Access = Depends(viewer)):
    """Кнопка у раздела «Поручения прошлого совещания» в окне записи."""
    return await _prev_set(req.file, req.on, who)


@router.post("/api/v1/previous_tasks")
async def v1_previous_tasks(file: str, on: bool, who: users.Access = Depends(viewer)):
    """То же для ИИ-клиентов и программ: on=false — убрать раздел, on=true — вернуть."""
    return await _prev_set(file, on, who)


async def _series_set(path: str, on: bool, who: users.Access) -> dict:
    """Включить или выключить у папки признак «серия совещаний». Действует со следующей обработки
    и пересборки: готовые протоколы не меняются — раздел в них убирают кнопкой у раздела."""
    if not _dir(path):
        raise HTTPException(400, ROOT_NO_SERIES)
    folder = _dict_folder(path, who)            # та же проверка, что у словаря: закрытая папка неотличима от несуществующей
    if not who.owns(folder):
        raise HTTPException(403, NO_SERIES_EDIT)
    with base.write():
        changed = serial.put(folder, on)
        if changed:
            jot("series", who, folder=folder, on=on)
    if changed:
        log(f"папка «{folder}»: серия совещаний {'включена' if on else 'выключена'} ({who.login}"
            + (f", ключ «{who.key}»" if who.key else "") + ")")
    return {"ok": True, "path": folder, "series": on}


class SeriesSet(BaseModel):
    dir: str
    on: bool


@router.post("/api/folder/series")
async def folder_series(req: SeriesSet, who: users.Access = Depends(viewer)):
    """Переключатель «Серия совещаний» в окне папки."""
    return await _series_set(req.dir, req.on, who)


@router.post("/api/v1/series")
async def v1_series(dir: str, on: bool, who: users.Access = Depends(viewer)):
    """То же для ИИ-клиентов и программ. Прочитать признак — поле series в /api/v1/tree."""
    return await _series_set(dir, on, who)
