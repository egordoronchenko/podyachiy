"""Шаблоны протокола, задания модели и словарь."""
from __future__ import annotations

import asyncio
import datetime
import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import templates, users
from .access import _os_fault, admin_guard, jot, page_admin, viewer
from .branding import _body
from .core import cfg, log, options
from .lifecycle import DEFAULT_PROMPTS

router = APIRouter()


# ---------- шаблоны протокола, задания модели и словарь ----------
# Оба режима. Файлы лежат в папке prompts и правятся ещё и в обход страницы, поэтому
# страница каждый раз читает сами файлы. Читать может администратор сервиса и его ключ
# «с управлением»; менять — только сам администратор на странице: шаблон определяет, что
# модель напишет в каждом протоколе. Старые протоколы от правки не меняются — меняются те, что соберут после неё.

def _default_text(file: str) -> str | None:
    """Стандартный текст из образа (prompts_default); None — у этого файла стандартного нет."""
    return templates.read(DEFAULT_PROMPTS / file)


def _template_item(item: str, meta: dict, notes: dict) -> dict:
    file = meta["file"]
    path = cfg.prompts_dir / file
    text, default = templates.read(path), _default_text(file)
    kind = templates.kind_of(item)
    out = {"id": item, "kind": kind, "title": meta["title"], "when": meta.get("when", ""), "file": file,
           "custom": bool(meta.get("custom")), "actions": bool(meta.get("actions")),
           "has_default": default is not None, "exists": text is not None,
           "chars": len(text or ""), "backups": templates.backups(cfg.prompts_dir, file),
           # какие куски общих правил подключает файл (у самого общего файла и словаря — никаких)
           "includes": templates.includes(text or "") if kind not in ("common", "glossary") else [],
           "state": "missing", "changed_at": "", "by": "", "outside": False, "problem": "", "warnings": []}
    if text is None:
        out["problem"] = "файла нет в папке шаблонов" + (" — верните стандартный" if default is not None else "")
        return out
    mine = templates.digest(text)
    out["state"] = ("custom" if default is None else
                    "standard" if mine == templates.digest(default) else "changed")
    out["changed_at"] = datetime.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    note = notes.get(item)
    if note and note.get("digest") == mine:
        out["by"], out["changed_at"] = note.get("by", ""), note.get("at") or out["changed_at"]
    elif out["state"] != "standard":
        out["outside"] = True                 # текст не тот, что сохраняли на странице: правили файлом
    try:
        out["warnings"] = templates.check_file(cfg.prompts_dir, kind, text)
    except templates.TemplateError as e:
        out["problem"] = str(e)
    return out


def _templates_view() -> dict:
    listed, problem = templates.types_listed(cfg.prompts_dir), templates.read_custom(cfg.prompts_dir)[1]
    notes = options.template_notes()
    return {"items": [_template_item(k, m, notes) for k, m in {**listed, **templates.SERVICE}.items()],
            "problem": problem, "writable": os.access(cfg.prompts_dir, os.W_OK),
            "limits": {"chars": templates.MAX_CHARS, "upload_bytes": templates.UPLOAD_MAX_BYTES,
                       "keep": templates.KEEP, "title": templates.TITLE_MAX, "when": templates.WHEN_MAX}}


def _template_file(item: str) -> str:
    try:
        return templates.file_of(cfg.prompts_dir, item)
    except templates.TemplateError as e:
        raise HTTPException(404, str(e))


@router.get("/api/admin/templates", dependencies=[Depends(admin_guard)])
async def admin_templates():
    """Шаблоны, задания и словарь: что лежит в файлах сейчас, чем отличается от стандартного,
    кто и когда менял, какие есть прошлые версии."""
    return await asyncio.to_thread(_templates_view)


def _template_text(item: str, version: str) -> str:
    file = _template_file(item)
    if version == "current":
        text = templates.read(cfg.prompts_dir / file)
    elif version == "default":
        text = _default_text(file)
    else:
        try:
            text = templates.backup_text(cfg.prompts_dir, file, version)
        except templates.TemplateError as e:
            raise HTTPException(404, str(e))
    if text is None:
        raise HTTPException(404, "стандартного текста у этого шаблона нет: это свой тип" if version == "default"
                            else "файла нет в папке шаблонов")
    return text


@router.get("/api/admin/templates/text", dependencies=[Depends(admin_guard)])
async def admin_template_text(id: str, version: str = "current", download: bool = False):
    """Текст шаблона: version=current — как в файле сейчас, default — стандартный из образа,
    иначе — метка прошлой версии. download — отдать файлом."""
    text = await asyncio.to_thread(_template_text, id, version)
    if not download:
        return {"id": id, "version": version, "text": text}
    name = _template_file(id)
    return Response(text, media_type="text/plain; charset=utf-8", headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"attachment; filename=\"{name}\""})


def _template_store(item: str, text: str, who: users.Access, how: str) -> dict:
    file = _template_file(item)
    try:
        warnings = templates.check_file(cfg.prompts_dir, templates.kind_of(item), text)
        changed = templates.save(cfg.prompts_dir, file, text)
    except templates.TemplateError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise _os_fault("не удалось записать файл в папку шаблонов", e)
    saved = templates.read(cfg.prompts_dir / file) or ""
    options.note_template(item, who.login, templates.digest(saved))
    if changed:
        log(f"шаблоны: {file} изменён ({who.login}, {HOW[how]}), знаков {len(saved)}"
            + (f"; предупреждений {len(warnings)}" if warnings else ""))
        jot("template_save", who, item=item, file=file, how=how, chars=len(saved), warnings=len(warnings))
    return {"ok": True, "changed": changed, "warnings": warnings, **_templates_view()}


