"""Обработка: модель распознавания, ручной запуск, резервная копия из сервиса, очередь."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import backup, speech, templates, users
from ..watcher import hidden
from . import core
from .access import admin_guard, jot, viewer
from .common import _in_work
from .core import cfg, log, options, speech_model
from .lifecycle import _backup_start, _backups
from .pages import _speech_choice

router = APIRouter()


class RunRequest(BaseModel):
    file: str
    # пусто — шаблон выберет разбор записи, как при автоматическом запуске
    template: str | None = None
    force: bool = False
    # модель распознавания для этого запуска; пусто — та, что выбрана для новых записей
    model: str | None = None


class SpeechModelRequest(BaseModel):
    model: str | None = None        # None — вернуть к значению из .env


@router.post("/api/admin/speech-model", dependencies=[Depends(admin_guard)])
async def admin_speech_model(req: SpeechModelRequest, who: users.Access = Depends(viewer)):
    """Модель распознавания для новых записей. Действует со следующей записи, без перезапуска;
    готовые расшифровки не трогает — их переделывает только ручной запуск с перезаписью."""
    if req.model is not None and req.model not in speech.ENGINES:
        raise HTTPException(400, "такой модели распознавания сервис не знает: " + ", ".join(speech.ENGINES))
    was = speech_model()
    await asyncio.to_thread(options.set_speech_model, req.model, who.login)
    now = speech_model()
    log(f"модель распознавания для новых записей: {now} — "
        + (f"выбрал {who.login} на странице" if req.model else f"{who.login} вернул значение из .env")
        + (f" (была {was})" if was != now else ""))
    jot("speech_model", who, model=now, was=was, source="page" if req.model else "env")
    return {"ok": True, **_speech_choice()}


# Ручной запуск берёт то же, что «Обработка» предлагает в списке файлов (Service.inbox_files): медиафайл
# входной папки, не временный и не недокопированный, не в служебной папке — корзине (.trash) и
# недогрузках (.uploads): удалённое и недогруженное в платное распознавание не идут — и не в выходной
# папке, если она внутри входной. Файл, который ещё дописывается (сканер ждёт, пока он перестанет
# меняться), не запускается: обрывок ушёл бы в распознавание, а за него платят.
NOT_RUNNABLE = ("вручную запускаются записи из списка файлов входной папки: медиафайл не из корзины и не из "
                "недогрузок, не временный и не недокопированный")
STILL_WRITTEN = ("файл ещё дописывается — сервис возьмёт его в обработку сам, когда он перестанет меняться; "
                 "ручной запуск — после этого")


@router.post("/api/admin/run", dependencies=[Depends(admin_guard)])
async def admin_run(req: RunRequest, who: users.Access = Depends(viewer)):
    assert core.service is not None
    if req.template is not None and req.template not in templates.types(cfg.prompts_dir):
        raise HTTPException(400, "неизвестный шаблон")
    if req.model and req.model not in speech.ENGINES:
        raise HTTPException(400, "такой модели распознавания сервис не знает: " + ", ".join(speech.ENGINES))
    if req.model and not req.force:
        # без перезаписи готовая расшифровка переиспользуется, и модель ни на что не повлияла бы
        raise HTTPException(400, "модель распознавания выбирается только вместе с перезаписью: "
                                 "без неё берётся готовая расшифровка")
    target = (cfg.inbox / req.file).resolve()
    root = cfg.inbox.resolve()
    if root != target and root not in target.parents:
        raise HTTPException(400, "путь вне входной папки")
    if not target.is_file():
        raise HTTPException(404, "файл не найден")
    rel = target.relative_to(root).as_posix()
    if rel != req.file:
        # «Проекты/../Снабжение/x.mp4», «./», ссылка на другой файл: путь не тот, что показан в списке файлов
        raise HTTPException(400, NOT_RUNNABLE)
    if hidden(rel) or core.service._ignore_reason(target) or core.service._under_outbox(rel):
        raise HTTPException(400, NOT_RUNNABLE)
    # файл, который сканер отложил до решения администратора (копия готовой записи, похож на
    # недокопированный), запускают именно вручную — так сервис и пишет; копируется ещё — нет
    watch = core.service.pending.get(rel)
    if watch is not None and not (watch.cut or watch.held):
        raise HTTPException(409, STILL_WRITTEN)
    if any(i.path == rel for i in core.service.queue) or _in_work(rel):
        raise HTTPException(409, "этот файл уже в очереди или обрабатывается")
    core.service.enqueue_manual(rel, req.template, req.force, who.login, "manual", req.model or "")
    return {"ok": True, "queued": rel}


# ---------- резервная копия из сервиса ----------

class BackupPlanRequest(BaseModel):
    on: bool
    time: str
    every: str = "day"
    weekday: int = 0
    inbox: bool = True
    keep: int = backup.PLAN["keep"]


@router.post("/api/admin/backup/plan", dependencies=[Depends(admin_guard)])
async def admin_backup_plan(req: BackupPlanRequest, who: users.Access = Depends(viewer)):
    """Расписание резервной копии: включено ли, время суток по часовому поясу сервиса, каждый день или раз в
    неделю, с исходными записями ли, сколько копий держать. Те же «с записями» и «сколько держать» — у кнопки
    «Сделать копию сейчас»."""
    try:
        plan = backup.plan_of(req.on, req.time, req.every, req.weekday, req.inbox, req.keep)
    except backup.BackupError as e:
        raise HTTPException(400, str(e))
    await asyncio.to_thread(options.set_backup_plan, plan, who.login)
    log(f"расписание резервной копии: {backup.described(plan)} — сохранил {who.login}")
    jot("backup_plan", who, plan=plan)
    return {"ok": True, "backups": _backups()}


@router.post("/api/admin/backup/now", dependencies=[Depends(admin_guard)])
async def admin_backup_now(who: users.Access = Depends(viewer)):
    """Копия сейчас, в фоне: ответ — сразу, итог — в GET /api/admin/status (storage.backup, backups)."""
    _backup_start(backup.PAGE, who.login)
    return {"ok": True, "backups": _backups()}


# ---------- очередь: снять запись, отменить прогон ----------

class QueueRequest(BaseModel):
    file: str


NOT_QUEUED = "этой записи в очереди нет: её уже взяли в работу или сняли — обновите страницу"
NOT_RUNNING = "эта запись сейчас не обрабатывается: прогон уже закончился или ещё не начался — обновите страницу"


@router.post("/api/admin/queue/remove", dependencies=[Depends(admin_guard)])
async def admin_queue_remove(req: QueueRequest, who: users.Access = Depends(viewer)):
    """Снять запись из очереди, пока её прогон не начался. Новая запись и повтор после сбоя сами в
    очередь не вернутся (remembered), пока файл не изменится или запись не запустят вручную."""
    assert core.service is not None
    remembered = core.service.unqueue(req.file, who.login)
    if remembered is None:
        raise HTTPException(409, NOT_QUEUED)
    jot("queue_remove", who, file=req.file, remembered=remembered)
    return {"ok": True, "file": req.file, "remembered": remembered}


@router.post("/api/admin/queue/cancel", dependencies=[Depends(admin_guard)])
async def admin_queue_cancel(req: QueueRequest, who: users.Access = Depends(viewer)):
    """Отменить идущий прогон. Он остановится перед следующим платным запросом: идущий запрос поставщику
    доработает (за него могли уже списать деньги), ffmpeg — тоже. Оплаченное — в расходах и на диске,
    повтор возьмёт его готовым; исход — «отменено администратором», по расписанию не повторяется."""
    assert core.service is not None
    mark = core.service.cancel(req.file, who.login)
    if mark is None:
        raise HTTPException(409, NOT_RUNNING)
    jot("run_cancel", who, file=req.file)
    return {"ok": True, "file": req.file, "cancel": mark}
