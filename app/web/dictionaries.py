"""Словарь папки и записи."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import dictionary, folders, names, pipeline, templates, users
from . import core
from .access import NO_REBUILD, NO_RECORD, _record, jot, viewer
from .browse import _dir, _uploaded
from .common import _parent_of, _rebuild_state
from .core import cfg, log, people, words

router = APIRouter()


# ---------- словарь папки и записи ----------
# Участники и сокращения — на папку и на отдельную запись (dictionary.py). Читает тот, кому
# папка открыта; правит её хозяин — как подписи говорящих. Ключу доступа открыто в тех же
# правах. Пересборка протокола с новым словарём платная и идёт под правом загрузки.

NO_DICT_EDIT = "словарь правит владелец папки или её администратор"


def _dict_folder(path: str, who: users.Access) -> str:
    """Папка, у которой может быть словарь. Закрытая человеку неотличима от несуществующей; путь с «..»,
    скрытой или служебной папкой — 400, как везде (_dir)."""
    folder = _dir(path)
    if not folder:
        raise HTTPException(400, "у корня записей своего словаря нет: общий словарь сервиса — в настройках, "
                                 "его правит администратор")
    if not who.sees(folder) or not ((cfg.inbox / folder).is_dir() or (cfg.outbox / folder).is_dir()):
        raise HTTPException(404, folders.NO_FOLDER)
    return folder


def _dict_record(file: str, who: users.Access) -> dict | None:
    """Запись, у которой может быть словарь: строка журнала обработки, а у только что загруженной
    записи, до которой сервис ещё не дошёл, — None. Словарь, заданный до обработки, учтётся
    уже в ней, в том числе в распознавании."""
    assert core.service is not None
    if core.service.ledger.get(file) is not None:
        return _record(file, who)
    if not (_uploaded(file) and who.sees(_parent_of(file))):
        raise HTTPException(404, NO_RECORD)                 # тот же отказ, что у чужой записи из журнала
    return None


def _changed_by(login: str) -> str:
    who = people.access(login) if login and not cfg.personal else None
    return who.name if who else ""


def _dict_view(who: users.Access, folder: str = "", file: str = "", record: dict | None = None) -> dict:
    """Словарь папки или записи для страницы и программы: свои строки (people, terms) и то, что
    приходит с папок выше (inherited — у каждой строки path, откуда она). common — действует ли
    ещё и общий словарь сервиса."""
    home = _parent_of(file) if file else folder
    own = words.get(dictionary.RECORDS if file else dictionary.FOLDERS, file or folder)
    view = {"scope": dictionary.RECORD if file else dictionary.FOLDER, "path": file or folder,
            "people": own["people"], "terms": own["terms"],
            "changed_at": own["at"], "changed_by": _changed_by(own["by"]),
            "inherited": dictionary.merge(words.layers(folder=home if file else _parent_of(folder))),
            "common": bool(templates.load_glossary(cfg.prompts_dir)),
            "can_edit": who.owns(home),
            "limits": {"people": dictionary.PEOPLE_MAX, "terms": dictionary.TERMS_MAX}}
    if file:
        # пересобрать протокол с новым словарём — хозяину с правом загрузки и только у записи,
        # которая уже распознана; у ещё не обработанной словарь учтётся сам
        ready = bool(record) and record.get("status") in ("ok", "no_summary")
        view.update({"processed": ready,
                     "can_rebuild": ready and who.owns(home) and who.uploads(home)})
        if record:
            stem = pipeline.stem_of(cfg.inbox, file, record)
            view.update(_rebuild_state(file, cfg.outbox / Path(file).parent / stem, stem))
    return view


class DictionarySave(BaseModel):
    path: str = ""                          # папка — либо
    file: str = ""                          # запись
    # текст (строка на человека или термин), список строк или список объектов; None — не менять.
    # Вид проверяет dictionary.parse_*: отказ приходит словами, а не перечнем полей
    people: Any = None
    terms: Any = None
    add: bool = False                       # дополнить словарь, а не заменить его целиком
    rebuild: bool = False                   # пересобрать протокол записи с новым словарём


def _edited_since(file: str, record: dict, moment: float) -> bool:
    """Правили ли после этого момента то, из чего собирается протокол записи: подписи говорящих,
    словарь записи или словари её папки и папок выше."""
    stem = pipeline.stem_of(cfg.inbox, file, record)
    if names.changed(cfg.outbox / Path(file).parent / stem, stem) > moment:
        return True
    return any(words.get(scope, path)["changed"] > moment for scope, path in
               [(dictionary.RECORDS, file)] + [(dictionary.FOLDERS, f) for f in dictionary.parents(_parent_of(file))])


REBUILD_QUEUED, REBUILD_ALREADY = "queued", "already"


def _queue_rebuild(file: str, record: dict, who: users.Access) -> str:
    """Протокол записи заново по готовой расшифровке — с нынешними подписями и словарём.
    Пересборка платная, поэтому на один набор правок она одна, сколько бы раз её ни просили.

    Уже ждёт в очереди — подписи и словарь прочитает сама, когда до неё дойдёт: вторая не нужна.
    Запись идёт прямо сейчас — прогон мог прочитать старые подписи и словарь, поэтому ещё один
    следом: он встанет в очередь и дождётся конца текущего (Service._take). Но только если с
    начала прогона что-то правили: иначе повторная просьба (двойное нажатие, повтор запроса)
    оплатила бы тот же протокол дважды. Отдаёт, что произошло: поставлена или уже была."""
    assert core.service is not None
    if any(i.path == file for i in core.service.queue):
        return REBUILD_ALREADY
    job = next((j for j in list(core.service.running) if j["file"] == file), None)
    if job and not _edited_since(file, record, job.get("began", 0)):
        return REBUILD_ALREADY
    template = None if record.get("template_auto", True) else record.get("template")
    if template is not None and template not in templates.types(cfg.prompts_dir):
        # шаблон задавали вручную, а этот свой тип с тех пор удалили — тип выберет разбор записи
        log(f"пересборка {file}: типа «{template}» больше нет — шаблон выберет разбор записи")
        template = None
    core.service.enqueue_manual(file, template, False, who.login, "rebuild")
    return REBUILD_QUEUED


async def _dict_save(req: DictionarySave, who: users.Access) -> dict:
    """Правка словаря папки или записи и, по просьбе, протокол записи заново с ним.

    Отказ в пересборке — раньше правки: запрос «сохранить и пересобрать» не выполняется
    наполовину. Распознавание при пересборке не повторяется: подсказки терминов дойдут до него
    только при полном прогоне записи заново — его запускает администратор сервиса."""
    if bool(req.path) == bool(req.file):
        raise HTTPException(400, "словарь — либо папки, либо записи: укажите что-то одно")
    record = _dict_record(req.file, who) if req.file else None
    folder = "" if req.file else _dict_folder(req.path, who)
    home = _parent_of(req.file) if req.file else folder
    if not who.owns(home):
        raise HTTPException(403, NO_DICT_EDIT)
    if req.rebuild:
        if not req.file:
            raise HTTPException(400, "пересобирается протокол записи, а не папки: укажите запись")
        if not who.uploads(home):
            raise HTTPException(403, NO_REBUILD)
        if not record or record.get("status") not in ("ok", "no_summary"):
            raise HTTPException(409, "у записи ещё нет расшифровки — пересобирать нечего; словарь учтётся "
                                     "при обработке")
    if req.people is None and req.terms is None and not req.rebuild:
        raise HTTPException(400, "нечего менять: нужны участники, сокращения или пересборка протокола")

    if req.people is not None or req.terms is not None:
        scope, path = (dictionary.RECORDS, req.file) if req.file else (dictionary.FOLDERS, folder)
        old = words.get(scope, path)
        try:
            fresh_people = old["people"] if req.people is None else dictionary.parse_people(req.people)
            fresh_terms = old["terms"] if req.terms is None else dictionary.parse_terms(req.terms)
            if req.add:
                fresh_people = dictionary.joined(old["people"], fresh_people, "name", dictionary.PEOPLE_MAX)
                fresh_terms = dictionary.joined(old["terms"], fresh_terms, "term", dictionary.TERMS_MAX)
        except dictionary.DictionaryError as e:
            raise HTTPException(400, str(e))
        await asyncio.to_thread(words.put, scope, path, fresh_people, fresh_terms, who.login)
        log(f"словарь {'записи' if req.file else 'папки'} «{path}»: участников {len(fresh_people)}, "
            f"сокращений {len(fresh_terms)} ({who.login}" + (f", ключ «{who.key}»" if who.key else "") + ")")
        # в журнал действий — кто, где и сколько строк; самих имён и слов там нет
        jot("dictionary", who, **({"file": req.file} if req.file else {"folder": folder}),
            people=len(fresh_people), terms=len(fresh_terms))
    rebuilt = {"rebuild": _queue_rebuild(req.file, record, who)} if req.rebuild else {}
    return {"ok": True, **_dict_view(who, folder, req.file, record), **rebuilt}


def _dict_read(path: str, file: str, who: users.Access) -> dict:
    if bool(path) == bool(file):
        raise HTTPException(400, "словарь — либо папки, либо записи: укажите что-то одно")
    if file:
        return _dict_view(who, file=file, record=_dict_record(file, who))
    return _dict_view(who, folder=_dict_folder(path, who))


@router.get("/api/dictionary")
def dictionary_read(path: str = "", file: str = "", who: users.Access = Depends(viewer)):
    """Словарь папки (path) или записи (file) — окну на странице."""
    return _dict_read(path, file, who)


@router.post("/api/dictionary")
async def dictionary_save(req: DictionarySave, who: users.Access = Depends(viewer)):
    return await _dict_save(req, who)


class DictionaryBody(BaseModel):
    people: Any = None
    terms: Any = None


@router.get("/api/v1/dictionary")
def v1_dictionary(dir: str = "", file: str = "", who: users.Access = Depends(viewer)):
    """То же для ИИ-клиентов и программ: словарь папки (dir) или записи (file)."""
    return _dict_read(dir, file, who)


@router.put("/api/v1/dictionary")
async def v1_dictionary_save(body: DictionaryBody = DictionaryBody(), dir: str = "", file: str = "", add: bool = False,
                             rebuild: bool = False, who: users.Access = Depends(viewer)):
    """Словарь папки или записи целиком (или дополнение — add=true); тело — JSON с people и terms.
    rebuild=true — пересобрать протокол записи с новым словарём: вместе с правкой или отдельным
    запросом без тела. Права — те же, что у страницы."""
    return await _dict_save(DictionarySave(path=dir, file=file, people=body.people, terms=body.terms,
                                           add=add, rebuild=rebuild), who)
