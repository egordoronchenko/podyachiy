"""Страницы и API.

Вход обязателен всегда (кроме личного режима): без него сервис данных не отдаёт — только
оболочки страниц (они пусты, содержимое приходит запросами), проверку живости, «кто я»,
оформление страницы входа и навык для ИИ-клиентов. Человек получает то, что лежит в открытых
ему папках (см. users.py); управление сервисом — администраторам сервиса. Вход — подписанной
кукой со страницы, ключом доступа или логином и паролем в самом запросе; права проверяются
на сервере при каждом запросе.

По HTTP пароли и ключи идут по сети открыто: без прокси с сертификатом сервис годится
только для внутренней сети (см. install/README.md). Вход под учётной записью домена
(directory.py) по HTTP не принимается вовсе: доменный пароль открывает человеку не только
этот сервис.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import dataclasses
import datetime
import io
import os
import re
import shutil
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from . import (about, apidoc, backup, brand, costbook, db, dictionary, directory, export, external, folders, journal, keys, mail,
               marks, media, migrate, mindmap, names, notify, pipeline, providers, records, registry, search, settings,
               signup, speech, templates, upload, users)
from .config import Settings
from .ledger import Ledger
from .logbook import Logbook
from .watcher import Service

STATIC = Path(__file__).parent / "static"
SKILL = Path(__file__).parent / "skill"      # навык для ИИ-клиентов и тексты для модели — шаблонами
SKILL_NAME = "podyachiy"
WRONG_PASSWORD_DELAY_SEC = 1.0

# что можно открыть на странице: подпапка результата и расширение
RESULT_KINDS = {
    "summary": ("summary", ".md"),
    "transcript": ("transcript", ".txt"),
    "srt": ("transcript", ".srt"),
}

# Записи, обработанные прежней связкой Whisper с Gemini, лежат в папке merged.
# Читать их страница должна по-прежнему: архив совещаний никуда не делся,
# а переобрабатывать его заново — платить второй раз за уже сделанное.
LEGACY_DIRS = {"transcript": "merged", "srt": "merged"}
RESULT_MAX_BYTES = 4_000_000

cfg = Settings.from_env()
log = Logbook(cfg.outbox)
service: Service | None = None
# База сервиса — в папке состояния, а она должна быть подключена с хоста: иначе база живёт внутри
# контейнера и пропадает при его пересоздании — вместе с пользователями, расходами и журналом
# обработки, после чего все записи пошли бы в обработку заново, за деньги.
state_kept = db.kept(cfg.state_dir)
try:
    # при первом запуске после обновления сюда же входит перенос прежних файлов состояния в базу
    base = migrate.open_base(cfg, log, state_kept)
except db.Refusal as refusal:
    log(f"ОТКАЗ ЗАПУСКА: {refusal}")
    raise SystemExit(f"сервис не запущен: {refusal}")
people = users.Users(base, cfg.admin_password)
trail = journal.Journal(base, log)
claims = signup.Requests(base)
options = settings.Store(base, log)              # что администратор выбрал на странице
look = brand.Brand(base, log)                    # логотип и название организации (корпоративный режим)
words = dictionary.Store(base)                   # участники и сокращения папок и записей
noted = marks.Store(base)                        # отметки «выполнено» и сроки поручений
task_cache = registry.Cache()        # разобранные поручения протоколов — чтобы не читать сотни файлов на запрос
domain = directory.Directory(cfg, log)           # вход под учётной записью домена (корпоративный режим)

# Неудачные входы через домен ограничены по логину и по адресу клиента. Без этого страница входа
# сервиса стала бы инструментом подбора доменных паролей и — хуже — блокировки чужих учётных
# записей: контроллер блокирует запись после нескольких неверных паролей подряд (обычный порог —
# 5–10 за 15–30 минут). Сервис отправляет в домен не больше 4 неверных паролей на один логин
# за 15 минут — ниже обычного порога блокировки. Считаются только отказы; локальных учётных
# записей и admin предел не касается: они — запасной вход, и запереть его посторонний не должен.
DOMAIN_FAILS_SEC = 15 * 60
domain_fails_by_login = signup.Throttle(4, DOMAIN_FAILS_SEC)
domain_fails_by_address = signup.Throttle(30, DOMAIN_FAILS_SEC)
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

# Формы «Запросить доступ» и «Забыл пароль» открыты без входа — частоту ограничиваем по
# адресу клиента. Адрес берётся из соединения (request.client). Заголовкам X-Forwarded-For и
# X-Forwarded-Proto uvicorn верит только от адресов из FORWARDED_ALLOW_IPS; переменная не
# задана (в Dockerfile нет и ключа --forwarded-allow-ips) — только от 127.0.0.1. Поэтому
# заголовок от клиента подделать нельзя, но за обратным прокси на другом адресе все посетители
# приходят с адреса прокси и делят один предел на всех. То же в Docker Desktop (Windows,
# macOS): опубликованный порт отдаёт сервису адрес шлюза, а не клиента. Ставя сервис за
# прокси, задайте в окружении контейнера FORWARDED_ALLOW_IPS=<адрес прокси> (не «*») — uvicorn
# начнёт брать адрес клиента из заголовка этого прокси. От наплыва заявок при общем адресе
# страхует предел числа ожидающих (signup.PENDING_MAX).
claim_tries = signup.Throttle(30)        # любые отправки формы заявки с одного адреса, в час
claim_accepted = signup.Throttle(5)      # принятые заявки с одного адреса, в час
reset_by_address = signup.Throttle(10)   # запросы сброса пароля с одного адреса, в час
reset_by_login = signup.Throttle(3)      # запросы сброса на один логин или почту, в час
# Отправок протокола на почту в час на одного человека. Разослать протокол участникам после
# совещания — одна-две отправки; десяти хватает на рабочий день с несколькими совещаниями,
# а взломанная или забытая открытой учётная запись не превратит сервис в рассылку: за час
# с неё уйдёт не больше 10 отправок по 20 адресов (notify.RECIPIENTS_MAX), каждому адресу — письмо.
SENDS_PER_HOUR = 10
protocol_sends = signup.Throttle(SENDS_PER_HOUR)
NOTE_MAX = 1000                          # знаков в приписке к письму с протоколом
SEND_FORMATS = {"pdf": "PDF", "docx": "DOCX", "none": ""}      # вложение → как оно названо в письме
CLEAN_EVERY_SEC = 86400         # корзина и недогрузки чистятся при запуске и дальше раз в сутки
upload_ready = False     # входная папка доступна на запись — выясняется при запуске
upload_lock = asyncio.Lock()


def speech_model() -> str:
    """Модель распознавания для новых записей: выбранная на странице, а не выбрана — из .env."""
    chosen = options.speech_model()
    return chosen["value"] if chosen else cfg.model


def branded() -> bool:
    """Своё оформление — возможность корпоративного режима."""
    return not cfg.personal


def organization() -> str:
    """Название организации для писем и скачиваемых файлов; пусто — не задано или режим личный."""
    return look.name() if branded() else ""


mail.organization = organization

DEFAULT_PROMPTS = Path(__file__).resolve().parent.parent / "prompts_default"


def _seed_prompts() -> None:
    """Кладёт в рабочую папку шаблонов файлы, которых там нет.

    Свежая установка монтирует пустую папку, и без этого шаблоны пришлось бы
    доставать из образа руками. Существующие файлы не трогаются: их правят
    на хосте, и обновление образа не должно затирать правки.
    """
    if not DEFAULT_PROMPTS.is_dir():
        return
    try:
        cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
        added = []
        for src in sorted(DEFAULT_PROMPTS.iterdir()):
            target = cfg.prompts_dir / src.name
            if src.is_file() and not target.exists():
                shutil.copyfile(src, target)
                added.append(src.name)
    except OSError as e:
        log(f"ВНИМАНИЕ: не удалось разложить шаблоны в {cfg.prompts_dir}: {e}")
        return
    if added:
        log(f"шаблоны по умолчанию разложены в {cfg.prompts_dir}: {', '.join(added)}")


def _startup_checks() -> None:
    _seed_prompts()
    if cfg.inbox.resolve() == cfg.outbox.resolve():
        raise RuntimeError(
            "INBOX_DIR и OUTBOX_DIR указывают на одну папку — результаты попадут "
            "во вход и пойдут в обработку по кругу. Задайте разные папки.")
    if not cfg.api_key:
        log("ВНИМАНИЕ: ключ API не задан (OPENAI_API_KEY в .env) — обработка будет падать")
    main, backup = cfg.speech_provider, cfg.backup_provider
    log(f"основной поставщик: {main.title}, стоимость в {main.currency}; "
        + (f"запасной: {backup.title}, стоимость в {backup.currency}" if backup
           else "запасного нет (BACKUP_BASE_URL и BACKUP_API_KEY в .env не заданы)"))
    if backup and not backup.api_key:
        log("ВНИМАНИЕ: у запасного поставщика нет ключа (BACKUP_API_KEY в .env) — "
            "при сбое основного он откажет")
    if cfg.backup_api_key and not backup:
        log("ВНИМАНИЕ: BACKUP_API_KEY задан, а BACKUP_BASE_URL нет — запасного поставщика нет")
    if cfg.personal:
        log("режим личный (MODE=personal): входа нет, сервис открыт любому, кто до него достаёт — "
            "не открывайте его в сеть")
    elif not cfg.admin_password:
        log("ВНИМАНИЕ: ADMIN_PASSWORD не задан в .env — войти в сервис некому: "
            "администратор входит под логином admin с этим паролем")
    listed, problem = templates.types_listed(cfg.prompts_dir), templates.read_custom(cfg.prompts_dir)[1]
    if problem:
        log(f"ВНИМАНИЕ: {problem}")
    for key in listed:
        try:
            templates.load_template(cfg.prompts_dir, key)
        except Exception as e:
            log(f"ВНИМАНИЕ: шаблон {key} недоступен: {e}")
    own = [k for k, m in listed.items() if m.get("custom")]
    if own:
        log(f"свои типы шаблонов: {', '.join(own)}")
    try:
        templates.load_triage(cfg.prompts_dir)
    except Exception as e:
        log(f"ВНИМАНИЕ: задание на разбор записи недоступно ({e}) — "
            f"протоколы делаться не будут, пока файл не появится")
    if not cfg.inbox.is_dir():
        log(f"ВНИМАНИЕ: входная папка не найдена: {cfg.inbox}")
    if not cfg.personal:
        log(f"пользователей: {people.count()}")
    if cfg.ldap_urls:
        off = domain_off()
        if off:
            log(f"ВНИМАНИЕ: вход через домен выключен — {off}")
        else:
            log(f"вход через домен включён: {', '.join(cfg.ldap_urls)}, база {cfg.ldap_base_dn}; группа доступа — "
                f"{cfg.ldap_access_group or 'не задана (любой пользователь домена)'}, группа администраторов — "
                f"{cfg.ldap_admin_group or 'не задана'}; перепроверка раз в {cfg.ldap_recheck_min} мин; "
                + ("принимается и по HTTP — исключение LDAP_ALLOW_HTTP" if cfg.ldap_allow_http
                   else "принимается только по HTTPS"))
            for warning in domain.warnings():
                log(f"ВНИМАНИЕ!!! НЕБЕЗОПАСНО: {warning}")
    if cfg.mail_on:
        log(f"почта включена: {cfg.smtp_host}, защита {cfg.smtp_security}; ссылки в письмах ведут на {cfg.public_url}")
        for warning in mail.warnings(cfg):
            log(f"ВНИМАНИЕ!!! НЕБЕЗОПАСНО: {warning}")
        if not cfg.admin_emails:
            log("ВНИМАНИЕ: ADMIN_EMAIL не задан — о новых заявках на доступ письмо никому не уйдёт, "
                "их будет видно только счётчиком в меню")
        log("протокол со страницы записи можно отправить " + (
            f"только на адреса доменов: {', '.join(cfg.mail_domains)}" if cfg.mail_domains
            else "на любой адрес — чтобы ограничить, задайте в .env MAIL_DOMAINS (домен почты организации)"))
    elif mail.why_off(cfg):
        log(f"ВНИМАНИЕ: почта выключена — {mail.why_off(cfg)}")
    for warning in storage_warnings:
        log(f"ВНИМАНИЕ: {warning}")
    global upload_ready
    upload_ready = cfg.inbox.is_dir() and upload.writable(cfg.inbox)
    if not upload_ready:
        log("входная папка доступна только для чтения — загрузка записей через страницу "
            "отключена (в compose.yml уберите :ro у входной папки, чтобы включить)")
    _clean()


# На чём лежит папка состояния с базой: тип файловой системы, откуда подключена, вид (db.filesystem).
storage = db.filesystem(cfg.state_dir)
# О чём администратору надо знать про хранение — в журнал работы при запуске и на страницу «Обработка».
storage_warnings: list[str] = migrate.storage_warnings(cfg, storage)
clean_skipped: dict[str, str] = {}      # чья корзина → причина, по которой её не чистили в прошлый раз


def _clean() -> None:
    """Стирает отжившее: брошенные недогрузки и корзину старше срока хранения. Корзины входной
    папки и папки результатов чистятся каждая сама по себе: входная, подключённая только для
    чтения, не мешает чистить корзину результатов. Причина пропуска пишется в журнал работы
    одной строкой — когда появилась или сменилась, а не при каждой чистке."""
    if upload_ready:
        stale = upload.sweep(cfg.inbox)
        if stale:
            log(f"убраны брошенные недогрузки старше недели: {len(stale)}")
    gone, skipped = folders.purge(cfg, log)
    if gone:
        log(f"корзина: стёрто папок старше {folders.TRASH_DAYS} дней — {gone}")
    for whose, why in skipped.items():
        if clean_skipped.get(whose) != why:
            log(f"корзина {whose} не чистится: {why}")
    clean_skipped.clear()
    clean_skipped.update(skipped)


async def _clean_loop() -> None:
    """Сервис работает месяцами без перезапуска — чистки при запуске мало."""
    while True:
        await asyncio.sleep(CLEAN_EVERY_SEC)
        try:
            await asyncio.to_thread(_clean)
        except Exception as e:  # сетевая папка отвалилась — попробуем через сутки
            log(f"сбой чистки корзины и недогрузок (продолжаю): {e!r}")


def _key_targets() -> list[tuple[str, providers.Provider, tuple[str, ...]]]:
    """Чьи ключи проверять: основной поставщик, запасной и свой адрес текстовых этапов, если
    заданы. При каждом — текстовые модели, которые он должен знать: у текста со своим адресом
    основной и запасной их не обслуживают."""
    text_models = tuple(dict.fromkeys((cfg.summary_model, *cfg.actions_models)))
    own_text = cfg.summary_base_url != cfg.base_url
    found = [("основной поставщик", cfg.speech_provider, () if own_text else text_models)]
    if cfg.backup_provider:
        found.append(("запасной поставщик", cfg.backup_provider, () if own_text else text_models))
    if own_text:
        found.append(("текстовые этапы", cfg.text_provider, text_models))
    return found


async def _check_keys() -> list[dict]:
    """Проверка ключей поставщиков бесплатными запросами (providers.check) — кнопке «Проверить
    ключ» и запуску сервиса. Ключей в ответе нет."""
    targets = _key_targets()
    verdicts = await asyncio.gather(*(providers.check(provider, models) for _, provider, models in targets))
    return [{"role": role, "title": provider.title, **verdict}
            for (role, provider, _), verdict in zip(targets, verdicts)]


async def _check_keys_on_start() -> None:
    """Один раз при запуске: неверный ключ иначе виден только на первой записи. Итог — строкой
    в журнал работы; отказ — предупреждение, сервис при этом работает."""
    for found in await _check_keys():
        line = f"проверка ключа: {found['role']} {found['title']} — {found['text']}"
        log(line if found["state"] == providers.CHECK_OK
            else f"ВНИМАНИЕ: {line}" + (f". Что делать: {found['hint']}" if found["hint"] else ""))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service
    _startup_checks()
    log(f"podyachiy {about.version() or 'без номера версии'} запущен: шаблон протокола выбирает разбор записи, "
        f"модели {speech_model()} + {cfg.summary_model}"
        + ("" if speech_model() == cfg.model
           else f" (распознавание выбрано на странице; в .env — {cfg.model})"))
    if cfg.summary_base_url != cfg.base_url:
        log(f"текстовые этапы идут по своему адресу: {cfg.summary_base_url} "
            f"(поручения: {', '.join(cfg.actions_models)}) — запасной поставщик на них не действует")
    service = Service(cfg=cfg, log=log, ledger=Ledger(base, log), journal=trail,
                      speech_model=speech_model, tell=_tell, words=words.effective, marked=noted)
    log(f"обработчиков: {cfg.workers} — столько записей идёт одновременно (WORKERS в .env)")
    tasks = [asyncio.create_task(service.scan_loop()),
             *(asyncio.create_task(service.worker_loop()) for _ in range(cfg.workers)),
             asyncio.create_task(_check_keys_on_start())]     # не ждём: запуск не зависит от поставщика
    tasks.append(asyncio.create_task(_clean_loop()))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        log("podyachiy остановлен")
        base.close()            # база дописывает журнал WAL в основной файл: после остановки она — один файл


# Описание API от FastAPI (/docs, /redoc, /openapi.json) выключено: оно показывало без входа все
# адреса сервиса, включая внутренние, а его страницы тянут сценарии из интернета. Внешний API
# описан в apidoc.py: GET /api/v1/openapi.json и страница /api — без входа, только /api/v1 и
# управление, открытое ключу; GET /api/v1 — то же текстом для модели, по входу.
app = FastAPI(title="Подьячий", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# Отказы сервиса — по-русски. Два из них пишет не наш код, и оба про тело запроса: оно не в
# UTF-8 (400) или в нём не JSON (422). Так бывает у curl из PowerShell: кириллица уходит в
# кодировке консоли, а кавычки внутри строки теряются.
BODY_NOT_PARSED = ("тело запроса не разобрано: ожидается JSON в кодировке UTF-8. Из PowerShell отправляйте "
                   "его из файла, сохранённого в UTF-8: curl.exe --data-binary \"@body.json\"")


@app.exception_handler(StarletteHTTPException)
async def body_unreadable(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 400 and exc.detail == "There was an error parsing the body":
        exc = HTTPException(400, BODY_NOT_PARSED)
    return await http_exception_handler(request, exc)


# Третий отказ не нашего кода — проверка параметров FastAPI (422): нет обязательного параметра
# или он не того типа. Её перечень loc/msg по-английски заменяем одной строкой по-русски, в том
# же виде, что у остальных отказов: {"detail": "не указан параметр file"}.
WRONG_KINDS = (("int", "должен быть целым числом"), ("float", "должен быть числом"),
               ("bool", "должен быть true или false"), ("string", "должен быть строкой"),
               ("list", "должен быть списком"), ("dict", "должен быть объектом"),
               ("model_attributes", "должен быть объектом"))
NO_PARAM, NO_FIELD = "не указан параметр ", "не указано поле "
NO_BODY = "нет тела запроса: ожидается JSON"
BODY_NOT_OBJECT = "тело запроса должно быть объектом JSON"


def _invalid(error: dict) -> str:
    """Одна ошибка проверки параметров словами: какой параметр или поле и что с ним не так."""
    where, *path = error["loc"]
    kind = error["type"]
    if where == "body" and not path:
        return NO_BODY if kind == "missing" else BODY_NOT_OBJECT
    name = ".".join(str(part) for part in path)
    if kind == "missing":
        return (NO_FIELD if where == "body" else NO_PARAM) + name
    what = next((text for prefix, text in WRONG_KINDS if kind.startswith(prefix)), "задан неверно")
    if where == "body":
        return f"поле {name} {what.replace('должен', 'должно').replace('задан', 'задано')}"
    return f"параметр {name} {what}"


@app.exception_handler(RequestValidationError)
async def params_refused(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    if any(e.get("type") == "json_invalid" for e in errors):
        return await http_exception_handler(request, HTTPException(422, BODY_NOT_PARSED))
    return await http_exception_handler(request, HTTPException(422, "; ".join(dict.fromkeys(map(_invalid, errors)))))


@app.middleware("http")
async def revalidate_pages(request, call_next):
    """Страницы и их скрипты браузер обязан сверять с сервером при каждом
    открытии. Без этого после обновления сервиса он часами показывает старый
    common.js из кэша — и новых кнопок у человека просто нет. Сверка дешёвая:
    файл не менялся — сервер отвечает 304 без тела."""
    response = await call_next(request)
    path = request.url.path
    if (path in ("/", "/admin", "/tasks", "/login", "/help", "/install-guide", "/about", "/api", "/set-password")
            or path.startswith(("/static/", "/skill"))):
        response.headers["Cache-Control"] = "no-cache"
    return response


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
    # scrypt считается десятки миллисекунд — не в событийном цикле
    if not await asyncio.to_thread(people.verify, login, password):
        await asyncio.sleep(WRONG_PASSWORD_DELAY_SEC)
        return None
    return people.access(login.strip().lower())


async def _bearer(request: Request) -> users.Access | None:
    """ИИ-клиенты и программы входят ключом доступа: Authorization: Bearer stg_…"""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    who = await asyncio.to_thread(people.by_key, header[7:].strip())
    if who is None:
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
NO_REBUILD = ("пересобирать протокол может тот, кому в этой папке дано право загрузки: пересборка "
              "платная — обратитесь к администратору папки или сервиса")


def _no_upload(who: users.Access, folder: str) -> str:
    """Отказ в загрузке словами: в личной папке право выдаёт администратор сервиса, в общей —
    ещё и администратор папки."""
    return NO_HOME_UPLOAD if who.home and external.inside(folder, who.home) else NO_UPLOAD


def _upload_denied(who: users.Access, folder: str) -> str:
    """Почему человеку нельзя загрузить запись в открытую на странице папку — подсказке у кнопки
    и ответу на перетаскивание. Пусто — можно. Общий список (folder пуст) не папка: из него
    запись уходит в личную папку, а если туда нельзя — человеку говорят открыть нужную."""
    if not upload_ready:
        return UPLOAD_OFF
    if who.uploads(folder) or (not folder and who.home and who.uploads(who.home)):
        return ""
    if not who.uploader:
        return NO_UPLOAD_AT_ALL
    return _no_upload(who, folder) if folder else UPLOAD_ELSEWHERE


PAGE_ONLY = ("это делается только на странице сервиса, даже ключом с управлением: ключи доступа, пароли "
             "и ссылки на пароль, удаление записей и папок, отправка протокола на почту")


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


def _base(request: Request) -> str:
    """Адрес сервиса, как его открывают люди: PUBLIC_URL из .env, а не задан — адрес запроса."""
    return cfg.public_url or str(request.base_url).rstrip("/")


def _record(file: str, who: users.Access) -> dict:
    """Запись из журнала — если она есть и лежит в открытой человеку папке.
    Чужая запись неотличима от несуществующей."""
    assert service is not None
    record = service.ledger.get(file)
    if record is None or not who.sees(_parent_of(file)):
        raise HTTPException(404, "файла нет в журнале обработки")
    return record


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    """Главная — обзор готовых результатов по папкам. Только чтение."""
    return FileResponse(STATIC / "index.html")


def _entrance(page: str) -> Response:
    """Страница входа или задания пароля. В личном режиме входа нет — человек попадает на главную."""
    return RedirectResponse("/", status_code=302) if cfg.personal else FileResponse(STATIC / page)


@app.get("/login")
def login_page():
    """Вход, а с неё же — заявка на доступ и «Забыл пароль»."""
    return _entrance("login.html")


@app.get("/set-password")
def set_password_page():
    """Страница, на которую ведёт ссылка из письма: человек задаёт себе пароль."""
    return _entrance("set-password.html")


@app.get("/help")
def help_page():
    """Справка для тех, кто пользуется сервисом: живёт в образе, чтобы за ней
    не нужно было ходить в репозиторий."""
    return FileResponse(STATIC / "help.html")


@app.get("/install-guide")
def install_guide_page():
    """Инструкция по установке и сопровождению — администратору, там же, где справка. Файл собран
    из install/README.md (tools/docs_build.py) и самодостаточен: данных установки в нём нет."""
    return FileResponse(STATIC / "install-guide.html")


@app.get("/about")
def about_page():
    """О сервисе: версия, автор, куда писать. Страница пуста: сведения приходят запросом по входу."""
    return FileResponse(STATIC / "about.html")


@app.get("/api")
def api_page():
    """Раздел «API»: методы для программ и ИИ-клиентов. Страница строит себя из
    /api/v1/openapi.json и, как он, открыта без входа: данных в ней нет."""
    return FileResponse(STATIC / "api.html")


@app.get("/brand/{version}/{name}")
def brand_file(version: str, name: str):
    """Логотип организации: полный, малый для меню, значок вкладки. Без входа — его показывает
    и страница входа. В адресе метка версии: файл по этому адресу не меняется никогда, поэтому
    браузеру разрешено хранить его год; новый логотип получает новый адрес. Отдаётся только
    PNG, который сервис собрал сам (brand.py), с запретом угадывать тип содержимого."""
    path = look.file(version, name) if branded() else None
    if path is None or not path.is_file():
        raise HTTPException(404, "такого файла нет")
    return FileResponse(path, media_type="image/png", headers={
        "X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/tasks")
def tasks_page():
    """Поручения всех протоколов одной таблицей. Страница пуста: строки приходят запросом."""
    return FileResponse(STATIC / "tasks.html")


@app.get("/admin")
def admin_page():
    """Состояние сервиса и управление — администратору. Сама страница пуста:
    всё, что на ней показано, приходит запросами, которые сервер проверяет."""
    return FileResponse(STATIC / "admin.html")


@app.get("/api/status")
def status():
    """Проверка, что сервис жив, — для Docker. Без входа, поэтому без данных: только номер версии."""
    return {"ok": True, "version": about.version()}


def _brand_public() -> dict:
    """Оформление для любой страницы, включая страницу входа: название организации и адреса
    картинок. Пусто — стандартное: наш логотип и надпись."""
    if not branded():
        return {"name": "", "logo": None}
    seen = look.view()
    return {"name": seen["name"],
            "logo": {kind: f"/brand/{seen['logo']}/{file}" for kind, file in
                     (("full", "logo.png"), ("small", "logo-small.png"), ("icon", "favicon.png"))}
            if seen["logo"] else None}


@app.get("/api/brand")
def brand_public():
    """Без входа: это то, что человек и так видит на странице входа."""
    return _brand_public()


@app.get("/api/about")
def about_service(who: users.Access = Depends(viewer)):
    """О сервисе — всем вошедшим: версия, режим, автор и как с ним связаться. Из настроек
    здесь только то, что нужно в обращении к автору: режим и модели. Адресов, логинов,
    названий записей и ключей нет."""
    return {"name": about.NAME, "tagline": about.TAGLINE, "version": about.version(), "build": about.build(),
            **about.contacts(),
            "mode": "personal" if cfg.personal else "corporate",
            "speech_model": speech_model(), "summary_model": cfg.summary_model,
            "documents": {name: about.document(name) is not None for name in about.DOCUMENTS}}


@app.get("/api/about/{name}")
def about_document(name: str, who: users.Access = Depends(viewer)):
    """Лицензия сервиса (license) и перечень сторонних компонентов с их лицензиями (third-party) —
    тексты, которые едут внутри образа."""
    if name not in about.DOCUMENTS:
        raise HTTPException(404, "такого документа нет: license или third-party")
    found = about.document(name)
    if found is None:
        raise HTTPException(404, "этого файла в образе нет — он кладётся при сборке образа")
    text, kind = found
    return Response(text, media_type=f"{kind}; charset=utf-8", headers={"X-Content-Type-Options": "nosniff"})


@app.get("/api/admin/status", dependencies=[Depends(admin_guard)])
def admin_status(log_from: int = 0, history_version: int = -1):
    """Сводка для страницы администрирования. Ключа API и путей папок на диске сервера здесь
    нет и быть не может.

    В ней имена всех записей, журнал работы и расходы, поэтому она только администратору.

    Страница опрашивает этот метод каждые несколько секунд, поэтому лог и
    история отдаются приростом: клиент сообщает, что у него уже есть, и
    получает только новое. Иначе каждая открытая вкладка тянула бы всю
    историю обработки и весь хвост лога по кругу.
    """
    assert service is not None
    log_start, log_lines = log.tail_from(max(log_from, 0))
    fresh_history = service.ledger.version != history_version
    totals, history = service.ledger.totals(), service.ledger.history() if fresh_history else None
    if history is not None:
        # output — папка результатов записи на диске сервера: путей диска в ответе нет
        history = [{k: v for k, v in row.items() if k != "output"} for row in history]
    return {
        "config": {
            "scan_interval_sec": cfg.scan_interval_sec,
            "stable_sec": cfg.stable_sec,
            "stable_samples": cfg.stable_samples,
            "workers": cfg.workers,
            "speech_model": speech_model(),
            "summary_model": cfg.summary_model,
            "language": cfg.language,
            "key_present": bool(cfg.api_key),
            "admin_enabled": bool(cfg.admin_password),
            "mode": "расшифровка с разделением говорящих одним вызовом",
        },
        # база и резервная копия: версия схемы, размер, на чём лежит папка состояния (тип файловой
        # системы без путей), предупреждения и когда команда копии отработала последний раз
        "storage": {"schema": base.version, "bytes": base.size(), "fs": storage["type"], "kind": storage["kind"],
                    "warnings": storage_warnings, "backup": backup.last(base)},
        "templates": {k: v["title"] for k, v in templates.types(cfg.prompts_dir).items()},
        "speech": _speech_choice(),
        "providers": _providers(),
        "now": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        **service.snapshot(),
        "totals": totals,
        "history": history,
        "history_version": service.ledger.version,
        "log": log_lines,
        "log_start": log_start,
        "log_total": log.total,
    }


def _speech_choice() -> dict:
    """Модель распознавания для новых записей: что действует, откуда взято и из чего выбирать."""
    return {"models": speech.models(), "current": speech_model(), "env": cfg.model,
            "chosen": options.speech_model()}


def _providers() -> dict:
    """Какие поставщики настроены — без ключей — и когда запрос последний раз уходил запасному."""
    backup, text = cfg.backup_provider, cfg.text_provider
    return {"main": cfg.speech_provider.public(),
            "backup": backup.public() if backup else None,
            # у текстовых этапов свой адрес — тогда запасной их не подхватывает
            "text": text.public() if cfg.summary_base_url != cfg.base_url else None,
            **providers.state()}


def _parent_of(rel: str) -> str:
    p = PurePosixPath(rel).parent.as_posix()
    return "" if p == "." else p


FLAT_LIMIT = 300     # «все записи» — это недавние: глубже ищут поиском или идут в папку

# какие результаты имеет смысл открывать при таком исходе обработки
KINDS_BY_STATUS = {
    "ok": ["summary", "transcript", "srt"],
    "no_summary": ["transcript", "srt"],
    "no_audio": [],
    "error": [],
}


@app.get("/api/browse")
def browse(path: str = "", flat: bool = False, who: users.Access = Depends(viewer)):
    """Обход результатов по подпапкам — так же, как записи лежат во входной папке.

    Дерево строится по журналу обработки, а не обходом диска: журнал и так
    знает про каждую запись, а выходная папка может оказаться сетевой,
    и ходить по ней на каждый запрос страницы незачем.

    flat — «все записи»: в списке всё, что человеку открыто, из всех папок сразу.
    """
    assert service is not None
    rel = "" if flat else path.strip("/")
    prefix = f"{rel}/" if rel else ""
    dirs: dict[str, dict] = {}
    files: list[dict] = []

    for key, rec in list(service.ledger.records.items()):
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
                "error": rec.get("error"),
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
    running = [job for job in list(service.running) if who.sees(_parent_of(job["file"]))]
    in_work = {job["file"] for job in running}
    pending = []
    for path, watch in list(service.pending.items()):
        if not who.sees(_parent_of(path)):
            continue
        pending.append({"file": path, "name": PurePosixPath(path).name,
                        "dir": _parent_of(path), "state": "copying",
                        "size": watch.size, "quiet_sec": round(watch.quiet_sec()),
                        "stable_sec": cfg.stable_sec})
    for item in list(service.queue):
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
                    and not (service._under_outbox(child))):
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
        "upload": {"enabled": upload_ready and who.uploads(rel), "max_bytes": cfg.upload_max_bytes,
                   "exts": sorted(media.MEDIA_EXTS), "anywhere": upload_ready and who.uploader,
                   "denied": _upload_denied(who, rel)},
        "mkdir": upload_ready and who.plants(rel),
        # хозяин папки правит её словарь и словари её записей (в том числе при загрузке)
        "owns": who.owns(rel),
        # из общего списка запись уходит в личную папку — если человеку разрешено в неё загружать
        "home_upload": upload_ready and bool(who.home) and who.uploads(who.home),
        # раздавать доступ можно к папке, не к корню: корень открывает только администратор
        "manage": not cfg.personal and rel != "" and who.rules(rel),
        # running — сколько записей в работе из тех, что человеку видны
        "service": {"running": len(running),
                    "queue": len(service.queue), "watching": len(service.pending)},
    }


def _upload_rel(folder: str, name: str, who: users.Access) -> str:
    if not upload_ready:
        raise HTTPException(503, "загрузка отключена: входная папка доступна только для чтения")
    try:
        rel = upload.target_rel(folder, name, cfg.outbox_inside_inbox)
    except upload.UploadError as e:
        raise HTTPException(e.status, e.detail)
    if not who.sees(_parent_of(rel)):
        raise HTTPException(403, "эта папка вам не открыта")
    if not who.uploads(_parent_of(rel)):
        raise HTTPException(403, _no_upload(who, _parent_of(rel)))
    return rel


class FolderRequest(BaseModel):
    dir: str


@app.post("/api/folder")
async def make_folder(req: FolderRequest, who: users.Access = Depends(viewer)):
    """Новая подпапка. Человек заводит их в своей личной папке, администратор — где угодно."""
    if not upload_ready:
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
    try:
        await asyncio.to_thread((cfg.inbox / rel).mkdir, parents=True)
    except OSError as e:
        raise HTTPException(500, f"не удалось создать папку: {e}")
    log(f"создана папка: {rel}" + (f" ({who.login})" if who.login else ""))
    jot("folder_create", who, folder=rel)
    return {"ok": True, "path": rel}


@app.get("/api/upload")
async def upload_state(name: str, size: int, dir: str = "", who: users.Access = Depends(viewer)):
    """Сколько байт записи уже принято: с этого места страница продолжает после обрыва."""
    rel = _upload_rel(dir, name, who)
    try:
        have = await asyncio.to_thread(upload.received, cfg.inbox, rel, size, cfg.upload_max_bytes)
    except upload.UploadError as e:
        raise HTTPException(e.status, e.detail)
    return {"offset": have, "chunk": upload.CHUNK_BYTES}


@app.put("/api/upload")
async def upload_chunk(request: Request, name: str, size: int, offset: int, dir: str = "",
                       who: users.Access = Depends(viewer)):
    """Очередная часть записи — телом запроса, как есть."""
    rel = _upload_rel(dir, name, who)
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
                upload.append, cfg.inbox, rel, size, offset, bytes(data))
    except upload.UploadError as e:
        raise HTTPException(e.status, e.detail)
    except OSError as e:
        raise HTTPException(500, f"не удалось записать файл: {e}")
    if done:
        log(f"загружена через страницу: {rel} ({size / 1048576:.1f} МБ)"
            + (f" ({who.login})" if who.login else ""))
        jot("upload", who, file=rel, size=size)
    return {"offset": have, "done": done, "file": rel}


@app.delete("/api/upload")
async def upload_cancel(name: str, size: int, dir: str = "", who: users.Access = Depends(viewer)):
    """Человек отменил загрузку: принятое больше не нужно."""
    rel = _upload_rel(dir, name, who)
    await asyncio.to_thread(upload.cancel, cfg.inbox, rel, size)
    return {"ok": True}


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
    return apidoc.build(app, _base(request), cfg.personal, f"{cfg.upload_max_bytes / 2**30:.0f}", about.version())


def _markdown(text: str) -> Response:
    return Response(text, media_type="text/markdown; charset=utf-8")


def _visible(who: users.Access) -> list[dict]:
    assert service is not None
    return [r for r in list(service.ledger.records.values()) if who.sees(_parent_of(r["file"]))]


def _in_progress(who: users.Access) -> dict[str, dict]:
    """Записи, которые ещё не готовы или пересобираются: путь → что с ней сейчас."""
    assert service is not None
    found: dict[str, dict] = {}
    for path, watch in list(service.pending.items()):
        found[path] = {"state": "waiting", "quiet_sec": round(watch.quiet_sec()), "needed_sec": cfg.stable_sec}
    for place, item in enumerate(list(service.queue), 1):
        found[item.path] = {"state": "queued", "position": place}
    for job in list(service.running):
        found[job["file"]] = {"state": "processing", "step": job["step"], "started_at": job["started_at"],
                              "parts_done": job["chunks_done"], "parts_total": job["chunks_total"]}
    return {path: state for path, state in found.items() if who.sees(_parent_of(path))}


def _folder_arg(value: str, who: users.Access) -> str:
    """Папка из параметра запроса: пусто — корень; иначе проверенный путь внутри входной папки.
    Папки нет или она человеку не открыта — один и тот же отказ 404: существование чужой папки
    не раскрывается. Пустая папка, которая есть, — не отказ. Папка «есть», если она открыта
    человеку и лежит на диске либо названа в его правах, или если в ней или глубже лежит
    открытая ему запись или папка: до той нужно как-то дойти от корня."""
    assert service is not None
    if not users.norm_folder(value):
        return ""
    try:
        folder = upload.folder_rel(value, cfg.outbox_inside_inbox)
    except upload.UploadError as e:
        raise HTTPException(e.status, e.detail)
    on_disk = who.sees(folder) and ((cfg.inbox / folder).is_dir() or (cfg.outbox / folder).is_dir())
    paths = (list(service.ledger.records) + list(service.pending) + [i.path for i in list(service.queue)]
             + [j["file"] for j in list(service.running)])
    if not (on_disk or any(external.inside(f, folder) for f in who.roots)
            or any(external.inside(_parent_of(f), folder) and who.sees(_parent_of(f)) for f in paths)):
        raise HTTPException(404, folders.NO_FOLDER)
    return folder


def _protocol(file: str, record: dict) -> str | None:
    """Протокол записи в Markdown, без служебных пометок сервиса."""
    path = pipeline.protocol_path(cfg, file, record)
    if not path.is_file():
        return None
    return re.sub(r"<!--.*?-->\s*", "", path.read_text(encoding="utf-8"), flags=re.S).strip() + "\n"


def _disk_folders(root: str, depth: int) -> list[str]:
    """Папки на диске внутри root: его подпапки и ещё depth уровней под ними."""
    assert service is not None
    found = []
    for path, dirs, _ in os.walk(cfg.inbox / root):
        rel = Path(path).relative_to(cfg.inbox).as_posix()
        rel = "" if rel == "." else rel
        level = 0 if rel == root else rel[len(root):].strip("/").count("/") + 1
        dirs[:] = [d for d in dirs if not d.startswith(".")] if level <= depth else []
        found += [f"{rel}/{d}" if rel else d for d in dirs]
    return [f for f in found if not service._under_outbox(f)]


def _tree(who: users.Access, root: str, depth: int) -> dict:
    done, progress = _visible(who), _in_progress(who)
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
               "can_upload": upload_ready and bool(path) and who.uploads(path)}
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


@app.get("/api/v1")
def v1_about(request: Request, who: users.Access = Depends(viewer)):
    """Описание адресов — Markdown, чтобы модель прочитала его одним запросом. Методы в него
    подставляются из apidoc.py — того же источника, что у openapi.json и страницы /api."""
    return _markdown(_skill_text("API.md", request).replace("{{API}}", apidoc.markdown(_api_spec(request))))


@app.get("/api/v1/openapi.json")
def v1_openapi(request: Request):
    """Машинное описание внешнего API (OpenAPI 3.1). Без входа, как навык: в нём только адрес
    сервиса и описание запросов — ни данных, ни настроек установки."""
    return _api_spec(request)


@app.get("/api/v1/whoami")
def v1_whoami(request: Request, who: users.Access = Depends(viewer)):
    """Проверка подключения: чьими глазами клиент видит сервис. Заодно — какая версия навыка
    у сервиса сейчас: клиент сверяет её со своей и обновляет навык сам."""
    return {"login": who.login, "name": who.name, "admin": who.admin,
            "via": "key" if who.key else "login", "key": who.key or None,
            "manage": bool(who.admin and (not who.key or who.key_kind == keys.MANAGE)),
            "service": _base(request), "version": about.version(), "upload": upload_ready,
            # есть ли у человека хоть одна папка, куда ему дано загружать (сами папки — /api/v1/folders)
            "can_upload": upload_ready and who.uploader,
            "skill_version": _skill_version(), "skill_url": _base(request) + "/skill/SKILL.md",
            "skill_changes": _base(request) + "/skill/CHANGELOG.md"}


@app.get("/api/v1/tree")
async def v1_tree(dir: str = "", depth: int = TREE_DEPTH, who: users.Access = Depends(viewer)):
    """Папки с записями, которые видит человек. depth — сколько уровней подпапок раскрыть:
    0 — только записи самой папки и имена подпапок."""
    root = _folder_arg(dir, who)
    # входная папка может быть сетевой: обход уводим из событийного цикла
    return await asyncio.to_thread(_tree, who, root, max(0, min(depth, FOLDER_DEPTH)))


@app.get("/api/v1/records")
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
            "records": external.listing(_visible(who), moment, _folder_arg(dir, who), _base(request))}


@app.get("/api/v1/record")
def v1_record(request: Request, file: str, transcript: bool = False, who: users.Access = Depends(viewer)):
    """Одна запись — сведения, протокол и поручения полями.

    Расшифровка отдаётся только по просьбе (transcript=true): она на порядок
    больше протокола и нужна редко.
    """
    record = _record(file, who)
    protocol = _protocol(file, record)
    out = {**external.brief(record, _base(request)), "protocol": protocol,
           "tasks": _marked(file, record, protocol) if protocol else [],
           "can_mark": who.owns(_parent_of(file))}
    out["links"]["audio"] = (_base(request) + "/api/audio?" + urlencode({"file": file}, quote_via=quote)
                             if _audio_path(file, record) else None)
    if transcript:
        stem = pipeline.stem_of(cfg.inbox, file, record)
        lines = names.view(cfg.outbox / Path(file).parent / stem, stem)["lines"]
        out["transcript"] = names.display_text(lines) if lines else None
    return out


@app.get("/api/v1/record.html")
def v1_record_html(file: str, who: users.Access = Depends(viewer)):
    """Карта, протокол и расшифровка одним файлом — тот же, что кнопка «Скачать HTML» на странице."""
    return _map(file, True, who)


@app.get("/api/v1/record.docx")
async def v1_record_docx(file: str, who: users.Access = Depends(viewer)):
    """Протокол документом Word — тот же файл, что «Скачать → DOCX» на странице."""
    return await _export(file, "docx", who)


@app.get("/api/v1/record.pdf")
async def v1_record_pdf(file: str, who: users.Access = Depends(viewer)):
    """Протокол в PDF — тот же файл, что «Скачать → PDF» на странице."""
    return await _export(file, "pdf", who)


def _excerpt(piece: dict) -> str:
    """Выдержка одной строкой, найденное слово выделено. Поиск отдаёт куски без пробелов по краям —
    возвращаем пробел там, где слово не примыкает к скобке, кавычке или знаку препинания."""
    before, after = piece["before"], piece["after"]
    return (before + (" " if before and before[-1] not in "(«[\"'" else "") + f'**{piece["match"]}**'
            + (" " if after[:1].isalnum() or after[:1] in "(«[—–-" else "") + after)


@app.get("/api/v1/search")
async def v1_search(q: str = "", dir: str = "", who: users.Access = Depends(viewer)):
    """Поиск по протоколам и расшифровкам: записи, где есть все слова запроса, с выдержками."""
    assert service is not None
    if len(q.strip()) < search.MIN_QUERY:
        raise HTTPException(400, "слишком короткий запрос: нужно хотя бы два знака")
    root = _folder_arg(dir, who)
    records = [r for r in service.ledger.history(limit=search.MAX_SCAN)
               if who.sees(_parent_of(r["file"])) and external.inside(_parent_of(r["file"]), root)]
    found = await asyncio.to_thread(search.find, records, cfg.outbox, q)
    ledger = {r["file"]: r for r in records}
    return {"query": q, "scanned": found["scanned"], "more": found.get("more", 0),
            "found": [{"file": f["file"], "title": ledger[f["file"]].get("title"),
                       "date": external.day_of(ledger[f["file"]]) or None, "hits": f["hits"],
                       "where": [KIND_NAMES[k] for k in f["kinds"]],
                       "excerpts": [_excerpt(s) for s in f["snippets"]]}
                      for f in found["files"]]}


def _marked(file: str, record: dict, protocol: str) -> list[dict]:
    """Поручения протокола с отметками «выполнено» и сроками, которые поставил человек."""
    found = marks.merged(external.tasks(protocol), noted.load(file))
    day, now = external.day_of(record), registry.today()
    return [{**task, **registry.state(task, day, now)} for task in found]


def _task_rows(who: users.Access, base: str, root: str, start: str, end: str) -> tuple[list[dict], int]:
    """Поручения из протоколов, которые человек видит: папка root с подпапками, совещания
    с start по end. Второе значение — сколько протоколов просмотрено."""
    assert service is not None
    ledger = service.ledger
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


@app.get("/api/v1/tasks")
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


@app.get("/api/v1/tasks.csv")
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
        sheet.writerow([owner, t["task"], marks.day(t["due"]) if t["due"] else "", t["due_text"] or "",
                        "" if t["due"] else t["due_date"] or "", "да" if t["overdue"] else "", t["status"] or "",
                        t["title"] or "", t["date"], t["dir"], PurePosixPath(t["file"]).name, t["at"] or "", t["link"],
                        "да" if t["done"] else "", t["done_by"] or "", (t["done_at"] or "")[:16].replace("T", " "),
                        t["comment"] or "", t["due_by"] or ""])
    # метка порядка байтов — чтобы Excel прочитал кириллицу
    return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="tasks.csv"'})


@app.get("/api/v1/status")
def v1_status(file: str, who: users.Access = Depends(viewer)):
    """Что сейчас с записью — чтобы дождаться результата после загрузки."""
    assert service is not None
    gone = HTTPException(404, "такой записи нет или её папка вам не открыта")
    if not who.sees(_parent_of(file)):
        raise gone
    state = _in_progress(who).get(file)
    record = service.ledger.get(file)
    if state is None and record is not None:
        state = {"state": external.state_of(record), "error": record.get("error"),
                 "finished_at": record.get("finished_at")}
    if state is None:
        target, root = (cfg.inbox / file).resolve(), cfg.inbox.resolve()
        if root not in target.parents or not target.is_file():
            raise gone
        state = {"state": "uploaded"}
    return {"file": file, **state, "text": external.STATE_TEXT[state["state"]],
            "done": state["state"] in FINAL_STATES}


def _disk_where(allowed) -> list[str]:
    """Папки на диске, для которых allowed(папка) истинно. Личные папки лежат в общей
    «Пользовательские» — сама она не предлагается никому."""
    assert service is not None
    found = []
    for root, dirs, _ in os.walk(cfg.inbox):
        rel = Path(root).relative_to(cfg.inbox).as_posix()
        depth = 0 if rel == "." else rel.count("/") + 1
        dirs[:] = [d for d in dirs if not d.startswith(".") and depth < FOLDER_DEPTH]
        found += [f for f in (d if rel == "." else f"{rel}/{d}" for d in dirs)
                  if f != users.HOME_ROOT and allowed(f) and not service._under_outbox(f)]
    return sorted(found)


def _upload_folders(who: users.Access) -> list[str]:
    """Куда человек может загрузить запись: папки, где ему дано право загрузки."""
    return _disk_where(who.uploads) if upload_ready else []


@app.get("/api/v1/folders")
async def v1_folders(who: users.Access = Depends(viewer)):
    return {"folders": await asyncio.to_thread(_upload_folders, who),
            "upload": {"enabled": upload_ready, "max_bytes": cfg.upload_max_bytes,
                       "extensions": sorted(e.lstrip(".") for e in media.MEDIA_EXTS)}}


@app.put("/api/v1/upload")
async def v1_upload(request: Request, dir: str = "", name: str = "", who: users.Access = Depends(viewer)):
    """Запись одним запросом: тело — сам файл, потоком (curl -T). Папка обязательна:
    клиент не должен сам решать, куда положить чужое совещание. В личном режиме папок с правами
    нет, и без папки запись ложится в корень записей — туда же, куда её кладёт кнопка на странице."""
    if cfg.personal and not name:
        raise HTTPException(400, "нужно имя файла: PUT /api/v1/upload?name=<имя файла>&dir=<папка>. "
                                 "Без dir запись ляжет в корень записей")
    if not name or not (users.norm_folder(dir) or cfg.personal):
        folders = await asyncio.to_thread(_upload_folders, who)
        raise HTTPException(400, "нужны папка и имя файла: PUT /api/v1/upload?dir=<папка>&name=<имя файла>. "
                            + ("Папки, куда вы можете загружать: " + "; ".join(folders) if folders
                               else "Папок, открытых вам для загрузки, сейчас нет: право загрузки выдаёт "
                                    "администратор папки или сервиса"))
    rel = _upload_rel(dir, name, who)
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
            raise HTTPException(500, f"не удалось записать файл: {e}")
        raise
    log(f"загружена запросом: {rel} ({size / 1048576:.1f} МБ) ({who.login}"
        + (f", ключ «{who.key}»" if who.key else "") + ")")
    jot("upload", who, file=rel, size=size)
    wait = cfg.scan_interval_sec + cfg.stable_sec
    return {"file": rel, "size": size, "state": "uploaded",
            "status": _base(request) + "/api/v1/status?" + urlencode({"file": rel}, quote_via=quote),
            "text": f"запись принята; сервис возьмёт её в работу примерно через {wait} с — "
                    "спрашивайте состояние по адресу status не чаще раза в минуту"}


# ---------- навык для ИИ-клиентов ----------
# Отдаётся без входа: в нём нет ничего, кроме адреса этой установки и описания запросов.
# Ключ человек вписывает сам в файл у себя в домашней папке.

@app.get("/skill")
def skill_install(request: Request):
    """Инструкция по установке навыка — текст для модели: его выполняет сам ИИ-клиент."""
    return PlainTextResponse(_skill_text("INSTALL.md", request))


@app.get("/skill/SKILL.md")
def skill_file(request: Request):
    return _markdown(_skill_text("SKILL.md", request))


@app.get("/skill/CHANGELOG.md")
def skill_changes(request: Request, since: int = 0):
    """Что нового в навыке после версии since — коротко, чтобы клиент сказал это человеку одной строкой."""
    fresh = [text for version, text in _skill_changes() if version > since]
    text = f"# Навык «Подьячий»: версия {_skill_version()}\n\n" + ("\n".join(fresh) if fresh else "Новее ничего нет.\n")
    return _markdown(text)


@app.get(f"/skill/{SKILL_NAME}.zip")
def skill_zip(request: Request):
    """Навык архивом — для ручной установки: внутри папка podyachiy с файлом SKILL.md."""
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{SKILL_NAME}/SKILL.md", _skill_text("SKILL.md", request))
    return Response(packed.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{SKILL_NAME}.zip"'})


# ---------- ключи доступа ----------

NO_KEYS_HERE = "сервис работает без входа — ключи доступа здесь не нужны"


def _keys_on() -> None:
    if cfg.personal:
        raise HTTPException(404, NO_KEYS_HERE)


class KeyRequest(BaseModel):
    name: str
    days: int = 365         # 0 — без срока (только у обычного ключа)
    manage: bool = False    # ключ «с управлением» — только администратору сервиса


def _own_keys(who: users.Access) -> dict:
    return {"keys": people.keys(who.login), "can_manage": who.admin,
            "days": {"plain": keys.DAYS[keys.PLAIN], "manage": keys.DAYS[keys.MANAGE]}}


@app.get("/api/keys")
def keys_list(who: users.Access = Depends(person)):
    """Ключи человека: имя, вид, начало ключа, когда выпущен, когда им пользовались, когда истечёт."""
    _keys_on()
    return _own_keys(who)


@app.post("/api/keys")
async def key_issue(req: KeyRequest, who: users.Access = Depends(person)):
    """Новый ключ. Сама строка есть только в этом ответе — второй раз сервис её не покажет."""
    _keys_on()
    try:
        token, key = await asyncio.to_thread(people.issue_key, who.login, req.name, req.days, req.manage)
    except users.UserError as e:
        raise HTTPException(403 if req.manage and not who.admin else 400, str(e))
    log(f"ключ доступа выпущен: «{key['name']}»" + (", с управлением" if req.manage else "") + f" ({who.login})")
    jot("key_issue", who, key=key["name"], key_kind=key["kind"], expires=key["expires"])
    return {"key": token, "issued": key, **_own_keys(who)}


class KeyRevoke(BaseModel):
    id: str


@app.post("/api/keys/revoke")
async def key_revoke(req: KeyRevoke, who: users.Access = Depends(person)):
    _keys_on()
    try:
        key = await asyncio.to_thread(people.revoke_key, req.id, who.login)
    except users.UserError as e:
        raise HTTPException(404, str(e))
    log(f"ключ доступа отозван: «{key['name']}» ({who.login})")
    jot("key_revoke", who, key=key["name"], key_kind=key["kind"])
    return {"ok": True, **_own_keys(who)}


@app.get("/api/admin/keys", dependencies=[Depends(admin_guard)])
async def admin_keys():
    """Все ключи доступа — администратору сервиса: чей, имя, вид, создан, последний раз
    использован, истекает; mark — expired (срок вышел) или stale (давно не использовался).
    Значений ключей нет: сервис их не хранит."""
    return {"keys": people.all_keys(), "stale_days": keys.STALE_DAYS}


@app.post("/api/admin/keys/revoke", dependencies=[Depends(page_admin)])
async def admin_key_revoke(req: KeyRevoke, who: users.Access = Depends(viewer)):
    """Администратор сервиса отзывает любой ключ — только сам, на странице."""
    try:
        key = await asyncio.to_thread(people.revoke_key, req.id)
    except users.UserError as e:
        raise HTTPException(404, str(e))
    log(f"ключ доступа отозван администратором: «{key['name']}» пользователя {key['owner']} ({who.login})")
    jot("key_revoke", who, key=key["name"], key_kind=key["kind"], owner=key["owner"])
    return {"ok": True, "keys": people.all_keys()}


@app.get("/api/search")
async def search_all(q: str = "", who: users.Access = Depends(viewer)):
    """Поиск по протоколам и расшифровкам всех записей сразу.

    Пароля не требует, как и чтение самих результатов: искать можно только по
    тому, что есть в журнале обработки.
    """
    assert service is not None
    if len(q.strip()) < search.MIN_QUERY:
        raise HTTPException(400, "слишком короткий запрос")
    records = [r for r in service.ledger.history(limit=search.MAX_SCAN)
               if who.sees(_parent_of(r["file"]))]
    # выходная папка может быть сетевой: чтение уводим из событийного цикла
    return await asyncio.to_thread(search.find, records, cfg.outbox, q)


@app.get("/api/map")
def result_map(file: str, download: bool = False, who: users.Access = Depends(viewer)):
    """Карта записи — самостоятельная страница: протокол, дерево его разделов и расшифровка.
    download=true — тот же файл для скачивания, без кнопки возврата в сервис.

    Отдаётся файл, лежащий рядом с протоколом; если протокол новее карты
    (его правили руками), карта перед этим пересобирается.
    """
    return _map(file, download, who)


def _map(file: str, download: bool, who: users.Access) -> Response:
    assert service is not None
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
                        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe="")})
    # страницу показывает сам сервис — в шапке кнопка возврата к этой записи; в скачанном файле её нет
    place = {"path": file.rpartition("/")[0], "open": file}
    back = "/?" + urlencode({k: v for k, v in place.items() if v}, quote_via=quote)
    return Response(mindmap.with_back(page, back), media_type="text/html; charset=utf-8")


@app.get("/api/export")
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


@app.get("/api/result")
def result(file: str, kind: str = "summary", who: users.Access = Depends(viewer)):
    """Текст готового результата для чтения на странице.

    Открыть можно только то, что есть в журнале обработки: журнал здесь служит
    белым списком, поэтому произвольный путь подставить нельзя. Сама выходная
    папка остаётся границей — путь проверяется ещё раз после разрешения.
    """
    assert service is not None
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
            "can_act": upload_ready and who.owns(_parent_of(file)),
            # отправить протокол на почту — тому же хозяину и только самому человеку, не его ключу
            "can_send": cfg.mail_on and not who.key and who.owns(_parent_of(file)),
            "text": text,
            # поручения протокола с отметками «выполнено» и сроками: line — строка текста, у которой
            # стоит флажок, due_col — в какой ячейке этой строки срок (None — такой колонки нет)
            **({"tasks": [{"line": n, "due_col": col["срок"], **task}
                          for (n, col, _), task in zip(external.tasks_at(text), _marked(file, record, text))],
                "can_mark": who.owns(_parent_of(file))} if kind == "summary" else {}),
            # ФИО из словаря записи и её папок — подсказки при подписи голосов
            "roster": [{"name": p["name"], "role": p["role"]} for p in words.effective(file)["people"]],
            # кто есть кто: части записи с их голосами, реплики с подписями
            **(signs if kind == "transcript" else {"parts": signs["parts"], "known": signs["known"]}),
            **_rebuild_state(file, base, stem)}


def _rebuild_state(file: str, base: Path, stem: str) -> dict:
    """Расходится ли протокол с подписями и со словарём записи и не пересобирается ли он уже.
    Словарь папки сюда не входит: его правка не делает устаревшими все протоколы папки разом."""
    assert service is not None
    try:
        built = (base / "summary" / f"{stem}.md").stat().st_mtime
    except OSError:
        built = None
    return {"names_newer": names.names_newer(base, stem),
            "dict_newer": built is not None and words.get(dictionary.RECORDS, file)["changed"] > built,
            "rebuilding": any(i.path == file for i in service.queue) or _in_work(file)}


def _in_work(file: str) -> bool:
    assert service is not None
    return any(job["file"] == file for job in list(service.running))


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


@app.get("/api/audio")
def audio(file: str, who: users.Access = Depends(viewer)):
    """Звук записи — чтобы переслушать момент, на который ссылается метка времени.

    Отдаётся с поддержкой Range: браузер тянет только тот кусок, который
    собрался играть, а не всю часовую дорожку ради одной минуты.
    """
    assert service is not None
    record = _record(file, who)
    path = _audio_path(file, record)
    if path is None:
        raise HTTPException(404, "звук этой записи не сохранён")
    # inline, а не attachment: ссылку могут открыть и прямо в браузере,
    # и тогда он должен проиграть запись, а не предложить её скачать
    return FileResponse(path, media_type="audio/mpeg", filename=path.name,
                        content_disposition_type="inline")


# ---------- действия с записью ----------

@asynccontextmanager
async def scan_paused():
    """Замок сканера на время действия с записью или папкой.

    Действие идёт под замком и прямо в событийном цикле, без потока: так между
    проверкой «запись не в работе» и переездом файлов не вклинится ни обход входной
    папки, ни обработчик (подробно — в records.py). Замок берётся опросом, а не в потоке:
    обход сетевой папки может идти долго, и ждать его, остановив весь сервис, нельзя.
    """
    assert service is not None
    while not service.scan_lock.acquire(blocking=False):
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        service.scan_lock.release()


async def _record_action(action, file: str, who: users.Access, *args, also=None):
    """Переименование, перенос и удаление записи — её хозяину. Чужая запись — 404."""
    assert service is not None
    _record(file, who)
    if not who.owns(_parent_of(file)):
        raise HTTPException(403, "это может владелец папки или её администратор")
    if not upload_ready:
        raise HTTPException(503, "входная папка доступна только для чтения")
    async with scan_paused():
        try:
            return action(cfg, service, file, *args, also=also)
        except records.RecordError as e:
            raise HTTPException(e.status, e.detail)
        except OSError as e:
            raise HTTPException(500, f"не получилось: {e}")


class RecordRename(BaseModel):
    file: str
    name: str


async def _rename(file: str, name: str, who: users.Access) -> dict:
    """Новое имя записи: переезжают исходник, результаты, строка журнала, отметки и словарь.
    Обработки не будет."""
    def also(new: str) -> None:
        words.move_record(file, new)
        jot("record_rename", who, file=file, to=new)

    new = await _record_action(records.rename, file, who, name, also=also)
    if new != file:
        log(f"запись переименована: {file} → {new} ({who.login})")
    return {"ok": True, "file": new}


async def _move(file: str, dir: str, who: users.Access) -> dict:
    """Запись в другую папку — туда, где человек тоже хозяин. Обработки не будет."""
    def also(new: str) -> None:
        words.move_record(file, new)
        jot("record_move", who, file=file, to=new)

    _record(file, who)
    if not who.owns(users.norm_folder(dir)):
        raise HTTPException(403, "переносить можно в папку, где вы владелец или администратор")
    new = await _record_action(records.move, file, who, dir, also=also)
    if new != file:
        log(f"запись перенесена: {file} → {new} ({who.login})")
    return {"ok": True, "file": new}


# Переименование и перенос ключу доступа открыты, удаление — нет: его делает сам человек.
@app.post("/api/record/rename")
async def record_rename(req: RecordRename, who: users.Access = Depends(viewer)):
    return await _rename(req.file, req.name, who)


@app.post("/api/v1/rename")
async def v1_rename(file: str, name: str, who: users.Access = Depends(viewer)):
    """То же для ИИ-клиентов и программ: параметры в адресе, как у остальных /api/v1."""
    return await _rename(file, name, who)


class RecordMove(BaseModel):
    file: str
    dir: str


@app.post("/api/record/move")
async def record_move(req: RecordMove, who: users.Access = Depends(viewer)):
    return await _move(req.file, req.dir, who)


@app.post("/api/v1/move")
async def v1_move(file: str, dir: str, who: users.Access = Depends(viewer)):
    return await _move(file, dir, who)


class RecordDelete(BaseModel):
    file: str


@app.post("/api/record/delete")
async def record_delete(req: RecordDelete, who: users.Access = Depends(person)):
    """Запись с результатами уходит в корзину на сервере; через месяц стирается."""
    def also(done: dict) -> None:
        words.drop_record(req.file)
        jot("record_delete", who, file=req.file, trash=done["trash"])

    done = await _record_action(records.remove, req.file, who, who.login, also=also)
    log(f"запись удалена в корзину: {req.file}, лежит в {done['trash']} ({who.login})")
    return {"ok": True, **done}


class TaskMark(BaseModel):
    file: str
    key: str
    done: bool | None = None        # не указано — отметку не трогать
    comment: str = ""
    due: str | None = None          # не указано — срок не трогать; пусто — вернуть срок из протокола


async def _mark(file: str, key: str, done: bool | None, comment: str, due: str | None, who: users.Access) -> dict:
    """Отметка «выполнено» и срок датой у поручения: ставит и снимает хозяин записи. Чужая
    запись — 404. done и due, которые не указаны (None), не трогаются; пустой due снимает срок.

    Идёт прямо в событийном цикле, без потока и без await, — как действия с записью:
    переезд записи не вклинится между чтением протокола и записью отметки. Отметка и строка
    о ней в журнале действий записываются одной транзакцией."""
    record = _record(file, who)
    if not who.owns(_parent_of(file)):
        raise HTTPException(403, "отметить поручение и задать ему срок может владелец папки или её администратор")
    comment = " ".join(comment.split())
    if len(comment) > marks.COMMENT_MAX:
        raise HTTPException(400, f"комментарий длиннее {marks.COMMENT_MAX} знаков")
    if due is not None:
        due = due.strip()
    if due:
        try:
            if not re.fullmatch(r"\d{4}-\d\d-\d\d", due):
                raise ValueError(due)
            due = datetime.date.fromisoformat(due).isoformat()
        except ValueError:
            raise HTTPException(400, "срок — датой ГГГГ-ММ-ДД, например 2026-10-20; пустой срок возвращает срок из протокола")
    protocol = _protocol(file, record)
    if not protocol:
        raise HTTPException(404, "у записи нет протокола — отмечать нечего")
    tasks = external.tasks(protocol)
    try:
        with base.write():
            if done is not None:
                task, changed = noted.put(file, tasks, key, done, who.login, comment)
                if changed:
                    jot("task_done" if done else "task_reopen", who, file=file,
                        task=task["task"][:120], owner=task["who"] or "")
            if due is not None:
                task, changed, was = noted.put_due(file, tasks, key, due or None, who.login)
                if changed:
                    # было → стало: дата, а где её нет — слова протокола
                    jot("task_due", who, file=file, task=task["task"][:120], owner=task["who"] or "",
                        was=was or task["due_text"] or "", now=task["due"] or task["due_text"] or "")
    except KeyError:
        raise HTTPException(404, "такого поручения в протоколе записи нет: возьмите key из её поручений заново")
    return {"ok": True, "task": {**task, **registry.state(task, external.day_of(record), registry.today())}}


# Отметка и срок открыты и ключу доступа — в рамках тех же прав; в журнале действий они с пометкой.
@app.post("/api/record/task")
async def record_task(req: TaskMark, who: users.Access = Depends(viewer)):
    if req.done is None and req.due is None:
        raise HTTPException(400, "укажите done или due")
    return await _mark(req.file, req.key, req.done, req.comment, req.due, who)


@app.post("/api/v1/task")
async def v1_task(file: str, key: str, done: bool | None = None, comment: str = "", due: str | None = None,
                  who: users.Access = Depends(viewer)):
    """То же для ИИ-клиентов и программ: параметры в адресе. key — из поручений записи или реестра.
    due=ГГГГ-ММ-ДД задаёт срок, пустой due возвращает срок из протокола. Без done и без due
    поручение отмечается выполненным."""
    return await _mark(file, key, True if done is None and due is None else done, comment, due, who)


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


def _sender(file: str, who: users.Access) -> dict:
    """Запись, протокол которой человек вправе отправить на почту. Чужая запись — 404."""
    _mail_needed()
    record = _record(file, who)
    if not who.owns(_parent_of(file)):
        raise HTTPException(403, "отправить протокол может владелец папки или её администратор")
    return record


@app.get("/api/record/recipients")
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


@app.post("/api/record/send")
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


@app.get("/api/record/folders")
async def record_folders(who: users.Access = Depends(viewer)):
    return {"folders": await asyncio.to_thread(_own_folders, who)}


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
        # scrypt считается десятки миллисекунд — не в событийном цикле
        if not await asyncio.to_thread(people.verify, plain, password):
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


@app.post("/api/login")
async def login(req: LoginRequest, request: Request, response: Response):
    started = time.monotonic()
    try:
        who, how = await _sign_in(req.login, req.password, request)
    except HTTPException:
        # отказ приходит не раньше этого срока, какой бы путь ни прошла проверка
        await asyncio.sleep(max(0.0, WRONG_PASSWORD_DELAY_SEC - (time.monotonic() - started)))
        raise
    jot("login", who, **how)
    # метка недоступна скриптам страницы и не уходит с запросами с чужих сайтов
    # выдана по HTTPS — по HTTP браузер её не отправит
    response.set_cookie(users.COOKIE, people.token(who.login), max_age=users.SESSION_SEC,
                        httponly=True, samesite="lax", path="/", secure=request.url.scheme == "https")
    return {"login": who.login, "name": who.name, "admin": who.admin}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(users.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request):
    """Кто вошёл — странице, чтобы показать имя или отправить на вход."""
    if cfg.personal:
        return {"login": OWNER.login, "name": "", "admin": True, "home": None, "personal": True,
                "home_root": users.HOME_ROOT, "mail": False, "signup": False, "requests": 0, "url": _base(request),
                "url_set": bool(cfg.public_url),
                "email": "", "notify_ready": False, "domain": {"on": False}, "domain_account": False, "notice": ""}
    who, notice = people.by_token(request.cookies.get(users.COOKIE, "")), ""
    try:
        who = who and await _confirmed(who)
    except HTTPException as e:
        who, notice = None, e.detail        # домен человеку отказал — страница входа скажет почему
    box = (who and people.mailbox(who.login)) or {"email": "", "notify_ready": False}
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
            # своя почта и письма о готовности записи — переключателю в меню учётной записи
            "email": box["email"], "notify_ready": box["notify_ready"],
            # сколько заявок ждёт решения — счётчик в меню, только администратору сервиса
            "requests": len(claims.pending()) if who and who.admin else 0}


class PasswordRequest(BaseModel):
    old: str
    new: str


@app.post("/api/password")
async def change_password(req: PasswordRequest, who: users.Access = Depends(person)):
    if who.login == users.ADMIN:
        raise HTTPException(400, "пароль администратора задаётся в .env на сервере")
    if who.domain:
        raise HTTPException(400, users.IN_DOMAIN)
    try:
        await asyncio.to_thread(people.set_password, who.login, req.old, req.new)
    except users.UserError as e:
        await asyncio.sleep(WRONG_PASSWORD_DELAY_SEC)
        raise HTTPException(400, str(e))
    log(f"пользователь {who.login} сменил пароль")
    jot("password", who)
    return {"ok": True}


class NotifyRequest(BaseModel):
    ready: bool


@app.post("/api/notify")
async def notify_set(req: NotifyRequest, who: users.Access = Depends(person)):
    """Письма «запись готова» — человек включает и отключает их себе сам."""
    try:
        await asyncio.to_thread(people.set_notify, who.login, req.ready)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    jot("notify", who, ready=req.ready)
    return {"ok": True, "notify_ready": req.ready}


# ---------- заявка на доступ и пароль по ссылке ----------

MAIL_OFF = "почта в сервисе не настроена — обратитесь к администратору"
TOO_OFTEN = "слишком много запросов с вашего адреса — попробуйте через час"


def _mail_needed() -> None:
    if not cfg.mail_on:
        raise HTTPException(503, MAIL_OFF)


def _client(request: Request) -> str:
    return request.client.host if request.client else "неизвестно"


async def _post(to: list[str], letter: tuple[str, str, str], reply_to: str = "") -> None:
    await asyncio.to_thread(mail.send, cfg, to, letter, reply_to=reply_to)


def _make_home(login: str) -> None:
    """Личная папка пользователя во входной папке."""
    home = people.get(login)["home"]
    if upload_ready:
        try:
            (cfg.inbox / home).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log(f"ВНИМАНИЕ: личная папка {home} не создана: {e}")


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


@app.post("/api/request")
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
                                        cfg.mail_on)
    except signup.RequestError as e:
        # X-Field — к какому полю формы относится отказ: страница покажет его под этим полем
        raise HTTPException(e.status, e.detail, headers={"X-Field": e.field} if e.field else None)
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


@app.post("/api/forgot")
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


@app.post("/api/set-password/check")
async def link_check(req: LinkRequest):
    """Действует ли ссылка и чья она — странице, прежде чем спрашивать пароль. Строка ссылки
    приходит телом запроса, а не в адресе: в журнал запросов она не попадает."""
    login = people.link_owner(req.token)
    if login is None:
        raise HTTPException(404, "ссылка недействительна или её срок вышел — запросите новую")
    return {"login": login, "name": people.get(login)["name"], "password_min": users.PASSWORD_MIN}


@app.post("/api/set-password")
async def link_use(req: LinkRequest):
    """Пароль по одноразовой ссылке: первый — после одобренной заявки, новый — после «Забыл пароль»."""
    try:
        login = await asyncio.to_thread(people.use_link, req.token, req.password)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    log(f"пользователь {login} задал пароль по одноразовой ссылке")
    trail.write("password_link", login)
    return {"ok": True, "login": login}


@app.get("/api/access")
def access_list(dir: str, who: users.Access = Depends(steward)):
    """Кому открыта папка — для администратора и для локального администратора этой папки."""
    folder = users.norm_folder(dir)
    if not who.admin and (not folder or not who.rules(folder)):
        raise HTTPException(403, "раздавать доступ к этой папке вы не можете")
    return {"path": folder, "can_manage": who.admin,
            "users": [u for u in people.holders(folder) if u["login"] != who.login]}


class AccessRequest(BaseModel):
    dir: str
    login: str
    read: str | None = None       # none | only | tree; не указано — не менять
    upload: str | None = None
    manage: str | None = None


@app.post("/api/access")
async def access_set(req: AccessRequest, who: users.Access = Depends(steward)):
    """Права человека на папку. Чтение и загрузку раздаёт и администратор папки,
    администрирование — только администратор сервиса: иначе администратор папки плодил бы
    себе равных."""
    folder = users.norm_folder(req.dir)
    if not who.admin and (not folder or not who.rules(folder)):
        raise HTTPException(403, "раздавать доступ к этой папке вы не можете")
    if req.manage is not None and not who.admin:
        raise HTTPException(403, "администраторов папок назначает администратор сервиса")
    try:
        await asyncio.to_thread(people.set_access, req.login, folder, req.read, req.manage, req.upload)
    except users.UserError as e:
        raise HTTPException(400, str(e))
    log(f"доступ: «{folder or 'все папки'}» — {req.login}: "
        + ", ".join(f"{k} {v}" for k, v in (("чтение", req.read), ("загрузка", req.upload),
                                             ("администрирование", req.manage)) if v)
        + f" ({who.login})")
    jot("access", who, folder=folder, login=req.login, read=req.read, upload=req.upload, manage=req.manage)
    return {"path": folder, "can_manage": who.admin,
            "users": [u for u in people.holders(folder) if u["login"] != who.login]}


FOLDER_DEPTH = 8      # сколько уровней вложенности видно в разделе «Папки и доступ»


def _known_folders() -> list[str]:
    """Папки, которые можно открыть человеку: всё, где есть записи, и папки на диске
    до FOLDER_DEPTH уровней — входная папка может быть сетевой, и обходить её без предела нельзя."""
    assert service is not None
    found: set[str] = set()
    for key in list(service.ledger.records):
        parent = PurePosixPath(_parent_of(key)) if _parent_of(key) else None
        while parent is not None and parent.as_posix() != ".":
            found.add(parent.as_posix())
            parent = parent.parent
    for root, dirs, _ in os.walk(cfg.inbox):
        rel = Path(root).relative_to(cfg.inbox).as_posix()
        depth = 0 if rel == "." else rel.count("/") + 1
        # личные папки и служебные не раздают — в них и не заходим
        dirs[:] = [d for d in dirs if not d.startswith(".") and not (depth == 0 and d == users.HOME_ROOT)
                   and depth < FOLDER_DEPTH]
        found.update(d if rel == "." else f"{rel}/{d}" for d in dirs)
    for user in people.public():
        found.update(f for f in list(user["read"]) + list(user["upload"]) + list(user["manage"]) if f)
    home = users.HOME_ROOT
    return sorted(f for f in found if f != home and not f.startswith(home + "/")
                  and not service._under_outbox(f))


@app.get("/api/admin/folders", dependencies=[Depends(admin_guard)])
async def admin_folders():
    """Папки для раздела «Папки и доступ». Общие — кто администратор, кому открыта, сколько
    записей. Личные — чья и сколько записей: раздавать их нельзя, но видеть администратор должен."""
    assert service is not None
    folders = await asyncio.to_thread(_known_folders)
    parents = [_parent_of(key) for key in list(service.ledger.records)]
    everyone = people.public()

    def records(f: str) -> int:
        return sum(1 for p in parents if p == f or p.startswith(f + "/"))

    def named(kind: str, f: str) -> list[str]:
        return [u["name"] + ("" if u[kind][f] == users.TREE else " · только эта папка")
                for u in everyone if f in u[kind]]

    return {"folders": [{
        "path": f,
        "records": records(f),
        "managers": named("manage", f),
        "viewers": named("read", f),
        "uploaders": named("upload", f),
    } for f in folders],
        "everything": named("read", ""),
        "personal": [{"path": u["home"], "login": u["login"], "name": u["name"], "records": records(u["home"]),
                      "upload": u["admin"] or u["home_upload"]}
                     for u in everyone],
        "can_create": upload_ready}


class FolderRename(BaseModel):
    dir: str
    name: str


@app.post("/api/admin/folders/rename", dependencies=[Depends(admin_guard)])
async def admin_folder_rename(req: FolderRename, who: users.Access = Depends(viewer)):
    """Новое имя папки: вместе с ней переезжают результаты, журнал обработки и права."""
    assert service is not None
    if not upload_ready:
        raise HTTPException(503, "входная папка доступна только для чтения")
    old = users.norm_folder(req.dir)

    def also(new: str) -> None:
        words.move_folder(old, new)
        jot("folder_rename", who, folder=old, to=new)

    async with scan_paused():
        try:
            new = folders.rename(cfg, service, people, req.dir, req.name, also=also)
        except folders.FolderError as e:
            raise HTTPException(e.status, e.detail)
        except OSError as e:
            raise HTTPException(500, f"не удалось переименовать: {e}")
    if new != old:
        log(f"папка переименована: {old} → {new} ({who.login})")
    return {"ok": True, "path": new}


class FolderDelete(BaseModel):
    dir: str


@app.post("/api/admin/folders/delete", dependencies=[Depends(page_admin)])
async def admin_folder_delete(req: FolderDelete, who: users.Access = Depends(viewer)):
    """Папка со всем содержимым уходит в корзину на сервере; через месяц стирается."""
    assert service is not None
    if not upload_ready:
        raise HTTPException(503, "входная папка доступна только для чтения")
    def also(done: dict) -> None:
        words.drop_folder(users.norm_folder(req.dir))
        jot("folder_delete", who, folder=users.norm_folder(req.dir), records=done["records"], trash=done["trash"])

    async with scan_paused():
        try:
            done = folders.remove(cfg, service, people, req.dir, who.login, also=also)
        except folders.FolderError as e:
            raise HTTPException(e.status, e.detail)
        except OSError as e:
            raise HTTPException(500, f"не удалось удалить: {e}")
    log(f"папка удалена в корзину: {users.norm_folder(req.dir)}, записей {done['records']}, "
        f"лежит в {done['trash']} ({who.login})")
    return {"ok": True, **done}


@app.post("/api/admin/users", dependencies=[Depends(admin_guard)])
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


@app.get("/api/admin/domain", dependencies=[Depends(admin_guard)])
def admin_domain(request: Request):
    if cfg.personal:
        raise HTTPException(404, "в личном режиме входа нет")
    return _domain_view(request)


class DomainCheck(BaseModel):
    login: str = ""         # необязательно: проверить вход человека
    password: str = ""


@app.post("/api/admin/domain/check", dependencies=[Depends(page_admin)])
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


@app.post("/api/admin/users/domain", dependencies=[Depends(page_admin)])
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


@app.post("/api/admin/users/save", dependencies=[Depends(admin_guard)])
async def admin_user_save(req: UserRequest, who: users.Access = Depends(viewer)):
    """Завести пользователя или поменять ему имя, почту, пароль, роль и загрузку в личную папку;
    чего в запросе нет, то у существующего не меняется. Права на папки — по папкам.
    Ключом с управлением пользователя заводят и правят, но пароль существующему не меняют."""
    known = people.get(req.login.strip().lower())
    new = known is None
    if who.key and req.password and not new:
        # ключом с управлением пользователя заводят (с первым паролем), но пароль не меняют
        raise HTTPException(403, PAGE_ONLY)
    name = req.name if req.name is not None else "" if new else known["name"]
    admin = req.admin if req.admin is not None else False if new else known["admin"]
    email = req.email
    if not new and people.source(req.login.strip().lower()) == users.DOMAIN:
        # у учётной записи домена ФИО и почта приходят из домена при каждом входе, пароля в сервисе
        # нет (put откажет), а роль при заданной группе администраторов определяет группа
        name, email = known["name"], None
        if cfg.ldap_admin_group and admin != known["admin"]:
            raise HTTPException(400, f"роль этой учётной записи задаёт группа домена «{cfg.ldap_admin_group}»: "
                                     "добавьте человека в группу или уберите из неё")
    try:
        was = None if new else known["home_upload"]
        await asyncio.to_thread(people.put, req.login, name, req.password, admin, email, False,
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


@app.post("/api/admin/users/delete", dependencies=[Depends(admin_guard)])
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


@app.post("/api/admin/users/link", dependencies=[Depends(page_admin)])
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


@app.get("/api/admin/requests", dependencies=[Depends(admin_guard)])
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


@app.post("/api/admin/requests/approve", dependencies=[Depends(admin_guard)])
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


@app.post("/api/admin/requests/reject", dependencies=[Depends(admin_guard)])
async def admin_request_reject(req: ClaimReject, who: users.Access = Depends(viewer)):
    """Отклоняет заявку. Письмо человеку не шлётся; заявка остаётся в файле с пометкой."""
    try:
        claim = await asyncio.to_thread(claims.decide, req.id, signup.REJECTED, who.login)
    except signup.RequestError as e:
        raise HTTPException(e.status, e.detail)
    log(f"заявка на доступ отклонена: {claim['login']} ({who.login})")
    jot("request_reject", who, login=claim["login"])
    return {"ok": True, "requests": claims.pending()}


@app.post("/api/admin/mail/test", dependencies=[Depends(admin_guard)])
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


@app.post("/api/admin/check", dependencies=[Depends(admin_guard)])
async def admin_check():
    return {"ok": True}


@app.post("/api/admin/providers/check", dependencies=[Depends(admin_guard)])
async def admin_providers_check():
    """Кнопка «Проверить ключ»: принят ли ключ каждым настроенным поставщиком и есть ли у него
    текстовые модели из .env. Запросы бесплатные — записи не обрабатываются."""
    checks = await _check_keys()
    return {"ok": all(c["state"] == providers.CHECK_OK for c in checks), "checks": checks}


@app.post("/api/admin/files", dependencies=[Depends(admin_guard)])
async def admin_files():
    assert service is not None
    # обход папки уводим в поток: INBOX может быть сетевой шарой с архивом
    # записей, и обход в событийном цикле подвесил бы весь сервис
    return {"files": await asyncio.to_thread(service.inbox_files)}


class NamesRequest(BaseModel):
    file: str
    # номер части → {метка → ФИО}; пустая строка снимает ручную подпись
    parts: dict[str, dict[str, str]] = {}
    # время начала реплики (как в names.line_key) → ФИО этой одной реплики
    lines: dict[str, str] = {}
    # пересобрать протокол с подписями: правки отдельных строк копятся
    # без пересборки, иначе каждый клик стоил бы протокола
    rebuild: bool = False


def _voices(file: str, who: users.Access) -> tuple[dict, Path, str, dict]:
    """Запись с говорящими: строка журнала, папка результата, имя и нынешние подписи."""
    record = _record(file, who)
    stem = pipeline.stem_of(cfg.inbox, file, record)
    base = cfg.outbox / Path(file).parent / stem
    current = names.view(base, stem)
    if not current["lines"]:
        raise HTTPException(409, "у записи нет расшифровки с говорящими")
    return record, base, stem, current


async def _sign(req: NamesRequest, who: users.Access) -> tuple[Path, str, dict]:
    """Подписи говорящих вручную и, по просьбе, протокол заново с этими ФИО.

    Расшифровка и разбор при пересборке переиспользуются — платить
    приходится только за протокол. Править может хозяин папки: владелец личной,
    локальный администратор общей, администратор сервиса. Тот, кому папка только
    открыта, читает, но не правит. Пересборка платная, поэтому хозяину личной папки
    она открыта только с правом загрузки в неё. Ключу доступа это открыто в тех же правах.
    Отдаёт папку результата и имя записи в ней — по ним вызывающий собирает ответ — и, если
    просили пересборку, что с ней стало: {"rebuild": "queued"} или {"rebuild": "already"}.
    """
    assert service is not None
    _record(req.file, who)              # чужая запись — 404, раньше любого другого ответа
    if not who.owns(_parent_of(req.file)):
        raise HTTPException(403, "подписывать ФИО может владелец папки или её администратор")
    if req.rebuild and not who.uploads(_parent_of(req.file)):
        # отказ раньше правки подписей: запрос «подписать и пересобрать» не выполняется наполовину
        raise HTTPException(403, NO_REBUILD)
    record, base, stem, current = _voices(req.file, who)
    in_part = {str(p["index"]): {s["label"] for s in p["speakers"]} for p in current["parts"]}
    for part, labels in req.parts.items():
        wrong = sorted(set(labels) - in_part.get(part, set()))
        if part not in in_part or wrong:
            raise HTTPException(400, f"в части {part} нет меток: {', '.join(wrong) or 'такой части нет'}")
    if set(req.lines) - {ln["key"] for ln in current["lines"]}:
        raise HTTPException(400, "такой реплики в расшифровке нет")

    signed = names.load(base, stem)
    for part, labels in req.parts.items():
        bucket = signed["parts"].setdefault(part, {})
        for label, name in labels.items():
            if names.clean(name):
                bucket[label] = names.clean(name)
            else:
                bucket.pop(label, None)
    for key, name in req.lines.items():
        if names.clean(name):
            signed["lines"][key] = names.clean(name)
        else:
            signed["lines"].pop(key, None)
    signed["parts"] = {k: v for k, v in signed["parts"].items() if v}
    if req.parts or req.lines:
        names.save(base, stem, signed)
        log(f"подписи говорящих: {req.file} — по частям "
            f"{sum(len(v) for v in signed['parts'].values())}, по строкам {len(signed['lines'])}"
            + (f" ({who.login}, ключ «{who.key}»)" if who.key else ""))
        jot("names", who, file=req.file)

    return base, stem, {"rebuild": _queue_rebuild(req.file, record, who)} if req.rebuild else {}


@app.post("/api/admin/names")
async def admin_names(req: NamesRequest, who: users.Access = Depends(viewer)):
    """Подписи говорящих со страницы записи."""
    base, stem, rebuilt = await _sign(req, who)
    return {"ok": True, **names.view(base, stem), **_rebuild_state(req.file, base, stem), **rebuilt}


def _speakers(file: str, base: Path, stem: str, who: users.Access) -> dict:
    """Говорящие записи для модели и программы: по частям — метка, нынешняя подпись, догадка
    разбора, доля речи и несколько реплик, по которым человека можно узнать."""
    seen = names.speakers(base, stem)
    return {"file": file, "can_edit": who.owns(_parent_of(file)),
            "can_rebuild": who.owns(_parent_of(file)) and who.uploads(_parent_of(file)),
            "parts": seen["parts"], "known": seen["known"],
            **_rebuild_state(file, base, stem)}


@app.get("/api/v1/speakers")
def v1_speakers(file: str, who: users.Access = Depends(viewer)):
    """Кто говорит в записи — чтобы подписать метки ФИО."""
    _, base, stem, _ = _voices(file, who)
    return _speakers(file, base, stem, who)


@app.post("/api/v1/speakers")
async def v1_speakers_sign(file: str, part: int | None = None, label: str = "", line: str = "",
                           name: str = "", rebuild: bool = False, who: users.Access = Depends(viewer)):
    """Подпись одной метки в части записи (part, label, name) или одной реплики (line, name);
    пустое name снимает подпись. rebuild=true — пересобрать протокол с подписями: можно
    вместе с подписью или отдельным запросом. Права и логика — те же, что у страницы."""
    if bool(label) != (part is not None) or (label and line):
        raise HTTPException(400, "подпись метки — part и label вместе; подпись реплики — line; не оба сразу")
    if not label and not line and not rebuild:
        raise HTTPException(400, "нечего менять: нужны part и label, или line, или rebuild=true")
    base, stem, rebuilt = await _sign(NamesRequest(file=file, parts={str(part): {label: name}} if label else {},
                                             lines={line: name} if line else {}, rebuild=rebuild), who)
    return {"ok": True, **_speakers(file, base, stem, who), **rebuilt}


# ---------- словарь папки и записи ----------
# Участники и сокращения — на папку и на отдельную запись (dictionary.py). Читает тот, кому
# папка открыта; правит её хозяин — как подписи говорящих. Ключу доступа открыто в тех же
# правах. Пересборка протокола с новым словарём платная и идёт под правом загрузки.

NO_DICT_EDIT = "словарь правит владелец папки или её администратор"


def _dict_folder(path: str, who: users.Access) -> str:
    """Папка, у которой может быть словарь. Закрытая человеку неотличима от несуществующей."""
    folder = users.norm_folder(path)
    if not folder:
        raise HTTPException(400, "у корня записей своего словаря нет: общий словарь сервиса — в настройках, "
                                 "его правит администратор")
    hidden = any(part.startswith(".") for part in folder.split("/"))
    if hidden or not who.sees(folder) or not ((cfg.inbox / folder).is_dir() or (cfg.outbox / folder).is_dir()):
        raise HTTPException(404, folders.NO_FOLDER)
    return folder


def _dict_record(file: str, who: users.Access) -> dict | None:
    """Запись, у которой может быть словарь: строка журнала обработки, а у только что загруженной
    записи, до которой сервис ещё не дошёл, — None. Словарь, заданный до обработки, учтётся
    уже в ней, в том числе в распознавании."""
    assert service is not None
    if service.ledger.get(file) is not None:
        return _record(file, who)
    canonical = bool(file) and file == users.norm_folder(file) and not any(p.startswith(".") for p in file.split("/"))
    if not (canonical and who.sees(_parent_of(file)) and PurePosixPath(file).suffix.lower() in media.MEDIA_EXTS
            and (cfg.inbox / file).is_file()):
        raise HTTPException(404, "такой записи нет")
    return None


def _changed_by(login: str) -> str:
    who = people.access(login) if login and not cfg.personal else None
    return who.name if who else ""


def _dict_view(who: users.Access, folder: str = "", file: str = "", record: dict | None = None) -> dict:
    """Словарь папки или записи для страницы и программы: свои строки (people, terms) и то, что
    приходит с папок выше (inherited — у каждой строки path, откуда она). common — действует ли
    ещё и общий словарь сервиса."""
    home = _parent_of(file) if file else folder
    own = words.get(dictionary.RECORDS if file else dictionary.FOLDERS, file or folder)
    view = {"scope": dictionary.RECORD if file else dictionary.FOLDER, "path": file or folder,
            "people": own["people"], "terms": own["terms"],
            "changed_at": own["at"], "changed_by": _changed_by(own["by"]),
            "inherited": dictionary.merge(words.layers(folder=home if file else _parent_of(folder))),
            "common": bool(templates.load_glossary(cfg.prompts_dir)),
            "can_edit": who.owns(home),
            "limits": {"people": dictionary.PEOPLE_MAX, "terms": dictionary.TERMS_MAX}}
    if file:
        # пересобрать протокол с новым словарём — хозяину с правом загрузки и только у записи,
        # которая уже распознана; у ещё не обработанной словарь учтётся сам
        ready = bool(record) and record.get("status") in ("ok", "no_summary")
        view.update({"processed": ready,
                     "can_rebuild": ready and who.owns(home) and who.uploads(home)})
        if record:
            stem = pipeline.stem_of(cfg.inbox, file, record)
            view.update(_rebuild_state(file, cfg.outbox / Path(file).parent / stem, stem))
    return view


class DictionarySave(BaseModel):
    path: str = ""                          # папка — либо
    file: str = ""                          # запись
    # текст (строка на человека или термин), список строк или список объектов; None — не менять.
    # Вид проверяет dictionary.parse_*: отказ приходит словами, а не перечнем полей
    people: Any = None
    terms: Any = None
    add: bool = False                       # дополнить словарь, а не заменить его целиком
    rebuild: bool = False                   # пересобрать протокол записи с новым словарём


def _edited_since(file: str, record: dict, moment: float) -> bool:
    """Правили ли после этого момента то, из чего собирается протокол записи: подписи говорящих,
    словарь записи или словари её папки и папок выше."""
    stem = pipeline.stem_of(cfg.inbox, file, record)
    if names.changed(cfg.outbox / Path(file).parent / stem, stem) > moment:
        return True
    return any(words.get(scope, path)["changed"] > moment for scope, path in
               [(dictionary.RECORDS, file)] + [(dictionary.FOLDERS, f) for f in dictionary.parents(_parent_of(file))])


REBUILD_QUEUED, REBUILD_ALREADY = "queued", "already"


def _queue_rebuild(file: str, record: dict, who: users.Access) -> str:
    """Протокол записи заново по готовой расшифровке — с нынешними подписями и словарём.
    Пересборка платная, поэтому на один набор правок она одна, сколько бы раз её ни просили.

    Уже ждёт в очереди — подписи и словарь прочитает сама, когда до неё дойдёт: вторая не нужна.
    Запись идёт прямо сейчас — прогон мог прочитать старые подписи и словарь, поэтому ещё один
    следом: он встанет в очередь и дождётся конца текущего (Service._take). Но только если с
    начала прогона что-то правили: иначе повторная просьба (двойное нажатие, повтор запроса)
    оплатила бы тот же протокол дважды. Отдаёт, что произошло: поставлена или уже была."""
    assert service is not None
    if any(i.path == file for i in service.queue):
        return REBUILD_ALREADY
    job = next((j for j in list(service.running) if j["file"] == file), None)
    if job and not _edited_since(file, record, job.get("began", 0)):
        return REBUILD_ALREADY
    template = None if record.get("template_auto", True) else record.get("template")
    if template is not None and template not in templates.types(cfg.prompts_dir):
        # шаблон задавали вручную, а этот свой тип с тех пор удалили — тип выберет разбор записи
        log(f"пересборка {file}: типа «{template}» больше нет — шаблон выберет разбор записи")
        template = None
    service.enqueue_manual(file, template, False, who.login, "rebuild")
    return REBUILD_QUEUED


async def _dict_save(req: DictionarySave, who: users.Access) -> dict:
    """Правка словаря папки или записи и, по просьбе, протокол записи заново с ним.

    Отказ в пересборке — раньше правки: запрос «сохранить и пересобрать» не выполняется
    наполовину. Распознавание при пересборке не повторяется: подсказки терминов дойдут до него
    только при полном прогоне записи заново — его запускает администратор сервиса."""
    if bool(req.path) == bool(req.file):
        raise HTTPException(400, "словарь — либо папки, либо записи: укажите что-то одно")
    record = _dict_record(req.file, who) if req.file else None
    folder = "" if req.file else _dict_folder(req.path, who)
    home = _parent_of(req.file) if req.file else folder
    if not who.owns(home):
        raise HTTPException(403, NO_DICT_EDIT)
    if req.rebuild:
        if not req.file:
            raise HTTPException(400, "пересобирается протокол записи, а не папки: укажите запись")
        if not who.uploads(home):
            raise HTTPException(403, NO_REBUILD)
        if not record or record.get("status") not in ("ok", "no_summary"):
            raise HTTPException(409, "у записи ещё нет расшифровки — пересобирать нечего; словарь учтётся "
                                     "при обработке")
    if req.people is None and req.terms is None and not req.rebuild:
        raise HTTPException(400, "нечего менять: нужны участники, сокращения или пересборка протокола")

    if req.people is not None or req.terms is not None:
        scope, path = (dictionary.RECORDS, req.file) if req.file else (dictionary.FOLDERS, folder)
        old = words.get(scope, path)
        try:
            fresh_people = old["people"] if req.people is None else dictionary.parse_people(req.people)
            fresh_terms = old["terms"] if req.terms is None else dictionary.parse_terms(req.terms)
            if req.add:
                fresh_people = dictionary.joined(old["people"], fresh_people, "name", dictionary.PEOPLE_MAX)
                fresh_terms = dictionary.joined(old["terms"], fresh_terms, "term", dictionary.TERMS_MAX)
        except dictionary.DictionaryError as e:
            raise HTTPException(400, str(e))
        await asyncio.to_thread(words.put, scope, path, fresh_people, fresh_terms, who.login)
        log(f"словарь {'записи' if req.file else 'папки'} «{path}»: участников {len(fresh_people)}, "
            f"сокращений {len(fresh_terms)} ({who.login}" + (f", ключ «{who.key}»" if who.key else "") + ")")
        # в журнал действий — кто, где и сколько строк; самих имён и слов там нет
        jot("dictionary", who, **({"file": req.file} if req.file else {"folder": folder}),
            people=len(fresh_people), terms=len(fresh_terms))
    rebuilt = {"rebuild": _queue_rebuild(req.file, record, who)} if req.rebuild else {}
    return {"ok": True, **_dict_view(who, folder, req.file, record), **rebuilt}


def _dict_read(path: str, file: str, who: users.Access) -> dict:
    if bool(path) == bool(file):
        raise HTTPException(400, "словарь — либо папки, либо записи: укажите что-то одно")
    if file:
        return _dict_view(who, file=file, record=_dict_record(file, who))
    return _dict_view(who, folder=_dict_folder(path, who))


@app.get("/api/dictionary")
def dictionary_read(path: str = "", file: str = "", who: users.Access = Depends(viewer)):
    """Словарь папки (path) или записи (file) — окну на странице."""
    return _dict_read(path, file, who)


@app.post("/api/dictionary")
async def dictionary_save(req: DictionarySave, who: users.Access = Depends(viewer)):
    return await _dict_save(req, who)


class DictionaryBody(BaseModel):
    people: Any = None
    terms: Any = None


@app.get("/api/v1/dictionary")
def v1_dictionary(dir: str = "", file: str = "", who: users.Access = Depends(viewer)):
    """То же для ИИ-клиентов и программ: словарь папки (dir) или записи (file)."""
    return _dict_read(dir, file, who)


@app.put("/api/v1/dictionary")
async def v1_dictionary_save(body: DictionaryBody = DictionaryBody(), dir: str = "", file: str = "", add: bool = False,
                             rebuild: bool = False, who: users.Access = Depends(viewer)):
    """Словарь папки или записи целиком (или дополнение — add=true); тело — JSON с people и terms.
    rebuild=true — пересобрать протокол записи с новым словарём: вместе с правкой или отдельным
    запросом без тела. Права — те же, что у страницы."""
    return await _dict_save(DictionarySave(path=dir, file=file, people=body.people, terms=body.terms,
                                           add=add, rebuild=rebuild), who)


def _day(value: str) -> str:
    try:
        return datetime.date.fromisoformat(value).isoformat() if value else ""
    except ValueError:
        raise HTTPException(400, "дата — в виде ГГГГ-ММ-ДД")


@app.get("/api/v1/costs", dependencies=[Depends(costs_guard)])
@app.get("/api/admin/report", dependencies=[Depends(costs_guard)])
async def admin_report(start: str = "", end: str = "", bucket: str = "day"):
    """Расходы за период: по папкам, людям, записям, этапам, поставщикам и по времени.
    Каждая сумма — по валютам, как её вернул поставщик: рубли и доллары не пересчитываются
    и не складываются. Второй адрес — тот же отчёт для внешних систем. Оба — только
    администратору сервиса: самому или его ключу «с управлением»."""
    if bucket not in journal.BUCKETS:
        raise HTTPException(400, "bucket: " + " | ".join(journal.BUCKETS))
    return {**await asyncio.to_thread(trail.report, _day(start), _day(end), bucket),
            "names": {u["login"]: u["name"] for u in people.public()}}


async def _costbook(start: str, end: str, bucket: str, who: users.Access) -> tuple[list, str, str]:
    """Отчёт о расходах для файла — весь, по выбранным периоду и шагу; с ним границы периода для имени файла."""
    if bucket not in journal.BUCKETS:
        raise HTTPException(400, "bucket: " + " | ".join(journal.BUCKETS))
    start, end = _day(start), _day(end)
    runs = await asyncio.to_thread(trail.within, start, end)
    book = costbook.sheets(journal.summary(runs, start, end, bucket), runs,
                           {u["login"]: u["name"] for u in people.public()},
                           datetime.datetime.now().astimezone().replace(microsecond=0), who.login)
    return book, start, end


@app.get("/api/admin/report.xlsx", dependencies=[Depends(costs_guard)])
async def admin_report_xlsx(start: str = "", end: str = "", bucket: str = "day", who: users.Access = Depends(viewer)):
    """Отчёт о расходах книгой Excel — целиком: сводка, по периодам, папкам, людям, записям,
    этапам, поставщикам и журнал прогонов, каждый разрез на своём листе."""
    book, start, end = await _costbook(start, end, bucket, who)
    return Response(await asyncio.to_thread(costbook.to_xlsx, book), media_type=costbook.XLSX, headers={
        "Content-Disposition": f'attachment; filename="{costbook.name(start, end, "xlsx")}"'})


@app.get("/api/admin/report.csv", dependencies=[Depends(costs_guard)])
async def admin_report_csv(start: str = "", end: str = "", bucket: str = "day", who: users.Access = Depends(viewer)):
    """Тот же отчёт одним файлом CSV: те же таблицы подряд, у каждой название и шапка."""
    book, start, end = await _costbook(start, end, bucket, who)
    return Response(costbook.to_csv(book), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{costbook.name(start, end, "csv")}"'})


@app.get("/api/admin/journal", dependencies=[Depends(admin_guard)])
async def admin_journal(limit: int = 200, kind: str = "", by: str = "", file: str = "", dir: str = "",
                        login: str = "", since: str = "", until: str = ""):
    """Последние действия людей: входы, загрузки, папки, права, пользователи. Отбор — по виду
    события (kind), по тому, кто сделал (by), по записи (file), папке с подпапками (dir),
    по пользователю, о котором событие (login), и по дням (since, until)."""
    return {"events": await asyncio.to_thread(trail.actions, max(1, min(limit, 2000)), kind, by, file,
                                              users.norm_folder(dir), login, _day(since), _day(until))}


# ---------- оформление: логотип и название организации ----------
# Только корпоративный режим: в личном режиме раздела нет, и адреса отвечают 404.
# Посмотреть может администратор сервиса и его ключ «с управлением»; менять — только сам
# администратор на странице: логотип видят все, включая страницу входа.

def _brand_on() -> None:
    if cfg.personal:
        raise HTTPException(404, "в личном режиме оформление не меняется")


async def _body(request: Request, limit: int, too_big: str) -> bytes:
    """Тело запроса целиком, но не больше limit: лишнее не читается и в память не попадает."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise HTTPException(413, too_big)
    data = bytearray()
    async for piece in request.stream():
        data += piece
        if len(data) > limit:
            raise HTTPException(413, too_big)
    return bytes(data)


