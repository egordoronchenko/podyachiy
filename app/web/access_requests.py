"""Заявка на доступ, «Забыл пароль» и пароль по ссылке."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from .. import mail, signup, users
from .common import _client
from .core import cfg, claim_accepted, claim_tries, claims, log, people, reset_by_address, reset_by_login, trail
from .login import _revoked

router = APIRouter()


# ---------- заявка на доступ и пароль по ссылке ----------

MAIL_OFF = "почта в сервисе не настроена — обратитесь к администратору"
TOO_OFTEN = "слишком много запросов с вашего адреса — попробуйте через час"


def _mail_needed() -> None:
    if not cfg.mail_on:
        raise HTTPException(503, MAIL_OFF)


async def _post(to: list[str], letter: tuple[str, str, str], reply_to: str = "") -> None:
    await asyncio.to_thread(mail.send, cfg, to, letter, reply_to=reply_to)


class ClaimRequest(BaseModel):
    name: str
    email: str
    login: str
    comment: str = ""


async def _notify_admins(claim: dict) -> None:
    """Письмо администраторам уходит после ответа странице: человек не ждёт почтовый сервер,
    а сбой почты заявку не отменяет — она уже сохранена и видна счётчиком в меню."""
    if not cfg.admin_emails:
        return
    try:
        # «Ответить» в письме ведёт к заявителю: администратору не нужно переписывать его адрес
        await _post(list(cfg.admin_emails), mail.notice(cfg, claim), claim["email"])
    except mail.MailError as e:
        log(f"ВНИМАНИЕ: письмо администраторам о заявке {claim['login']} не ушло: {e}")
        trail.write("mail_failed", "", what="notice", login=claim["login"], error=str(e)[:300])


async def _tell_known(login: str, email: str) -> None:
    """Письмо владельцу ящика: с его почтой подали заявку, а учётная запись у него уже есть."""
    user = people.get(login)
    if not user:
        return
    try:
        await _post([email], mail.known(cfg, user["name"], login, user["source"] == users.DOMAIN))
    except mail.MailError as e:
        log(f"ВНИМАНИЕ: письмо «учётная запись уже есть» ({login}) не ушло: {e}")
        trail.write("mail_failed", "", what="known", login=login, error=str(e)[:300])


@router.post("/api/request")
async def claim_new(req: ClaimRequest, request: Request, background: BackgroundTasks):
    """Заявка на доступ — от человека без учётной записи, без входа. Почта сервису для этого
    не нужна: заявка ложится в список у администратора, а с почтой ему ещё и уходит письмо."""
    if cfg.personal:
        raise HTTPException(404, "сервис работает без входа — заявки на доступ здесь не нужны")
    address = _client(request)
    if not claim_tries.allow(address) or claim_accepted.full(address):
        raise HTTPException(429, TOO_OFTEN)
    try:
        claim = await asyncio.to_thread(claims.add, req.name, req.email, req.login, req.comment, people,
                                        cfg.mail_on, cfg.mail_domains)
    except signup.RequestError as e:
        # X-Field — к какому полю формы относится отказ: страница покажет его под этим полем
        raise HTTPException(e.status, e.detail, headers={"X-Field": e.field} if e.field else None)
    except signup.Known as e:
        # ответ — как на любую заявку, и в счёт принятых она идёт так же: по ответу и по пределу не
        # узнать, что почта заведена. Владельцу ящика — письмо после ответа
        claim_accepted.allow(address)
        log(f"заявка на доступ с почтой учётной записи {e.login} — не сохранена, владельцу ушло письмо ({address})")
        trail.write("request_known", "", login=e.login)
        background.add_task(_tell_known, e.login, e.email)
        return {"ok": True}
    claim_accepted.allow(address)
    log(f"заявка на доступ: {claim['login']} ({address})")
    trail.write("request_new", "", login=claim["login"], name=claim["name"], email=claim["email"])
    if cfg.mail_on:
        background.add_task(_notify_admins, claim)
    return {"ok": True}


class ForgotRequest(BaseModel):
    login: str      # логин или почта


async def _send_reset(typed: str) -> None:
    """Ссылка для нового пароля — если такой пользователь есть и у него указана почта.
    У администратора из .env сброса нет: его пароль лежит в .env на сервере. У учётной записи
    домена тоже: её пароль меняется в домене."""
    login = typed if people.source(typed) else people.by_email(typed)
    user = people.get(login) if login else None
    if not user or not user["email"] or user["source"] == users.DOMAIN:
        return
    token = await asyncio.to_thread(people.issue_link, login)
    try:
        await _post([user["email"]], mail.reset(cfg, user["name"], login, token))
    except mail.MailError as e:
        log(f"ВНИМАНИЕ: письмо со ссылкой для нового пароля ({login}) не ушло: {e}")
        trail.write("mail_failed", "", what="link", login=login, why="reset", error=str(e)[:300])
        return
    log(f"пользователю {login} выслана ссылка для нового пароля")
    trail.write("link_sent", "", login=login, why="reset")


@router.post("/api/forgot")
async def forgot(req: ForgotRequest, request: Request, background: BackgroundTasks):
    """«Забыл пароль». Ответ один и тот же, есть такой пользователь или нет: иначе адрес
    подсказывал бы, какие логины и почты заведены. Письмо уходит после ответа — по времени
    ответа тоже ничего не узнать."""
    _mail_needed()
    typed = req.login.strip().lower()[:users.EMAIL_MAX]
    if not typed:
        raise HTTPException(400, "укажите логин или почту")
    if not reset_by_address.allow(_client(request)) or not reset_by_login.allow(typed):
        raise HTTPException(429, "слишком много запросов — попробуйте через час")
    trail.write("reset_request", "", login=typed[:64])
    background.add_task(_send_reset, typed)
    return {"ok": True}


class LinkRequest(BaseModel):
    token: str
    password: str = ""
    revoke_keys: bool = False     # заодно отозвать ключи доступа человека


@router.post("/api/set-password/check")
async def link_check(req: LinkRequest):
    """Действует ли ссылка и чья она — странице, прежде чем спрашивать пароль. Строка ссылки
    приходит телом запроса, а не в адресе: в журнал запросов она не попадает. keys — сколько у
    человека ключей доступа (если есть): страница предложит отозвать их вместе с паролем."""
    login = people.link_owner(req.token)
    if login is None:
        raise HTTPException(404, "ссылка недействительна или её срок вышел — запросите новую")
    held = len(people.keys(login))
    return {"login": login, "name": people.get(login)["name"], "password_min": users.PASSWORD_MIN,
            **({"keys": held} if held else {})}


@router.post("/api/set-password")
async def link_use(req: LinkRequest):
    """Пароль по одноразовой ссылке: первый — после одобренной заявки, новый — после «Забыл пароль».
    Прежние входы человека перестают действовать; revoke_keys — отзываются и его ключи доступа."""
    try:
        login, dropped = await asyncio.to_thread(people.use_link, req.token, req.password, None, req.revoke_keys)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    log(f"пользователь {login} задал пароль по одноразовой ссылке"
        + (f" и отозвал свои ключи доступа: {dropped}" if dropped else ""))
    trail.write("password_link", login, **({"keys": dropped} if dropped else {}))
    return {"ok": True, "login": login, **_revoked(dropped)}
