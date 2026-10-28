"""Обзор записей по папкам, новая папка и загрузка записи со страницы."""
from __future__ import annotations

import asyncio
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import folders, keys, media, templates, upload, users
from ..ledger import SOURCE_CHANGED, SOURCE_MISSING, SOURCE_STATE
from . import core
from .access import _no_upload, _os_fault, _upload_denied, jot, viewer
from .common import _parent_of
from .core import cfg, log, serial, upload_lock

router = APIRouter()


def _dir(value: str) -> str:
    """Папка из параметра запроса: пусто — корень записей. Проверка одна на все адреса — та же, что у
    новой папки (upload.folder_arg): права сверяются по началу пути, и «Проекты/..» иначе прошёл бы
    у читателя «Проектов» как его папка, а вёл бы к корню записей и выше. Негодный путь — 400."""
    try:
        return upload.folder_arg(value, cfg.outbox_inside_inbox)
    except upload.UploadError as e:
        raise HTTPException(e.status, e.detail)


def _uploaded(file: str) -> bool:
    """Лежит ли во входной папке файл записи, до которой сервис ещё не дошёл. Путь — той же проверкой,
    что при загрузке (upload.target_rel): с «..» и скрытыми частями он из открытой папки не выведет."""
    try:
        canonical = upload.target_rel(_parent_of(file), PurePosixPath(file).name, cfg.outbox_inside_inbox) == file
    except upload.UploadError:
        return False
    return canonical and (cfg.inbox / file).is_file()


# Текст сбоя обработки — каким его видит человек. В нём бывает ответ поставщика как есть, а в ответе —
# баланс счёта (при 402) и суммы, а их видит только тот, кому открыты расходы. Остальным — общий текст
# по исходу записи. «Нет звука» пишет сам сервис, денег в этом тексте нет — его видят все.
FAILED_HIDDEN = "обработать запись не удалось — причину видит администратор сервиса"
NO_SUMMARY_HIDDEN = "протокол собрать не удалось — причину видит администратор сервиса"


def _sees_costs(who: users.Access) -> bool:
    """Открыты ли человеку суммы и расходы: администратору сервиса — самому или его ключу «с управлением»
    (как у costs_guard)."""
    return who.admin and (not who.key or who.key_kind == keys.MANAGE)


def _public_error(rec: dict) -> str | None:
    """Текст сбоя, каким его видит тот, кому расходы не открыты: без ответа поставщика."""
    error = rec.get("error")
    if not error or rec.get("status") == "no_audio":
        return error
    return NO_SUMMARY_HIDDEN if rec.get("status") == "no_summary" else FAILED_HIDDEN


def _failure(rec: dict, who: users.Access) -> str | None:
    return rec.get("error") if _sees_costs(who) else _public_error(rec)


def _shown(rec: dict, who: users.Access) -> dict:
    """Строка журнала обработки с текстом сбоя, каким его видит этот человек."""
    return {**rec, "error": _failure(rec, who)} if rec.get("error") else rec


def _source(file: str, rec: dict) -> str | None:
    """Что с исходником записи: "missing" — его нет во входной папке (по последнему обходу), "changed" —
    изменился после обработки (докопировали, перезаписали), None — на месте и тот же."""
    assert core.service is not None
    if file in core.service.missing:
        return SOURCE_MISSING
    return SOURCE_CHANGED if rec.get(SOURCE_STATE) == SOURCE_CHANGED else None


FLAT_LIMIT = 300     # «все записи» — это недавние: глубже ищут поиском или идут в папку
HELD_CUT = "похоже, файл докопирован не до конца — обработка начнётся, когда он изменится"
HELD_DECISION = "в обработку сама не пойдёт — ждёт решения администратора сервиса"

# какие результаты имеет смысл открывать при таком исходе обработки
KINDS_BY_STATUS = {
    "ok": ["summary", "transcript", "srt"],
    "no_summary": ["transcript", "srt"],
    "no_audio": [],
    "error": [],
}