def _brand_view() -> dict:
    return {**look.view(),
            "public": _brand_public(), "requirements": brand.REQUIREMENTS, "svg": brand.SVG_REFUSED,
            "state_kept": state_kept}


@app.get("/api/admin/brand", dependencies=[Depends(admin_guard)])
def admin_brand():
    _brand_on()
    return _brand_view()


@app.put("/api/admin/brand/logo", dependencies=[Depends(page_admin)])
async def admin_brand_logo(request: Request, who: users.Access = Depends(viewer)):
    """Логотип: тело запроса — сам файл. Формат определяется по содержимому, размеры и
    целость проверяются, наружу отдаются только копии, собранные сервисом (brand.py)."""
    _brand_on()
    data = await _body(request, brand.MAX_BYTES, "файл больше 2 МБ — уменьшите картинку")
    try:
        info = await asyncio.to_thread(look.set_logo, data, who.login)
    except brand.BrandError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"не удалось записать логотип в папку состояния: {e}")
    log(f"оформление: логотип заменён ({who.login}) — {info['format']}, {info['width']}×{info['height']}, "
        f"{info['bytes'] / 1024:.0f} КБ")
    jot("brand_logo", who, format=info["format"], width=info["width"], height=info["height"], size=info["bytes"])
    return {"ok": True, "advice": info["advice"], **_brand_view()}


