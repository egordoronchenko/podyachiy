"""Вход и права: перепроверка входа через домен, кто делает запрос (viewer, steward, person),
охрана адресов управления и расходов, тексты отказов, журнал действий, запись по правам."""
from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import errno
import time

from fastapi import Depends, HTTPException, Request

from .. import directory, external, keys, users
from . import core
from .common import _client, _make_home, _parent_of
from .core import (KEY_TOO_OFTEN, PASSWORD_TOO_OFTEN, WRONG_PASSWORD_DELAY_SEC, cfg, domain, key_fails_by_address, log,
                   password_fails_by_address, password_fails_by_login, people, trail)


# Вошедшего под учётной записью домена сервис перепроверяет в домене: вход действует 30 дней, а
# отключённый в домене или исключённый из группы доступа должен потерять доступ быстро. Не чаще
# раза в столько секунд на человека (LDAP_RECHECK_MIN, по умолчанию 15 минут) при его обращении
# к сервису — страницей или ключом доступа.
RECHECK_SEC = cfg.ldap_recheck_min * 60
# Контроллер домена не отвечает — людей сразу не выкидываем: верим последнему подтверждению
# домена, но не дольше этого срока. Четыре часа: перезагрузка контроллера или обрыв связи не
# останавливают работу, пока их чинят, а отключённый в домене человек при пропавшей связи с
# доменом не остаётся в сервисе дольше половины рабочего дня. Потом — отказ до ответа домена;
# локальные учётные записи и admin из .env входят всегда.
GRACE_SEC = 4 * 3600
RETRY_SEC = 60                  # контроллер не ответил — следующая попытка перепроверки не раньше
domain_seen: dict[str, tuple[float, str]] = {}      # логин → (когда перепроверен, отказ или "")
domain_asking: dict[str, asyncio.Future] = {}       # перепроверки, которые идут сейчас: одна на человека
domain_down = False             # последний запрос к домену остался без ответа — чтобы писать в лог раз, а не на каждый


def domain_off() -> str:
    """Почему вход под учётной записью домена выключен для всей установки; пусто — включён.
    Возможность корпоративного режима."""
    if cfg.personal:
        return "сервис работает в личном режиме: входа в нём нет"
    if not cfg.ldap_urls:
        return "не настроен: в .env не задан адрес контроллера домена (LDAP_URL)"
    return domain.problem()


def _secure(request: Request) -> bool:
    """Можно ли принять по этому запросу доменный пароль: он пришёл по HTTPS либо с самого
    сервера. Схему и адрес клиента за прокси uvicorn берёт из заголовков X-Forwarded-Proto и
    X-Forwarded-For — и только от прокси, чей адрес назван в FORWARDED_ALLOW_IPS (по умолчанию
    127.0.0.1); заголовок, присланный кем-то ещё, он не слушает, так что подделать «https»
    клиент не может. LDAP_ALLOW_HTTP — исключение для стенда."""
    host = request.client.host if request.client else ""
    return cfg.ldap_allow_http or request.url.scheme == "https" or host in ("127.0.0.1", "::1")


def _note_domain(fault: str = "") -> None:
    """Контроллеры перестали отвечать или ответили снова — в лог один раз, а не на каждый запрос."""
    global domain_down
    if bool(fault) != domain_down:
        domain_down = bool(fault)
        log(f"ВНИМАНИЕ: домен недоступен — {fault}. Вошедшие под учётной записью домена работают по последнему "
            f"подтверждению не дольше {GRACE_SEC // 3600} ч; локальные учётные записи и admin входят как обычно"
            if fault else "домен снова отвечает")


async def _settle(person: directory.Person) -> dict:
    """Домен подтвердил человека: завести его в сервисе или обновить ФИО, почту и роль.
    Заведение — тем же кодом, что у администратора и заявки (users.py), с личной папкой."""
    changed = await asyncio.to_thread(people.adopt, person.login, person.name, person.email, person.admin)
    if changed["new"]:
        _make_home(person.login)
        log(f"вход через домен: заведён пользователь {person.login} ({person.name})"
            + (", администратор сервиса по группе домена" if changed["admin"] else ""))
        trail.write("domain_user_new", person.login, name=person.name, admin=changed["admin"])
    elif changed["role"] is not None:
        log(f"вход через домен: {person.login} — " + ("стал администратором сервиса" if changed["role"]
            else "больше не администратор сервиса") + " по группе домена")
        trail.write("domain_role", person.login, admin=changed["role"])
    domain_seen[person.login] = (time.time(), "")
    return changed


