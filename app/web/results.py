"""Поиск и результаты записи: карта, выгрузки, протокол и расшифровка, звук."""
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse

from .. import export, external, marks, media, mindmap, names, pipeline, registry, search, users
from . import core
from .access import _record, viewer
from .common import _marked, _page_policy, _parent_of, _protocol, _protocol_state, _rebuild_state
from .core import LEGACY_DIRS, RESULT_KINDS, RESULT_MAX_BYTES, cfg, noted, organization, words
from .tasks import _prev_view

router = APIRouter()


@router.get("/api/search")
async def search_all(q: str = "", who: users.Access = Depends(viewer)):
    """Поиск по протоколам и расшифровкам всех записей сразу.

    Пароля не требует, как и чтение самих результатов: искать можно только по
    тому, что есть в журнале обработки.
    """
    assert core.service is not None
    if len(q.strip()) < search.MIN_QUERY:
        raise HTTPException(400, "слишком короткий запрос")
    records = [r for r in core.service.ledger.history(limit=search.MAX_SCAN)
               if who.sees(_parent_of(r["file"]))]
    # выходная папка может быть сетевой: чтение уводим из событийного цикла
    return await asyncio.to_thread(search.find, records, cfg.outbox, q)


@router.get("/api/map")
def result_map(file: str, download: bool = False, who: users.Access = Depends(viewer)):
    """Карта записи — самостоятельная страница: протокол, дерево его разделов и расшифровка.
    download=true — тот же файл для скачивания, без кнопки возврата в сервис.

    Отдаётся файл, лежащий рядом с протоколом; если протокол новее карты
    (его правили руками), карта перед этим пересобирается.
    """
    return _map(file, download, who)


def _map(file: str, download: bool, who: users.Access) -> Response:
    assert core.service is not None
    record = _record(file, who)
    stem = pipeline.stem_of(cfg.inbox, file, record)
    protocol = (cfg.outbox / Path(file).parent / stem / "summary" / f"{stem}.md").resolve()
    if cfg.outbox.resolve() not in protocol.parents:
        raise HTTPException(400, "путь вне выходной папки")
    if not protocol.is_file():
        raise HTTPException(404, "у записи нет протокола — карту строить не из чего")
    ready, name = mindmap.ensure(protocol, noted.load(file), noted.changed(file)), f"{stem}{mindmap.FILE_SUFFIX}"
    page = ready.read_text(encoding="utf-8")
    org = organization()
    if org:
        # название организации — в шапку при отдаче: файл на диске от оформления не зависит
        page = mindmap.with_org(page, org)
    if download:
        return Response(page, media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe=""),
                                 "Content-Security-Policy": _page_policy(page)})
    # страницу показывает сам сервис — в шапке кнопка возврата к этой записи; в скачанном файле её нет
    place = {"path": file.rpartition("/")[0], "open": file}
    back = "/?" + urlencode({k: v for k, v in place.items() if v}, quote_via=quote)
    page = mindmap.with_back(page, back)
    return Response(page, media_type="text/html; charset=utf-8", headers={"Content-Security-Policy": _page_policy(page)})


@router.get("/api/export")
async def result_export(file: str, format: str, who: users.Access = Depends(viewer)):
    """Протокол записи файлом для людей: format=docx — документ Word, format=pdf — PDF."""
    if format not in export.FORMATS:
        raise HTTPException(400, "неизвестный формат: нужен docx или pdf")
    return await _export(file, format, who)


def _export_file(file: str, record: dict, fmt: str) -> tuple[bytes, str] | None:
    protocol = _protocol(file, record)
    if not protocol:
        return None
    # отметки «выполнено» — в колонке статуса: в файле их больше взять неоткуда
    protocol = marks.stamp(protocol, noted.load(file))
    # ссылки на моменты записи — только на постоянный адрес сервиса: файл уходит из сервиса
    # насовсем, и адрес, по которому его случайно скачали, в нём не годится
    return export.build(protocol, record, fmt, cfg.public_url, organization())


async def _export(file: str, fmt: str, who: users.Access) -> Response:
    record = _record(file, who)
    # протокол читается с диска (папка бывает сетевой), сборка файла занимает до секунды —
    # всё это в отдельном потоке; файл собирается в памяти и в выходную папку не пишется
    built = await asyncio.to_thread(_export_file, file, record, fmt)
    if built is None:
        raise HTTPException(404, "у записи нет протокола — выгружать нечего")
    data, name = built
    return Response(data, media_type=export.FORMATS[fmt], headers={
        "Content-Disposition": f"attachment; filename=\"protocol.{fmt}\"; filename*=UTF-8''{quote(name, safe='')}"})