class BrandName(BaseModel):
    name: str = ""


@app.post("/api/admin/brand/name", dependencies=[Depends(page_admin)])
async def admin_brand_name(req: BrandName, who: users.Access = Depends(viewer)):
    """Короткое название организации: рядом с логотипом вместо надписи, в заголовке вкладки,
    в письмах и в колонтитуле скачиваемых файлов. Пусто — убрать."""
    _brand_on()
    was = look.name()
    try:
        name = await asyncio.to_thread(look.set_name, req.name, who.login)
    except brand.BrandError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"не удалось записать оформление в папку состояния: {e}")
    log(f"оформление: название организации — «{name}» ({who.login})" if name
        else f"оформление: название организации убрано ({who.login})")
    jot("brand_name", who, name=name, was=was)
    return {"ok": True, **_brand_view()}


@app.post("/api/admin/brand/reset", dependencies=[Depends(page_admin)])
async def admin_brand_reset(who: users.Access = Depends(viewer)):
    """Вернуть стандартное оформление: логотип и надпись сервиса, без названия организации."""
    _brand_on()
    try:
        await asyncio.to_thread(look.reset, who.login)
    except OSError as e:
        raise HTTPException(500, f"не удалось записать оформление в папку состояния: {e}")
    log(f"оформление: возвращено стандартное ({who.login})")
    jot("brand_reset", who)
    return {"ok": True, **_brand_view()}


