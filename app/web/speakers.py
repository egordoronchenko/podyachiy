"""Подпись говорящих."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import names, pipeline, users
from . import core
from .access import NO_REBUILD, _record, jot, viewer
from .common import _parent_of, _rebuild_state
from .core import cfg, log, words
from .dictionaries import _queue_rebuild

router = APIRouter()


class NamesRequest(BaseModel):
    file: str
    # номер части → {метка → ФИО}; пустая строка снимает ручную подпись
    parts: dict[str, dict[str, str]] = {}
    # время начала реплики (как в names.line_key) → ФИО этой одной реплики
    lines: dict[str, str] = {}
    # пересобрать протокол с подписями: правки отдельных строк копятся
    # без пересборки, иначе каждый клик стоил бы протокола
    rebuild: bool = False


def _voices(file: str, who: users.Access) -> tuple[dict, Path, str, dict]:
    """Запись с говорящими: строка журнала, папка результата, имя и нынешние подписи."""
    record = _record(file, who)
    stem = pipeline.stem_of(cfg.inbox, file, record)
    base = cfg.outbox / Path(file).parent / stem
    current = names.view(base, stem)
    if not current["lines"]:
        raise HTTPException(409, "у записи нет расшифровки с говорящими")
    return record, base, stem, current


async def _sign(req: NamesRequest, who: users.Access) -> tuple[Path, str, dict]:
    """Подписи говорящих вручную и, по просьбе, протокол заново с этими ФИО.

    Расшифровка и разбор при пересборке переиспользуются — платить
    приходится только за протокол. Править может хозяин папки: владелец личной,
    локальный администратор общей, администратор сервиса. Тот, кому папка только
    открыта, читает, но не правит. Пересборка платная, поэтому хозяину личной папки
    она открыта только с правом загрузки в неё. Ключу доступа это открыто в тех же правах.
    Отдаёт папку результата и имя записи в ней — по ним вызывающий собирает ответ, — кого из
    подписанных добавили в словарь папки ({"dictionary_added": [ФИО]}, words.welcome) и, если
    просили пересборку, что с ней стало: {"rebuild": "queued"} или {"rebuild": "already"}.
    """
    assert core.service is not None
    _record(req.file, who)              # чужая запись — 404, раньше любого другого ответа
    if not who.owns(_parent_of(req.file)):
        raise HTTPException(403, "подписывать ФИО может владелец папки или её администратор")
    if req.rebuild and not who.uploads(_parent_of(req.file)):
        # отказ раньше правки подписей: запрос «подписать и пересобрать» не выполняется наполовину
        raise HTTPException(403, NO_REBUILD)
    record, base, stem, current = _voices(req.file, who)
    in_part = {str(p["index"]): {s["label"] for s in p["speakers"]} for p in current["parts"]}
    for part, labels in req.parts.items():
        wrong = sorted(set(labels) - in_part.get(part, set()))
        if part not in in_part or wrong:
            raise HTTPException(400, f"в части {part} нет меток: {', '.join(wrong) or 'такой части нет'}")
    if set(req.lines) - {ln["key"] for ln in current["lines"]}:
        raise HTTPException(400, "такой реплики в расшифровке нет")

    signed = names.load(base, stem)
    for part, labels in req.parts.items():
        bucket = signed["parts"].setdefault(part, {})
        for label, name in labels.items():
            if names.clean(name):
                bucket[label] = names.clean(name)
            else:
                bucket.pop(label, None)
    for key, name in req.lines.items():
        if names.clean(name):
            signed["lines"][key] = names.clean(name)
        else:
            signed["lines"].pop(key, None)
    signed["parts"] = {k: v for k, v in signed["parts"].items() if v}
    added: list[str] = []
    if req.parts or req.lines:
        names.save(base, stem, signed)
        # подписанное ФИО — подтверждённое имя: в словарь папки, чтобы следующие записи папки его
        # уже знали. Право то же, что у подписи, — словарь папки правит её хозяин (NO_DICT_EDIT)
        given = [names.clean(n) for labels in req.parts.values() for n in labels.values()]
        added = await asyncio.to_thread(words.welcome, _parent_of(req.file),
                                        given + [names.clean(n) for n in req.lines.values()], who.login)
        log(f"подписи говорящих: {req.file} — по частям "
            f"{sum(len(v) for v in signed['parts'].values())}, по строкам {len(signed['lines'])}"
            + (f"; в словарь папки «{_parent_of(req.file)}» добавлено ФИО: {len(added)}" if added else "")
            + (f" ({who.login}, ключ «{who.key}»)" if who.key else ""))
        # в журнал действий — сколько ФИО ушло в словарь, самих имён там нет
        jot("names", who, file=req.file, dictionary=len(added))

    return base, stem, {"dictionary_added": added,
                        **({"rebuild": _queue_rebuild(req.file, record, who)} if req.rebuild else {})}


@router.post("/api/admin/names")
async def admin_names(req: NamesRequest, who: users.Access = Depends(viewer)):
    """Подписи говорящих со страницы записи."""
    base, stem, rebuilt = await _sign(req, who)
    return {"ok": True, **names.view(base, stem), **_rebuild_state(req.file, base, stem), **rebuilt}


def _speakers(file: str, base: Path, stem: str, who: users.Access) -> dict:
    """Говорящие записи для модели и программы: по частям — метка, нынешняя подпись, догадка
    разбора, доля речи и несколько реплик, по которым человека можно узнать."""
    seen = names.speakers(base, stem)
    return {"file": file, "can_edit": who.owns(_parent_of(file)),
            "can_rebuild": who.owns(_parent_of(file)) and who.uploads(_parent_of(file)),
            "parts": seen["parts"], "known": seen["known"],
            **_rebuild_state(file, base, stem)}


@router.get("/api/v1/speakers")
def v1_speakers(file: str, who: users.Access = Depends(viewer)):
    """Кто говорит в записи — чтобы подписать метки ФИО."""
    _, base, stem, _ = _voices(file, who)
    return _speakers(file, base, stem, who)


@router.post("/api/v1/speakers")
async def v1_speakers_sign(file: str, part: int | None = None, label: str = "", line: str = "",
                           name: str = "", rebuild: bool = False, who: users.Access = Depends(viewer)):
    """Подпись одной метки в части записи (part, label, name) или одной реплики (line, name);
    пустое name снимает подпись. rebuild=true — пересобрать протокол с подписями: можно
    вместе с подписью или отдельным запросом. Права и логика — те же, что у страницы."""
    if bool(label) != (part is not None) or (label and line):
        raise HTTPException(400, "подпись метки — part и label вместе; подпись реплики — line; не оба сразу")
    if not label and not line and not rebuild:
        raise HTTPException(400, "нечего менять: нужны part и label, или line, или rebuild=true")
    base, stem, rebuilt = await _sign(NamesRequest(file=file, parts={str(part): {label: name}} if label else {},
                                             lines={line: name} if line else {}, rebuild=rebuild), who)
    return {"ok": True, **_speakers(file, base, stem, who), **rebuilt}
