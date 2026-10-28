"""Приложение FastAPI: отказы по-русски, заголовки и проверки на каждый запрос, приведение путей."""
from __future__ import annotations

import ipaddress
import json
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import upload
from . import core
from .common import CSP, HSTS, NO_STORE
from .core import cfg
from .lifecycle import lifespan


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


# Личный режим входа не спрашивает: сервис отдаёт всё любому, кто до него достучался. Достучаться может
# и чужой сайт, открытый в браузере на этом же компьютере. Его домен переводят на 127.0.0.1 (DNS
# rebinding) — и страница чужого сайта читает сервис как свой. Поэтому в личном режиме сервис отвечает
# только по своим адресам: localhost (и имена *.localhost), адрес IP (его чужой домен не подменит), имя
# компьютера без точки (так его открывают из локальной сети) и адрес из PUBLIC_URL. В корпоративном
# режиме без входа сервис не отдаёт ничего, а кука входа к чужому домену не уходит.
#
# Изменяющий запрос со страницы другого сайта (CSRF) сервис не выполняет в обоих режимах. Браузер сам
# прикладывает к запросу куку входа, с какой бы страницы тот ни ушёл, — и с соседнего поддомена:
# SameSite=Lax его своим считает. В личном режиме и куки не нужно, а пересборка протокола платная.
# Чужая страница — заголовок Origin не того адреса, по которому сервис открыт, и не PUBLIC_URL; Origin
# срезан (прокси, расширение браузера) — Sec-Fetch-Site «cross-site» или «same-site». Запрос с ключом
# или паролем в заголовке Authorization не проверяется: этот заголовок браузер сам не приложит, а чужой
# странице поставить его не даст без разрешения CORS, которого у сервиса нет. Программы (curl,
# ИИ-клиенты) Origin не шлют и работают как раньше. Вход и заявки с чужой страницы — тоже отказ.
FOREIGN_HOST = ("сервис открыт не по своему адресу: в личном режиме он отвечает по localhost, адресу IP, "
                "имени компьютера и адресу из PUBLIC_URL в .env")
FOREIGN_PAGE = "запрос пришёл со страницы другого сайта — сервис его не выполняет"
CHANGING = ("POST", "PUT", "PATCH", "DELETE")
FOREIGN_FETCH = ("cross-site", "same-site")


def _own_host(host: str) -> bool:
    """Свой ли это адрес сервиса — заголовок Host запроса."""
    try:
        name = urlsplit(f"//{host}").hostname or ""
    except ValueError:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return ("." not in name or name.endswith(".localhost")
            or bool(cfg.public_url) and name == urlsplit(cfg.public_url).hostname)


def _netloc(address: str) -> str:
    """Имя и порт из адреса — без порта по умолчанию: «https://Имя:443» и «https://имя» — один адрес."""
    parts = urlsplit(address)
    port = {"http": 80, "https": 443}.get(parts.scheme)
    netloc = parts.netloc.lower()
    return netloc.removesuffix(f":{port}") if port else netloc


def _own_page(origin: str, host: str) -> bool:
    """Пришёл ли запрос со страницы самого сервиса: Origin — тот адрес, по которому он открыт, или
    PUBLIC_URL (за прокси Host бывает внутренним). Origin «null» — неизвестно откуда, то есть не своя."""
    seen = _netloc(origin)
    return bool(seen) and (seen == _netloc(f"{urlsplit(origin).scheme}://{host}")
                           or bool(cfg.public_url) and seen == _netloc(cfg.public_url))


def _foreign_page(request: Request) -> bool:
    """Изменяющий запрос, который браузер отправил со страницы другого сайта сам, без ключа."""
    if request.method not in CHANGING or "authorization" in request.headers:
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return request.headers.get("sec-fetch-site", "") in FOREIGN_FETCH
    return not _own_page(origin, request.headers.get("host", ""))


@app.middleware("http")
async def own_addresses_only(request: Request, call_next):
    if cfg.personal:
        host = request.headers.get("host", "")
        if host and not _own_host(host):
            return JSONResponse({"detail": FOREIGN_HOST}, status_code=400)
    if _foreign_page(request):
        return JSONResponse({"detail": FOREIGN_PAGE}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def protective_headers(request: Request, call_next):
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("Content-Security-Policy", CSP)
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "same-origin"
    if request.url.scheme == "https":
        headers["Strict-Transport-Security"] = HSTS
    if request.url.path.startswith("/api/") and "cache-control" not in headers:
        headers["Cache-Control"] = NO_STORE
    return response


# Путь записи (параметр file) — в составной форме Unicode (NFC), как ключи журнала обработки: сканер
# приводит к ней имена файлов и папок. macOS присылает «ё» и «й» разложенными (NFD) — буква и знак над ней
# отдельно: на вид то же, а запись под таким путём не нашлась бы ни одним методом. Поэтому путь приводится
# здесь, до всех методов, — и в адресе запроса, и в поле file тела JSON. Папки (dir, path) приводит к той же
# форме их общая проверка (upload.folder_arg). Не приводится путь, под которым сервис запись именно и знает:
# её двойник в составной форме лежит рядом (сервис оставил её имя как есть и сказал об этом в журнале
# работы), и найтись должна она сама, а не двойник — иначе удаление одной унесло бы другую.
def _one_form(file: str) -> str:
    if unicodedata.is_normalized("NFC", file) or core.service is None:
        return file
    known = (core.service.ledger.get(file) is not None or file in core.service.pending
             or any(i.path == file for i in list(core.service.queue)) or any(j["file"] == file for j in list(core.service.running)))
    return file if known else upload.nfc(file)


def _one_form_query(query: bytes) -> bytes:
    pairs = parse_qsl(query.decode("latin-1"), keep_blank_values=True)       # так же разбирает адрес Starlette
    fixed = [(name, _one_form(value) if name == "file" else value) for name, value in pairs]
    return query if fixed == pairs else urlencode(fixed).encode("ascii")


def _one_form_body(body: bytes) -> bytes:
    try:
        data = json.loads(body)
    except ValueError:
        return body                     # не JSON — отказ даст сам метод, тем же текстом, что и без приведения
    if not isinstance(data, dict) or not isinstance(data.get("file"), str) or _one_form(data["file"]) == data["file"]:
        return body
    return json.dumps({**data, "file": _one_form(data["file"])}, ensure_ascii=False).encode("utf-8")


class OneFormPaths:
    """Приведение пути записи к составной форме (_one_form) — для всех методов /api/ разом."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        if b"file=" in scope.get("query_string", b""):
            scope = {**scope, "query_string": _one_form_query(scope["query_string"])}
        headers = dict(scope["headers"])
        # тело читается целиком только у JSON: загрузку записи (поток байт) так держать в памяти нельзя
        if scope["method"] in CHANGING and b"json" in headers.get(b"content-type", b""):
            chunks = []
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    return                      # клиент ушёл, не дослав тело
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    break
            body = _one_form_body(b"".join(chunks))
            headers[b"content-length"] = str(len(body)).encode("ascii")
            scope = {**scope, "headers": list(headers.items())}
            whole = [{"type": "http.request", "body": body, "more_body": False}]

            async def replayed():
                return whole.pop() if whole else await receive()

            return await self.app(scope, replayed, send)
        return await self.app(scope, receive, send)


app.add_middleware(OneFormPaths)