# ---------- шаблоны протокола, задания модели и словарь ----------
# Оба режима. Файлы лежат в папке prompts и правятся ещё и в обход страницы, поэтому
# страница каждый раз читает сами файлы. Читать может администратор сервиса и его ключ
# «с управлением»; менять — только сам администратор на странице: шаблон определяет, что
# модель напишет в каждом протоколе. Старые протоколы от правки не меняются — меняются те, что соберут после неё.

def _default_text(file: str) -> str | None:
    """Стандартный текст из образа (prompts_default); None — у этого файла стандартного нет."""
    return templates.read(DEFAULT_PROMPTS / file)


def _template_item(item: str, meta: dict, notes: dict) -> dict:
    file = meta["file"]
    path = cfg.prompts_dir / file
    text, default = templates.read(path), _default_text(file)
    kind = templates.kind_of(item)
    out = {"id": item, "kind": kind, "title": meta["title"], "when": meta.get("when", ""), "file": file,
           "custom": bool(meta.get("custom")), "actions": bool(meta.get("actions")),
           "has_default": default is not None, "exists": text is not None,
           "chars": len(text or ""), "backups": templates.backups(cfg.prompts_dir, file),
           "state": "missing", "changed_at": "", "by": "", "outside": False, "problem": "", "warnings": []}
    if text is None:
        out["problem"] = "файла нет в папке шаблонов" + (" — верните стандартный" if default is not None else "")
        return out
    mine = templates.digest(text)
    out["state"] = ("custom" if default is None else
                    "standard" if mine == templates.digest(default) else "changed")
    out["changed_at"] = datetime.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    note = notes.get(item)
    if note and note.get("digest") == mine:
        out["by"], out["changed_at"] = note.get("by", ""), note.get("at") or out["changed_at"]
    elif out["state"] != "standard":
        out["outside"] = True                 # текст не тот, что сохраняли на странице: правили файлом
    try:
        out["warnings"] = templates.check(kind, text)
    except templates.TemplateError as e:
        out["problem"] = str(e)
    return out


