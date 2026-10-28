"""Письма о записи: «готово» и webhook, отправка протокола на почту; папки для переноса записи."""
from __future__ import annotations

import asyncio
import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import export, mail, notify, users
from .access import _record, jot, person, viewer
from .access_requests import _mail_needed
from .api_v1 import _disk_where
from .browse import _public_error
from .common import _parent_of, _protocol
from .core import NOTE_MAX, SENDS_PER_HOUR, SEND_FORMATS, cfg, hooks, log, people, protocol_sends, trail
from .results import _export_file

router = APIRouter()


# ---------- письма о записи ----------

def _about(record: dict) -> dict:
    """Сведения о записи для письма; читает протокол с диска — звать из потока."""
    return notify.about(record, _protocol(record["file"], record), cfg.public_url)


def _ready_letter(record: dict, kind: str, login: str, box: dict, by_hand: bool) -> None:
    """Письмо об исходе обработки. Идёт в своём потоке: сбой почты запись не трогает —
    она остаётся готовой, а сбой виден в логе и в журнале действий."""
    file = record["file"]
    try:
        about = _about(record)
        letter = (mail.ready(cfg, box["name"], about) if kind == notify.READY
                  else mail.failed(cfg, box["name"], about, record["status"], notify.retrying(record), by_hand))
        mail.send(cfg, [box["email"]], letter)
    except Exception as e:      # поток не должен умирать молча ни на почте, ни на чтении протокола
        log(f"ВНИМАНИЕ: письмо о записи {file} для {login} не ушло: {e}")
        trail.write("mail_failed", "", what=kind, file=file, to=login, error=str(e)[:300])
        return
    log(f"письмо «{'запись готова' if kind == notify.READY else 'запись не обработана'}»: {file} → {login}")
    trail.write("ready_mail", "", file=file, to=login, outcome=kind)


def _tell(record: dict, kind: str, login: str, by_hand: bool = False) -> threading.Thread | None:
    """Обработчик закончил прогон, и человеку положено письмо (кому и когда — notify.py).
    by_hand — запись запустил вручную сам получатель: о сбое ему пишется иначе, чем автору загрузки.
    Уходит оно, только если почта включена, у человека она указана и письма он не отключил.
    Отправка — в отдельном потоке с таймаутом почты (mail.TIMEOUT_SEC): обработку не задерживает."""
    box = people.mailbox(login)
    if not cfg.mail_on or not box or not box["email"] or not box["notify_ready"]:
        return None
    thread = threading.Thread(target=_ready_letter, args=(dict(record), kind, login, box, by_hand), daemon=True)
    thread.start()
    return thread


def _hook(record: dict, kind: str) -> None:
    """Событие webhook об исходе обработки (когда — webhook.due) — в очередь доставки. Запись в теле — без
    сумм и без ответа поставщика в тексте сбоя: получатель — внешняя система, а не администратор сервиса."""
    hooks.add(kind, {**record, "error": _public_error(record)}, cfg.public_url)


def _sender(file: str, who: users.Access) -> dict:
    """Запись, протокол которой человек вправе отправить на почту. Чужая запись — 404."""
    _mail_needed()
    record = _record(file, who)
    if not who.owns(_parent_of(file)):
        raise HTTPException(403, "отправить протокол может владелец папки или её администратор")
    return record


@router.get("/api/record/recipients")
def record_recipients(file: str, who: users.Access = Depends(person)):
    """Окну «Отправить протокол»: кого предложить в получатели и какие действуют пределы.
    Предлагаются пользователи сервиса, которым папка записи открыта и у которых указана почта."""
    _sender(file, who)
    folder = _parent_of(file)
    return {"people": [{"name": u["name"], "email": u["email"]} for u in people.public()
                       if u["email"] and notify.allowed(u["email"], cfg.mail_domains)
                       and people.access(u["login"]).sees(folder)],
            "max": notify.RECIPIENTS_MAX, "note_max": NOTE_MAX, "domains": list(cfg.mail_domains)}


class ProtocolSend(BaseModel):
    file: str
    to: str                 # адреса как их вписал человек: через запятую или с новой строки
    format: str = "pdf"     # pdf | docx | none — без вложения, только ссылка
    note: str = ""


