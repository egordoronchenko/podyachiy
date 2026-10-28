"""Вход и выход, «кто я», смена пароля, какие письма человек получает."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import directory, users
from .access import OWNER, _base, _confirmed, _note_domain, _secure, _settle, domain_off, jot, person
from .common import _client
from .core import (DOMAIN_FAILS_SEC, PASSWORD_TOO_OFTEN, WRONG_PASSWORD_DELAY_SEC, cfg, claims, domain,
                   domain_fails_by_address, domain_fails_by_login, log, password_fails_by_address,
                   password_fails_by_login, people, state_kept, trail)

router = APIRouter()


class LoginRequest(BaseModel):
    login: str
    password: str


DOMAIN_OVER_HTTP = ("С учётной записью домена войти можно только по защищённому адресу сервиса (https://): "
                    "по незащищённому соединению доменный пароль не принимается и в домен не отправляется. "
                    "Администратору: включите HTTPS — install/README.md, «Включить HTTPS»")
DOMAIN_TOO_OFTEN = (f"слишком много неудачных попыток входа — подождите {DOMAIN_FAILS_SEC // 60} минут. Сервис "
                    "придерживает попытки, чтобы учётную запись не заблокировал домен")
DOMAIN_DOWN = ("домен сейчас не отвечает — войти с учётной записью домена нельзя. Попробуйте позже или "
               "обратитесь к администратору сервиса")


async def _sign_in(typed: str, password: str, request: Request) -> tuple[users.Access, dict]:
    """Кто входит и где проверяется его пароль — ровно в одном месте.

    Правило. Логин приводится к короткому виду: user, DOMAIN\\user и user@domain.tld — это user.
    Короткое имя, занятое в сервисе локально, — встроенный admin или учётная запись с источником
    «локальный» (в том числе приглашённая, ещё без пароля), — принадлежит сервису: пароль
    сверяется только с хешем в сервисе и в домен не уходит никогда. Набранное с именем домена
    (CORP\\ivanov при локальном ivanov) не проверяется нигде. Все остальные логины — которых в
    сервисе нет или у которых источник «домен» — проверяет только домен: с хешем в сервисе их
    пароль не сверяется (у доменной записи хеша и нет). Вход через домен выключен — все логины
    проверяются локально, как раньше.

    Отказ «неверный логин или пароль» одинаков для локального и доменного логина, существующего
    и нет; время ответа выравнивает вызывающий."""
    plain, short, off = typed.strip().lower(), directory.short_login(typed), domain_off()

    def refuse(reason: str = "", status: int = 401, text: str = "") -> HTTPException:
        trail.write("login_failed", (short or plain)[:64], **({"reason": reason} if reason else {}))
        # По HTTP доменный вход не принимается — и это сказано в отказе любому логину, чтобы
        # по тексту нельзя было отличить локальный логин от доменного.
        return HTTPException(status, text or (directory.INVALID + (
            "" if off or _secure(request) else ". " + DOMAIN_OVER_HTTP)))

    local = short == users.ADMIN or people.source(short) == users.LOCAL
    if off or (local and plain == short):
        address = _client(request)
        if password_fails_by_login.full(plain) or password_fails_by_address.full(address):
            raise refuse("слишком много неудачных попыток", 429, PASSWORD_TOO_OFTEN)
        # scrypt считается десятки миллисекунд — не в событийном цикле
        if not await asyncio.to_thread(people.verify, plain, password):
            password_fails_by_login.allow(plain)
            password_fails_by_address.allow(address)
            raise refuse()
        return people.access(plain), {}
    if local:
        raise refuse("логин локальной учётной записи набран с именем домена")
    if not _secure(request):
        raise refuse("вход через домен по незащищённому соединению (HTTP) не принят")
    address = _client(request)
    if domain_fails_by_login.full(short) or domain_fails_by_address.full(address):
        raise refuse("слишком много неудачных попыток", 429, DOMAIN_TOO_OFTEN)
    try:
        person = await asyncio.to_thread(domain.authenticate, typed, password)
    except directory.Denied as e:
        domain_fails_by_login.allow(short)
        domain_fails_by_address.allow(address)
        raise refuse(f"домен: {e.text}", text="" if e.reason == "invalid" else e.text)
    except directory.Unavailable as e:
        _note_domain(str(e))
        raise refuse("домен недоступен", 503, DOMAIN_DOWN)
    _note_domain()
    try:
        await _settle(person)
    except users.UserError as e:
        # логин занят локальной учётной записью либо не годится сервису (admin, длина, знаки)
        raise refuse(f"домен подтвердил, сервис не принял: {e}", 403, str(e))
    return people.access(person.login), {"source": users.DOMAIN}


@router.post("/api/login")
async def login(req: LoginRequest, request: Request, response: Response):
    started = time.monotonic()
    try:
        who, how = await _sign_in(req.login, req.password, request)
    except HTTPException:
        # отказ приходит не раньше этого срока, какой бы путь ни прошла проверка
        await asyncio.sleep(max(0.0, WRONG_PASSWORD_DELAY_SEC - (time.monotonic() - started)))
        raise
    jot("login", who, **how)
    _session_cookie(response, request, who.login)
    return {"login": who.login, "name": who.name, "admin": who.admin}


def _session_cookie(response: Response, request: Request, login: str) -> None:
    # метка недоступна скриптам страницы и не уходит с запросами с чужих сайтов
    # выдана по HTTPS — по HTTP браузер её не отправит
    response.set_cookie(users.COOKIE, people.token(login), max_age=users.SESSION_SEC,
                        httponly=True, samesite="lax", path="/", secure=request.url.scheme == "https")


@router.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(users.COOKIE, path="/")
    return {"ok": True}


@router.post("/api/logout/all")
async def logout_everywhere(request: Request, response: Response, who: users.Access = Depends(person)):
    """«Выйти везде»: все входы человека на других браузерах и устройствах перестают действовать,
    этот получает новую метку и остаётся — человек сам нажал кнопку здесь. Ключи доступа — отдельные
    пропуска программ: они не отзываются, для этого есть окно «Ключи доступа»."""
    await asyncio.to_thread(people.end_sessions, who.login)
    _session_cookie(response, request, who.login)
    log(f"пользователь {who.login} вышел на всех устройствах")
    jot("logout_all", who)
    return {"ok": True}


@router.get("/api/me")
async def me(request: Request):
    """Кто вошёл — странице, чтобы показать имя или отправить на вход."""
    if cfg.personal:
        return {"login": OWNER.login, "name": "", "admin": True, "home": None, "personal": True,
                "home_root": users.HOME_ROOT, "mail": False, "signup": False, "requests": 0, "url": _base(request),
                "url_set": bool(cfg.public_url),
                "email": "", "notify_ready": False, "notify_due": False, "domain": {"on": False}, "domain_account": False,
                "notice": ""}
    who, notice = people.by_token(request.cookies.get(users.COOKIE, "")), ""
    try:
        who = who and await _confirmed(who)
    except HTTPException as e:
        who, notice = None, e.detail        # домен человеку отказал — страница входа скажет почему
    box = (who and people.mailbox(who.login)) or {"email": "", "notify_ready": False, "notify_due": False}
    configured = not domain_off()
    return {"login": who and who.login, "name": who and who.name,
            # вход под учётной записью домена — странице входа: предлагать ли его по этому соединению.
            # Настроен, но соединение незащищённое (blocked) — страница говорит об этом и даёт адрес https
            "domain": {"on": configured and _secure(request), "blocked": configured and not _secure(request),
                       "name": cfg.ldap_domain if configured else "",
                       "https": cfg.public_url if configured and cfg.public_url.startswith("https://") else ""},
            # своя учётная запись — из домена: пароль в сервисе не меняется
            "domain_account": bool(who and who.domain), "notice": notice,
            "admin": bool(who and who.admin), "home": who and who.home, "personal": False,
            "home_root": users.HOME_ROOT,
            # адрес сервиса — справке «Интеграция с ИИ»: он стоит в тексте, который вставляют в чат клиента
            "url": _base(request),
            # задан ли он в .env (PUBLIC_URL): нет — в навык попадает адрес, по которому открыта страница
            "url_set": bool(cfg.public_url),
            # включена ли почта — странице входа: без неё нет «Забыл пароль», а заявка идёт без писем
            "mail": cfg.mail_on,
            # принимаются ли заявки на доступ: всегда, пока учётные записи есть где хранить
            "signup": state_kept,
            # своя почта, письма о готовности записи и напоминания о сроках — переключателям в меню учётной записи
            "email": box["email"], "notify_ready": box["notify_ready"], "notify_due": box["notify_due"],
            # сколько заявок ждёт решения — счётчик в меню, только администратору сервиса
            "requests": len(claims.pending()) if who and who.admin else 0}


class PasswordRequest(BaseModel):
    old: str
    new: str
    revoke_keys: bool = False     # заодно отозвать свои ключи доступа


def _revoked(count: int) -> dict:
    """Сколько ключей отозвано вместе с паролем — в ответ и в журнал, если отозваны."""
    return {"keys_revoked": count} if count else {}


@router.post("/api/password")
async def change_password(req: PasswordRequest, who: users.Access = Depends(person)):
    """Смена своего пароля. Все входы человека, и этот тоже, перестают действовать: метка входа
    подписана вместе с паролем. revoke_keys — отозвать и ключи доступа: по умолчанию нет, на них
    работают интеграции человека, и обрывать их без его слова нельзя."""
    if who.login == users.ADMIN:
        raise HTTPException(400, "пароль администратора задаётся в .env на сервере")
    if who.domain:
        raise HTTPException(400, users.IN_DOMAIN)
    try:
        dropped = await asyncio.to_thread(people.set_password, who.login, req.old, req.new, req.revoke_keys)
    except users.UserError as e:
        await asyncio.sleep(WRONG_PASSWORD_DELAY_SEC)
        raise HTTPException(400, str(e))
    log(f"пользователь {who.login} сменил пароль" + (f" и отозвал свои ключи доступа: {dropped}" if dropped else ""))
    jot("password", who, **({"keys": dropped} if dropped else {}))
    return {"ok": True, **_revoked(dropped)}


class NotifyRequest(BaseModel):
    ready: bool | None = None       # письма «запись готова»; не указано — не трогать
    due: bool | None = None         # напоминания о сроках поручений; не указано — не трогать


@router.post("/api/notify")
async def notify_set(req: NotifyRequest, who: users.Access = Depends(person)):
    """Письма «запись готова» и напоминания о сроках поручений — человек включает и отключает их себе сам."""
    if req.ready is None and req.due is None:
        raise HTTPException(400, "укажите ready или due: какие письма включить или отключить")
    try:
        await asyncio.to_thread(people.set_notify, who.login, req.ready, req.due)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    jot("notify", who, **{k: v for k, v in (("ready", req.ready), ("due", req.due)) if v is not None})
    box = people.mailbox(who.login)
    return {"ok": True, "notify_ready": box["notify_ready"], "notify_due": box["notify_due"]}
