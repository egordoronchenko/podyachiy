"""Пользователи и заявки, вход через домен, проверки почты, ключей и файлов."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import directory, mail, providers, signup, users
from . import core
from .access import (GRACE_SEC, PAGE_ONLY, RECHECK_SEC, _secure, admin_guard, domain_off, domain_seen, jot, page_admin,
                     viewer)
from .access_requests import _mail_needed, _post
from .common import _make_home
from .core import DOMAIN_FAILS_SEC, base, cfg, claims, domain, domain_fails_by_login, log, people
from .folders_access import _known_folders
from .lifecycle import _check_keys

router = APIRouter()


@router.post("/api/admin/users", dependencies=[Depends(admin_guard)])
async def admin_users():
    return {"users": people.public(), "folders": await asyncio.to_thread(_known_folders)}


# ---------- вход через домен: состояние, проверка подключения, привязка учётной записи ----------

def _domain_view(request: Request) -> dict:
    """Что администратор видит о входе через домен. Параметры — для чтения: они задаются
    в .env. Пароля служебной учётной записи здесь нет — только задан ли он."""
    off = domain_off()
    return {"on": not off, "why_off": off, "configured": bool(cfg.ldap_urls),
            "secure": _secure(request), "https": request.url.scheme == "https",
            "warnings": domain.warnings() if cfg.ldap_urls else [],
            "params": {"urls": list(cfg.ldap_urls), "base_dn": cfg.ldap_base_dn, "bind_dn": cfg.ldap_bind_dn,
                       "bind_password": bool(cfg.ldap_bind_password), "user_filter": domain.user_filter,
                       "access_group": cfg.ldap_access_group, "admin_group": cfg.ldap_admin_group,
                       "ca_file": directory.ca_path(cfg), "tls_verify": cfg.ldap_tls_verify,
                       "starttls": cfg.ldap_starttls, "allow_http": cfg.ldap_allow_http, "domain": cfg.ldap_domain},
            "recheck_min": RECHECK_SEC // 60, "grace_hours": GRACE_SEC // 3600,
            "fails": {"limit": domain_fails_by_login.limit, "minutes": DOMAIN_FAILS_SEC // 60}}


@router.get("/api/admin/domain", dependencies=[Depends(admin_guard)])
def admin_domain(request: Request):
    if cfg.personal:
        raise HTTPException(404, "в личном режиме входа нет")
    return _domain_view(request)


class DomainCheck(BaseModel):
    login: str = ""         # необязательно: проверить вход человека
    password: str = ""


@router.post("/api/admin/domain/check", dependencies=[Depends(page_admin)])
async def admin_domain_check(req: DomainCheck, who: users.Access = Depends(viewer)):
    """«Проверить подключение»: пошаговый отчёт — соединение и сертификат, служебная учётная
    запись, база поиска, группы и, если введены логин и пароль, вход человека. Только со
    страницы: сюда вводят чужой пароль. Он уходит в домен на проверку и больше никуда — ни в
    лог, ни в журнал, ни в отчёт; учётная запись в сервисе этой проверкой не заводится."""
    if cfg.personal:
        raise HTTPException(404, "в личном режиме входа нет")
    steps = await asyncio.to_thread(domain.probe, req.login[:directory.LOGIN_MAX], req.password)
    passed = all(s["ok"] is not False for s in steps)
    jot("domain_check", who, ok=passed, **({"login": directory.short_login(req.login)[:64]} if req.login.strip() else {}))
    return {"ok": passed, "steps": steps, "state": domain_off()}


class DomainBind(BaseModel):
    login: str


@router.post("/api/admin/users/domain", dependencies=[Depends(page_admin)])
async def admin_user_domain(req: DomainBind, who: users.Access = Depends(viewer)):
    """«Привязать к домену»: локальная учётная запись становится доменной — сама она этого
    не делает никогда. Пароль в сервисе стирается (дальше его проверяет домен), права на папки,
    личная папка и ключи доступа остаются. Привязать можно только того, кого домен подтверждает
    сейчас: с тем же логином, действующего и из группы доступа."""
    login = req.login.strip().lower()
    if people.source(login) != users.LOCAL:
        raise HTTPException(404, "локальной учётной записи с таким логином нет")
    off = domain_off()
    if off:
        raise HTTPException(400, f"вход через домен выключен: {off}")
    try:
        person = await asyncio.to_thread(domain.check, login)
        await asyncio.to_thread(people.adopt, person.login, person.name, person.email, person.admin, True)
    except directory.Denied as e:
        raise HTTPException(400, f"привязать нельзя: {e.text}")
    except directory.Unavailable as e:
        log(f"ВНИМАНИЕ: привязка {login} к домену не выполнена: {e}")
        raise HTTPException(503, "домен не отвечает — подробности покажет «Проверить подключение»")
    except users.UserError as e:
        raise HTTPException(400, str(e))
    domain_seen[login] = (time.time(), "")
    log(f"пользователь {login}: привязан к домену, пароль в сервисе стёрт ({who.login})")
    jot("domain_bind", who, login=login)
    return {"ok": True, "users": people.public()}


class UserRequest(BaseModel):
    # чего в запросе нет (None), то у существующего пользователя не меняется; у нового — умолчания:
    # ФИО — логин, не администратор, почты нет, загрузки в личную папку нет
    login: str
    name: str | None = None
    password: str = ""            # пусто — пароль не менять
    admin: bool | None = None
    email: str | None = None
    home_upload: bool | None = None


@router.post("/api/admin/users/save", dependencies=[Depends(admin_guard)])
async def admin_user_save(req: UserRequest, who: users.Access = Depends(viewer)):
    """Завести пользователя или поменять ему имя, почту, пароль, роль и загрузку в личную папку;
    чего в запросе нет, то у существующего не меняется. Права на папки — по папкам.

    Ключом с управлением пользователя заводят и правят, но входа на страницу ключ не даёт ни под чьим
    именем: пароля он не задаёт даже новому, роли администратора не даёт и не снимает, почту не
    меняет — на неё «Забыл пароль» прислал бы ссылку на пароль. Заведённому ключом пароль задаётся
    ссылкой, которую администратор выпускает на странице."""
    known = people.get(req.login.strip().lower())
    new = known is None
    name = req.name if req.name is not None else "" if new else known["name"]
    admin = req.admin if req.admin is not None else False if new else known["admin"]
    email = req.email
    if who.key and (req.password or admin != (False if new else known["admin"])
                    or (email is not None and email.strip().lower() != ("" if new else known["email"]))):
        raise HTTPException(403, PAGE_ONLY)
    if not new and people.source(req.login.strip().lower()) == users.DOMAIN:
        # у учётной записи домена ФИО и почта приходят из домена при каждом входе, пароля в сервисе
        # нет (put откажет), а роль при заданной группе администраторов определяет группа
        name, email = known["name"], None
        if cfg.ldap_admin_group and admin != known["admin"]:
            raise HTTPException(400, f"роль этой учётной записи задаёт группа домена «{cfg.ldap_admin_group}»: "
                                     "добавьте человека в группу или уберите из неё")
    try:
        was = None if new else known["home_upload"]
        # ключом новый заводится без пароля, как по одобренной заявке: пароль — ссылкой со страницы
        await asyncio.to_thread(people.put, req.login, name, req.password, admin, email, bool(who.key),
                                req.home_upload)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    user = people.get(req.login.strip().lower())
    _make_home(req.login.strip().lower())
    log(f"пользователь {req.login.strip().lower()}: {'заведён' if new else 'изменён'}, "
        f"{'администратор сервиса' if user['admin'] else 'пользователь'}"
        + (", пароль задан заново" if req.password and not new else "")
        + ("" if user["home_upload"] == bool(was) else
           ", загрузка в личную папку " + ("разрешена" if user["home_upload"] else "снята")))
    # home_upload в журнале — только когда право загрузки в личную папку выдано или снято
    jot("user_create" if new else "user_change", who, login=req.login.strip().lower(),
                admin=user["admin"], password=bool(req.password),
                **({} if user["home_upload"] == bool(was) else {"home_upload": user["home_upload"]}))
    return {"ok": True, "users": people.public()}


class UserDelete(BaseModel):
    login: str


@router.post("/api/admin/users/delete", dependencies=[Depends(admin_guard)])
async def admin_user_delete(req: UserDelete, who: users.Access = Depends(viewer)):
    """Убирает учётную запись. Личная папка и записи в ней остаются на месте."""
    login = req.login.strip().lower()
    if people.source(login) is None:
        raise HTTPException(404, "такого пользователя нет")

    def gone() -> None:
        # учётная запись, её права и ключи и строка журнала действий — одной транзакцией
        with base.write():
            people.delete(login)
            jot("user_delete", who, login=login)

    await asyncio.to_thread(gone)
    domain_seen.pop(login, None)
    log(f"пользователь {login}: удалён")
    return {"ok": True, "users": people.public()}


class UserSessions(BaseModel):
    login: str


@router.post("/api/admin/users/sessions", dependencies=[Depends(admin_guard)])
async def admin_user_sessions(req: UserSessions, who: users.Access = Depends(viewer)):
    """Завершает все входы человека на страницу: его браузеры выходят, войти заново он может
    своим паролем. Ключи доступа не трогаются — их отзывают отдельно. Управление входом не даёт,
    поэтому открыто и ключу с управлением. У admin из .env так нельзя: его, как и удалить или
    разжаловать, не могут другие администраторы, — он выходит везде сам, из своего меню."""
    login = req.login.strip().lower()
    if login == users.ADMIN or people.source(login) is None:
        raise HTTPException(404, "такого пользователя нет")
    try:
        await asyncio.to_thread(people.end_sessions, login)
    except users.UserError as e:
        raise HTTPException(404, str(e))
    log(f"пользователь {login}: все входы завершены ({who.login})")
    jot("sessions_end", who, login=login)
    return {"ok": True, "login": login}


class UserLink(BaseModel):
    login: str


async def _send_invite(login: str, by: users.Access, why: str) -> str | None:
    """Выпускает ссылку «задать пароль» и шлёт её человеку. Отдаёт ошибку почты, если письмо не ушло.
    Тому, у кого пароля ещё нет, — письмо «доступ открыт»; у кого есть — «новый пароль»."""
    user = people.get(login)
    letter = mail.reset if user["password"] else mail.invite
    token = await asyncio.to_thread(people.issue_link, login)
    try:
        await _post([user["email"]], letter(cfg, user["name"], login, token))
    except mail.MailError as e:
        log(f"ВНИМАНИЕ: письмо со ссылкой «задать пароль» ({login}) не ушло: {e}")
        jot("mail_failed", by, what="link", login=login, why=why, error=str(e)[:300])
        return str(e)
    log(f"пользователю {login} выслана ссылка «задать пароль» ({by.login})")
    jot("link_sent", by, login=login, why=why)
    return None


def _by_letter(login: str) -> bool:
    """Уйдёт ли ссылка «задать пароль» письмом: почта включена и у человека она указана.
    Иначе ссылку получает администратор и передаёт сам."""
    return cfg.mail_on and bool(people.get(login)["email"])


async def _show_link(login: str, by: users.Access, why: str) -> dict:
    """Выпускает ссылку «задать пароль» и отдаёт её администратору — один раз, в ответе на его
    запрос: в сервисе хранится только хеш. Отдаются путь и строка ссылки; полный адрес страница
    строит от того, по которому её открыл администратор: PUBLIC_URL может быть не задан."""
    token = await asyncio.to_thread(people.issue_link, login)
    log(f"ссылка «задать пароль» для {login} показана администратору ({by.login})")
    jot("link_shown", by, login=login, why=why)
    return {"path": "/set-password", "token": token, "hours": users.LINK_SEC // 3600}


@router.post("/api/admin/users/link", dependencies=[Depends(page_admin)])
async def admin_user_link(req: UserLink, who: users.Access = Depends(viewer)):
    """Новая ссылка «задать пароль»: письмо потерялось, срок вышел или человек забыл пароль.
    Прежняя ссылка перестаёт действовать. С почтой ссылка уходит человеку письмом; без почты
    (в сервисе или у человека) её получает администратор — link в ответе. Письмо не ушло — тоже:
    иначе при сломанной почте ссылку не получить никак (mail_error — ответ почтового сервера)."""
    login = req.login.strip().lower()
    user = people.get(login)
    if user is None:
        raise HTTPException(404, "такого пользователя нет")
    if user["source"] == users.DOMAIN:
        raise HTTPException(400, users.IN_DOMAIN)
    if not _by_letter(login):
        return {"ok": True, "login": login, "link": await _show_link(login, who, "admin")}
    failed = await _send_invite(login, who, "admin")
    if failed:
        return {"ok": True, "login": login, "email": user["email"], "mail_error": failed,
                "link": await _show_link(login, who, "admin")}
    return {"ok": True, "login": login, "email": user["email"]}


def _probe_to(who: users.Access) -> list[str]:
    """Куда уйдёт пробное письмо: вошедшему администратору, а нет у него почты — на ADMIN_EMAIL."""
    own = (people.get(who.login) or {}).get("email")
    return [own] if own else list(cfg.admin_emails)


@router.get("/api/admin/requests", dependencies=[Depends(admin_guard)])
async def admin_requests(who: users.Access = Depends(viewer)):
    """Заявки, ждущие решения, и состояние почты — для раздела «Пользователи»."""
    return {"requests": claims.pending(),
            "mail": {"on": cfg.mail_on, "why_off": mail.why_off(cfg), "admin_emails": list(cfg.admin_emails),
                     "probe_to": _probe_to(who), "warnings": mail.warnings(cfg) if cfg.mail_on else []}}


class ClaimApprove(BaseModel):
    id: str
    login: str      # администратор может поправить логин и ФИО перед одобрением
    name: str
    home_upload: bool = False       # разрешить ли сразу загрузку в личную папку


@router.post("/api/admin/requests/approve", dependencies=[Depends(admin_guard)])
async def admin_request_approve(req: ClaimApprove, who: users.Access = Depends(viewer)):
    """Открывает доступ по заявке: пользователь с личной папкой и без пароля, пароль он задаст
    сам по одноразовой ссылке. С почтой ссылка уходит ему письмом; без почты (в сервисе или
    в заявке) её получает администратор — link в ответе — и передаёт человеку сам; так же, если
    письмо не ушло (mail_error — ответ почтового сервера). Ключу с управлением ссылка не отдаётся
    (link_on_page): её выпускают на странице."""
    def approve(claim: dict, login: str) -> None:
        # учётная запись, отметка в заявке и строка журнала действий — одной транзакцией: одобренная
        # заявка без пользователя (или наоборот) не остаётся
        with base.write():
            people.put(login, req.name, "", False, claim["email"], True, req.home_upload)
            claims.decide(req.id, signup.APPROVED, who.login, login)
            jot("request_approve", who, login=login, **({"home_upload": True} if req.home_upload else {}))

    try:
        claim = claims.get(req.id)
        login = users.norm_login(req.login)
        if people.source(login) is not None:
            raise HTTPException(409, "такой логин уже занят — поправьте логин в заявке")
        await asyncio.to_thread(approve, claim, login)
    except signup.RequestError as e:
        raise HTTPException(e.status, e.detail)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    _make_home(login)
    log(f"заявка на доступ одобрена: {login} ({who.login})")
    sent = {"mail_error": await _send_invite(login, who, "invite")} if _by_letter(login) else {}
    if not sent or sent["mail_error"]:
        # письма нет или оно не ушло — ссылку человеку передаёт администратор. Ключу её не отдаём:
        # её выпустит администратор на странице, «Показать ссылку»
        sent |= ({"link": None, "link_on_page": True} if who.key
                 else {"link": await _show_link(login, who, "invite")})
    return {"ok": True, "login": login, "email": claim["email"], **sent,
            "requests": claims.pending(), "users": people.public()}


class ClaimReject(BaseModel):
    id: str


@router.post("/api/admin/requests/reject", dependencies=[Depends(admin_guard)])
async def admin_request_reject(req: ClaimReject, who: users.Access = Depends(viewer)):
    """Отклоняет заявку. Письмо человеку не шлётся; заявка остаётся в файле с пометкой."""
    try:
        claim = await asyncio.to_thread(claims.decide, req.id, signup.REJECTED, who.login)
    except signup.RequestError as e:
        raise HTTPException(e.status, e.detail)
    log(f"заявка на доступ отклонена: {claim['login']} ({who.login})")
    jot("request_reject", who, login=claim["login"])
    return {"ok": True, "requests": claims.pending()}


@router.post("/api/admin/mail/test", dependencies=[Depends(admin_guard)])
async def admin_mail_test(who: users.Access = Depends(viewer)):
    """Пробное письмо — проверить настройки почты. Ошибка почтового сервера отдаётся как есть."""
    _mail_needed()
    to = _probe_to(who)
    if not to:
        raise HTTPException(400, "некуда слать: укажите свою почту в учётной записи или ADMIN_EMAIL в .env")
    try:
        await _post(to, mail.probe(cfg))
    except mail.MailError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "to": to}


@router.post("/api/admin/check", dependencies=[Depends(admin_guard)])
async def admin_check():
    return {"ok": True}


@router.post("/api/admin/providers/check", dependencies=[Depends(admin_guard)])
async def admin_providers_check():
    """Кнопка «Проверить ключ»: принят ли ключ каждым настроенным поставщиком и есть ли у него
    текстовые модели из .env. Запросы бесплатные — записи не обрабатываются."""
    checks = await _check_keys()
    return {"ok": all(c["state"] == providers.CHECK_OK for c in checks), "checks": checks}


@router.post("/api/admin/files", dependencies=[Depends(admin_guard)])
async def admin_files():
    assert core.service is not None
    # обход папки уводим в поток: INBOX может быть сетевой шарой с архивом
    # записей, и обход в событийном цикле подвесил бы весь сервис
    return {"files": await asyncio.to_thread(core.service.inbox_files)}