async def _recheck(login: str) -> tuple[float, str]:
    """Спрашивает домен о вошедшем человеке: действует ли он и остался ли в группе доступа.
    Отдаёт, когда спросили и текст отказа (пусто — человек действует)."""
    now, off = time.time(), domain_off()
    if off:
        verdict = (now, f"вход под учётной записью домена выключен ({off}) — обратитесь к администратору сервиса")
    else:
        try:
            person = await asyncio.to_thread(domain.check, login)
        except directory.Denied as e:
            await asyncio.to_thread(people.unconfirm, login)
            verdict = (now, e.text)
        except directory.Unavailable as e:
            _note_domain(str(e))
            held = now - people.checked(login) <= GRACE_SEC
            # ответа нет: решение действует до следующей попытки, а она — через RETRY_SEC
            verdict = (now - RECHECK_SEC + RETRY_SEC, "" if held else (
                f"домен недоступен дольше {GRACE_SEC // 3600} ч — проверить учётную запись нельзя. Попробуйте позже "
                f"или обратитесь к администратору сервиса"))
        else:
            _note_domain()
            if people.source(login) == users.DOMAIN:        # пока спрашивали, запись могли убрать из сервиса
                await _settle(person)
            verdict = (now, "")
    if verdict[1] and verdict[1] != domain_seen.get(login, (0, ""))[1]:
        log(f"вход через домен: {login} больше не допускается — {verdict[1]}")
        trail.write("domain_recheck_denied", login, reason=verdict[1])
    domain_seen[login] = verdict
    return verdict


async def _confirmed(who: users.Access) -> users.Access | None:
    """Вошедший под учётной записью домена — если домен его недавно подтвердил или подтверждает
    сейчас; иначе отказ. Остальные возвращаются как есть. Права берутся заново: перепроверка
    могла снять роль администратора."""
    if not who.domain:
        return who
    seen = domain_seen.get(who.login)
    if seen is None or time.time() - seen[0] >= RECHECK_SEC:
        asking = domain_asking.get(who.login)
        if asking is None:
            asking = domain_asking[who.login] = asyncio.ensure_future(_recheck(who.login))
            asking.add_done_callback(lambda _: domain_asking.pop(who.login, None))
        seen = await asyncio.shield(asking)
    if seen[1]:
        raise HTTPException(401, seen[1])
    fresh = people.access(who.login)
    return fresh and dataclasses.replace(fresh, key=who.key, key_kind=who.key_kind)


