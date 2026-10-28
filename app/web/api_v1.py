"""/api/v1: для ИИ-клиентов и внешних систем."""
from __future__ import annotations

import asyncio
import csv
import datetime
import io
import os
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.requests import ClientDisconnect

from .. import (about, apidoc, costbook, external, folders, keys, marks, media, names, pipeline, registry, search,
                upload, users)
from . import core
from .access import _base, _os_fault, _record, jot, viewer
from .browse import _dir, _failure, _not_in_ledger, _shown, _source, _upload_rel, _uploaded
from .common import _marked, _parent_of, _protocol, _protocol_state
from .core import SKILL, cfg, log, noted, serial, task_cache, upload_lock
from .folders_access import FOLDER_DEPTH
from .reports import _day
from .results import _audio_path, _export, _map

router = APIRouter()


# ---------- /api/v1: для ИИ-клиентов и внешних систем ----------
# Ответы рассчитаны на чтение моделью и программой: JSON с понятными полями и Markdown.
# Права те же, что у страницы: чужая папка не видна ни в дереве, ни в поиске, ни в поручениях.

TREE_DEPTH = 2          # сколько уровней папок дерево раскрывает, если не попросили иначе
TREE_RECORDS = 200      # записей одной папки в дереве; остальное — счётчиком
FINAL_STATES = ("ready", "no_protocol", "no_audio", "error")
KIND_NAMES = {"summary": "protocol", "transcript": "transcript", "triage": "topics"}


def _skill_changes() -> list[tuple[int, str]]:
    """Список изменений навыка: (версия, текст раздела), от новых к старым. Файл CHANGELOG.md —
    единственное место, где записана версия навыка: верхний раздел «## N» и есть нынешняя."""
    text = (SKILL / "CHANGELOG.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    return [(int(head), f"## {head}{body}".rstrip() + "\n")
            for head, body in re.findall(r"^## (\d+)(.*?)(?=^## |\Z)", text, flags=re.S | re.M)]


def _skill_version() -> int:
    return _skill_changes()[0][0]


def _skill_text(name: str, request: Request) -> str:
    """Текст для модели из папки шаблонов — с адресом этой установки, версией навыка и только
    теми кусками, что относятся к режиму: {{#personal}}…{{/personal}} остаётся в личном,
    {{#corporate}}…{{/corporate}} — в корпоративном."""
    base = _base(request)
    text = (SKILL / name).read_text(encoding="utf-8").replace("\r\n", "\n")
    keep, drop = ("personal", "corporate") if cfg.personal else ("corporate", "personal")
    text = re.sub(r"\{\{#%s\}\}\n?.*?\{\{/%s\}\}\n?" % (drop, drop), "", text, flags=re.S)
    text = re.sub(r"\{\{[#/]%s\}\}\n?" % keep, "", text)
    return (text.replace("{{URL}}", base).replace("{{HOST}}", urlsplit(base).hostname or "")
            .replace("{{MAX_GB}}", f"{cfg.upload_max_bytes / 2**30:.0f}")
            .replace("{{VERSION}}", str(_skill_version())))


def _api_spec(request: Request) -> dict:
    """Описание внешнего API для этой установки: её адрес, режим и предел размера записи."""
    return apidoc.build(request.app, _base(request), cfg.personal, f"{cfg.upload_max_bytes / 2**30:.0f}", about.version())


def _markdown(text: str) -> Response:
    return Response(text, media_type="text/markdown; charset=utf-8")


def _visible(who: users.Access) -> list[dict]:
    assert core.service is not None
    return [r for r in list(core.service.ledger.records.values()) if who.sees(_parent_of(r["file"]))]


def _in_progress(who: users.Access) -> dict[str, dict]:
    """Записи, которые ещё не готовы или пересобираются: путь → что с ней сейчас."""
    assert core.service is not None
    found: dict[str, dict] = {}
    for path, watch in list(core.service.pending.items()):
        found[path] = {"state": "waiting", "quiet_sec": round(watch.quiet_sec()), "needed_sec": cfg.stable_sec}
    for place, item in enumerate(list(core.service.queue), 1):
        found[item.path] = {"state": "queued", "position": place}
    for job in list(core.service.running):
        found[job["file"]] = {"state": "processing", "step": job["step"], "started_at": job["started_at"],
                              "parts_done": job["chunks_done"], "parts_total": job["chunks_total"]}
    return {path: state for path, state in found.items() if who.sees(_parent_of(path))}