def _templates_view() -> dict:
    listed, problem = templates.types_listed(cfg.prompts_dir), templates.read_custom(cfg.prompts_dir)[1]
    notes = options.template_notes()
    return {"items": [_template_item(k, m, notes) for k, m in {**listed, **templates.SERVICE}.items()],
            "problem": problem, "writable": os.access(cfg.prompts_dir, os.W_OK),
            "limits": {"chars": templates.MAX_CHARS, "upload_bytes": templates.UPLOAD_MAX_BYTES,
                       "keep": templates.KEEP, "title": templates.TITLE_MAX, "when": templates.WHEN_MAX}}


def _template_file(item: str) -> str:
    try:
        return templates.file_of(cfg.prompts_dir, item)
    except templates.TemplateError as e:
        raise HTTPException(404, str(e))


@app.get("/api/admin/templates", dependencies=[Depends(admin_guard)])
async def admin_templates():
    """Шаблоны, задания и словарь: что лежит в файлах сейчас, чем отличается от стандартного,
    кто и когда менял, какие есть прошлые версии."""
    return await asyncio.to_thread(_templates_view)


def _template_text(item: str, version: str) -> str:
    file = _template_file(item)
    if version == "current":
        text = templates.read(cfg.prompts_dir / file)
    elif version == "default":
        text = _default_text(file)
    else:
        try:
            text = templates.backup_text(cfg.prompts_dir, file, version)
        except templates.TemplateError as e:
            raise HTTPException(404, str(e))
    if text is None:
        raise HTTPException(404, "стандартного текста у этого шаблона нет: это свой тип" if version == "default"
                            else "файла нет в папке шаблонов")
    return text