@router.get("/api/browse")
def browse(path: str = "", flat: bool = False, who: users.Access = Depends(viewer)):
    """Обход результатов по подпапкам — так же, как записи лежат во входной папке.

    Дерево строится по журналу обработки, а не обходом диска: журнал и так
    знает про каждую запись, а выходная папка может оказаться сетевой,
    и ходить по ней на каждый запрос страницы незачем.

    flat — «все записи»: в списке всё, что человеку открыто, из всех папок сразу.
    """
    assert core.service is not None
    rel = "" if flat else _dir(path)
    prefix = f"{rel}/" if rel else ""
    dirs: dict[str, dict] = {}
    files: list[dict] = []

    for key, rec in list(core.service.ledger.records.items()):
        parent = _parent_of(key)
        if not who.sees(parent):
            continue
        if parent == rel or flat:
            status = rec.get("status", "error")
            files.append({
                "file": key,
                "dir": parent,
                "name": PurePosixPath(key).name,
                "status": status,
                "kinds": KINDS_BY_STATUS.get(status, []),
                "finished_at": rec.get("finished_at"),
                "duration_sec": rec.get("duration_sec"),
                "audio_duration_sec": rec.get("audio_duration_sec"),
                "speakers": rec.get("speakers"),
                "speech_model": rec.get("speech_model"),
                "unlabeled": rec.get("unlabeled"),
                "speakers_unreliable": rec.get("speakers_unreliable"),
                "crowded_meeting": rec.get("crowded_meeting"),
                "template": rec.get("template"),
                "template_auto": rec.get("template_auto"),
                "title": rec.get("title"),
                "gist": rec.get("gist"),
                "record_date": rec.get("record_date"),
                "named_speakers": rec.get("named_speakers"),
                "type_unsure": rec.get("type_unsure"),
                "type_confidence": rec.get("type_confidence"),
                "type_reason": rec.get("type_reason"),
                "timestamps": rec.get("timestamps"),
                "timestamps_unknown": rec.get("timestamps_unknown"),
                "runs": rec.get("runs"),
                "error": _failure(rec, who),
                "source": _source(key, rec),
            })
        if parent == rel:
            continue
        if not parent.startswith(prefix):
            continue
        name = parent[len(prefix):].split("/", 1)[0]
        entry = dirs.setdefault(name, {"name": name, "path": prefix + name,
                                       "files": 0, "last": None})
        entry["files"] += 1
        finished = rec.get("finished_at")
        if finished and (entry["last"] is None or finished > entry["last"]):
            entry["last"] = finished

    files.sort(key=lambda f: f.get("finished_at") or "", reverse=True)
    if flat:
        del files[FLAT_LIMIT:]

    # Записи в работе тоже показываем в их папке: человек кладёт файл и хочет
    # видеть, что с ним происходит, а не пустой список до самого конца.
    # list() снимает копию: сканер меняет эти структуры из своего потока.
    running = [job for job in list(core.service.running) if who.sees(_parent_of(job["file"]))]
    in_work = {job["file"] for job in running}
    pending = []
    for path, watch in list(core.service.pending.items()):
        if not who.sees(_parent_of(path)):
            continue
        # сам в обработку не пойдёт: похож на недокопированный или ждёт решения администратора (копия готовой
        # записи) — человеку без подробностей: чья это копия, ему знать незачем
        held = (HELD_CUT if watch.cut else HELD_DECISION if watch.held else "")
        pending.append({"file": path, "name": PurePosixPath(path).name,
                        "dir": _parent_of(path), "state": "held" if held else "copying",
                        "size": watch.size, "quiet_sec": round(watch.quiet_sec()),
                        "stable_sec": cfg.stable_sec, **({"note": held} if held else {})})
    for item in list(core.service.queue):
        # пересборка, запрошенная посреди прогона, ждёт его конца: запись и так в списке
        if not who.sees(_parent_of(item.path)) or item.path in in_work:
            continue
        pending.append({"file": item.path, "name": PurePosixPath(item.path).name,
                        "dir": _parent_of(item.path), "state": "queued",
                        "template": item.template, "manual": item.manual})
    for job in running:
        pending.append({"file": job["file"], "name": PurePosixPath(job["file"]).name,
                        "dir": _parent_of(job["file"]), "state": "running",
                        "template": job["template"], "started_at": job["started_at"],
                        "step": job["step"], "chunks_done": job["chunks_done"],
                        "chunks_total": job["chunks_total"]})

    # Папка, в которой ещё нет ни одной готовой записи, тоже должна быть видна:
    # иначе новая папка появляется на странице только после первого результата.
    for item in pending:
        parent = item["dir"]
        if parent == rel or not parent.startswith(prefix):
            continue
        name = parent[len(prefix):].split("/", 1)[0]
        entry = dirs.setdefault(name, {"name": name, "path": prefix + name,
                                       "files": 0, "last": None})
        entry["busy"] = entry.get("busy", 0) + 1

    # Пустые папки тоже нужны: в только что созданную папку ещё предстоит загрузить
    # первую запись. Открытые человеку папки — из его списка, остальное — с диска,
    # причём читается одна папка, а не всё дерево.
    def show(folder: str) -> None:
        if folder != rel and folder.startswith(prefix):
            name = folder[len(prefix):].split("/", 1)[0]
            dirs.setdefault(name, {"name": name, "path": prefix + name, "files": 0, "last": None})

    for folder in who.roots:
        show(folder)
    try:
        for p in (cfg.inbox / rel).iterdir():
            child = prefix + p.name
            if (p.is_dir() and not p.name.startswith(".") and who.sees(child)
                    and not (core.service._under_outbox(child))):
                show(child)
    except OSError:
        pass

    return {
        "path": rel,
        "parent": None if rel == "" else _parent_of(rel),
        "dirs": sorted(dirs.values(), key=lambda d: d["name"].lower()),
        "files": files,
        "pending": pending,
        "templates": {k: v["title"] for k, v in templates.types(cfg.prompts_dir).items()},
        # enabled — можно ли загружать в открытую папку; anywhere — есть ли у человека хоть одно
        # место для загрузки: без него кнопки «Загрузить запись» на странице нет вовсе;
        # denied — почему нельзя сюда, словами для подсказки и для ответа на перетаскивание
        "upload": {"enabled": core.upload_ready and who.uploads(rel), "max_bytes": cfg.upload_max_bytes,
                   "exts": sorted(media.MEDIA_EXTS), "anywhere": core.upload_ready and who.uploader,
                   "denied": _upload_denied(who, rel)},
        "mkdir": core.upload_ready and who.plants(rel),
        # хозяин папки правит её словарь и словари её записей (в том числе при загрузке) и включает
        # и выключает у неё признак «серия совещаний» (series — включён ли; у корня его нет)
        "owns": who.owns(rel),
        # у закрытой папки признака не видно: выключенный выдал бы, что такая папка есть
        "series": who.sees(rel) and serial.on(rel),
        # из общего списка запись уходит в личную папку — если человеку разрешено в неё загружать
        "home_upload": core.upload_ready and bool(who.home) and who.uploads(who.home),
        # раздавать доступ можно к папке, не к корню: корень открывает только администратор
        "manage": not cfg.personal and rel != "" and who.rules(rel),
        # running — сколько записей в работе из тех, что человеку видны
        "service": {"running": len(running),
                    "queue": len(core.service.queue), "watching": len(core.service.pending)},
    }