def _folder_arg(value: str, who: users.Access) -> str:
    """Папка из параметра запроса: пусто — корень; иначе проверенный путь внутри входной папки.
    Папки нет или она человеку не открыта — один и тот же отказ 404: существование чужой папки
    не раскрывается. Пустая папка, которая есть, — не отказ. Папка «есть», если она открыта
    человеку и лежит на диске либо названа в его правах, или если в ней или глубже лежит
    открытая ему запись или папка: до той нужно как-то дойти от корня."""
    assert core.service is not None
    folder = _dir(value)
    if not folder:
        return ""
    on_disk = who.sees(folder) and ((cfg.inbox / folder).is_dir() or (cfg.outbox / folder).is_dir())
    paths = (list(core.service.ledger.records) + list(core.service.pending) + [i.path for i in list(core.service.queue)]
             + [j["file"] for j in list(core.service.running)])
    if not (on_disk or any(external.inside(f, folder) for f in who.roots)
            or any(external.inside(_parent_of(f), folder) and who.sees(_parent_of(f)) for f in paths)):
        raise HTTPException(404, folders.NO_FOLDER)
    return folder


def _disk_folders(root: str, depth: int) -> list[str]:
    """Папки на диске внутри root: его подпапки и ещё depth уровней под ними."""
    assert core.service is not None
    found = []
    for path, dirs, _ in os.walk(cfg.inbox / root):
        rel = Path(path).relative_to(cfg.inbox).as_posix()
        rel = "" if rel == "." else rel
        level = 0 if rel == root else rel[len(root):].strip("/").count("/") + 1
        dirs[:] = [d for d in dirs if not d.startswith(".")] if level <= depth else []
        found += [f"{rel}/{d}" if rel else d for d in dirs]
    return [f for f in found if not core.service._under_outbox(f)]


def _tree(who: users.Access, root: str, depth: int) -> dict:
    done, progress = _visible(who), _in_progress(who)
    loose = serial.off()
    named = set(who.roots) | {_parent_of(r["file"]) for r in done} | {_parent_of(f) for f in progress}
    named |= {f for f in _disk_folders(root, depth) if who.sees(f)}
    # вместе с папками выше: до открытой папки нужно как-то дойти от корня
    known: set[str] = set()
    for folder in named:
        while folder:
            known.add(folder)
            folder = _parent_of(folder)
    known = {f for f in known if external.inside(f, root)}

    by_folder: dict[str, list[dict]] = {}
    ledger = {r["file"]: r for r in done}
    for file, state in progress.items():
        row = external.short(ledger[file]) if file in ledger else {
            "file": file, "title": None, "date": None, "duration_sec": None, "state": "", "has_protocol": False}
        by_folder.setdefault(_parent_of(file), []).append({**row, "state": state["state"]})
    for rec in sorted(done, key=external.day_of, reverse=True):
        if rec["file"] not in progress:
            by_folder.setdefault(_parent_of(rec["file"]), []).append(external.short(rec))

    def node(path: str, level: int) -> dict:
        out = {"path": path, "name": PurePosixPath(path).name,
               "can_upload": core.upload_ready and bool(path) and who.uploads(path),
               "series": bool(path) and path not in loose}
        if level > depth:
            # глубже не раскрываем: содержимое — отдельным запросом с dir=<path>
            out["records_inside"] = sum(len(rows) for f, rows in by_folder.items() if external.inside(f, path))
            return out
        rows = by_folder.get(path, [])
        out["records"] = rows[:TREE_RECORDS]
        if len(rows) > TREE_RECORDS:
            out["records_more"] = len(rows) - TREE_RECORDS
        out["folders"] = [node(f, level + 1) for f in sorted(known, key=str.lower) if _parent_of(f) == path]
        return out

    return node(root, 0)


@router.get("/api/v1")
def v1_about(request: Request, who: users.Access = Depends(viewer)):
    """Описание адресов — Markdown, чтобы модель прочитала его одним запросом. Методы в него
    подставляются из apidoc.py — того же источника, что у openapi.json и страницы /api."""
    return _markdown(_skill_text("API.md", request).replace("{{API}}", apidoc.markdown(_api_spec(request))))