@app.get("/api/admin/templates/text", dependencies=[Depends(admin_guard)])
async def admin_template_text(id: str, version: str = "current", download: bool = False):
    """Текст шаблона: version=current — как в файле сейчас, default — стандартный из образа,
    иначе — метка прошлой версии. download — отдать файлом."""
    text = await asyncio.to_thread(_template_text, id, version)
    if not download:
        return {"id": id, "version": version, "text": text}
    name = _template_file(id)
    return Response(text, media_type="text/plain; charset=utf-8", headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"attachment; filename=\"{name}\""})


def _template_store(item: str, text: str, who: users.Access, how: str) -> dict:
    file = _template_file(item)
    try:
        warnings = templates.check(templates.kind_of(item), text)
        changed = templates.save(cfg.prompts_dir, file, text)
    except templates.TemplateError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"не удалось записать файл в папку шаблонов: {e}")
    saved = templates.read(cfg.prompts_dir / file) or ""
    options.note_template(item, who.login, templates.digest(saved))
    if changed:
        log(f"шаблоны: {file} изменён ({who.login}, {HOW[how]}), знаков {len(saved)}"
            + (f"; предупреждений {len(warnings)}" if warnings else ""))
        jot("template_save", who, item=item, file=file, how=how, chars=len(saved), warnings=len(warnings))
    return {"ok": True, "changed": changed, "warnings": warnings, **_templates_view()}