def _upload_rel(folder: str, name: str, who: users.Access) -> str:
    if not core.upload_ready:
        raise HTTPException(503, "загрузка отключена: входная папка доступна только для чтения")
    try:
        rel = upload.target_rel(folder, name, cfg.outbox_inside_inbox)
    except upload.UploadError as e:
        raise HTTPException(e.status, e.detail)
    if not who.sees(_parent_of(rel)):
        raise HTTPException(404, folders.NO_FOLDER)         # закрытая папка неотличима от несуществующей
    if not who.uploads(_parent_of(rel)):
        raise HTTPException(403, _no_upload(who, _parent_of(rel)))
    return rel


# Журнал обработки ведётся по пути: файл, загруженный по пути записи, чей исходник убрали с диска
# руками, сервис счёл бы уже обработанным и в работу не взял бы никогда.
UPLOAD_KNOWN = ("запись с таким именем в этой папке уже есть в журнале обработки (её файл могли убрать с диска "
                "вручную) — загрузите запись под другим именем или сначала удалите прежнюю запись на странице")


def _not_in_ledger(rel: str) -> None:
    assert core.service is not None
    if core.service.ledger.get(rel) is not None:
        raise HTTPException(409, UPLOAD_KNOWN)


class FolderRequest(BaseModel):
    dir: str