@router.get("/api/v1/openapi.json")
def v1_openapi(request: Request):
    """Машинное описание внешнего API (OpenAPI 3.1). Без входа, как навык: в нём только адрес
    сервиса и описание запросов — ни данных, ни настроек установки."""
    return _api_spec(request)


@router.get("/api/v1/whoami")
def v1_whoami(request: Request, who: users.Access = Depends(viewer)):
    """Проверка подключения: чьими глазами клиент видит сервис. Заодно — какая версия навыка
    у сервиса сейчас: клиент сверяет её со своей и обновляет навык сам."""
    return {"login": who.login, "name": who.name, "admin": who.admin,
            "via": "key" if who.key else "login", "key": who.key or None,
            "manage": bool(who.admin and (not who.key or who.key_kind == keys.MANAGE)),
            "service": _base(request), "version": about.version(), "upload": core.upload_ready,
            # есть ли у человека хоть одна папка, куда ему дано загружать (сами папки — /api/v1/folders)
            "can_upload": core.upload_ready and who.uploader,
            "skill_version": _skill_version(), "skill_url": _base(request) + "/skill/SKILL.md",
            "skill_changes": _base(request) + "/skill/CHANGELOG.md"}


@router.get("/api/v1/tree")
async def v1_tree(dir: str = "", depth: int = TREE_DEPTH, who: users.Access = Depends(viewer)):
    """Папки с записями, которые видит человек. depth — сколько уровней подпапок раскрыть:
    0 — только записи самой папки и имена подпапок."""
    root = _folder_arg(dir, who)
    # входная папка может быть сетевой: обход уводим из событийного цикла
    return await asyncio.to_thread(_tree, who, root, max(0, min(depth, FOLDER_DEPTH)))


@router.get("/api/v1/records")
def v1_records(request: Request, since: str = "", dir: str = "", who: users.Access = Depends(viewer)):
    """Записи, обработка которых закончилась после since.

    Порядок — от старых к новым: finished_at последней записи ответа подставляют
    в since следующего запроса и так получают только новое. Пересобранный протокол
    появляется в списке снова — у него новое время готовности.
    """
    try:
        moment = external.parse_since(since)
    except ValueError:
        raise HTTPException(400, "since: нужен момент в виде 2026-03-12T14:30:00+03:00")
    return {"now": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "records": external.listing([_shown(r, who) for r in _visible(who)], moment, _folder_arg(dir, who),
                                        _base(request))}


@router.get("/api/v1/record")
def v1_record(request: Request, file: str, transcript: bool = False, who: users.Access = Depends(viewer)):
    """Одна запись — сведения, протокол и поручения полями.

    Расшифровка отдаётся только по просьбе (transcript=true): она на порядок
    больше протокола и нужна редко.
    """
    record = _record(file, who)
    protocol = _protocol(file, record)
    out = {**external.brief(_shown(record, who), _base(request)), "protocol": protocol,
           "tasks": _marked(file, record, protocol) if protocol else [],
           "can_mark": who.owns(_parent_of(file)), "source": _source(file, record),
           **(_protocol_state(pipeline.protocol_path(cfg, file, record).parent, pipeline.stem_of(cfg.inbox, file, record))
              if protocol else {"version": None, "edited": None})}
    out["links"]["audio"] = (_base(request) + "/api/audio?" + urlencode({"file": file}, quote_via=quote)
                             if _audio_path(file, record) else None)
    if transcript:
        stem = pipeline.stem_of(cfg.inbox, file, record)
        lines = names.view(cfg.outbox / Path(file).parent / stem, stem)["lines"]
        out["transcript"] = names.display_text(lines) if lines else None
    return out


@router.get("/api/v1/record.html")
def v1_record_html(file: str, who: users.Access = Depends(viewer)):
    """Карта, протокол и расшифровка одним файлом — тот же, что кнопка «Скачать HTML» на странице."""
    return _map(file, True, who)


@router.get("/api/v1/record.docx")
async def v1_record_docx(file: str, who: users.Access = Depends(viewer)):
    """Протокол документом Word — тот же файл, что «Скачать → DOCX» на странице."""
    return await _export(file, "docx", who)


@router.get("/api/v1/record.pdf")
async def v1_record_pdf(file: str, who: users.Access = Depends(viewer)):
    """Протокол в PDF — тот же файл, что «Скачать → PDF» на странице."""
    return await _export(file, "pdf", who)