HOW = {"page": "правка на странице", "file": "загрузка файла", "default": "возврат стандартного",
       "backup": "возврат прошлой версии"}


class TemplateSave(BaseModel):
    id: str
    text: str


@app.post("/api/admin/templates/save", dependencies=[Depends(page_admin)])
async def admin_template_save(req: TemplateSave, who: users.Access = Depends(viewer)):
    """Текст из поля правки. Обязательного нет — отказ с объяснением; желательного нет —
    сохраняется с предупреждениями. Прежний текст остаётся в прошлых версиях."""
    return await asyncio.to_thread(_template_store, req.id, req.text, who, "page")


@app.put("/api/admin/templates/upload", dependencies=[Depends(page_admin)])
async def admin_template_upload(request: Request, id: str, who: users.Access = Depends(viewer)):
    """Шаблон файлом (.md или .txt в UTF-8): тело запроса — сам файл."""
    _template_file(id)
    data = await _body(request, templates.UPLOAD_MAX_BYTES,
                       f"файл больше {templates.UPLOAD_MAX_BYTES // 1024} КБ — шаблон такого размера сервис не принимает")
    try:
        text = templates.decode(data)
    except templates.TemplateError as e:
        raise HTTPException(400, str(e))
    return await asyncio.to_thread(_template_store, id, text, who, "file")