@router.get("/api/result")
def result(file: str, kind: str = "summary", who: users.Access = Depends(viewer)):
    """Текст готового результата для чтения на странице.

    Открыть можно только то, что есть в журнале обработки: журнал здесь служит
    белым списком, поэтому произвольный путь подставить нельзя. Сама выходная
    папка остаётся границей — путь проверяется ещё раз после разрешения.
    """
    assert core.service is not None
    if kind not in RESULT_KINDS:
        raise HTTPException(400, "неизвестный вид результата")
    record = _record(file, who)

    stem = pipeline.stem_of(cfg.inbox, file, record)
    sub, ext = RESULT_KINDS[kind]
    root = cfg.outbox.resolve()
    base = cfg.outbox / Path(file).parent / stem
    path = (base / sub / f"{stem}{ext}").resolve()
    if not path.is_file() and kind in LEGACY_DIRS:
        path = (base / LEGACY_DIRS[kind] / f"{stem}{ext}").resolve()
    if root not in path.parents:
        raise HTTPException(400, "путь вне выходной папки")
    if not path.is_file():
        raise HTTPException(404, "такого результата нет: возможно, задание не дошло до этого шага")
    if path.stat().st_size > RESULT_MAX_BYTES:
        raise HTTPException(413, "файл слишком велик для показа на странице")
    text = path.read_text(encoding="utf-8")
    signs = names.view(base, stem)
    if kind == "transcript" and signs["lines"]:
        text = names.display_text(signs["lines"])
    elif kind == "srt":
        text = names.name_srt(text, signs["lines"])
    return {"file": file, "kind": kind, "name": path.name,
            "audio": _audio_path(file, record) is not None,
            # карта собирается из протокола: без него кнопкам «Карта» и «Скачать HTML» вести некуда
            "has_map": (base / "summary" / f"{stem}.md").is_file(),
            # подписывать ФИО и пересобирать протокол может хозяин папки, без пароля администратора
            "can_edit": who.owns(_parent_of(file)),
            # пересборка протокола платная: хозяину — и только с правом загрузки в этой папке
            "can_rebuild": who.owns(_parent_of(file)) and who.uploads(_parent_of(file)),
            # переименовать, перенести и удалить запись — тому же хозяину, если входная папка пишется
            "can_act": core.upload_ready and who.owns(_parent_of(file)),
            # отправить протокол на почту — тому же хозяину и только самому человеку, не его ключу
            "can_send": cfg.mail_on and not who.key and who.owns(_parent_of(file)),
            "text": text,
            # поручения протокола с отметками «выполнено» и сроками: line — строка текста, у которой
            # стоит флажок, due_col — в какой ячейке этой строки срок (None — такой колонки нет)
            **({"tasks": [{"line": n, "due_col": col["срок"], **task}
                          for (n, col, _), task in zip(external.tasks_at(text), _marked(file, record, text))],
                "can_mark": who.owns(_parent_of(file)),
                # раздел «Поручения прошлого совещания»: есть ли он, убран ли вручную; убрать и вернуть
                # его может тот же хозяин папки (can_edit), title — заголовок, у которого стоит кнопка
                "previous": {**_prev_view(file, record, text), "title": registry.BLOCK_TITLE},
                # отпечаток протокола (base для правки) и кто его правил вручную: тогда пересборка моделью
                # соберёт протокол заново, а правленый текст уйдёт в версии — страница предупреждает до неё
                **_protocol_state(path.parent, path.stem)}
               if kind == "summary" else {}),
            # ФИО из словаря записи и её папок — подсказки при подписи голосов
            "roster": [{"name": p["name"], "role": p["role"]} for p in words.effective(file)["people"]],
            # кто есть кто: части записи с их голосами, реплики с подписями
            **(signs if kind == "transcript" else {"parts": signs["parts"], "known": signs["known"]}),
            **_rebuild_state(file, base, stem)}


def _audio_path(file: str, record: dict) -> Path | None:
    """Звук записи для прослушивания на странице, если он сохранился."""
    stem = pipeline.stem_of(cfg.inbox, file, record)
    out_dir = cfg.outbox / Path(file).parent / stem
    found = media.existing_audio(out_dir, stem)
    if found is None:
        return None
    found = found.resolve()
    # ничего вне выходной папки отдавать нельзя
    return found if cfg.outbox.resolve() in found.parents else None


@router.get("/api/audio")
def audio(file: str, who: users.Access = Depends(viewer)):
    """Звук записи — чтобы переслушать момент, на который ссылается метка времени.

    Отдаётся с поддержкой Range: браузер тянет только тот кусок, который
    собрался играть, а не всю часовую дорожку ради одной минуты.
    """
    assert core.service is not None
    record = _record(file, who)
    path = _audio_path(file, record)
    if path is None:
        raise HTTPException(404, "звук этой записи не сохранён")
    # inline, а не attachment: ссылку могут открыть и прямо в браузере,
    # и тогда он должен проиграть запись, а не предложить её скачать
    return FileResponse(path, media_type="audio/mpeg", filename=path.name,
                        content_disposition_type="inline")