def _excerpt(piece: dict) -> str:
    """Выдержка одной строкой, найденное слово выделено. Поиск отдаёт куски без пробелов по краям —
    возвращаем пробел там, где слово не примыкает к скобке, кавычке или знаку препинания."""
    before, after = piece["before"], piece["after"]
    return (before + (" " if before and before[-1] not in "(«[\"'" else "") + f'**{piece["match"]}**'
            + (" " if after[:1].isalnum() or after[:1] in "(«[—–-" else "") + after)


@router.get("/api/v1/search")
async def v1_search(q: str = "", dir: str = "", who: users.Access = Depends(viewer)):
    """Поиск по протоколам и расшифровкам: записи, где есть все слова запроса, с выдержками."""
    assert core.service is not None
    if len(q.strip()) < search.MIN_QUERY:
        raise HTTPException(400, "слишком короткий запрос: нужно хотя бы два знака")
    root = _folder_arg(dir, who)
    records = [r for r in core.service.ledger.history(limit=search.MAX_SCAN)
               if who.sees(_parent_of(r["file"])) and external.inside(_parent_of(r["file"]), root)]
    found = await asyncio.to_thread(search.find, records, cfg.outbox, q)
    ledger = {r["file"]: r for r in records}
    return {"query": q, "scanned": found["scanned"], "more": found.get("more", 0),
            "found": [{"file": f["file"], "title": ledger[f["file"]].get("title"),
                       "date": external.day_of(ledger[f["file"]]) or None, "hits": f["hits"],
                       "where": [KIND_NAMES[k] for k in f["kinds"]],
                       "excerpts": [_excerpt(s) for s in f["snippets"]]}
                      for f in found["files"]]}


def _task_rows(who: users.Access, base: str, root: str, start: str, end: str) -> tuple[list[dict], int]:
    """Поручения из протоколов, которые человек видит: папка root с подпапками, совещания
    с start по end. Второе значение — сколько протоколов просмотрено."""
    assert core.service is not None
    ledger = core.service.ledger
    task_cache.prune(ledger.version, lambda: [pipeline.protocol_path(cfg, r["file"], r)
                                              for r in list(ledger.records.values())])
    picked = [r for r in _visible(who)
              if r.get("status") == "ok" and external.inside(_parent_of(r["file"]), root)
              and (not start or external.day_of(r) >= start) and (not end or external.day_of(r) <= end)]
    found, scanned = registry.rows(picked, lambda r: pipeline.protocol_path(cfg, r["file"], r), task_cache, base,
                                   marked=noted)
    for row in found:
        row["can_mark"] = who.owns(row["dir"])          # отметку «выполнено» ставит хозяин записи
    return found, scanned


async def _tasks(request: Request, who: users.Access, dir: str, since: str, until: str, name: str,
                 due: bool, only_open: bool, q: str, sort: str, overdue: bool) -> dict:
    if sort not in registry.SORTS:
        raise HTTPException(400, "sort: " + " | ".join(registry.SORTS))
    # выходная папка может быть сетевой: проверку файлов уводим из событийного цикла
    found, scanned = await asyncio.to_thread(_task_rows, who, _base(request), _folder_arg(dir, who),
                                             _day(since), _day(until))
    picked = registry.pick(found, name, due, False, q, overdue)
    still = [row for row in picked if row["open"]]
    # total и open — счёт под тем же отбором без «только незакрытые»: «открыто N из M»
    return {"tasks": registry.order(still if only_open else picked, sort), "records": scanned,
            "people": registry.people(found), "total": len(picked), "open": len(still)}


@router.get("/api/v1/tasks")
async def v1_tasks(request: Request, dir: str = "", since: str = "", until: str = "",
                   name: str = Query("", alias="who"), due: bool = False,
                   only_open: bool = Query(False, alias="open"), q: str = "", sort: str = "date",
                   overdue: bool = False, who: users.Access = Depends(viewer)):
    """Сквозной реестр поручений — его же показывает страница «Поручения».

    Отборы: dir — папка с подпапками, who — исполнитель (часть имени), since и until — дни
    совещаний, due — только со сроком, open — только незакрытые, overdue — только просроченные,
    q — слова в тексте. sort: date — свежие совещания первыми, due — по сроку. people —
    исполнители в этой папке и за этот период, до отбора по человеку и тексту: подсказка, как
    имя записано в протоколах.

    У поручения status — слова протокола, done — отметка «выполнено» человека (done_by, done_at,
    comment), open — итог: не закрыто ни тем, ни другим. Срок: due — дата, которую задал человек
    (due_by, due_at), иначе null; due_text — слова протокола; due_date — дата для сортировки
    (заданная, а без неё вычисленная из слов); overdue — заданная дата прошла, а поручение
    не закрыто. total и open в ответе — сколько поручений под отбором всего и сколько не закрыто."""
    return await _tasks(request, who, dir, since, until, name, due, only_open, q, sort, overdue)