async def _basic(request: Request) -> users.Access | None:
    """Внешние системы входят логином и паролем в каждом запросе (HTTP Basic). Только с локальной
    учётной записью: people.verify сверяет пароль с хешем в сервисе и в домен не ходит, а у доменной
    записи хеша нет. Доменный пароль в настройках клиента лежать не должен — такому человеку
    нужен ключ доступа."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        login, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (binascii.Error, ValueError):
        return None
    login, address = login.strip().lower(), _client(request)
    if password_fails_by_login.full(login) or password_fails_by_address.full(address):
        raise HTTPException(429, PASSWORD_TOO_OFTEN)
    # scrypt считается десятки миллисекунд — не в событийном цикле
    if not await asyncio.to_thread(people.verify, login, password):
        password_fails_by_login.allow(login)
        password_fails_by_address.allow(address)
        await asyncio.sleep(WRONG_PASSWORD_DELAY_SEC)
        return None
    return people.access(login)


async def _bearer(request: Request) -> users.Access | None:
    """ИИ-клиенты и программы входят ключом доступа: Authorization: Bearer stg_…"""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    address = _client(request)
    if key_fails_by_address.full(address):
        raise HTTPException(429, KEY_TOO_OFTEN)
    who = await asyncio.to_thread(people.by_key, header[7:].strip())
    if who is None:
        key_fails_by_address.allow(address)
        await asyncio.sleep(WRONG_PASSWORD_DELAY_SEC)
    return who


# В личном режиме человек один, и он хозяин всего: входить ему некуда и незачем.
OWNER = users.Access("owner", "", True)
BY_KEY_ONLY_READS = ("по ключу доступа это недоступно: ключом читают записи, загружают новые (если у "
                     "владельца ключа есть право загрузки), переименовывают и переносят их, отмечают "
                     "поручения выполненными, подписывают говорящих; удаление и отправку протокола на почту человек делает сам на странице "
                     "сервиса, а управлять сервисом можно только ключом с управлением")
# Обработка записи стоит денег у поставщика, поэтому всё, что к ней ведёт, — загрузка и
# пересборка протокола — под правом загрузки (users.Access.uploads), а не под чтением.
NO_UPLOAD = ("загружать записи в эту папку может тот, кому дано право загрузки, — "
             "обратитесь к администратору папки или сервиса")
NO_HOME_UPLOAD = ("загрузка в личную папку вам не разрешена: обработка записей платная, и право "
                  "загрузки выдаёт администратор сервиса — обратитесь к нему")
NO_UPLOAD_AT_ALL = ("загружать записи вам не разрешено: обработка записей платная, и право загрузки "
                    "выдаётся отдельно — обратитесь к администратору папки или сервиса")
UPLOAD_ELSEWHERE = "откройте папку, в которую вам можно загружать записи"
UPLOAD_OFF = "загрузка отключена: папка записей подключена только для чтения"
NO_REBUILD = ("пересобирать протокол может хозяин папки, у которого есть право загрузки в неё: пересборка "
              "платная — обратитесь к администратору папки или сервиса")


def _no_upload(who: users.Access, folder: str) -> str:
    """Отказ в загрузке словами: в личной папке право выдаёт администратор сервиса, в общей —
    ещё и администратор папки."""
    return NO_HOME_UPLOAD if who.home and external.inside(folder, who.home) else NO_UPLOAD


def _upload_denied(who: users.Access, folder: str) -> str:
    """Почему человеку нельзя загрузить запись в открытую на странице папку — подсказке у кнопки
    и ответу на перетаскивание. Пусто — можно. Общий список (folder пуст) не папка: из него
    запись уходит в личную папку, а если туда нельзя — человеку говорят открыть нужную."""
    if not core.upload_ready:
        return UPLOAD_OFF
    if who.uploads(folder) or (not folder and who.home and who.uploads(who.home)):
        return ""
    if not who.uploader:
        return NO_UPLOAD_AT_ALL
    return _no_upload(who, folder) if folder else UPLOAD_ELSEWHERE


PAGE_ONLY = ("это делается только на странице сервиса, даже ключом с управлением: ключи доступа, пароли "
             "и ссылки на пароль, «Выйти везде», роль администратора сервиса и почта пользователя, удаление записей и папок "
             "и возврат их из корзины, "
             "отправка протокола на почту, правка шаблонов и оформления, проверка подключения к домену и "
             "привязка к нему")


async def viewer(request: Request) -> users.Access:
    """Кто делает запрос и что ему открыто. Без входа сервис не отдаёт ничего —
    кроме личного режима, где вход не нужен вовсе. Вход — кукой со страницы, ключом
    доступа или логином и паролем в самом запросе."""
    if cfg.personal:
        return OWNER
    who = (people.by_token(request.cookies.get(users.COOKIE, ""))
           or await _bearer(request) or await _basic(request))
    # вошедшего под учётной записью домена — и его ключ доступа — подтверждает домен
    who = who and await _confirmed(who)
    if who is None:
        raise HTTPException(401, "нужно войти: нет или не подходит ключ доступа, логин или пароль"
                            + ("" if domain_off() else ". С учётной записью домена программы входят ключом "
                                                       "доступа, а не логином и паролем"))
    return who


# Три уровня доступа — три зависимости; каждый адрес стоит ровно на одной из них:
#   viewer  — человек и любой его ключ: чтение, загрузка, действия с записью в рамках прав
#             (загрузка и пересборка протокола — только с правом загрузки: они платные);
#   steward — человек и его ключ «с управлением»: пользователи, права, папки, настройки;
#   person  — только сам человек на странице: ключи, пароли, ссылки на пароль, удаление
#             записей и папок, отправка протокола на почту.
# Кто именно (администратор сервиса, хозяин папки) — решают права человека поверх уровня.
# Расходы стоят на costs_guard: это уровень steward, но не-администратору отказ своими словами.
# tests/test_web.py обходит все адреса обоими видами ключа и сверяет с явными списками:
# новый адрес не откроется ключу, пока его не впишут в список.

async def steward(who: users.Access = Depends(viewer)) -> users.Access:
    """Человек или его ключ «с управлением». Обычному ключу управление закрыто."""
    if who.key and who.key_kind != keys.MANAGE:
        raise HTTPException(403, BY_KEY_ONLY_READS)
    return who


async def person(who: users.Access = Depends(viewer)) -> users.Access:
    """Сам человек на странице, а не его ключ — какого бы вида ключ ни был."""
    if who.key:
        raise HTTPException(403, PAGE_ONLY if who.key_kind == keys.MANAGE else BY_KEY_ONLY_READS)
    return who


def _admin(who: users.Access) -> None:
    if not who.admin:
        raise HTTPException(403, "это может только администратор сервиса")


async def admin_guard(who: users.Access = Depends(steward)) -> None:
    """Управление сервисом — администратору: самому или его ключу «с управлением»."""
    _admin(who)


async def page_admin(who: users.Access = Depends(person)) -> None:
    """То из управления, что остаётся только на странице: администратору самому, без ключа."""
    _admin(who)


NO_COSTS = "расходы видит только администратор сервиса"


async def costs_guard(who: users.Access = Depends(viewer)) -> None:
    """Расходы и суммы — только администратору сервиса: самому или его ключу «с управлением».
    Остальным знать траты незачем — им отказ своими словами, каким бы входом они ни пришли;
    обычному ключу администратора — тот же отказ, что и в управлении сервисом."""
    if not who.admin:
        raise HTTPException(403, NO_COSTS)
    await steward(who)


def _via(who: users.Access) -> dict:
    """Пометка в журнале действий: сделано ключом доступа, а не самим человеком, — каким и какого вида."""
    return {"via": "key", "key": who.key, "key_kind": who.key_kind} if who.key else {}


def jot(kind: str, who: users.Access, **fields) -> None:
    """Действие человека — в журнал действий; пришло с ключа — с пометкой об этом."""
    trail.write(kind, who.login, **fields, **_via(who))


# Сбой файловой системы — отказ 500. Клиенту — что не получилось и почему, словами и без путей
# сервера: текст OSError их содержит («[Errno 13] Permission denied: '/data/inbox/…'»), а клиенту
# устройство сервера знать незачем. Целиком, с путём, — в журнал работы.
OS_FAULTS = {errno.EACCES: "нет прав на запись в папку", errno.EPERM: "нет прав на запись в папку",
             errno.EROFS: "папка подключена только для чтения", errno.ENOSPC: "на диске нет места",
             errno.ENOENT: "папки или файла уже нет", errno.ENOTEMPTY: "папка не пуста",
             errno.EIO: "сбой чтения или записи на диске"}
OS_FAULT_OTHER = "сбой файловой системы, подробности — в журнале работы сервиса"


def _os_fault(what: str, e: OSError) -> HTTPException:
    log(f"ВНИМАНИЕ: {what}: {e}")
    return HTTPException(500, f"{what}: {OS_FAULTS.get(e.errno, OS_FAULT_OTHER)}")


def _base(request: Request) -> str:
    """Адрес сервиса, как его открывают люди: PUBLIC_URL из .env, а не задан — адрес запроса."""
    return cfg.public_url or str(request.base_url).rstrip("/")


NO_RECORD = "файла нет в журнале обработки"


def _record(file: str, who: users.Access) -> dict:
    """Запись из журнала — если она есть и лежит в открытой человеку папке.
    Чужая запись неотличима от несуществующей."""
    assert core.service is not None
    record = core.service.ledger.get(file)
    if record is None or not who.sees(_parent_of(file)):
        raise HTTPException(404, NO_RECORD)
    return record