class TemplateRestore(BaseModel):
    id: str
    version: str          # default — стандартный из образа; иначе метка прошлой версии


@app.post("/api/admin/templates/restore", dependencies=[Depends(page_admin)])
async def admin_template_restore(req: TemplateRestore, who: users.Access = Depends(viewer)):
    """Вернуть стандартный текст или одну из прошлых версий. Текущий текст при этом сам
    уходит в прошлые версии — возврат тоже можно отменить."""
    if req.version == "current":
        raise HTTPException(400, "нужна версия: default или метка прошлой версии")
    text = await asyncio.to_thread(_template_text, req.id, req.version)
    return await asyncio.to_thread(_template_store, req.id, text, who,
                                   "default" if req.version == "default" else "backup")


class TemplateType(BaseModel):
    key: str = ""             # пусто — сервис составит из названия
    title: str
    when: str
    actions: bool = False     # выписывать поручения отдельным шагом перед протоколом, как у совещания
    new: bool = True
    base: str = ""            # с какого шаблона взять текст для нового типа
    text: str | None = None   # или сам текст


def _type_store(req: TemplateType, who: users.Access) -> dict:
    try:
        if not req.new:
            templates.change_type(cfg.prompts_dir, req.key, req.title, req.when, req.actions)
            log(f"шаблоны: описание своего типа «{req.key}» изменено ({who.login})")
            jot("type_change", who, item=req.key, title=" ".join(req.title.split()))
            return {"ok": True, "key": req.key, "warnings": [], **_templates_view()}
        text = req.text if req.text is not None else _template_text(req.base or "meeting", "current")
        warnings = templates.check("type", text)
        key = templates.add_type(cfg.prompts_dir, req.key, req.title, req.when, text, req.actions)
    except templates.TemplateError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"не удалось записать файл в папку шаблонов: {e}")
    options.note_template(key, who.login, templates.digest(templates.read(cfg.prompts_dir / f"{key}.md") or ""))
    log(f"шаблоны: добавлен свой тип «{key}» — {' '.join(req.title.split())} ({who.login})")
    jot("type_create", who, item=key, title=" ".join(req.title.split()), base=req.base if req.text is None else "")
    return {"ok": True, "key": key, "warnings": warnings, **_templates_view()}


@app.post("/api/admin/templates/type", dependencies=[Depends(page_admin)])
async def admin_template_type(req: TemplateType, who: users.Access = Depends(viewer)):
    """Свой тип записи: ключ, название, «когда применять» и текст шаблона. С этого момента
    разбор записи выбирает и из него, а ручной запуск показывает его в списке шаблонов."""
    return await asyncio.to_thread(_type_store, req, who)


class TemplateTypeDelete(BaseModel):
    key: str


def _type_delete(key: str, who: users.Access) -> dict:
    title = templates.types_listed(cfg.prompts_dir).get(key, {}).get("title", "")
    try:
        templates.remove_type(cfg.prompts_dir, key)
    except templates.TemplateError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"не удалось изменить папку шаблонов: {e}")
    options.note_template(key, who.login, None)
    log(f"шаблоны: свой тип «{key}» удалён ({who.login}); текст шаблона — в папке {templates.HISTORY_DIR}")
    jot("type_delete", who, item=key, title=title)
    return {"ok": True, **_templates_view()}


@app.post("/api/admin/templates/type/delete", dependencies=[Depends(page_admin)])
async def admin_template_type_delete(req: TemplateTypeDelete, who: users.Access = Depends(viewer)):
    """Убрать свой тип. Записи, обработанные по нему, остаются как есть; при пересборке такой
    записи шаблон заново выбирает разбор из оставшихся типов. Стандартные типы не удаляются."""
    return await asyncio.to_thread(_type_delete, req.key, who)


class RunRequest(BaseModel):
    file: str
    # пусто — шаблон выберет разбор записи, как при автоматическом запуске
    template: str | None = None
    force: bool = False
    # модель распознавания для этого запуска; пусто — та, что выбрана для новых записей
    model: str | None = None


class SpeechModelRequest(BaseModel):
    model: str | None = None        # None — вернуть к значению из .env


@app.post("/api/admin/speech-model", dependencies=[Depends(admin_guard)])
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


@app.post("/api/admin/run", dependencies=[Depends(admin_guard)])
async def admin_run(req: RunRequest, who: users.Access = Depends(viewer)):
    assert service is not None
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
    if any(i.path == rel for i in service.queue) or _in_work(rel):
        raise HTTPException(409, "этот файл уже в очереди или обрабатывается")
    service.enqueue_manual(rel, req.template, req.force, who.login, "manual", req.model or "")
    return {"ok": True, "queued": rel}