@router.get("/api/v1/tasks.csv")
async def v1_tasks_csv(request: Request, dir: str = "", since: str = "", until: str = "",
                       name: str = Query("", alias="who"), due: bool = False,
                       only_open: bool = Query(False, alias="open"), q: str = "", sort: str = "date",
                       overdue: bool = False, who: users.Access = Depends(viewer)):
    """Тот же реестр с теми же отборами — таблицей для Excel."""
    tasks = (await _tasks(request, who, dir, since, until, name, due, only_open, q, sort, overdue))["tasks"]
    out = io.StringIO()
    sheet = csv.writer(out, delimiter=";")
    # «Срок (дата)» — заданный человеком; «Срок по словам протокола» — вычисленный от дня совещания
    sheet.writerow(["Ответственный", "Поручение", "Срок (дата)", "Срок в протоколе", "Срок по словам протокола",
                    "Просрочено", "Статус", "Совещание", "Дата совещания",
                    "Папка", "Запись", "Время в записи", "Ссылка", "Выполнено", "Кто отметил", "Когда отмечено",
                    "Комментарий", "Кто задал срок"])
    for t in tasks:
        owner = (t["who"] or "не назван") + (" (не участвовал)" if t["absent"] else "")
        row = [owner, t["task"], marks.day(t["due"]) if t["due"] else "", t["due_text"] or "",
               "" if t["due"] else t["due_date"] or "", "да" if t["overdue"] else "", t["status"] or "",
               t["title"] or "", t["date"], t["dir"], PurePosixPath(t["file"]).name, t["at"] or "", t["link"],
               "да" if t["done"] else "", t["done_by"] or "", (t["done_at"] or "")[:16].replace("T", " "),
               t["comment"] or "", t["due_by"] or ""]
        # строки протокола и имена записей — не формулы для Excel (costbook.cell)
        sheet.writerow([costbook.cell(value) for value in row])
    # метка порядка байтов — чтобы Excel прочитал кириллицу
    return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="tasks.csv"'})