def _send_parts(record: dict, fmt: str) -> tuple[dict, tuple[bytes, str] | None] | None:
    """Сведения о записи для письма и вложение (содержимое, имя файла). Нет протокола — None."""
    if not _protocol(record["file"], record):
        return None
    return _about(record), (None if fmt == "none" else _export_file(record["file"], record, fmt))


@router.post("/api/record/send")
async def record_send(req: ProtocolSend, who: users.Access = Depends(person)):
    """Протокол записи на почту. Это содержимое совещания, уходящее из сервиса на произвольные
    адреса, поэтому: только хозяин папки и только сам человек (ключу доступа адрес закрыт),
    домены получателей — из MAIL_DOMAINS, пределы на число адресов и отправок, запись в журнал
    действий с адресами полностью."""
    record = _sender(req.file, who)
    if req.format not in SEND_FORMATS:
        raise HTTPException(400, "вложение: pdf, docx или none (без вложения)")
    note = "\n".join(line.rstrip() for line in req.note.strip().splitlines())
    if len(note) > NOTE_MAX:
        raise HTTPException(400, f"приписка длиннее {NOTE_MAX} знаков", headers={"X-Field": "note"})
    try:
        to = notify.recipients(req.to, cfg.mail_domains)
    except notify.AddressError as e:
        raise HTTPException(400, str(e), headers={"X-Field": "to"})
    # протокол читается с диска, файл собирается до секунды — в отдельном потоке
    parts = await asyncio.to_thread(_send_parts, record, req.format)
    if parts is None:
        raise HTTPException(404, "у записи нет протокола — отправлять нечего")
    about, built = parts
    if built and len(built[0]) > mail.ATTACH_MAX_BYTES:
        raise HTTPException(413, f"файл протокола — {len(built[0]) / 2**20:.1f} МБ, больше предела "
                                 f"{mail.ATTACH_MAX_BYTES / 2**20:.0f} МБ для вложения: отправьте без вложения, ссылкой")
    if protocol_sends.full(who.login):
        raise HTTPException(429, f"вы отправили протокол уже {SENDS_PER_HOUR} раз за час — "
                                 "это предел; попробуйте позже")
    attachments = [(built[0], export.FORMATS[req.format], built[1])] if built else []
    box = people.mailbox(who.login)
    sent = {"file": req.file, "to": to, "format": req.format, "note": bool(note)}
    try:
        # каждому получателю — своё письмо: адресов друг друга они не видят
        refused = await asyncio.to_thread(mail.send_each, cfg, to, mail.protocol(
            cfg, who.name or who.login, about, note, SEND_FORMATS[req.format]), attachments,
            box["email"] if box else "")
    except mail.MailError as e:
        # человеку — что случилось его словами; ответ сервера как есть — в журнал работы и журнал действий
        log(f"ВНИМАНИЕ: протокол {req.file} не отправлен ({who.login} → {', '.join(to)}): {e}")
        jot("mail_failed", who, what="protocol", error=str(e)[:300], **sent)
        if e.refused:
            raise HTTPException(502, "почтовый сервер не принял ни один адрес: "
                                + "; ".join(f"{email} ({why})" for email, why in e.refused.items())
                                + ". Проверьте адреса", headers={"X-Field": "to"})
        raise HTTPException(502, e.plain)
    protocol_sends.allow(who.login)         # в предел идёт только удавшаяся отправка: письмо ушло хотя бы одному
    log(f"протокол отправлен на почту: {req.file} ({who.login} → {', '.join(to)}; "
        f"вложение {SEND_FORMATS[req.format] or 'нет'})"
        + (f"; не ушло: {', '.join(f'{email} ({why})' for email, why in refused.items())}" if refused else ""))
    # в журнале отправка — одно действие, сколько бы писем ни ушло
    jot("protocol_sent", who, refused=list(refused), **sent)
    return {"ok": True, "to": [e for e in to if e not in refused],
            "refused": [{"email": email, "why": why} for email, why in refused.items()]}


def _own_folders(who: users.Access) -> list[str]:
    """Папки на диске, где человек хозяин записей, — куда ему можно перенести запись."""
    return ([""] if who.owns("") else []) + _disk_where(who.owns)


@router.get("/api/record/folders")
async def record_folders(who: users.Access = Depends(viewer)):
    return {"folders": await asyncio.to_thread(_own_folders, who)}