@router.post("/api/folder")
async def make_folder(req: FolderRequest, who: users.Access = Depends(viewer)):
    """Новая подпапка. Человек заводит их в своей личной папке, администратор — где угодно."""
    if not core.upload_ready:
        raise HTTPException(503, "входная папка доступна только для чтения")
    try:
        rel = upload.folder_rel(req.dir, cfg.outbox_inside_inbox)
    except upload.UploadError as e:
        raise HTTPException(e.status, e.detail)
    if not who.plants(_parent_of(rel)):
        raise HTTPException(403, "подпапки можно создавать в личной папке и там, где вам дано "
                                 "администрирование с подпапками")
    if (cfg.inbox / rel).exists():
        raise HTTPException(409, "такая папка уже есть")
    # новая папка и каждая заводимая заодно над ней — не на месте результатов записи с тем же именем
    parts = rel.split("/")
    made = [p for p in ("/".join(parts[:n]) for n in range(1, len(parts) + 1)) if not (cfg.inbox / p).exists()]
    if any((cfg.outbox / p).exists() for p in made):
        raise HTTPException(409, folders.RESULTS_TAKEN)
    try:
        await asyncio.to_thread((cfg.inbox / rel).mkdir, parents=True)
    except OSError as e:
        raise _os_fault("не удалось создать папку", e)
    log(f"создана папка: {rel}" + (f" ({who.login})" if who.login else ""))
    jot("folder_create", who, folder=rel)
    return {"ok": True, "path": rel}


@router.get("/api/upload")
async def upload_state(name: str, size: int, stamp: str, dir: str = "", who: users.Access = Depends(viewer)):
    """Сколько байт записи уже принято: с этого места страница продолжает после обрыва.
    stamp — время изменения файла у человека: по нему и по логину загрузка не продолжит чужую
    с тем же именем и размером (upload.part_path)."""
    rel = _upload_rel(dir, name, who)
    _not_in_ledger(rel)
    try:
        have = await asyncio.to_thread(upload.received, cfg.inbox, rel, size, cfg.upload_max_bytes, who.login, stamp)
    except upload.UploadError as e:
        raise HTTPException(e.status, e.detail)
    return {"offset": have, "chunk": upload.CHUNK_BYTES}


@router.put("/api/upload")
async def upload_chunk(request: Request, name: str, size: int, offset: int, stamp: str, dir: str = "",
                       who: users.Access = Depends(viewer)):
    """Очередная часть записи — телом запроса, как есть."""
    rel = _upload_rel(dir, name, who)
    _not_in_ledger(rel)
    if size > cfg.upload_max_bytes:
        raise HTTPException(413, f"файл больше предела {cfg.upload_max_bytes / 2**30:.0f} ГБ")
    data = bytearray()
    async for piece in request.stream():
        data += piece
        if len(data) > upload.CHUNK_BYTES:
            raise HTTPException(413, "часть больше допустимого размера")
    try:
        async with upload_lock:
            have, done = await asyncio.to_thread(
                upload.append, cfg.inbox, rel, size, offset, bytes(data), who.login, stamp)
    except upload.UploadError as e:
        raise HTTPException(e.status, e.detail)
    except OSError as e:
        raise _os_fault("не удалось записать файл", e)
    if done:
        log(f"загружена через страницу: {rel} ({size / 1048576:.1f} МБ)"
            + (f" ({who.login})" if who.login else ""))
        jot("upload", who, file=rel, size=size)
    return {"offset": have, "done": done, "file": rel}


@router.delete("/api/upload")
async def upload_cancel(name: str, size: int, stamp: str, dir: str = "", who: users.Access = Depends(viewer)):
    """Человек отменил загрузку: принятое больше не нужно."""
    rel = _upload_rel(dir, name, who)
    await asyncio.to_thread(upload.cancel, cfg.inbox, rel, size, who.login, stamp)
    return {"ok": True}
