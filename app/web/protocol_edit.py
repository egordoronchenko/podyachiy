"""Правка протокола человеком и его версии."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import external, journal, mindmap, pipeline, summarize, users
from . import core
from .access import _os_fault, _record, jot, viewer
from .common import _in_work, _parent_of, _protocol, _protocol_state
from .core import base, cfg, log, noted

router = APIRouter()


# ---------- правка протокола человеком и его версии ----------
# Протокол — производный документ: модель его собирает, а человек может поправить — опечатку в ФИО,
# формулировку решения, пропущенное поручение. Правка — без модели: текст встаёт в файл протокола,
# прежний уходит в версии протокола с тем, кто и когда его сделал (summarize.py, «правка протокола»).
# Права — как у подписи говорящих: хозяин папки; ключу доступа — в тех же правах. Карта собирается
# заново, выгрузки DOCX и PDF, поиск, реестр поручений и письма читают протокол с диска и правку видят
# сами; отметки «выполнено» и сроки переходят к поручениям нового текста (marks.Store.follow).
# Пересборка моделью (после подписей или словаря) потом соберёт протокол заново: правленый текст при
# этом не пропадает, а уходит в версии — страница предупреждает об этом до пересборки (edited).

NO_PROTOCOL_EDIT = "править протокол и возвращать его версии может владелец папки или её администратор"
NO_PROTOCOL_HERE = "у записи нет протокола — править и возвращать нечего"
PROTOCOL_CHANGED = ("протокол изменился с тех пор, как вы его открыли: его поправил кто-то ещё или собрала заново "
                    "модель — обновите протокол и внесите правку в новый текст")
PROTOCOL_BUSY = "протокол записи сейчас собирается — дождитесь окончания"
PROTOCOL_EMPTY = "текст протокола пустой — нечего сохранять"
PROTOCOL_TOO_LONG = f"текст протокола длиннее {summarize.EDIT_MAX_CHARS} знаков — такой сервис не принимает"
PROTOCOL_NOT_TEXT = "в тексте протокола есть управляющие знаки или он не в UTF-8 — нужен обычный текст Markdown"
NO_VERSION = "такой версии протокола нет: возьмите stamp из списка версий заново"
# управляющие знаки, которых в тексте не бывает: всё, кроме табуляции и перевода строки
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _protocol_at(file: str, who: users.Access, edit: bool) -> tuple[dict, Path, str]:
    """Запись, папка её протокола (summary/) и stem — если протокол есть. edit — для правки: тогда
    нужен хозяин папки, и запись не должна стоять в очереди или обрабатываться."""
    assert core.service is not None
    record = _record(file, who)
    if edit and not who.owns(_parent_of(file)):
        raise HTTPException(403, NO_PROTOCOL_EDIT)
    path = pipeline.protocol_path(cfg, file, record)
    if not path.is_file():
        raise HTTPException(404, NO_PROTOCOL_HERE)
    if edit and (any(i.path == file for i in core.service.queue) or _in_work(file)):
        raise HTTPException(409, PROTOCOL_BUSY)
    return record, path.parent, path.stem


def _protocol_put(file: str, who: users.Access, fresh, since: str, how: str, stamp: str = "") -> dict:
    """Новый текст протокола: fresh(прежний текст) — каким он станет. since — отпечаток протокола, с
    которого человек начал; протокол с тех пор изменился — 409, и правка не ложится поверх чужой.

    Идёт прямо в событийном цикле, без await, — как «Убрать раздел»: пересборка не встанет между
    проверкой «запись не в работе» и записью файла. Файл, переход отметок к новым поручениям и строка
    в журнале действий — одной транзакцией базы: не записался файл — в базе ничего не меняется."""
    record, out_dir, stem = _protocol_at(file, who, edit=True)
    path = out_dir / f"{stem}.md"
    made = {"by": who.login, "at": journal.now_iso(), "how": how, **({"from": stamp} if stamp else {}),
            **({"key": who.key} if who.key else {})}
    moved = 0
    try:
        with base.write():
            changed = summarize.rewrite(out_dir, stem, fresh(path.read_text(encoding="utf-8")), made, since)
            if changed:
                text = _protocol(file, record) or ""
                moved = noted.follow(file, external.tasks(text))
                jot("protocol_edit" if how == "edit" else "protocol_restore", who, file=file, chars=len(text),
                    **({"stamp": stamp} if stamp else {}))
                mindmap.write_for(path, noted.load(file))
    except summarize.Changed:
        raise HTTPException(409, PROTOCOL_CHANGED)
    except OSError as e:
        raise _os_fault("не удалось записать протокол", e)
    if changed:
        log(f"протокол {file}: " + ("поправлен вручную" if how == "edit" else f"возвращена версия {stamp}")
            + f" ({who.login}" + (f", ключ «{who.key}»" if who.key else "") + ")"
            + (f"; отметок поручений перешло к новым формулировкам: {moved}" if moved else ""))
    return {"ok": True, "file": file, "changed": changed, **_protocol_state(out_dir, stem)}


def _protocol_text(text: str) -> str:
    """Текст правки, если его можно сохранить: не пустой, не длиннее предела, обычный текст."""
    if len(text) > summarize.EDIT_MAX_CHARS:
        raise HTTPException(413, PROTOCOL_TOO_LONG)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:              # одиночная половинка суррогатной пары из \ud800 в JSON
        raise HTTPException(400, PROTOCOL_NOT_TEXT)
    if CONTROL_RE.search(text.replace("\r", "")):
        raise HTTPException(400, PROTOCOL_NOT_TEXT)
    if not summarize.body_of(text).strip():
        raise HTTPException(400, PROTOCOL_EMPTY)
    return text


class ProtocolEdit(BaseModel):
    file: str
    text: str                   # протокол в Markdown целиком, без служебной пометки сервиса в начале
    base: str                   # version протокола, с которого начата правка


@router.post("/api/v1/protocol")
async def v1_protocol(req: ProtocolEdit, who: users.Access = Depends(viewer)):
    """Сохранить правку протокола: текст встаёт на место, прежний — в версии. Страница зовёт этот же метод."""
    text = _protocol_text(req.text)
    return _protocol_put(req.file, who, lambda old: summarize.with_header(old, text), req.base, "edit")


class ProtocolRestore(BaseModel):
    file: str
    stamp: str                  # метка прежней версии из GET /api/v1/versions
    base: str


@router.post("/api/v1/protocol/restore")
async def v1_protocol_restore(req: ProtocolRestore, who: users.Access = Depends(viewer)):
    """Вернуть прежнюю версию протокола — как правку: текущий текст уходит в версии, его тоже можно вернуть."""
    _, out_dir, stem = _protocol_at(req.file, who, edit=True)
    text = summarize.version_text(out_dir, stem, req.stamp)
    if text is None:
        raise HTTPException(404, NO_VERSION)
    return _protocol_put(req.file, who, lambda old: text, req.base, "restore", req.stamp)


@router.get("/api/v1/versions")
def v1_versions(file: str, who: users.Access = Depends(viewer)):
    """Версии протокола записи: текущая (её отпечаток и кто правил) и прежние, свежие первыми."""
    _, out_dir, stem = _protocol_at(file, who, edit=False)
    return {"file": file, **_protocol_state(out_dir, stem), "can_edit": who.owns(_parent_of(file)),
            "versions": summarize.versions(out_dir, stem)}


@router.get("/api/v1/version")
def v1_version(file: str, stamp: str, who: users.Access = Depends(viewer)):
    """Текст одной прежней версии протокола — посмотреть перед тем, как вернуть."""
    _, out_dir, stem = _protocol_at(file, who, edit=False)
    found = next((v for v in summarize.versions(out_dir, stem) if v["stamp"] == stamp), None)
    text = summarize.version_text(out_dir, stem, stamp) if found else None
    if text is None:
        raise HTTPException(404, NO_VERSION)
    return {"file": file, **found, "text": summarize.body_of(text)}