@router.get("/api/v1/tasks.ics")
async def v1_tasks_ics(request: Request, dir: str = "", since: str = "", until: str = "",
                       name: str = Query("", alias="who"), due: bool = False,
                       only_open: bool = Query(False, alias="open"), q: str = "", sort: str = "date",
                       overdue: bool = False, who: users.Access = Depends(viewer)):
    """Тот же реестр с теми же отборами — календарём ICS: событие на день срока у каждого поручения
    с датой срока (marks.ics). Поручения без даты срока в файл не попадают."""
    tasks = (await _tasks(request, who, dir, since, until, name, due, only_open, q, sort, overdue))["tasks"]
    return Response(marks.ics(tasks), media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="tasks.ics"'})


@router.get("/api/v1/status")
def v1_status(file: str, who: users.Access = Depends(viewer)):
    """Что сейчас с записью — чтобы дождаться результата после загрузки."""
    assert core.service is not None
    gone = HTTPException(404, "такой записи нет или её папка вам не открыта")
    if not who.sees(_parent_of(file)):
        raise gone
    state = _in_progress(who).get(file)
    record = core.service.ledger.get(file)
    if state is None and record is not None:
        state = {"state": external.state_of(record), "error": _failure(record, who),
                 "finished_at": record.get("finished_at")}
    if state is None:
        if not _uploaded(file):
            raise gone
        state = {"state": "uploaded"}
    return {"file": file, **state, "text": external.STATE_TEXT[state["state"]],
            "done": state["state"] in FINAL_STATES}


def _disk_where(allowed) -> list[str]:
    """Папки на диске, для которых allowed(папка) истинно. Личные папки лежат в общей
    «Пользовательские» — сама она не предлагается никому."""
    assert core.service is not None
    found = []
    for root, dirs, _ in os.walk(cfg.inbox):
        rel = Path(root).relative_to(cfg.inbox).as_posix()
        depth = 0 if rel == "." else rel.count("/") + 1
        dirs[:] = [d for d in dirs if not d.startswith(".") and depth < FOLDER_DEPTH]
        found += [f for f in (d if rel == "." else f"{rel}/{d}" for d in dirs)
                  if f != users.HOME_ROOT and allowed(f) and not core.service._under_outbox(f)]
    return sorted(found)


def _upload_folders(who: users.Access) -> list[str]:
    """Куда человек может загрузить запись: папки, где ему дано право загрузки."""
    return _disk_where(who.uploads) if core.upload_ready else []


@router.get("/api/v1/folders")
async def v1_folders(who: users.Access = Depends(viewer)):
    return {"folders": await asyncio.to_thread(_upload_folders, who),
            "upload": {"enabled": core.upload_ready, "max_bytes": cfg.upload_max_bytes,
                       "extensions": sorted(e.lstrip(".") for e in media.MEDIA_EXTS)}}


@router.put("/api/v1/upload")
async def v1_upload(request: Request, dir: str = "", name: str = "", who: users.Access = Depends(viewer)):
    """Запись одним запросом: тело — сам файл, потоком (curl -T). Папка обязательна:
    клиент не должен сам решать, куда положить чужое совещание. В личном режиме папок с правами
    нет, и без папки запись ложится в корень записей — туда же, куда её кладёт кнопка на странице."""
    if cfg.personal and not name:
        raise HTTPException(400, "нужно имя файла: PUT /api/v1/upload?name=<имя файла>&dir=<папка>. "
                                 "Без dir запись ляжет в корень записей")
    if not name or not (_dir(dir) or cfg.personal):
        folders = await asyncio.to_thread(_upload_folders, who)
        raise HTTPException(400, "нужны папка и имя файла: PUT /api/v1/upload?dir=<папка>&name=<имя файла>. "
                            + ("Папки, куда вы можете загружать: " + "; ".join(folders) if folders
                               else "Папок, открытых вам для загрузки, сейчас нет: право загрузки выдаёт "
                                    "администратор папки или сервиса"))
    rel = _upload_rel(dir, name, who)
    _not_in_ledger(rel)
    length = request.headers.get("content-length", "")
    try:
        part = await asyncio.to_thread(upload.stream_start, cfg.inbox, rel,
                                       int(length) if length.isdigit() else None, cfg.upload_max_bytes)
    except upload.UploadError as e:
        raise HTTPException(e.status, e.detail)
    size = 0
    try:
        with open(part, "wb") as f:
            async for piece in request.stream():
                size += len(piece)
                if size > cfg.upload_max_bytes:
                    raise upload.UploadError(413, f"файл больше предела {cfg.upload_max_bytes / 2**30:.0f} ГБ")
                await asyncio.to_thread(f.write, piece)
        async with upload_lock:
            size = await asyncio.to_thread(upload.stream_finish, cfg.inbox, rel, part)
    except BaseException as e:
        part.unlink(missing_ok=True)        # продолжить такую загрузку нельзя — держать обрывок незачем
        if isinstance(e, upload.UploadError):
            raise HTTPException(e.status, e.detail)
        if isinstance(e, ClientDisconnect):
            log(f"загрузка оборвалась: {rel}, принято {size / 1048576:.1f} МБ ({who.login})")
            raise HTTPException(400, "загрузка оборвалась: соединение закрылось раньше, чем файл принят целиком, — "
                                     "отправьте файл заново")
        if isinstance(e, OSError):
            raise _os_fault("не удалось записать файл", e)
        raise
    log(f"загружена запросом: {rel} ({size / 1048576:.1f} МБ) ({who.login}"
        + (f", ключ «{who.key}»" if who.key else "") + ")")
    jot("upload", who, file=rel, size=size)
    wait = cfg.scan_interval_sec + cfg.stable_sec
    return {"file": rel, "size": size, "state": "uploaded",
            "status": _base(request) + "/api/v1/status?" + urlencode({"file": rel}, quote_via=quote),
            "text": f"запись принята; сервис возьмёт её в работу примерно через {wait} с — "
                    "спрашивайте состояние по адресу status не чаще раза в минуту"}