HOW = {"page": "правка на странице", "file": "загрузка файла", "default": "возврат стандартного",
       "backup": "возврат прошлой версии"}


class TemplateSave(BaseModel):
    id: str
    text: str


@router.post("/api/admin/templates/save", dependencies=[Depends(page_admin)])
async def admin_template_save(req: TemplateSave, who: users.Access = Depends(viewer)):
    """Текст из поля правки. Обязательного нет — отказ с объяснением; желательного нет —
    сохраняется с предупреждениями. Прежний текст остаётся в прошлых версиях."""
    return await asyncio.to_thread(_template_store, req.id, req.text, who, "page")


@router.put("/api/admin/templates/upload", dependencies=[Depends(page_admin)])
async def admin_template_upload(request: Request, id: str, who: users.Access = Depends(viewer)):
    """Шаблон файлом (.md или .txt в UTF-8): тело запроса — сам файл."""
    _template_file(id)
    data = await _body(request, templates.UPLOAD_MAX_BYTES,
                       f"файл больше {templates.UPLOAD_MAX_BYTES // 1024} КБ — шаблон такого размера сервис не принимает")
    try:
        text = templates.decode(data)
    except templates.TemplateError as e:
        raise HTTPException(400, str(e))
    return await asyncio.to_thread(_template_store, id, text, who, "file")


class TemplateRestore(BaseModel):
    id: str
    version: str          # default — стандартный из образа; иначе метка прошлой версии


@router.post("/api/admin/templates/restore", dependencies=[Depends(page_admin)])
async def admin_template_restore(req: TemplateRestore, who: users.Access = Depends(viewer)):
    """Вернуть стандартный текст или одну из прошлых версий. Текущий текст при этом сам
    уходит в прошлые версии — возврат тоже можно отменить."""
    if req.version == "current":
        raise HTTPException(400, "нужна версия: default или метка прошлой версии")
    text = await asyncio.to_thread(_template_text, req.id, req.version)
    return await asyncio.to_thread(_template_store, req.id, text, who,
                                   "default" if req.version == "default" else "backup")


class TemplateType(BaseModel):
    key: str = ""             # пусто — сервис составит из названия
    title: str
    when: str
    actions: bool = False     # выписывать поручения отдельным шагом перед протоколом, как у совещания
    new: bool = True
    base: str = ""            # с какого шаблона взять текст для нового типа
    text: str | None = None   # или сам текст


def _type_store(req: TemplateType, who: users.Access) -> dict:
    try:
        if not req.new:
            templates.change_type(cfg.prompts_dir, req.key, req.title, req.when, req.actions)
            log(f"шаблоны: описание своего типа «{req.key}» изменено ({who.login})")
            jot("type_change", who, item=req.key, title=" ".join(req.title.split()))
            return {"ok": True, "key": req.key, "warnings": [], **_templates_view()}
        text = req.text if req.text is not None else _template_text(req.base or "meeting", "current")
        warnings = templates.check_file(cfg.prompts_dir, "type", text)
        key = templates.add_type(cfg.prompts_dir, req.key, req.title, req.when, text, req.actions)
    except templates.TemplateError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise _os_fault("не удалось записать файл в папку шаблонов", e)
    options.note_template(key, who.login, templates.digest(templates.read(cfg.prompts_dir / f"{key}.md") or ""))
    log(f"шаблоны: добавлен свой тип «{key}» — {' '.join(req.title.split())} ({who.login})")
    jot("type_create", who, item=key, title=" ".join(req.title.split()), base=req.base if req.text is None else "")
    return {"ok": True, "key": key, "warnings": warnings, **_templates_view()}


@router.post("/api/admin/templates/type", dependencies=[Depends(page_admin)])
async def admin_template_type(req: TemplateType, who: users.Access = Depends(viewer)):
    """Свой тип записи: ключ, название, «когда применять» и текст шаблона. С этого момента
    разбор записи выбирает и из него, а ручной запуск показывает его в списке шаблонов."""
    return await asyncio.to_thread(_type_store, req, who)


class TemplateTypeDelete(BaseModel):
    key: str


def _type_delete(key: str, who: users.Access) -> dict:
    title = templates.types_listed(cfg.prompts_dir).get(key, {}).get("title", "")
    try:
        templates.remove_type(cfg.prompts_dir, key)
    except templates.TemplateError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise _os_fault("не удалось изменить папку шаблонов", e)
    options.note_template(key, who.login, None)
    log(f"шаблоны: свой тип «{key}» удалён ({who.login}); текст шаблона — в папке {templates.HISTORY_DIR}")
    jot("type_delete", who, item=key, title=title)
    return {"ok": True, **_templates_view()}


@router.post("/api/admin/templates/type/delete", dependencies=[Depends(page_admin)])
async def admin_template_type_delete(req: TemplateTypeDelete, who: users.Access = Depends(viewer)):
    """Убрать свой тип. Записи, обработанные по нему, остаются как есть; при пересборке такой
    записи шаблон заново выбирает разбор из оставшихся типов. Стандартные типы не удаляются."""
    return await asyncio.to_thread(_type_delete, req.key, who)
