"""Приложение целиком: запуск, основные адреса, роли.

Остальные тесты проверяют модули по отдельности и app.web не импортируют, поэтому
ошибка запуска сервиса или забытая проверка прав до них не доходит. Здесь сервис
поднимается так же, как в контейнере: настройки из окружения, запуск сканера
и обработчика, запросы по HTTP.

app.web читает окружение при импорте, поэтому на каждый тест модуль импортируется
заново — с временными папками и нужным режимом. Наружу ничего не уходит: входная
папка пуста, сканер после первого обхода спит час, адрес провайдера — закрытый
порт на этой же машине.
"""
import dataclasses
import datetime
import importlib
import io
import json
import os
import re
import sys
import threading
import time
import types
import zipfile
from contextlib import contextmanager

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import backup, costbook, db, folders, keys, mail, marks, migrate, notify, pipeline, providers, settings, signup, upload, users, watcher
from tests import legacy

ADMIN_PASSWORD = "тест-админ-пароль"
PASSWORD = "пароль-1234"
HOME = users.HOME_ROOT

# Всё, что закрыто admin_guard в app/web.py: метод, адрес, тело запроса.
# test_admin_list_matches_the_routes следит, чтобы список не отстал от кода.
ADMIN_ONLY = [
    ("GET", "/api/admin/status", None),
    ("GET", "/api/admin/folders", None),
    ("POST", "/api/admin/folders/rename", {"dir": "Проекты", "name": "Новое имя"}),
    ("POST", "/api/admin/folders/delete", {"dir": "Проекты"}),
    ("POST", "/api/admin/users", None),
    ("POST", "/api/admin/users/save", {"login": "sidorov", "password": PASSWORD}),
    ("POST", "/api/admin/users/delete", {"login": "petrov"}),
    ("POST", "/api/admin/check", None),
    ("POST", "/api/admin/files", None),
    ("POST", "/api/admin/run", {"file": "запись.mp4"}),
    ("POST", "/api/admin/speech-model", {"model": "deepgram/nova-3"}),
    ("POST", "/api/admin/providers/check", None),
    ("GET", "/api/admin/report", None),
    ("GET", "/api/admin/report.csv", None),
    ("GET", "/api/admin/report.xlsx", None),
    ("GET", "/api/admin/journal", None),
    ("GET", "/api/admin/requests", None),
    ("POST", "/api/admin/requests/approve", {"id": "нет", "login": "sidorov", "name": "Сидоров"}),
    ("POST", "/api/admin/requests/reject", {"id": "нет"}),
    ("POST", "/api/admin/users/link", {"login": "petrov"}),
    ("POST", "/api/admin/mail/test", None),
    ("GET", "/api/admin/keys", None),
    ("POST", "/api/admin/keys/revoke", {"id": "нет"}),
    ("GET", "/api/admin/brand", None),
    ("PUT", "/api/admin/brand/logo", None),
    ("POST", "/api/admin/brand/name", {"name": "ООО Ромашка"}),
    ("POST", "/api/admin/brand/reset", None),
    ("GET", "/api/admin/templates", None),
    ("GET", "/api/admin/templates/text", None),
    ("POST", "/api/admin/templates/save", {"id": "meeting", "text": "## Раздел"}),
    ("PUT", "/api/admin/templates/upload", None),
    ("POST", "/api/admin/templates/restore", {"id": "meeting", "version": "default"}),
    ("POST", "/api/admin/templates/type", {"title": "Разбор инцидента", "when": "Разбирают один сбой: что случилось и почему."}),
    ("POST", "/api/admin/templates/type/delete", {"key": "incident"}),
    ("GET", "/api/admin/domain", None),
    ("POST", "/api/admin/domain/check", {}),
    ("POST", "/api/admin/users/domain", {"login": "petrov"}),
]
# Параметры адреса для запросов из ADMIN_ONLY, которым без них отвечать нечем.
QUERY = {"/api/admin/templates/text": {"id": "meeting"}, "/api/admin/templates/upload": {"id": "meeting"}}
# Лежит под /api/admin, но открыт хозяину папки, а не только администратору сервиса.
OWNER_NOT_ADMIN = {("POST", "/api/admin/names")}
# Отвечают без входа: проверка живости, сам вход и «кто я».
OPEN = {"/api/status", "/api/login", "/api/logout", "/api/me"}
# Оформление (логотип и название организации) видно уже на странице входа — оно открыто, как и сама страница.
OPEN |= {"/api/brand"}
# И то, чем пользуется человек, у которого входа ещё нет или пароль забыт: заявка на доступ,
# «Забыл пароль» и задание пароля по одноразовой ссылке из письма. Открыты намеренно;
# заявка и сброс работают только при включённой почте и ограничены по частоте.
OPEN |= {"/api/request", "/api/forgot", "/api/set-password", "/api/set-password/check"}
# Описание внешнего API: в нём только адрес этой установки и описание запросов — как в навыке.
# Что в нём нет ни внутренних адресов, ни данных, проверяет tests/test_apidoc.py.
OPEN |= {"/api/v1/openapi.json"}
# Всё, что отвечает без входа и лежит не под /api: страницы (они пусты, данные приходят запросами)
# и навык для ИИ-клиентов. Навык открыт намеренно: в нём только адрес этой установки и описание
# запросов, а ставит его клиент, у которого ключа ещё нет. Описания API от FastAPI среди них нет:
# оно выключено; страница /api строит себя из /api/v1/openapi.json.
PAGES = {"/", "/login", "/admin", "/tasks", "/help", "/install-guide", "/about", "/api", "/set-password", "/static"}
# Картинки логотипа организации: их показывает и страница входа. Отдаётся только PNG, собранный сервисом.
PAGES |= {"/brand/{version}/{name}"}
FASTAPI_DOCS = ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json")
SKILL_OPEN = {"/skill", "/skill/SKILL.md", "/skill/CHANGELOG.md", "/skill/podyachiy.zip"}
# Три уровня доступа: страница (сам человек) / ключ «с управлением» / обычный ключ.
# test_key_levels_match_the_routes обходит все адреса приложения обоими видами ключа администратора
# и сверяет отказы с этими списками: новый адрес не откроется ключу, пока его сюда не впишут.
#
# PAGE_ONLY — только сам человек на странице, никаким ключом: ключи доступа, пароли и ссылки на
# пароль, удаление записей и папок, отправка протокола на почту, свои письма о готовности.
PAGE_ONLY = {
    ("GET", "/api/keys"), ("POST", "/api/keys"), ("POST", "/api/keys/revoke"),
    ("POST", "/api/admin/keys/revoke"),
    ("POST", "/api/password"), ("POST", "/api/admin/users/link"),
    ("POST", "/api/record/delete"), ("POST", "/api/admin/folders/delete"),
    # протокол на почту — содержимое совещания на произвольные адреса
    ("GET", "/api/record/recipients"), ("POST", "/api/record/send"),
    ("POST", "/api/notify"),
    # оформление видят все, включая страницу входа; шаблон определяет, что модель напишет в каждом
    # протоколе: менять их — только со страницы (читать шаблоны и оформление ключ «с управлением» может)
    ("PUT", "/api/admin/brand/logo"), ("POST", "/api/admin/brand/name"), ("POST", "/api/admin/brand/reset"),
    ("POST", "/api/admin/templates/save"), ("PUT", "/api/admin/templates/upload"),
    ("POST", "/api/admin/templates/restore"), ("POST", "/api/admin/templates/type"),
    ("POST", "/api/admin/templates/type/delete"),
    # вход через домен: в проверку подключения вводят чужой доменный пароль, а привязка к домену
    # стирает у человека пароль сервиса (смотреть состояние ключ «с управлением» может)
    ("POST", "/api/admin/domain/check"), ("POST", "/api/admin/users/domain"),
}
# MANAGE_KEY — управление сервисом: человеку и его ключу «с управлением»; обычному ключу закрыто.
# Сюда же входят расходы для внешних систем (/api/v1/costs): их видит только администратор
# сервиса (test_costs_are_for_the_service_admin_only).
MANAGE_KEY = ({(m, p) for m, p, _ in ADMIN_ONLY} - PAGE_ONLY) | {
    ("GET", "/api/access"), ("POST", "/api/access"), ("GET", "/api/v1/costs"),
}
# Всё остальное открыто любому ключу в рамках прав человека: чтение, загрузка, переименование
# и перенос записи, отметки и сроки поручений, подписи говорящих.
# Всё, что отдаёт суммы: отчёт для внешних систем, он же для страницы и он же файлом.
COSTS = ("/api/v1/costs", "/api/admin/report", "/api/admin/report.csv", "/api/admin/report.xlsx")


@contextmanager
def running(tmp_path, mode="corporate", extra=None, kept=True):
    """Сервис с чистыми временными папками. Отдаёт модуль app.web и клиента без входа.
    extra — добавка к окружению, например настройки почты; kept — подключена ли папка состояния с хоста."""
    env = {
        "MODE": mode,
        "INBOX_DIR": str(tmp_path / "inbox"),
        "OUTBOX_DIR": str(tmp_path / "outbox"),
        "STATE_DIR": str(tmp_path / "state"),
        "PROMPTS_DIR": str(tmp_path / "prompts"),
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
        "OPENAI_API_KEY": "sk-test-not-a-key",
        "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        "SPEECH_MODEL": "deepgram/nova-3",
        "SCAN_INTERVAL_SEC": "3600",
        "STABLE_SEC": "3600",
    }
    (tmp_path / "inbox").mkdir(parents=True, exist_ok=True)     # второй запуск над теми же папками — перезапуск сервиса
    with pytest.MonkeyPatch.context() as patch:
        for key, value in env.items():
            patch.setenv(key, value)
        patch.delenv("OPENROUTER_API_KEY", raising=False)
        for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_SECURITY",
                    "PUBLIC_URL", "ADMIN_EMAIL", "MAIL_DOMAINS", "BACKUP_BASE_URL", "BACKUP_API_KEY", "SUMMARY_BASE_URL",
                    "SUMMARY_API_KEY", "COST_CURRENCY", "LDAP_URL", "LDAP_BASE_DN", "LDAP_BIND_DN",
                    "LDAP_BIND_PASSWORD", "LDAP_USER_FILTER", "LDAP_ACCESS_GROUP", "LDAP_ADMIN_GROUP", "LDAP_CA_FILE",
                    "LDAP_TLS_VERIFY", "LDAP_STARTTLS", "LDAP_ALLOW_HTTP", "LDAP_DOMAIN", "LDAP_RECHECK_MIN"):
            patch.delenv(key, raising=False)
        for key, value in (extra or {}).items():
            patch.setenv(key, value)
        # Папка состояния во временной папке — не точка подключения: сервис решил бы, что база
        # пропадёт вместе с контейнером, и отказался бы запускаться. В тестах она «подключена»;
        # сам отказ и проверку блокировок (она запускает второй процесс) проверяют отдельные тесты.
        patch.setattr(db, "kept", lambda path: kept)
        patch.setattr(db, "probe", lambda path: None)
        sys.modules.pop("app.web", None)
        # app.web при импорте подставляет почте свою функцию «название организации»; после теста
        # возвращаем прежнюю, чтобы оформление одного запуска не подписывало письма следующих тестов
        patch.setattr(mail, "organization", mail.organization)
        web = importlib.import_module("app.web")
        patch.setattr(web, "WRONG_PASSWORD_DELAY_SEC", 0)
        try:
            with TestClient(web.app) as client:
                yield web, client
        finally:
            sys.modules.pop("app.web", None)


@pytest.fixture
def site(tmp_path):
    with running(tmp_path) as (web, client):
        yield web, client


def enter(web, login, password=PASSWORD):
    """Отдельный браузер: вошёл и дальше ходит со своей кукой."""
    client = TestClient(web.app)
    assert client.post("/api/login", json={"login": login, "password": password}).status_code == 200
    return client


def staffed(site):
    """Администратор и два пользователя, каждый в своём браузере. Обоим разрешена загрузка
    в личную папку: без неё человек только читает (это проверяют тесты раздельных прав)."""
    web, _ = site
    admin = enter(web, "admin", ADMIN_PASSWORD)
    for login, name in (("ivanov", "Иванов"), ("petrov", "Петров")):
        saved = admin.post("/api/admin/users/save", json={"login": login, "name": name, "password": PASSWORD,
                                                          "home_upload": True})
        assert saved.status_code == 200
    return web, admin, enter(web, "ivanov"), enter(web, "petrov")


def call(client, method, path, body=None, **kw):
    return client.request(method, path, json=body, **kw)


def dir_names(client, path=""):
    return [d["name"] for d in client.get("/api/browse", params={"path": path}).json()["dirs"]]


ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def seed(web):
    """Шаблоны из поставки — в папку шаблонов, как при первом запуске из образа: в образе они
    лежат в prompts_default, в репозитории — в prompts."""
    web.DEFAULT_PROMPTS = ROOT / "prompts"
    web._seed_prompts()


def api_routes(web):
    return sorted((method, route.path) for route in web.app.routes
                  for method in getattr(route, "methods", None) or ()
                  if route.path.startswith("/api/") and method not in ("HEAD", "OPTIONS"))


# ---------- запуск и открытые адреса ----------

def test_service_starts_and_answers_the_health_check_without_login(site):
    web, client = site
    assert client.get("/api/status").json() == {"ok": True, "version": (ROOT / "VERSION").read_text().strip()}
    assert web.service is not None and web.service.queue == [] and web.upload_ready


def test_pages_and_static_files_are_served(site):
    _, client = site
    for path in ("/", "/login", "/admin", "/tasks", "/help", "/install-guide", "/about", "/api", "/set-password"):
        page = client.get(path)
        assert page.status_code == 200 and "text/html" in page.headers["content-type"], path
        assert page.headers["cache-control"] == "no-cache"
    for name in ("common.js", "app.css", "logo.png", "favicon.png", "fonts/Vertograd.woff2"):
        assert client.get(f"/static/{name}").status_code == 200, name


# ---------- вход ----------

def test_every_closed_address_answers_401_without_login(site):
    """Новый адрес без проверки входа сюда попадёт сам: список берётся из приложения.
    Адрес, открытый намеренно, нужно дописать в OPEN."""
    web, client = site
    closed = [(m, p) for m, p in api_routes(web) if p not in OPEN]
    assert len(closed) > 20
    for method, path in closed:
        assert client.request(method, path).status_code == 401, f"{method} {path}"


def test_wrong_password_is_refused_and_written_to_the_journal(site):
    web, client = site
    for login, password in (("admin", "не тот"), ("nobody", PASSWORD), ("admin", "")):
        assert client.post("/api/login", json={"login": login, "password": password}).status_code == 401
    assert users.COOKIE not in client.cookies
    assert client.get("/api/me").json()["login"] is None
    assert client.get("/api/browse", auth=("admin", "не тот")).status_code == 401
    assert [e["kind"] for e in web.trail.read()] == ["login_failed"] * 3


def test_admin_login_sets_the_cookie_and_me_shows_him(site):
    _, client = site
    entered = client.post("/api/login", json={"login": " Admin ", "password": ADMIN_PASSWORD})
    assert entered.json() == {"login": "admin", "name": "Администратор", "admin": True}
    assert "httponly" in entered.headers["set-cookie"].lower() and users.COOKIE in client.cookies
    me = client.get("/api/me").json()
    assert me["login"] == "admin" and me["admin"] and me["personal"] is False and me["home"] is None
    assert client.get("/api/admin/status").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/browse").status_code == 401


def test_everything_outside_the_api_is_a_known_open_address(site):
    """Новый адрес не под /api проверка входа выше не видит — он должен быть назван здесь."""
    web, client = site
    outside = {route.path for route in web.app.routes if not route.path.startswith("/api/")}
    assert outside == PAGES | SKILL_OPEN
    for path in SKILL_OPEN:
        assert client.get(path).status_code == 200, path


def test_fastapi_docs_are_switched_off(site):
    """Список всех адресов сервиса без входа не отдаётся — ни вошедшему, ни постороннему."""
    _, client = site
    for path in FASTAPI_DOCS:
        assert client.get(path).status_code == 404, path
    client.post("/api/login", json={"login": "admin", "password": ADMIN_PASSWORD})
    for path in FASTAPI_DOCS:
        assert client.get(path).status_code == 404, path


def test_http_basic_opens_the_external_address(site):
    _, client = site
    answer = client.get("/api/v1/records", auth=("admin", ADMIN_PASSWORD))
    assert answer.status_code == 200 and answer.json()["records"] == []
    assert client.get("/api/v1/records", params={"since": "вчера"}, auth=("admin", ADMIN_PASSWORD)).status_code == 400
    assert client.get("/api/v1/records").status_code == 401          # вход запросом куку не оставил


# ---------- пользователи и роли ----------

def test_admin_creates_a_user_who_then_logs_in(site):
    web, admin, ivanov, _ = staffed(site)
    me = ivanov.get("/api/me").json()
    assert me["login"] == "ivanov" and me["name"] == "Иванов" and not me["admin"]
    assert me["home"] == f"{HOME}/ivanov" and (web.cfg.inbox / HOME / "ivanov").is_dir()
    listed = admin.post("/api/admin/users").json()["users"]
    assert [u["login"] for u in listed] == ["ivanov", "petrov"] and "password" not in listed[0]
    assert listed[0]["has_password"] and listed[0]["email"] == "" and "link" not in listed[0]
    short = admin.post("/api/admin/users/save", json={"login": "sidorov", "password": "123"})
    assert short.status_code == 400
    assert admin.post("/api/admin/users/save", json={"login": "admin", "password": PASSWORD}).status_code == 400


def test_service_does_not_start_when_the_state_folder_is_not_mounted(tmp_path, monkeypatch):
    """В папке состояния база: внутри контейнера она пропала бы при его пересоздании, и все записи
    пошли бы в обработку заново. Сервис с такой папкой не запускается и называет причину."""
    with running(tmp_path) as (web, _):                     # обычный запуск: папка «подключена»
        assert web.state_kept and (tmp_path / "state" / db.FILE).is_file()
    for mode in ("corporate", "personal"):
        with pytest.raises(SystemExit, match="не подключена с хоста"):
            with running(tmp_path / mode, mode, kept=False):
                pytest.fail("сервис запустился без подключённой папки состояния")
        assert not (tmp_path / mode / "state" / db.FILE).exists()                # и базу в контейнере не завёл
        assert "ОТКАЗ ЗАПУСКА" in (tmp_path / mode / "outbox" / "_logs" / "podyachiy.log").read_text(encoding="utf-8")


def on_disk(web) -> str:
    """Всё, что база держит на диске, одним текстом: сам файл и журнал незакрытых записей."""
    return "".join(path.read_bytes().decode("utf-8", "replace")
                   for path in sorted(web.cfg.state_dir.glob(db.FILE + "*")))


def test_admin_list_matches_the_routes(site):
    web, _ = site
    guarded = {(m, p) for m, p in api_routes(web)
               if p.startswith("/api/admin/")} - OWNER_NOT_ADMIN
    assert {(m, p) for m, p, _ in ADMIN_ONLY} == guarded


def test_ordinary_user_gets_403_on_everything_for_the_admin(site):
    web, admin, ivanov, _ = staffed(site)
    admin.post("/api/folder", json={"dir": "Проекты"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "tree", "manage": "tree"})
    for method, path, body in ADMIN_ONLY:      # даже администратор папки — не администратор сервиса
        assert call(ivanov, method, path, body, params=QUERY.get(path)).status_code == 403, f"{method} {path}"
        assert call(site[1], method, path, body, auth=("ivanov", PASSWORD),
                    params=QUERY.get(path)).status_code == 403, f"basic {path}"
    assert sorted(web.people.users) == ["ivanov", "petrov"] and (web.cfg.inbox / "Проекты").is_dir()


def test_service_admin_made_by_the_checkbox_passes_everywhere(site):
    web, admin, ivanov, _ = staffed(site)
    seed(web)
    admin.post("/api/admin/users/save", json={"login": "ivanov", "name": "Иванов", "admin": True})
    assert ivanov.get("/api/me").json()["admin"]
    for method, path, body in ADMIN_ONLY:
        if method == "GET":
            assert call(ivanov, method, path, params=QUERY.get(path)).status_code == 200, path


def test_password_change_and_user_removal_end_old_sessions(site):
    web, admin, ivanov, petrov = staffed(site)
    assert ivanov.post("/api/password", json={"old": "не тот", "new": "новый-пароль-1"}).status_code == 400
    assert ivanov.post("/api/password", json={"old": PASSWORD, "new": "новый-пароль-1"}).status_code == 200
    assert ivanov.get("/api/browse").status_code == 401            # старая кука больше не подходит
    assert enter(web, "ivanov", "новый-пароль-1").get("/api/browse").status_code == 200
    assert admin.post("/api/password", json={"old": ADMIN_PASSWORD, "new": "новый-пароль-1"}).status_code == 400
    assert admin.post("/api/admin/users/delete", json={"login": "petrov"}).status_code == 200
    assert petrov.get("/api/browse").status_code == 401
    assert (web.cfg.inbox / HOME / "petrov").is_dir()              # личная папка остаётся на диске


# ---------- папки и права ----------

def test_shared_folder_is_hidden_until_reading_is_granted(site):
    web, admin, ivanov, _ = staffed(site)
    assert admin.post("/api/folder", json={"dir": "Проекты"}).json() == {"ok": True, "path": "Проекты"}
    assert admin.post("/api/folder", json={"dir": "Проекты"}).status_code == 409
    assert "Проекты" in dir_names(admin) and "Проекты" not in dir_names(ivanov)
    assert not ivanov.get("/api/browse", params={"path": "Проекты"}).json()["upload"]["enabled"]
    assert ivanov.get("/api/access", params={"dir": "Проекты"}).status_code == 403

    granted = admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "tree"})
    assert granted.status_code == 200
    assert {u["login"]: u["read"] for u in granted.json()["users"]} == {"ivanov": "tree", "petrov": "none"}
    assert "Проекты" in dir_names(ivanov)
    inside = ivanov.get("/api/browse", params={"path": "Проекты"}).json()
    # чтение загрузки не даёт: она выдаётся отдельно
    assert not inside["upload"]["enabled"] and not inside["mkdir"] and not inside["manage"]
    assert "право загрузки" in inside["upload"]["denied"]

    assert admin.post("/api/access", json={"dir": "Проекты", "login": "nobody", "read": "tree"}).status_code == 400
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "none"})
    assert "Проекты" not in dir_names(ivanov)


def test_records_are_visible_only_in_open_folders(site):
    web, admin, ivanov, petrov = staffed(site)
    admin.post("/api/folder", json={"dir": "Проекты"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "only"})
    summary = web.cfg.outbox / "Проекты" / "вкс" / "summary"
    summary.mkdir(parents=True)
    (summary / "вкс.md").write_text("# Протокол\n\nРешили.\n", encoding="utf-8")
    web.service.ledger.put({"file": "Проекты/вкс.mp4", "status": "ok", "stem": "вкс",
                            "finished_at": "2026-10-09T12:00:00+03:00"})
    query = {"file": "Проекты/вкс.mp4"}

    assert [f["file"] for f in ivanov.get("/api/browse", params={"flat": True}).json()["files"]] == ["Проекты/вкс.mp4"]
    shown = ivanov.get("/api/result", params=query).json()
    assert "Решили." in shown["text"] and shown["has_map"] and not shown["can_edit"]
    assert [r["file"] for r in ivanov.get("/api/v1/records").json()["records"]] == ["Проекты/вкс.mp4"]
    assert "Решили." in ivanov.get("/api/v1/record", params=query).json()["protocol"]
    assert ivanov.get("/api/map", params=query).status_code == 200
    assert ivanov.post("/api/admin/names", json=query).status_code == 403      # читает, но не правит

    # карта, которую показывает сервис, — с кнопкой возврата к этой же записи; скачанный файл — без неё
    shown_map = ivanov.get("/api/map", params=query).text
    back = '<a class="back" href="/?path=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B&amp;open=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B%2F%D0%B2%D0%BA%D1%81.mp4">'
    assert back in shown_map and shown_map.index(back) < shown_map.index("<h1>") < shown_map.index('role="tablist"')
    assert "content-disposition" not in ivanov.get("/api/map", params=query).headers
    for saved in (ivanov.get("/api/map", params={**query, "download": True}), ivanov.get("/api/v1/record.html", params=query)):
        assert saved.status_code == 200 and "attachment" in saved.headers["content-disposition"]
        assert 'class="back"' not in saved.text and "К записи" not in saved.text and "Решили." in saved.text
        assert saved.text == (summary / "вкс.map.html").read_text(encoding="utf-8")
    # главная страница по этому адресу открывает запись: параметры — те, что читает index.html
    index = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'start.get("path")' in index and 'start.get("open")' in index

    # чужая запись неотличима от несуществующей
    assert petrov.get("/api/browse", params={"flat": True}).json()["files"] == []
    assert petrov.get("/api/v1/records", auth=("petrov", PASSWORD)).json()["records"] == []
    for path in ("/api/result", "/api/v1/record", "/api/map", "/api/audio"):
        assert petrov.get(path, params=query).status_code == 404, path
    assert petrov.post("/api/admin/names", json=query).status_code == 404
    assert admin.get("/api/admin/folders").json()["folders"][0]["records"] == 1


def test_user_makes_subfolders_at_home_but_not_elsewhere(site):
    web, admin, ivanov, _ = staffed(site)
    admin.post("/api/folder", json={"dir": "Проекты"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "tree"})
    made = ivanov.post("/api/folder", json={"dir": f"{HOME}/ivanov/Совещания"})
    assert made.status_code == 200 and (web.cfg.inbox / HOME / "ivanov" / "Совещания").is_dir()
    assert "Совещания" in dir_names(ivanov, f"{HOME}/ivanov")
    for foreign in (f"{HOME}/petrov/Чужое", "Проекты/Сметы", "Новая общая", f"{HOME}/sidorov"):
        assert ivanov.post("/api/folder", json={"dir": foreign}).status_code == 403, foreign
        assert not (web.cfg.inbox / foreign).exists()
    for bad in ("", "..", "Проекты/../x", ".скрытая"):
        assert admin.post("/api/folder", json={"dir": bad}).status_code == 400, bad


def test_folder_admin_grants_reading_but_not_administration(site):
    web, admin, ivanov, petrov = staffed(site)
    admin.post("/api/folder", json={"dir": "Проекты"})
    admin.post("/api/folder", json={"dir": "Снабжение"})
    appointed = admin.post("/api/access", json={"dir": "Проекты", "login": "petrov", "manage": "tree"})
    assert appointed.status_code == 200 and appointed.json()["can_manage"]

    seen = petrov.get("/api/access", params={"dir": "Проекты"}).json()
    assert not seen["can_manage"] and [u["login"] for u in seen["users"]] == ["ivanov"]
    assert petrov.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "only"}).status_code == 200
    assert "Проекты" in dir_names(ivanov)
    assert petrov.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "manage": "only"}).status_code == 403
    assert petrov.post("/api/access", json={"dir": "Проекты", "login": "ivanov",
                                            "read": "tree", "manage": "tree"}).status_code == 403
    assert web.people.users["ivanov"]["manage"] == {} and web.people.users["ivanov"]["read"] == {"Проекты": "only"}
    # администрирование с подпапками: подпапку завести можно, чужую папку и корень раздать нельзя
    assert petrov.post("/api/folder", json={"dir": "Проекты/Сметы"}).status_code == 200
    for foreign in ("Снабжение", "", f"{HOME}/ivanov"):
        assert petrov.post("/api/access", json={"dir": foreign, "login": "ivanov", "read": "tree"}).status_code == 403
    # читатель никому ничего не раздаёт
    assert ivanov.post("/api/access", json={"dir": "Проекты", "login": "petrov", "read": "tree"}).status_code == 403


# ---------- загрузка ----------

def test_upload_lands_in_own_folder_and_is_written_to_the_journal(site):
    web, admin, ivanov, _ = staffed(site)
    home = f"{HOME}/ivanov"
    file = {"name": "планёрка.mp3", "size": 10, "dir": home}
    assert ivanov.get("/api/upload", params=file).json()["offset"] == 0
    first = ivanov.put("/api/upload", params={**file, "offset": 0}, content=b"12345")
    assert first.json() == {"offset": 5, "done": False, "file": f"{home}/планёрка.mp3"}
    assert ivanov.get("/api/upload", params=file).json()["offset"] == 5       # докачка с этого места
    assert ivanov.put("/api/upload", params={**file, "offset": 0}, content=b"12345").status_code == 409
    assert ivanov.put("/api/upload", params={**file, "offset": 5}, content=b"67890").json()["done"]
    assert (web.cfg.inbox / home / "планёрка.mp3").read_bytes() == b"1234567890"
    assert ivanov.get("/api/upload", params=file).status_code == 409           # такая запись уже есть

    events = admin.get("/api/admin/journal").json()["events"]
    uploads = [e for e in events if e["kind"] == "upload"]
    assert len(uploads) == 1
    assert uploads[0]["by"] == "ivanov" and uploads[0]["file"] == f"{home}/планёрка.mp3" and uploads[0]["size"] == 10
    assert web.trail.uploaded == {f"{home}/планёрка.mp3": "ivanov"}
    assert [f["path"] for f in admin.post("/api/admin/files").json()["files"]] == [f"{home}/планёрка.mp3"]


def test_upload_into_a_closed_folder_is_refused(site):
    web, admin, ivanov, _ = staffed(site)
    admin.post("/api/folder", json={"dir": "Проекты"})
    for folder in (f"{HOME}/petrov", "Проекты", ""):
        file = {"name": "запись.mp3", "size": 5, "dir": folder}
        assert ivanov.get("/api/upload", params=file).status_code == 403, folder
        assert ivanov.put("/api/upload", params={**file, "offset": 0}, content=b"12345").status_code == 403, folder
        assert ivanov.delete("/api/upload", params=file).status_code == 403, folder
    refused = ivanov.put("/api/upload", params={"name": "вирус.exe", "size": 5, "offset": 0,
                                                "dir": f"{HOME}/ivanov"}, content=b"12345")
    assert refused.status_code == 400
    assert [p for p in web.cfg.inbox.rglob("*") if p.is_file()] == []
    assert not [e for e in web.trail.read() if e["kind"] == "upload"]


# ---------- действия с папками, отчёт ----------

def test_admin_renames_and_deletes_a_folder(site):
    web, admin, ivanov, _ = staffed(site)
    admin.post("/api/folder", json={"dir": "Проекты"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "tree", "upload": "tree"})
    file = {"name": "вкс.mp3", "size": 5, "dir": "Проекты", "offset": 0}
    assert ivanov.put("/api/upload", params=file, content=b"12345").json()["done"]
    web.service.ledger.put({"file": "Проекты/вкс.mp3", "status": "ok"})

    renamed = admin.post("/api/admin/folders/rename", json={"dir": "Проекты", "name": "Проекты отдела"})
    assert renamed.json() == {"ok": True, "path": "Проекты отдела"}
    assert (web.cfg.inbox / "Проекты отдела" / "вкс.mp3").is_file() and not (web.cfg.inbox / "Проекты").exists()
    assert "Проекты отдела" in dir_names(ivanov) and "Проекты" not in dir_names(ivanov)      # права переехали
    assert web.people.users["ivanov"]["upload"] == {"Проекты отдела": "tree"}
    assert [f["file"] for f in ivanov.get("/api/browse", params={"path": "Проекты отдела"}).json()["files"]] \
        == ["Проекты отдела/вкс.mp3"]
    assert admin.post("/api/admin/folders/rename", json={"dir": "Нет такой", "name": "Имя"}).status_code == 404
    assert admin.post("/api/admin/folders/rename", json={"dir": "Проекты отдела", "name": "а/б"}).status_code == 400

    assert admin.post("/api/admin/folders/delete", json={"dir": f"{HOME}/ivanov"}).status_code == 400
    deleted = admin.post("/api/admin/folders/delete", json={"dir": "Проекты отдела"}).json()
    assert deleted["ok"] and deleted["records"] == 1
    assert not (web.cfg.inbox / "Проекты отдела").exists()
    assert (web.cfg.inbox / deleted["trash"] / "Проекты отдела" / "вкс.mp3").is_file()
    assert "Проекты отдела" not in dir_names(admin) and web.service.ledger.records == {}
    assert web.people.users["ivanov"]["read"] == {} and web.people.users["ivanov"]["upload"] == {}

    kinds = [e["kind"] for e in admin.get("/api/admin/journal").json()["events"]]
    assert kinds[:2] == ["folder_delete", "folder_rename"] and "folder_create" in kinds


def test_folder_actions_wait_for_the_scanner_lock(site):
    web, admin, _, _ = staffed(site)
    for name in ("Проекты", "Архив"):
        admin.post("/api/folder", json={"dir": name})
    answers = []
    web.service.scan_lock.acquire()                                 # сканер посреди обхода
    try:
        calls = [threading.Thread(target=lambda p=path, b=body: answers.append(admin.post(p, json=b)))
                 for path, body in (("/api/admin/folders/rename", {"dir": "Проекты", "name": "Проекты отдела"}),
                                    ("/api/admin/folders/delete", {"dir": "Архив"}))]
        for t in calls:
            t.start()
        time.sleep(0.4)
        # пока обход не кончился, папки на месте: сканер не увидит переезд наполовину
        assert answers == [] and (web.cfg.inbox / "Проекты").is_dir() and (web.cfg.inbox / "Архив").is_dir()
    finally:
        web.service.scan_lock.release()
    for t in calls:
        t.join(10)
    assert [a.status_code for a in answers] == [200, 200]
    assert (web.cfg.inbox / "Проекты отдела").is_dir() and not (web.cfg.inbox / "Архив").exists()
    assert admin.post("/api/admin/folders/rename", json={"dir": "Нет такой", "name": "Имя"}).status_code == 404
    assert not web.service.scan_lock.locked()                       # отказ замок сканера не держит


def test_trash_and_stale_uploads_are_cleaned_while_the_service_runs(site):
    web, _ = site
    old = web.cfg.inbox / folders.TRASH / "20260101-000000"
    fresh = web.cfg.inbox / folders.TRASH / "20261009-000000"
    part = web.cfg.inbox / upload.PARTS_DIR / "брошено.part"
    for p in (old, fresh, part.parent):
        p.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"x")
    long_ago = time.time() - (folders.TRASH_DAYS + 1) * 86400
    os.utime(old, (long_ago, long_ago))
    os.utime(part, (long_ago, long_ago))
    web._clean()
    assert not old.exists() and fresh.is_dir() and not part.exists()
    assert web.CLEAN_EVERY_SEC == 86400


def test_cost_report_is_served_and_checks_its_arguments(site):
    web, client = site
    web.trail.write("run", "", file="Проекты/вкс.mp4", uploaded_by="ivanov", reason="new", status="ok",
                    audio_sec=600, cost=0.25, stages=[{"stage": "speech", "model": "deepgram/nova-3", "cost": 0.25}])
    admin = enter(web, "admin", ADMIN_PASSWORD)
    for path in ("/api/admin/report", "/api/v1/costs"):
        report = admin.get(path, params={"bucket": "month"}).json()
        # строка журнала прежнего вида, без валюты и поставщика: доллары OpenRouter
        assert report["total"] == {"cost": {"USD": 0.25}, "runs": 1, "records": 1, "audio_sec": 600.0}, path
        assert report["currencies"] == ["USD"] and report["providers"][0]["provider"] == "OpenRouter"
        assert report["folders"][0]["folder"] == "Проекты" and report["users"][0]["login"] == "ivanov"
        assert len(report["series"]) == 1 and report["bucket"] == "month"
    assert client.get("/api/v1/costs", auth=("admin", ADMIN_PASSWORD)).status_code == 200
    assert admin.get("/api/admin/report", params={"bucket": "hour"}).status_code == 400
    assert admin.get("/api/admin/report", params={"start": "10.10.2026"}).status_code == 400
    assert admin.get("/api/admin/report", params={"end": "2000-01-01"}).json()["total"]["runs"] == 0


def csv_tables(text: str) -> dict[str, list[list[str]]]:
    """Таблицы отчёта из файла CSV: название → шапка и строки."""
    blocks = [b.splitlines() for b in text.replace("\r\n", "\n").strip().split("\n\n")]
    return {b[0]: [line.split(";") for line in b[1:]] for b in blocks}


def test_report_csv_is_the_whole_report_in_one_file(site):
    web, client = site
    web.trail.write("run", "petrov", file="Снабжение/вкс.mp4", reason="manual", status="ok", audio_sec=90, cost=0.5)
    sheet = client.get("/api/admin/report.csv", params={"bucket": "month"}, auth=("admin", ADMIN_PASSWORD))
    assert sheet.status_code == 200 and sheet.headers["content-type"].startswith("text/csv")
    assert sheet.headers["content-disposition"] == 'attachment; filename="costs.csv"'
    assert sheet.content.startswith("﻿".encode("utf-8"))        # метка для Excel
    tables = csv_tables(sheet.content.decode("utf-8-sig"))
    assert list(tables) == ["Параметры отчёта", "Итоги", "По валютам", "По периодам", "Папки", "Пользователи", "Записи",
                            "Этапы и модели", "Поставщики", "Прогоны"]
    assert ["Шаг по времени", "по месяцам"] in tables["Параметры отчёта"]
    assert ["Кем сформирован", "admin"] in tables["Параметры отчёта"]
    assert tables["По валютам"] == [["Валюта", "Потрачено", "Оплачено звука, мин", "За час звука"],
                                    ["USD", "0.500000", "1.5", "20.000000"]]
    assert tables["Папки"][1] == ["Снабжение", "0.500000", "0.500000", "1", "1.5", "1"]
    assert tables["Прогоны"][0] == ["Время", "Папка", "Запись", "Кто потратил", "Загрузил", "Запустил", "Причина",
                                    "Статус", "Звука, мин", "Поставщик", "Валюта", "Потрачено"]
    assert tables["Прогоны"][1][1:] == ["Снабжение", "вкс.mp4", "petrov", "", "petrov", "ручной запуск", "готово", "1.5",
                                        "OpenRouter", "USD", "0.500000"]
    # период — в имени файла; за пустой период таблицы на месте, строк в них нет
    empty = client.get("/api/admin/report.csv", params={"start": "2000-01-01", "end": "2000-01-31"},
                       auth=("admin", ADMIN_PASSWORD))
    assert empty.headers["content-disposition"] == 'attachment; filename="costs_2000-01-01_2000-01-31.csv"'
    assert len(csv_tables(empty.content.decode("utf-8-sig"))["Прогоны"]) == 1
    for path in ("/api/admin/report.csv", "/api/admin/report.xlsx"):
        assert client.get(path, params={"start": "завтра"}, auth=("admin", ADMIN_PASSWORD)).status_code == 400, path
        assert client.get(path, params={"bucket": "hour"}, auth=("admin", ADMIN_PASSWORD)).status_code == 400, path


def test_report_xlsx_holds_the_same_numbers_as_the_report(site):
    """Книга Excel — тот же отчёт, что отдаёт /api/admin/report: листы по разрезам, числа числами,
    даты датами, и суммы на листах сходятся с итогом."""
    web, client = site
    web.trail.write("upload", "ivanov", file="Проекты/Сметы/а.mp4", size=10)
    web.trail.run({"file": "Проекты/Сметы/а.mp4", "status": "ok", "audio_duration_sec": 3600,
                   "cost": {"RUB": 12.0, "USD": 0.05},
                   "cost_models": [{"stage": "speech", "model": "scribe", "provider": "RouterAI", "currency": "RUB", "cost": 12.0},
                                   {"stage": "summary", "model": "deepseek", "provider": "OpenRouter", "currency": "USD",
                                    "cost": 0.05}],
                   "cost_providers": [{"provider": "RouterAI", "currency": "RUB", "cost": 12.0},
                                      {"provider": "OpenRouter", "currency": "USD", "cost": 0.05}]}, "", "new")
    web.trail.run({"file": "=вне папок.mp4", "status": "error", "audio_duration_sec": 90, "cost": {"RUB": 3.5},
                   "cost_models": [{"stage": "speech", "model": "scribe", "provider": "RouterAI", "currency": "RUB", "cost": 3.5}],
                   "cost_providers": [{"provider": "RouterAI", "currency": "RUB", "cost": 3.5}]}, "admin", "manual")
    admin = enter(web, "admin", ADMIN_PASSWORD)
    report = admin.get("/api/admin/report", params={"bucket": "month"}).json()
    answer = admin.get("/api/admin/report.xlsx", params={"bucket": "month"})
    assert answer.status_code == 200 and answer.headers["content-type"] == costbook.XLSX
    assert answer.headers["content-disposition"] == 'attachment; filename="costs.xlsx"'
    book = openpyxl.load_workbook(io.BytesIO(answer.content))
    assert book.sheetnames == ["Сводка", "По периодам", "Папки", "Пользователи", "Записи", "Этапы и модели",
                               "Поставщики", "Прогоны"]
    rows = {name: [[c.value for c in row] for row in book[name].iter_rows()] for name in book.sheetnames}

    brief = {row[0]: row[1:] for row in rows["Сводка"] if row[0]}
    assert brief["Шаг по времени"][0] == "по месяцам" and brief["Кем сформирован"][0] == "admin"
    assert isinstance(brief["Сформирован"][0], datetime.datetime) and brief["Период с"][0] == "с начала журнала"
    assert (brief["Записей"][0], brief["Звука, мин"][0], brief["Прогонов"][0]) == (2, 61.5, 2)
    assert brief["RUB"] == [15.5, 61.5, pytest.approx(15.5 / (3690 / 3600))] and brief["USD"][:2] == [0.05, 60.0]

    # шапки — по столбцу на валюту; суммы на листах сходятся с итогом отчёта
    assert rows["Записи"][0] == ["Папка", "Запись", "Потрачено, ₽", "Потрачено, $", "Звука, мин", "Прогонов"]
    total = report["total"]["cost"]
    for name, first in (("По периодам", 1), ("Пользователи", 2), ("Записи", 2), ("Этапы и модели", 2), ("Поставщики", 1)):
        body = rows[name][1:]
        assert [round(sum(r[first + n] for r in body), 6) for n in range(2)] == [total["RUB"], total["USD"]], name
        assert len(body) == len(report[{"По периодам": "series", "Пользователи": "users", "Записи": "records",
                                        "Этапы и модели": "stages", "Поставщики": "providers"}[name]]), name
    # у папок сумма «без подпапок» вместе со строкой записей вне папок — тоже итог
    assert [r[0] for r in rows["Папки"][1:]] == ["Проекты", "Проекты/Сметы", "(вне папок)"]
    assert round(sum(r[3] for r in rows["Папки"][1:]), 6) == total["RUB"]
    assert rows["Папки"][1][1:5] == [12.0, 0.05, 0.0, 0.0]
    assert [r[:2] for r in rows["Пользователи"][1:]] == [["ivanov", "ivanov"], ["admin", "admin"]]

    # типы ячеек: текст — текстом (и имя с «=» не стало формулой), числа — числами, даты — датами
    records = book["Записи"]
    loose = next(row for row in records.iter_rows(min_row=2) if row[1].value == "=вне папок.mp4")
    assert loose[1].data_type == "s" and loose[2].data_type == "n" and loose[2].number_format == costbook.MONEY
    assert loose[4].value == 1.5 and loose[4].number_format == costbook.MINUTES and loose[5].value == 1
    period = book["По периодам"]["A2"]
    assert period.is_date and period.number_format == costbook.MONTH and period.value.day == 1
    log = book["Прогоны"]
    assert log.max_row == 4 and log["A2"].is_date and isinstance(log["A2"].value, datetime.datetime)
    assert [c.value for c in log[4]][1:] == [None, "=вне папок.mp4", "admin", None, "admin", "ручной запуск", "ошибка", 1.5,
                                              "RouterAI", "RUB", 3.5]
    # шапка закреплена и выделена, стоит автофильтр, ширина — по содержимому
    for name in book.sheetnames[1:]:
        sheet = book[name]
        assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref.startswith("A1:"), name
        assert all(c.font.bold and c.fill.fill_type == "solid" for c in sheet[1]), name
    assert book["Сводка"].freeze_panes is None and book["Сводка"]["A1"].value == "Отчёт о расходах"
    assert records.column_dimensions["B"].width >= len("=вне папок.mp4")
    # день и неделя — датой; год — числом
    daily = openpyxl.load_workbook(io.BytesIO(admin.get("/api/admin/report.xlsx").content))["По периодам"]["A2"]
    assert daily.is_date and daily.number_format == costbook.DAY
    yearly = openpyxl.load_workbook(io.BytesIO(admin.get("/api/admin/report.xlsx", params={"bucket": "year"}).content))
    assert yearly["По периодам"]["A2"].value == datetime.date.today().year
    # тот же отчёт в CSV: те же таблицы и те же итоги
    tables = csv_tables(admin.get("/api/admin/report.csv", params={"bucket": "month"}).content.decode("utf-8-sig"))
    assert list(tables)[3:] == book.sheetnames[1:] and tables["По периодам"][1][1:3] == ["15.500000", "0.050000"]
    assert tables["По периодам"][1][0] == datetime.date.today().isoformat()[:7]


def test_admin_status_gives_the_summary_without_the_key(site):
    web, client = site
    summary = client.get("/api/admin/status", auth=("admin", ADMIN_PASSWORD))
    body = summary.json()
    assert body["config"]["key_present"] and body["running"] == [] and body["queue"] == []
    assert body["config"]["workers"] == 3 and "current" not in body
    assert body["totals"]["files"] == 0 and body["log_total"] > 0
    assert "sk-test-not-a-key" not in summary.text and ADMIN_PASSWORD not in summary.text
    assert client.post("/api/admin/run", json={"file": "../x.mp4"}, auth=("admin", ADMIN_PASSWORD)).status_code == 400
    assert client.post("/api/admin/run", json={"file": "нет.mp4"}, auth=("admin", ADMIN_PASSWORD)).status_code == 404


# ---------- несколько записей в работе ----------

def job(file, **fields):
    """Запись в работе — так, как её ведёт обработчик (watcher.Service._take)."""
    return {"file": file, "template": None, "force": False, "manual": False,
            "started_at": "2026-10-10T12:00:00+03:00", "chunks_done": 0, "chunks_total": 0, "step": "", **fields}


def test_status_and_pages_show_every_record_in_work(site, monkeypatch):
    web, admin, ivanov, petrov = staffed(site)
    mine, his = f"{HOME}/ivanov/планёрка.mp4", f"{HOME}/petrov/лекция.mp4"
    monkeypatch.setattr(web.service, "running", [
        job(mine, step="расшифровка с разделением говорящих", chunks_done=1, chunks_total=3),
        job(his, step="протокол по шаблону «Лекция»", manual=True)])

    running = admin.get("/api/admin/status").json()["running"]
    assert [(j["file"], j["step"], j["chunks_done"], j["chunks_total"]) for j in running] == [
        (mine, "расшифровка с разделением говорящих", 1, 3), (his, "протокол по шаблону «Лекция»", 0, 0)]

    everything = admin.get("/api/browse", params={"flat": True}).json()
    assert everything["service"] == {"running": 2, "queue": 0, "watching": 0}
    assert [(p["file"], p["state"], p["step"]) for p in everything["pending"]] == [
        (mine, "running", "расшифровка с разделением говорящих"), (his, "running", "протокол по шаблону «Лекция»")]
    # человеку видна только запись из его папки — и в списке, и в счётчике строки состояния
    own = ivanov.get("/api/browse", params={"flat": True}).json()
    assert own["service"]["running"] == 1 and [p["file"] for p in own["pending"]] == [mine]
    assert petrov.get("/api/browse", params={"flat": True}).json()["pending"][0]["file"] == his


def test_record_in_work_is_not_started_again_and_a_rebuild_waits_in_the_queue(site, monkeypatch):
    web, admin, _, _ = staffed(site)
    file = "Проекты/вкс.mp4"
    recorded(web, file)
    out = web.cfg.outbox / "Проекты" / "вкс" / "transcript"
    out.mkdir()
    (out / "вкс.json").write_text(json.dumps({"segments": [
        {"start": 0.0, "end": 2.0, "speaker": "S1", "text": "Добрый день."}]}), encoding="utf-8")
    monkeypatch.setattr(web.service, "running", [job(file)])
    monkeypatch.setattr(web.service, "queue", [])

    assert admin.post("/api/admin/run", json={"file": file}).status_code == 409       # вручную второй раз не пустить
    signed = admin.post("/api/admin/names", json={"file": file, "parts": {"1": {"S1": "Иванов"}}, "rebuild": True})
    assert signed.status_code == 200 and signed.json()["rebuilding"]
    # пересборка встала в очередь, но пока запись в работе, её никто не берёт
    assert [(i.path, i.reason) for i in web.service.queue] == [(file, "rebuild")]
    assert web.service._take() is None and len(web.service.queue) == 1
    # на странице запись одна — «обрабатывается», а не ещё и «в очереди»
    assert [p["state"] for p in admin.get("/api/browse", params={"path": "Проекты"}).json()["pending"]] == ["running"]
    web.service.running.clear()                                                        # прогон закончился
    item, taken = web.service._take()
    assert item.reason == "rebuild" and taken["file"] == file and web.service.queue == []


def test_repeated_rebuild_request_never_pays_twice_for_the_same_edits(site):
    """Пересборка протокола платная: на один набор правок она одна, сколько бы раз и откуда её ни
    просили. Подписи без rebuild её не запускают. Ждёт в очереди — вторая не ставится; уже идёт —
    следующая ставится, только если подписи или словарь с её начала правили, и только одна."""
    web, admin, _, _ = staffed(site)
    file = voiced(web, "Проекты/вкс.mp4")
    take, web.service._take = web.service._take, lambda: None      # обработчик очередь не разбирает: берём сами
    robot, _ = keyed(web, admin)
    sign = lambda **q: robot.post("/api/v1/speakers", params={"file": file, **q}).json()
    words = lambda **q: robot.put("/api/v1/dictionary", params={"file": file, **q.pop("params", {})}, **q).json()
    queued = lambda: [(i.path, i.reason) for i in web.service.queue]

    # все подписи сразу, без rebuild: сохранены, пересборки нет и в ответе о ней ни слова
    for label, name in (("S1", "Петрова Анна"), ("S2", "Иванов Иван")):
        signed = sign(part=1, label=label, name=name)
        assert signed["ok"] and "rebuild" not in signed and signed["rebuilding"] is False and signed["names_newer"]
    assert queued() == []
    # одна пересборка в конце; повтор — от говорящих, от словаря и со страницы — вторую не ставит
    assert (sign(rebuild=True)["rebuild"], queued()) == ("queued", [(file, "rebuild")])
    for again in (sign(rebuild=True), words(params={"rebuild": True}),
                  admin.post("/api/admin/names", json={"file": file, "rebuild": True}).json(),
                  admin.post("/api/dictionary", json={"file": file, "rebuild": True}).json(),
                  sign(part=2, label="S3", name="Сидоров Пётр", rebuild=True)):     # правка легла, очередь прежняя
        assert again["rebuild"] == "already" and again["rebuilding"] is True
    assert queued() == [(file, "rebuild")]

    # пересборка началась; двойное нажатие и повтор запроса без новых правок — ничего не ставят
    item, job = take()
    assert job["file"] == file and job["began"] > 0 and queued() == []
    time.sleep(0.05)
    for again in (sign(rebuild=True), words(params={"rebuild": True}),
                  admin.post("/api/admin/names", json={"file": file, "rebuild": True}).json()):
        assert again["rebuild"] == "already" and again["rebuilding"] is True
    assert queued() == []
    # правили после её начала — одна следующая, она дождётся конца текущей; больше — ни одной
    assert sign(part=1, label="S1", name="Петрова Анна Сергеевна", rebuild=True)["rebuild"] == "queued"
    assert words(params={"rebuild": True}, json={"people": ["Ершова Лидия Марковна — главный бухгалтер"]})["rebuild"] == "already"
    assert sign(rebuild=True)["rebuild"] == "already" and queued() == [(file, "rebuild")]
    assert take() is None                                           # пока идёт текущая, следующую не берут
    # текущая закончилась, началась следующая; то же от словаря: правка словаря записи после начала
    # прогона — основание для одной следующей, просьба без правки — нет
    web.service.running.clear()
    assert take()[1]["file"] == file and queued() == []
    time.sleep(0.05)
    assert words(params={"rebuild": True})["rebuild"] == "already" and queued() == []
    edited = robot.put("/api/v1/dictionary", params={"file": file, "rebuild": True}, json={"terms": ["КС-2 — акт"]}).json()
    assert edited["rebuild"] == "queued" and edited["dict_newer"] and queued() == [(file, "rebuild")]
    assert words(params={"rebuild": True})["rebuild"] == "already" and len(web.service.queue) == 1


# ---------- действия с записью ----------

def recorded(web, rel, by=None):
    """Готовая запись: исходник, результаты, строка журнала обработки и расход в журнале событий."""
    folder, _, name = rel.rpartition("/")
    stem = name.rsplit(".", 1)[0]
    (web.cfg.inbox / folder).mkdir(parents=True, exist_ok=True)
    (web.cfg.inbox / rel).write_bytes(b"video")
    out = web.cfg.outbox / folder / stem
    (out / "summary").mkdir(parents=True)
    (out / "summary" / f"{stem}.md").write_text("# Протокол\n\nРешили.\n", encoding="utf-8")
    if by:
        web.trail.write("upload", by, file=rel, size=5)
    web.service.ledger.put({"file": rel, "status": "ok", "stem": stem, "output": str(out), "cost": 0.25,
                            "finished_at": "2026-10-09T12:00:00+03:00"})
    web.trail.run(web.service.ledger.get(rel), "", "new")


def idle(web):
    """Два обхода подряд: новый файл сканер замечает на первом и ставит в очередь не раньше второго."""
    web.service.scan_once()
    web.service.scan_once()
    return web.service.queue == [] and web.service.pending == {}


def test_owner_renames_moves_and_deletes_a_record_without_reprocessing(site):
    web, admin, ivanov, _ = staffed(site)
    home = f"{HOME}/ivanov"
    old = f"{home}/вкс.mp4"
    recorded(web, old, by="ivanov")
    assert ivanov.post("/api/folder", json={"dir": f"{home}/Архив"}).status_code == 200
    assert ivanov.get("/api/result", params={"file": old}).json()["can_act"]
    assert ivanov.get("/api/record/folders").json()["folders"] == [home, f"{home}/Архив"]

    renamed = ivanov.post("/api/record/rename", json={"file": old, "name": "Планёрка"})
    new = f"{home}/Планёрка.mp4"
    assert renamed.json() == {"ok": True, "file": new}
    assert (web.cfg.inbox / new).is_file() and not (web.cfg.inbox / old).exists()
    assert "Решили." in ivanov.get("/api/result", params={"file": new}).json()["text"]
    assert ivanov.get("/api/result", params={"file": old}).status_code == 404
    assert idle(web)                                                    # сканер не принял её за новую

    moved = ivanov.post("/api/record/move", json={"file": new, "dir": f"{home}/Архив"})
    last = f"{home}/Архив/Планёрка.mp4"
    assert moved.json() == {"ok": True, "file": last}
    assert "Решили." in ivanov.get("/api/v1/record", params={"file": last}).json()["protocol"]
    assert [f["file"] for f in ivanov.get("/api/browse", params={"path": f"{home}/Архив"}).json()["files"]] == [last]
    assert idle(web)
    assert web.service.ledger.get(last)["runs"] == 1 and web.trail.uploaded == {last: "ivanov"}

    deleted = ivanov.post("/api/record/delete", json={"file": last}).json()
    assert deleted["ok"] and deleted["keep_days"] == 30
    assert (web.cfg.inbox / deleted["trash"] / "Планёрка.mp4").is_file()
    assert (web.cfg.outbox / deleted["trash"] / "Планёрка" / "summary" / "Планёрка.md").is_file()
    assert web.service.ledger.records == {} and not (web.cfg.inbox / last).exists()
    assert ivanov.get("/api/result", params={"file": last}).status_code == 404
    assert idle(web)

    events = admin.get("/api/admin/journal").json()["events"]
    assert [(e["kind"], e["by"], e["file"], e.get("to")) for e in events[:3]] == [
        ("record_delete", "ivanov", last, None), ("record_move", "ivanov", new, last),
        ("record_rename", "ivanov", old, new)]
    assert events[0]["trash"] == deleted["trash"]
    # расходы остались и привязаны к последнему пути записи
    report = admin.get("/api/admin/report").json()
    assert report["total"]["cost"] == {"USD": 0.25} and report["records"][0]["file"] == last
    assert report["users"][0]["login"] == "ivanov"


def test_scanner_notices_the_same_file_when_the_ledger_row_is_missing(site):
    """Обратная проверка к idle(): файл без строки в журнале обработки сканер берёт на заметку."""
    web, _ = site
    (web.cfg.inbox / "новая.mp4").write_bytes(b"video")
    assert not idle(web) and list(web.service.pending) == ["новая.mp4"]


def test_record_actions_follow_the_rights(site):
    web, admin, ivanov, petrov = staffed(site)
    for path in ("Проекты", "Снабжение"):
        admin.post("/api/folder", json={"dir": path})
    file = "Проекты/вкс.mp4"
    recorded(web, file)
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "tree"})
    actions = (("/api/record/rename", {"file": file, "name": "Итоги"}),
               ("/api/record/move", {"file": file, "dir": f"{HOME}/ivanov"}),
               ("/api/record/delete", {"file": file}))
    for path, body in actions:
        assert petrov.post(path, json=body).status_code == 404, path          # чужая — как несуществующая
        assert ivanov.post(path, json=body).status_code == 403, path          # читает, но не хозяин
    assert not ivanov.get("/api/result", params={"file": file}).json()["can_act"]

    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "manage": "only"})
    assert ivanov.get("/api/result", params={"file": file}).json()["can_act"]
    assert ivanov.get("/api/record/folders").json()["folders"] == [f"{HOME}/ivanov", "Проекты"]
    # переносить — только туда, где он тоже хозяин
    for folder in ("Снабжение", f"{HOME}/petrov", "", "Нет такой"):
        assert ivanov.post("/api/record/move", json={"file": file, "dir": folder}).status_code == 403, folder
    assert admin.post("/api/record/move", json={"file": file, "dir": "Нет такой"}).status_code == 404
    assert "" in admin.get("/api/record/folders").json()["folders"]
    for name in ("", "..", "а/б", ".скрытая"):
        assert ivanov.post("/api/record/rename", json={"file": file, "name": name}).status_code == 400, name
    recorded(web, "Проекты/смета.mp4")
    assert ivanov.post("/api/record/rename", json={"file": file, "name": "смета"}).status_code == 409
    assert (web.cfg.inbox / file).is_file() and not [e for e in web.trail.read() if e["kind"].startswith("record_")]

    moved = ivanov.post("/api/record/move", json={"file": file, "dir": f"{HOME}/ivanov"})
    assert moved.json()["file"] == f"{HOME}/ivanov/вкс.mp4"
    assert petrov.get("/api/browse", params={"flat": True}).json()["files"] == []


def test_record_in_the_queue_or_in_work_is_not_touched(site, monkeypatch):
    web, admin, _, _ = staffed(site)
    file = "Проекты/вкс.mp4"
    recorded(web, file)
    actions = (("/api/record/rename", {"file": file, "name": "Итоги"}),
               ("/api/record/move", {"file": file, "dir": ""}),
               ("/api/record/delete", {"file": file}))
    monkeypatch.setattr(web.service, "running", [job(file)])
    for path, body in actions:
        assert admin.post(path, json=body).status_code == 409, path
    monkeypatch.setattr(web.service, "running", [])
    monkeypatch.setitem(web.service.pending, file, object())       # сканер ещё следит за файлом
    for path, body in actions:
        assert admin.post(path, json=body).status_code == 409, path
    monkeypatch.delitem(web.service.pending, file)
    assert (web.cfg.inbox / file).is_file() and web.service.ledger.get(file)

    monkeypatch.setattr(web, "upload_ready", False)                 # входная папка только для чтения
    for path, body in actions:
        assert admin.post(path, json=body).status_code == 503, path
    assert not admin.get("/api/result", params={"file": file}).json()["can_act"]
    assert not web.service.scan_lock.locked()                       # отказ замок сканера не держит


def test_user_is_deleted_whatever_the_case_of_the_login_and_unknown_one_is_404(site):
    web, admin, _, petrov = staffed(site)
    assert admin.post("/api/admin/users/delete", json={"login": "nobody"}).status_code == 404
    assert not [e for e in web.trail.read() if e["kind"] == "user_delete"]
    assert admin.post("/api/admin/users/delete", json={"login": " Petrov "}).status_code == 200
    assert sorted(web.people.users) == ["ivanov"] and petrov.get("/api/browse").status_code == 401
    assert [e["login"] for e in web.trail.read() if e["kind"] == "user_delete"] == ["petrov"]


# ---------- личный режим ----------

def test_personal_mode_needs_no_login(tmp_path):
    with running(tmp_path, mode="personal") as (web, client):
        me = client.get("/api/me").json()
        assert me["personal"] is True and me["admin"] and me["login"] == "owner"
        assert client.get("/api/browse").status_code == 200
        assert client.get("/api/admin/status").status_code == 200
        assert client.post("/api/folder", json={"dir": "Совещания"}).status_code == 200
        done = client.put("/api/upload", params={"name": "запись.mp3", "size": 3, "offset": 0, "dir": "Совещания"},
                          content=b"123")
        assert done.json()["done"] and (web.cfg.inbox / "Совещания" / "запись.mp3").is_file()
        assert not client.get("/api/browse", params={"path": "Совещания"}).json()["manage"]


# ---------- сервис работает сразу после установки ----------

def test_fresh_install_works_in_both_modes_and_nothing_asks_for_payment(tmp_path):
    """Ключей и сроков у сервиса нет: чистая установка принимает и обрабатывает записи сразу,
    и ни один адрес приложения не отвечает 402."""
    for mode in ("corporate", "personal"):
        (tmp_path / mode).mkdir()
    with running(tmp_path / "corporate") as site:
        web, admin, ivanov, _ = staffed(site)
        home = f"{HOME}/ivanov"
        file = voiced(web, f"{home}/планёрка.mp4")
        assert not {(m, p) for m, p in api_routes(web) if p not in ("/api/login", "/api/logout")
                    and admin.request(m, p).status_code == 402}
        robot, _ = keyed(web, ivanov)
        sent = robot.put("/api/v1/upload", params={"dir": home, "name": "новая.mp3"}, content=b"123")
        assert sent.status_code == 200 and (web.cfg.inbox / home / "новая.mp3").read_bytes() == b"123"
        assert ivanov.get("/api/browse", params={"path": home}).json()["upload"]["enabled"] is True
        who = robot.get("/api/v1/whoami").json()
        assert who["upload"] is True and "license" not in who and "license" not in ivanov.get("/api/me").json()
        assert ivanov.post("/api/admin/names", json={"file": file, "rebuild": True}).json()["rebuilding"] is True
    with running(tmp_path / "personal", mode="personal") as (web, client):
        assert "license" not in client.get("/api/me").json()
        assert client.put("/api/v1/upload", params={"dir": "Совещания", "name": "б.mp3"}, content=b"1").status_code == 200


# ---------- заявка на доступ, почта, пароль по ссылке ----------

MAIL_ENV = {"SMTP_HOST": "smtp.test", "SMTP_FROM": "podyachiy@example.test", "SMTP_SECURITY": "none",
            "PUBLIC_URL": "http://podyachiy.test/", "ADMIN_EMAIL": "boss@example.test, IT@example.test"}
CLAIM = {"name": "Сидоров <b>Пётр</b>", "email": "Sidorov@example.test", "login": "Sidorov",
         "comment": "Отдел продаж, протоколы планёрок"}


class Post:
    """Вместо почтового сервера: письма складываются сюда. broken — ответ сервера на отправку."""

    def __init__(self):
        self.sent, self.broken, self.refused = [], None, {}

    def send(self, cfg, to, letter, attachments=(), reply_to=""):
        if self.broken:
            raise self.broken
        self.sent.append(types.SimpleNamespace(to=to, subject=letter[0], text=letter[1], html=letter[2],
                                               attachments=list(attachments), reply_to=reply_to))
        return self.refused

    def send_each(self, cfg, to, letter, attachments=(), reply_to=""):
        """Как mail.send_each: каждому адресу своё письмо; refused — адрес → причина, им письма нет."""
        if self.broken:
            raise self.broken
        for email in to:
            if email not in self.refused:
                self.send(cfg, [email], letter, attachments, reply_to)
        return self.refused

    def token(self, index=-1):
        return re.search(r"/set-password#(\S+)", self.sent[index].text).group(1)


@pytest.fixture
def office(tmp_path, monkeypatch):
    """Сервис с включённой почтой: модуль, клиент без входа, вошедший администратор и «почта»."""
    with running(tmp_path, extra=MAIL_ENV) as (web, client):
        post = Post()
        monkeypatch.setattr(web.mail, "send", post.send)
        monkeypatch.setattr(web.mail, "send_each", post.send_each)
        yield web, client, enter(web, "admin", ADMIN_PASSWORD), post


def kinds(web):
    return [e["kind"] for e in web.trail.read()]


def test_request_to_login_chain(office):
    web, guest, admin, post = office
    assert guest.get("/api/me").json()["mail"] is True              # страница входа покажет ссылки
    assert guest.get("/set-password").status_code == 200

    # 1. человек без учётной записи оставляет заявку
    assert guest.post("/api/request", json=CLAIM).json() == {"ok": True}
    notice, = post.sent
    assert notice.to == ["boss@example.test", "it@example.test"]
    assert notice.subject == "заявка на доступ — sidorov" and "http://podyachiy.test/admin#users" in notice.text
    assert "Сидоров <b>Пётр</b>" in notice.text and "&lt;b&gt;Пётр&lt;/b&gt;" in notice.html and "<b>Пётр" not in notice.html
    assert not web.people.users                                      # заявка ещё никого не завела

    # 2. администратор видит счётчик и саму заявку
    assert admin.get("/api/me").json()["requests"] == 1 and guest.get("/api/me").json()["requests"] == 0
    listed = admin.get("/api/admin/requests").json()
    claim, = listed["requests"]
    assert (claim["name"], claim["email"], claim["login"], claim["comment"], claim["status"]) == (
        "Сидоров <b>Пётр</b>", "sidorov@example.test", "sidorov", "Отдел продаж, протоколы планёрок", "pending")
    assert listed["mail"] == {"on": True, "why_off": None, "admin_emails": ["boss@example.test", "it@example.test"],
                              "probe_to": ["boss@example.test", "it@example.test"], "warnings": []}
    assert notice.reply_to == "sidorov@example.test"                 # «Ответить» в письме о заявке ведёт к заявителю

    # 3. открывает доступ, поправив логин и ФИО
    approved = admin.post("/api/admin/requests/approve",
                          json={"id": claim["id"], "login": "Sidorov.P", "name": "Сидоров Пётр"}).json()
    assert approved["ok"] and approved["login"] == "sidorov.p" and approved["mail_error"] is None
    assert approved["requests"] == [] and admin.get("/api/me").json()["requests"] == 0
    user, = approved["users"]
    assert (user["login"], user["name"], user["email"], user["has_password"], user["admin"]) == (
        "sidorov.p", "Сидоров Пётр", "sidorov@example.test", False, False)
    assert user["read"] == {} and (web.cfg.inbox / HOME / "sidorov.p").is_dir()      # только личная папка
    saved = web.claims.all()
    assert saved[0]["status"] == "approved" and saved[0]["decided_by"] == "admin" and saved[0]["login"] == "sidorov.p"

    # 4. человеку пришло письмо с логином и ссылкой, пароля в нём нет — его ещё не существует
    letter = post.sent[1]
    assert letter.to == ["sidorov@example.test"] and letter.subject == "доступ открыт"
    assert "Ваш логин: sidorov.p" in letter.text and "http://podyachiy.test/set-password#" in letter.text
    token = post.token()
    assert f'href="http://podyachiy.test/set-password#{token}"' in letter.html
    for password in ("", PASSWORD):                                   # без пароля войти нельзя
        assert guest.post("/api/login", json={"login": "sidorov.p", "password": password}).status_code == 401
    assert guest.get("/api/browse", auth=("sidorov.p", "")).status_code == 401

    # 5. по ссылке задаёт пароль
    assert guest.post("/api/set-password/check", json={"token": token}).json() == {
        "login": "sidorov.p", "name": "Сидоров Пётр", "password_min": users.PASSWORD_MIN}
    assert guest.post("/api/set-password", json={"token": token, "password": "мало"}).status_code == 400
    assert guest.post("/api/set-password", json={"token": token, "password": "пароль-из-письма"}).json() == {
        "ok": True, "login": "sidorov.p"}

    # 6. и входит
    sidorov = enter(web, "sidorov.p", "пароль-из-письма")
    me = sidorov.get("/api/me").json()
    assert me["name"] == "Сидоров Пётр" and me["home"] == f"{HOME}/sidorov.p" and me["requests"] == 0
    assert dir_names(sidorov, HOME) == ["sidorov.p"]

    # ссылка одноразовая
    assert guest.post("/api/set-password/check", json={"token": token}).status_code == 404
    again = guest.post("/api/set-password", json={"token": token, "password": "другой-пароль-1"})
    assert again.status_code == 400 and "недействительна" in again.json()["detail"]
    assert enter(web, "sidorov.p", "пароль-из-письма")

    assert [k for k in kinds(web) if k not in ("login", "login_failed")] == [
        "request_new", "request_approve", "link_sent", "password_link"]
    events = {e["kind"]: e for e in web.trail.read()}
    assert events["request_new"]["by"] == "" and events["request_new"]["login"] == "sidorov"
    assert events["link_sent"]["by"] == "admin" and events["link_sent"]["why"] == "invite"
    assert events["password_link"]["by"] == "sidorov.p"
    # ни строка ссылки, ни пароль не осели ни в одном файле сервиса
    for path in list(web.cfg.state_dir.rglob("*")) + list(web.cfg.outbox.rglob("*")):
        if path.is_file() and path.name != "session.key":
            content = path.read_text(encoding="utf-8", errors="replace")
            assert token not in content and "пароль-из-письма" not in content, path.name


def test_expired_link_is_refused(office):
    web, guest, admin, post = office
    guest.post("/api/request", json=CLAIM)
    claim, = admin.get("/api/admin/requests").json()["requests"]
    admin.post("/api/admin/requests/approve", json={"id": claim["id"], "login": "sidorov", "name": "Сидоров"})
    token = post.token()
    assert web.people.users["sidorov"]["link"]["expires"] - time.time() == pytest.approx(48 * 3600, abs=60)
    with web.base.write() as con:                                                  # 48 часов прошли
        con.execute("UPDATE users SET link_expires = ? WHERE login = 'sidorov'", (int(time.time()) - 1,))
    assert guest.post("/api/set-password/check", json={"token": token}).status_code == 404
    late = guest.post("/api/set-password", json={"token": token, "password": "пароль-из-письма"})
    assert late.status_code == 400 and "срок вышел" in late.json()["detail"]
    assert guest.post("/api/login", json={"login": "sidorov", "password": "пароль-из-письма"}).status_code == 401
    for junk in ("", "мусор"):
        assert guest.post("/api/set-password/check", json={"token": junk}).status_code == 404
        assert guest.post("/api/set-password", json={"token": junk, "password": "пароль-из-письма"}).status_code == 400

    # администратор высылает новую ссылку — она работает, старая так и не действует
    sent = admin.post("/api/admin/users/link", json={"login": "Sidorov"})
    assert sent.json() == {"ok": True, "login": "sidorov", "email": "sidorov@example.test"}
    assert post.sent[-1].subject == "доступ открыт" and post.token() != token
    assert guest.post("/api/set-password", json={"token": post.token(), "password": "пароль-из-письма"}).status_code == 200
    assert enter(web, "sidorov", "пароль-из-письма")
    assert [e["why"] for e in web.trail.read() if e["kind"] == "link_sent"] == ["invite", "admin"]
    # у кого пароль уже есть, тому по кнопке администратора уходит письмо «новый пароль», а не «доступ открыт»
    assert admin.post("/api/admin/users/link", json={"login": "sidorov"}).status_code == 200
    assert post.sent[-1].subject == "новый пароль" and enter(web, "sidorov", "пароль-из-письма")
    assert admin.post("/api/admin/users/link", json={"login": "nobody"}).status_code == 404


def test_forgotten_password_is_reset_by_the_link(office):
    web, guest, admin, post = office
    for login, email in (("ivanov", "Ivanov@example.test"), ("petrov", "")):
        saved = admin.post("/api/admin/users/save", json={"login": login, "name": login.title(),
                                                          "password": PASSWORD, "email": email})
        assert saved.status_code == 200
    assert [u["email"] for u in admin.post("/api/admin/users").json()["users"]] == ["ivanov@example.test", ""]
    ivanov = enter(web, "ivanov")

    # ответ один и тот же, есть пользователь или нет; письмо уходит только тому, у кого есть почта
    for typed in ("nobody", "nobody@example.test", "admin", "petrov"):
        assert guest.post("/api/forgot", json={"login": typed}).json() == {"ok": True}, typed
    assert post.sent == [] and "link" not in web.people.users["petrov"]
    assert guest.post("/api/forgot", json={"login": " "}).status_code == 400

    assert guest.post("/api/forgot", json={"login": " Ivanov "}).json() == {"ok": True}
    letter, = post.sent
    assert letter.to == ["ivanov@example.test"] and letter.subject == "новый пароль" and "Ваш логин: ivanov" in letter.text
    first = post.token()
    assert enter(web, "ivanov") and ivanov.get("/api/browse").status_code == 200    # прежний пароль пока действует

    assert guest.post("/api/forgot", json={"login": "IVANOV@example.test"}).json() == {"ok": True}   # и по почте
    second = post.token()
    assert len(post.sent) == 2 and second != first
    assert guest.post("/api/set-password/check", json={"token": first}).status_code == 404      # новая гасит прежнюю
    assert guest.post("/api/set-password", json={"token": second, "password": "новый-пароль-5678"}).status_code == 200
    assert guest.post("/api/login", json={"login": "ivanov", "password": PASSWORD}).status_code == 401
    assert ivanov.get("/api/browse").status_code == 401                             # старые входы закрыты
    assert enter(web, "ivanov", "новый-пароль-5678")

    journal = [(e["kind"], e.get("login"), e.get("why")) for e in web.trail.read()
               if e["kind"] in ("reset_request", "link_sent", "password_link")]
    assert journal == [("reset_request", "nobody", None), ("reset_request", "nobody@example.test", None),
                       ("reset_request", "admin", None), ("reset_request", "petrov", None),
                       ("reset_request", "ivanov", None), ("link_sent", "ivanov", "reset"),
                       ("reset_request", "ivanov@example.test", None), ("link_sent", "ivanov", "reset"),
                       ("password_link", None, None)]


def test_open_forms_are_rate_limited(office):
    web, guest, admin, post = office
    for n in range(5):
        sent = guest.post("/api/request", json={"name": "Имя", "email": f"user{n}@example.test", "login": f"user{n}"})
        assert sent.status_code == 200, n
    sixth = guest.post("/api/request", json={"name": "Имя", "email": "user5@example.test", "login": "user5"})
    assert sixth.status_code == 429 and "через час" in sixth.json()["detail"]
    assert len(admin.get("/api/admin/requests").json()["requests"]) == 5 and len(post.sent) == 5
    # отказы по содержанию принятыми не считаются, но и им есть предел — 30 отправок в час
    web.claim_accepted._hits.clear()
    for n in range(24):
        assert guest.post("/api/request", json={"name": "", "email": "x@example.test", "login": "x1"}).status_code == 400
    assert guest.post("/api/request", json={"name": "", "email": "x@example.test", "login": "x1"}).status_code == 429

    # сброс пароля: три запроса в час на один логин, десять — с одного адреса (отказанный тоже в счёт)
    for n in range(3):
        assert guest.post("/api/forgot", json={"login": "ivanov"}).status_code == 200
    assert guest.post("/api/forgot", json={"login": " Ivanov "}).status_code == 429
    for n in range(6):
        assert guest.post("/api/forgot", json={"login": f"other{n}"}).status_code == 200
    assert guest.post("/api/forgot", json={"login": "one-more"}).status_code == 429
    assert kinds(web).count("reset_request") == 9 and kinds(web).count("request_new") == 5


def test_request_form_refuses_bad_and_repeated_data_and_names_the_field(office, monkeypatch):
    web, guest, admin, post = office
    admin.post("/api/admin/users/save", json={"login": "ivanov", "password": PASSWORD, "email": "ivanov@example.test"})

    def refused(status, field, **change):
        answer = guest.post("/api/request", json={**CLAIM, **change})
        assert answer.status_code == status and answer.headers.get("x-field") == field, change
        return answer.json()["detail"]

    refused(400, "name", name="  ")
    refused(400, "name", name="Я" * 121)
    refused(400, "email", email="не почта")
    refused(400, "email", email="a@b.ru\r\nBcc: x@y.ru")
    refused(400, "login", login="Сидоров")
    refused(400, "login", login="admin")
    refused(400, "comment", comment="ы" * 501)
    assert "занят" in refused(409, "login", login="ivanov")
    assert "Забыл пароль" in refused(409, "email", email="IVANOV@example.test")
    assert post.sent == [] and admin.get("/api/admin/requests").json()["requests"] == []

    assert guest.post("/api/request", json=CLAIM).status_code == 200
    assert "уже подана" in refused(409, None)
    assert "уже подана" in refused(409, None, login="another")
    monkeypatch.setattr(web.signup, "PENDING_MAX", 1)
    assert "слишком много заявок" in refused(503, None, login="petrov", email="petrov@example.test")
    assert len(post.sent) == 1 and admin.get("/api/me").json()["requests"] == 1

    # логин заняли, пока заявка ждала: одобрить можно, только поправив его
    claim, = admin.get("/api/admin/requests").json()["requests"]
    admin.post("/api/admin/users/save", json={"login": "sidorov", "password": PASSWORD})
    taken = admin.post("/api/admin/requests/approve", json={"id": claim["id"], "login": "sidorov", "name": "Сидоров"})
    assert taken.status_code == 409 and admin.get("/api/me").json()["requests"] == 1
    bad = admin.post("/api/admin/requests/approve", json={"id": claim["id"], "login": "Сидоров", "name": "Сидоров"})
    assert bad.status_code == 400
    assert admin.post("/api/admin/requests/approve", json={"id": "нет", "login": "x1", "name": "Имя"}).status_code == 404
    assert len(post.sent) == 1


def test_rejected_request_gets_no_letter_and_is_marked(office):
    web, guest, admin, post = office
    guest.post("/api/request", json=CLAIM)
    claim, = admin.get("/api/admin/requests").json()["requests"]
    rejected = admin.post("/api/admin/requests/reject", json={"id": claim["id"]})
    assert rejected.json() == {"ok": True, "requests": []}
    assert len(post.sent) == 1 and not web.people.users              # ушло только уведомление администраторам
    saved = web.claims.all()
    assert saved[0]["status"] == "rejected" and saved[0]["decided_by"] == "admin"
    assert admin.post("/api/admin/requests/reject", json={"id": claim["id"]}).status_code == 404
    assert admin.post("/api/admin/requests/approve",
                      json={"id": claim["id"], "login": "sidorov", "name": "Сидоров"}).status_code == 404
    assert [e for e in web.trail.read() if e["kind"] == "request_reject"][0]["login"] == "sidorov"
    assert guest.post("/api/request", json=CLAIM).status_code == 200  # подать заново можно


def test_trial_letter_and_smtp_error_shown_as_it_is(office):
    web, guest, admin, post = office
    assert admin.post("/api/admin/mail/test").json() == {"ok": True, "to": ["boss@example.test", "it@example.test"]}
    assert post.sent[0].subject == "пробное письмо" and "http://podyachiy.test" in post.sent[0].text
    # у вошедшего администратора своя почта — пробное письмо идёт ему
    admin.post("/api/admin/users/save", json={"login": "chief", "password": PASSWORD, "admin": True,
                                              "email": "chief@example.test"})
    chief = enter(web, "chief")
    assert chief.get("/api/admin/requests").json()["mail"]["probe_to"] == ["chief@example.test"]
    assert chief.post("/api/admin/mail/test").json()["to"] == ["chief@example.test"]

    post.broken = web.mail.MailError("SMTPAuthenticationError: (535, b'5.7.8 Username and Password not accepted')")
    failed = admin.post("/api/admin/mail/test")
    assert failed.status_code == 502
    assert failed.json()["detail"] == "SMTPAuthenticationError: (535, b'5.7.8 Username and Password not accepted')"

    # почта сломалась в момент одобрения: пользователь заведён, об ошибке сказано, а ссылку получает
    # администратор — как без почты: иначе её не получить, пока почта не починена
    guest.post("/api/request", json=CLAIM)
    assert web.trail.read()[-1]["kind"] == "mail_failed" and web.trail.read()[-1]["what"] == "notice"
    assert web.trail.read()[-1]["login"] == "sidorov" and "535" in web.trail.read()[-1]["error"]
    claim, = admin.get("/api/admin/requests").json()["requests"]
    approved = admin.post("/api/admin/requests/approve", json={"id": claim["id"], "login": "sidorov", "name": "Сидоров"})
    assert approved.status_code == 200 and "535" in approved.json()["mail_error"]
    assert "sidorov" in web.people.users and approved.json()["requests"] == []
    assert guest.post("/api/set-password/check", json={"token": approved.json()["link"]["token"]}).status_code == 200
    again = admin.post("/api/admin/users/link", json={"login": "sidorov"})
    assert again.status_code == 200 and "535" in again.json()["mail_error"] and again.json()["email"] == "sidorov@example.test"
    assert again.json()["link"]["path"] == "/set-password" and again.json()["link"]["hours"] == 48
    # новая ссылка гасит прежнюю и работает
    assert guest.post("/api/set-password/check", json={"token": approved.json()["link"]["token"]}).status_code == 404
    assert guest.post("/api/set-password/check", json={"token": again.json()["link"]["token"]}).status_code == 200
    assert "link_sent" not in kinds(web) and kinds(web).count("link_shown") == 2
    # каждое не ушедшее письмо — в журнале действий, с ответом сервера
    failed = [e for e in web.trail.read() if e["kind"] == "mail_failed" and e["what"] == "link"]
    assert [(e["by"], e["login"], e["why"]) for e in failed] == [("admin", "sidorov", "invite"), ("admin", "sidorov", "admin")]
    assert all("535" in e["error"] for e in failed)
    # ключу с управлением ссылка не отдаётся и при сбое почты
    guest.post("/api/request", json={**CLAIM, "login": "orlova", "email": "orlova@example.test"})
    claim, = admin.get("/api/admin/requests").json()["requests"]
    robot, _ = keyed(web, admin, "Claude Code", 30, manage=True)
    by_key = robot.post("/api/admin/requests/approve", json={"id": claim["id"], "login": "orlova", "name": "Орлова"}).json()
    assert by_key["link"] is None and by_key["link_on_page"] is True and "535" in by_key["mail_error"]
    # «Забыл пароль»: человеку ответ тот же, а сбой письма записан
    admin.post("/api/admin/users/save", json={"login": "ivanov", "password": PASSWORD, "email": "ivanov@example.test"})
    assert guest.post("/api/forgot", json={"login": "ivanov"}).json() == {"ok": True}
    event = web.trail.read()[-1]
    assert (event["kind"], event["what"], event["by"], event["login"], event["why"]) == ("mail_failed", "link", "", "ivanov", "reset")
    post.broken = None
    assert admin.post("/api/admin/users/link", json={"login": "sidorov"}).json() == {
        "ok": True, "login": "sidorov", "email": "sidorov@example.test"}
    assert guest.post("/api/set-password", json={"token": post.token(), "password": "пароль-из-письма"}).status_code == 200
    # у пользователя почты нет — слать некуда: ссылку получает администратор и передаёт сам
    admin.post("/api/admin/users/save", json={"login": "petrov", "password": PASSWORD})
    letters = len(post.sent)
    shown = admin.post("/api/admin/users/link", json={"login": "petrov"}).json()
    assert shown["link"]["path"] == "/set-password" and shown["link"]["hours"] == 48 and len(post.sent) == letters
    assert guest.post("/api/set-password", json={"token": shown["link"]["token"], "password": "новый-пароль-1"}).status_code == 200
    assert enter(web, "petrov", "новый-пароль-1")


def test_user_email_is_edited_by_the_admin_and_must_be_unique(office):
    web, guest, admin, post = office
    admin.post("/api/admin/users/save", json={"login": "ivanov", "password": PASSWORD, "email": "ivanov@example.test"})
    assert admin.post("/api/admin/users/save", json={"login": "ivanov", "name": "Иванов"}).status_code == 200
    assert web.people.users["ivanov"]["email"] == "ivanov@example.test"          # почту не называли — осталась
    assert admin.post("/api/admin/users/save", json={"login": "ivanov", "email": "не почта"}).status_code == 400
    clash = admin.post("/api/admin/users/save", json={"login": "petrov", "password": PASSWORD,
                                                      "email": "Ivanov@example.test"})
    assert clash.status_code == 400 and "petrov" not in web.people.users
    assert admin.post("/api/admin/users/save", json={"login": "ivanov", "email": ""}).status_code == 200
    assert web.people.users["ivanov"]["email"] == ""


def test_without_smtp_the_mail_features_are_off(site):
    web, guest = site
    admin = enter(web, "admin", ADMIN_PASSWORD)
    shown = guest.get("/api/me").json()
    assert shown["mail"] is False and shown["signup"] is True and admin.get("/api/me").json()["mail"] is False
    off = guest.post("/api/forgot", json={"login": "ivanov"})
    assert off.status_code == 503 and "почта в сервисе не настроена" in off.json()["detail"]
    assert admin.post("/api/admin/mail/test").status_code == 503
    # заявка и ссылка «задать пароль» от почты не зависят: нет такой заявки и такого пользователя — 404
    assert admin.post("/api/admin/users/link", json={"login": "ivanov"}).status_code == 404
    assert admin.post("/api/admin/requests/approve", json={"id": "x", "login": "sidorov", "name": "Сидоров"}).status_code == 404
    state = admin.get("/api/admin/requests").json()
    assert state["requests"] == [] and state["mail"]["on"] is False and state["mail"]["why_off"] is None
    assert web.claims.all() == [] and kinds(web) == ["login"]
    # страница задания пароля отдаётся и без почты: уже высланные ссылки остаются в силе
    assert guest.get("/set-password").status_code == 200
    assert guest.post("/api/set-password/check", json={"token": "мусор"}).status_code == 404


def test_half_configured_mail_stays_off_and_says_why(tmp_path):
    env = {k: v for k, v in MAIL_ENV.items() if k != "PUBLIC_URL"}
    with running(tmp_path, extra=env) as (web, guest):
        assert guest.get("/api/me").json()["mail"] is False
        assert guest.post("/api/request", json=CLAIM).status_code == 200          # заявке почта не нужна
        why = guest.get("/api/admin/requests", auth=("admin", ADMIN_PASSWORD)).json()["mail"]["why_off"]
        assert "PUBLIC_URL" in why and any("почта выключена" in line and "PUBLIC_URL" in line for line in web.log.lines)


def test_missing_ca_file_turns_the_mail_off_and_says_why(tmp_path):
    gone = str(tmp_path / "smtp-ca.pem")
    with running(tmp_path, extra={**MAIL_ENV, "SMTP_SECURITY": "starttls", "SMTP_CA_FILE": gone}) as (web, guest):
        assert web.cfg.smtp_ca_file == gone and guest.get("/api/me").json()["mail"] is False
        state = guest.get("/api/admin/requests", auth=("admin", ADMIN_PASSWORD)).json()["mail"]
        assert state["on"] is False and "SMTP_CA_FILE" in state["why_off"] and "такого файла" in state["why_off"]
        assert any("почта выключена" in line and "SMTP_CA_FILE" in line for line in web.log.lines)
    (tmp_path / "smtp-ca.pem").write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    with running(tmp_path, extra={**MAIL_ENV, "SMTP_SECURITY": "starttls", "SMTP_CA_FILE": gone}) as (web, guest):
        assert guest.get("/api/me").json()["mail"] is True            # файл на месте — почта включена


def test_mail_password_in_the_clear_is_warned_about_at_start_and_in_the_mail_block(tmp_path):
    with running(tmp_path, extra={**MAIL_ENV, "SMTP_USER": "robot", "SMTP_PASSWORD": "secret"}) as (web, guest):
        warning, = guest.get("/api/admin/requests", auth=("admin", ADMIN_PASSWORD)).json()["mail"]["warnings"]
        assert "SMTP_SECURITY=none" in warning and "открытым текстом" in warning and "secret" not in warning
        assert any("ВНИМАНИЕ" in line and "открытым текстом" in line for line in web.log.lines)
    (tmp_path / "tls").mkdir()
    with running(tmp_path / "tls", extra={**MAIL_ENV, "SMTP_USER": "robot", "SMTP_SECURITY": "starttls"}) as (web, guest):
        assert guest.get("/api/admin/requests", auth=("admin", ADMIN_PASSWORD)).json()["mail"]["warnings"] == []
        assert not any("открытым текстом" in line for line in web.log.lines)


def test_pages_say_what_the_mail_did():
    """То, что страницы показывают о почте: проверено и в браузере, здесь — чтобы не потерялось."""
    static = ROOT / "app" / "static"
    login = (static / "login.html").read_text(encoding="utf-8")
    assert "Если заявку отклонят, письма не будет" in login
    common = (static / "common.js").read_text(encoding="utf-8")
    # «отправлен не всем» — со знаком предупреждения, а не с галочкой успеха
    assert '`${icon("alert", 17)}<b>Протокол отправлен не всем</b>`' in common
    assert '`${icon("ok", 17)}<b>Протокол отправлен</b>`' in common
    # «Не отправлено» стоит над кнопкой «Отправить», объявляется и прокручивается в видимую часть окна
    assert common.index('id="smsg" role="alert"') < common.index('<button id="sgo">')
    assert "msg.scrollIntoView(" in common and "Не отправлено: ${esc(err.message)}" in common
    admin = (static / "admin.html").read_text(encoding="utf-8")
    assert "m.warnings.map(" in admin and "done.mail_error" in admin and 'e.what === "link"' in admin


def test_personal_mode_has_no_mail_even_with_smtp(tmp_path):
    with running(tmp_path, mode="personal", extra=MAIL_ENV) as (web, guest):
        me = guest.get("/api/me").json()
        assert me["mail"] is False and me["signup"] is False and me["requests"] == 0
        assert guest.post("/api/request", json=CLAIM).status_code == 404          # входа нет — и заявок нет


# ---------- ключи доступа ----------

def keyed(web, client, name="Cursor", days=0, manage=False):
    """Человек выпускает себе ключ; отдаёт клиента, который ходит с этим ключом и без куки."""
    issued = client.post("/api/keys", json={"name": name, "days": days, "manage": manage})
    assert issued.status_code == 200, issued.text
    robot = TestClient(web.app)
    robot.headers["Authorization"] = "Bearer " + issued.json()["key"]
    return robot, issued.json()


def key_file(web):
    """Ключи, как они лежат в базе, — прочитанные с диска другим соединением."""
    return [dict(row) for row in db.Database(web.cfg.state_dir).rows("SELECT * FROM keys ORDER BY n")]


def test_person_issues_a_key_and_the_client_works_with_his_rights(site):
    web, admin, ivanov, petrov = staffed(site)
    admin.post("/api/folder", json={"dir": "Проекты"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "tree"})
    days = {"plain": [0, 90, 365], "manage": [7, 30, 90]}
    assert ivanov.get("/api/keys").json() == {"keys": [], "can_manage": False, "days": days}
    robot, issued = keyed(web, ivanov, "Cursor на ноутбуке", 90)
    token = issued["key"]
    assert token.startswith("stg_") and issued["issued"]["prefix"] == token[:12]
    assert issued["issued"]["expires"] - time.time() == pytest.approx(90 * 86400, abs=60)
    listed, = ivanov.get("/api/keys").json()["keys"]
    assert listed["name"] == "Cursor на ноутбуке" and listed["used"] is None and "hash" not in listed
    assert (listed["owner"], listed["kind"]) == ("ivanov", "plain")
    stored = on_disk(web)
    assert token not in stored and token[12:] not in stored and keys.digest(token) in stored    # в базе только отпечаток
    assert key_file(web)[0]["name"] == "Cursor на ноутбуке"

    who = robot.get("/api/v1/whoami").json()
    assert {k: who[k] for k in ("login", "name", "admin", "via", "key", "manage", "service", "upload")} == {
        "login": "ivanov", "name": "Иванов", "admin": False, "via": "key", "key": "Cursor на ноутбуке",
        "manage": False, "service": "http://testserver", "upload": True}
    assert ivanov.get("/api/v1/whoami").json()["via"] == "login"
    assert ivanov.get("/api/keys").json()["keys"][0]["used"] == pytest.approx(time.time(), abs=60)
    assert "Проекты" in dir_names(robot) and robot.get("/api/browse", params={"path": f"{HOME}/petrov"}).json()["files"] == []
    assert robot.post("/api/folder", json={"dir": f"{HOME}/ivanov/От клиента"}).status_code == 200

    for bad in ("stg_wrong", token[:-2], "", token.upper()):
        assert site[1].get("/api/v1/whoami", headers={"Authorization": f"Bearer {bad}"}).status_code == 401, bad
    assert ivanov.post("/api/keys", json={"name": "Cursor на ноутбуке"}).status_code == 400       # имя занято
    assert ivanov.post("/api/keys", json={"name": "ещё", "days": 7}).status_code == 400
    assert ivanov.post("/api/keys", json={"name": "по умолчанию"}).json()["issued"]["expires"] - time.time() \
        == pytest.approx(365 * 86400, abs=60)                                                    # без срока в запросе — год
    by_default = ivanov.get("/api/keys").json()["keys"][1]["id"]
    assert ivanov.post("/api/keys/revoke", json={"id": by_default}).status_code == 200

    assert petrov.post("/api/keys/revoke", json={"id": listed["id"]}).status_code == 404   # чужой ключ не отозвать
    assert robot.get("/api/v1/whoami").status_code == 200
    assert ivanov.post("/api/keys/revoke", json={"id": listed["id"]}).json() == {
        "ok": True, "keys": [], "can_manage": False, "days": days}
    assert robot.get("/api/v1/whoami").status_code == 401

    events = [e for e in web.trail.read() if e["kind"] in ("key_issue", "key_revoke", "folder_create")]
    assert [(e["kind"], e["by"], e.get("key")) for e in events] == [
        ("folder_create", "admin", None), ("key_issue", "ivanov", "Cursor на ноутбуке"),
        ("folder_create", "ivanov", "Cursor на ноутбуке"), ("key_issue", "ivanov", "по умолчанию"),
        ("key_revoke", "ivanov", "по умолчанию"), ("key_revoke", "ivanov", "Cursor на ноутбуке")]
    assert (events[2]["via"], events[2]["key_kind"]) == ("key", "plain") and "via" not in events[0]
    assert "via" not in events[1] and events[1]["key_kind"] == "plain"       # выпуск — сам человек, не ключ
    assert token not in on_disk(web) and token not in str(web.trail.read())


def test_builtin_admin_issues_a_key_for_his_own_client(site):
    """У admin из .env учётной записи в базе нет, но ключ ему нужен так же: без него интеграция
    с ИИ у администратора не работала."""
    web, admin, _, _ = staffed(site)
    assert admin.get("/api/keys").json()["can_manage"] is True
    robot, issued = keyed(web, admin, "Claude Code", 90)
    who = robot.get("/api/v1/whoami").json()
    assert (who["login"], who["admin"], who["via"], who["manage"]) == ("admin", True, "key", False)
    assert robot.get("/api/v1/tree").status_code == 200 and robot.get("/api/v1/costs").status_code == 403
    assert robot.get("/api/admin/status").status_code == 403                 # обычный ключ сервисом не управляет
    assert [(k["owner"], k["name"]) for k in key_file(web)] == [("admin", "Claude Code")]
    assert "admin" not in web.people.users
    assert admin.post("/api/keys/revoke", json={"id": issued["issued"]["id"]}).status_code == 200
    assert robot.get("/api/v1/whoami").status_code == 401


def test_key_dies_with_its_term_and_with_the_user(site):
    web, admin, ivanov, petrov = staffed(site)
    robot, issued = keyed(web, ivanov, days=90)
    other, _ = keyed(web, petrov)
    with web.base.write() as con:                                        # 90 дней прошли
        con.execute("UPDATE keys SET expires = ? WHERE owner = 'ivanov'", (int(time.time()) - 1,))
    assert robot.get("/api/v1/tree").status_code == 401
    assert admin.post("/api/admin/users/delete", json={"login": "petrov"}).status_code == 200
    assert other.get("/api/v1/tree").status_code == 401
    assert [k["owner"] for k in key_file(web)] == ["ivanov"]            # ключи удалённого убраны и из базы


def test_keys_kept_in_the_users_file_move_on_start_and_keep_working(tmp_path):
    """Перенос данных при запуске: ключи из users.json прежнего вида переезжают в базу, к остальным ключам."""
    token = "stg_" + "b" * 43
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "users.json").write_text(json.dumps({"users": {"ivanov": {
        "name": "Иванов", "email": "", "password": users.hash_password(PASSWORD), "home": f"{HOME}/ivanov",
        "admin": False, "read": {}, "manage": {}, "notify_ready": True,
        "keys": [{"id": "0a1b2c3d", "name": "Cursor", "prefix": token[:12], "hash": keys.digest(token),
                  "created": 1000, "expires": None, "used": None}]}}}), encoding="utf-8")
    with running(tmp_path) as (web, client):
        who = client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {token}"})
        assert who.status_code == 200 and (who.json()["login"], who.json()["key"]) == ("ivanov", "Cursor")
        assert [(k["id"], k["owner"], k["kind"]) for k in key_file(web)] == [("0a1b2c3d", "ivanov", "plain")]
        assert not (web.cfg.state_dir / "users.json").exists()                  # прежний файл убран в «до-базы-…»
        assert len(list(web.cfg.state_dir.glob("до-базы-*/users.json"))) == 1
        assert [k["name"] for k in enter(web, "ivanov").get("/api/keys").json()["keys"]] == ["Cursor"]
    with running(tmp_path) as (web, client):                # и после следующего запуска — один ключ, не два
        assert len(key_file(web)) == 1
        assert client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def refusals(web, robot):
    """Каким адресам приложения ключ получил отказ и с какими словами."""
    plain, page = set(), set()
    for method, path in api_routes(web):
        if path in ("/api/login", "/api/logout"):
            continue
        answer = robot.request(method, path)
        detail = answer.json().get("detail") if answer.status_code == 403 else None
        if detail == web.BY_KEY_ONLY_READS:
            plain.add((method, path))
        elif detail == web.PAGE_ONLY:
            page.add((method, path))
        else:
            assert answer.status_code != 403, f"{method} {path}"         # администратору отказать больше не в чем
    return plain, page


def test_key_levels_match_the_routes(site):
    """Каждый адрес приложения спрошен обоими ключами администратора сервиса. Обычному ключу
    закрыто всё из PAGE_ONLY и MANAGE_KEY; ключу с управлением — только PAGE_ONLY. Новый адрес
    без зависимости steward или person сюда попадёт как лишний открытый — и тест это покажет."""
    web, admin, ivanov, _ = staffed(site)
    admin.post("/api/admin/users/save", json={"login": "ivanov", "name": "Иванов", "admin": True})
    assert PAGE_ONLY.isdisjoint(MANAGE_KEY) and PAGE_ONLY | MANAGE_KEY <= set(api_routes(web))
    for owner in (ivanov, admin):                                   # заведённый администратор и встроенный
        robot, _ = keyed(web, owner, "обычный")
        assert refusals(web, robot) == (PAGE_ONLY | MANAGE_KEY, set())
        steward, _ = keyed(web, owner, "с управлением", 30, manage=True)
        assert refusals(web, steward) == (set(), PAGE_ONLY)
        assert steward.get("/api/v1/whoami").json()["manage"] is True
        # вместе с кукой ключ не нужен: запрет действует на вход ключом, а не на человека
        assert owner.get("/api/admin/status").status_code == 200
        # расходы для внешних систем — тоже управление: обычному ключу закрыты, с управлением — открыты
        assert robot.get("/api/v1/costs").status_code == 403 and steward.get("/api/v1/costs").status_code == 200
    assert sorted(web.people.users) == ["ivanov", "petrov"] and len(key_file(web)) == 4


def test_manage_key_is_issued_only_to_service_admins_and_for_ninety_days(site):
    web, admin, ivanov, _ = staffed(site)
    refused = ivanov.post("/api/keys", json={"name": "управление", "days": 30, "manage": True})
    assert refused.status_code == 403 and "администратор" in refused.json()["detail"]
    for days in (0, 365):
        too_long = admin.post("/api/keys", json={"name": "управление", "days": days, "manage": True})
        assert too_long.status_code == 400 and "90" in too_long.json()["detail"]
    _, issued = keyed(web, admin, "управление", 90, manage=True)
    assert issued["issued"]["kind"] == "manage"
    assert issued["issued"]["expires"] - time.time() == pytest.approx(90 * 86400, abs=60)
    event = [e for e in web.trail.read() if e["kind"] == "key_issue"][-1]
    assert (event["by"], event["key"], event["key_kind"]) == ("admin", "управление", "manage")
    assert key_file(web) and not ivanov.get("/api/keys").json()["keys"]


def test_manage_key_runs_the_service_but_leaves_passwords_keys_and_deletion_to_the_page(site):
    web, admin, ivanov, _ = staffed(site)
    admin.post("/api/folder", json={"dir": "Проекты"})
    recorded(web, "Проекты/вкс.mp4")
    steward, _ = keyed(web, admin, "Claude Code", 30, manage=True)
    plain, _ = keyed(web, admin, "обычный")

    # заводит пользователя с первым паролем, даёт право на папку, меняет имя и роль
    new = {"login": "sidorov", "name": "Сидоров", "password": PASSWORD}
    assert plain.post("/api/admin/users/save", json=new).status_code == 403 and "sidorov" not in web.people.users
    assert steward.post("/api/admin/users/save", json=new).status_code == 200
    assert enter(web, "sidorov").get("/api/me").json()["name"] == "Сидоров"
    grant = {"dir": "Проекты", "login": "sidorov", "read": "tree", "manage": "only"}
    assert plain.post("/api/access", json=grant).json()["detail"] == web.BY_KEY_ONLY_READS
    assert steward.post("/api/access", json=grant).status_code == 200
    assert web.people.users["sidorov"]["read"] == {"Проекты": "tree"}
    assert [u["login"] for u in steward.get("/api/access", params={"dir": "Проекты"}).json()["users"]] == [
        "ivanov", "petrov", "sidorov"]
    assert steward.post("/api/admin/users/save", json={"login": "sidorov", "name": "Сидоров С. С."}).status_code == 200
    assert web.people.users["sidorov"]["name"] == "Сидоров С. С."
    assert steward.post("/api/admin/folders/rename", json={"dir": "Проекты", "name": "Стройка"}).status_code == 200
    assert steward.get("/api/admin/report").status_code == 200 and steward.get("/api/admin/keys").status_code == 200
    assert steward.post("/api/admin/speech-model", json={"model": "deepgram/nova-3"}).status_code == 200

    # а это — только на странице: пароль, ссылка на пароль, ключи, удаление записи и папки
    was = web.people.users["sidorov"]["password"]
    for path, body in (("/api/admin/users/save", {"login": "sidorov", "name": "Сидоров", "password": "новый-пароль-1"}),
                       ("/api/admin/users/link", {"login": "sidorov"}),
                       ("/api/keys", {"name": "ещё один"}),
                       ("/api/admin/keys/revoke", {"id": key_file(web)[0]["id"]}),
                       ("/api/record/delete", {"file": "Стройка/вкс.mp4"}),
                       ("/api/admin/folders/delete", {"dir": "Стройка"}),
                       ("/api/record/send", {"file": "Стройка/вкс.mp4", "to": "a@example.test"})):
        refused = steward.post(path, json=body)
        assert refused.status_code == 403 and refused.json()["detail"] == web.PAGE_ONLY, path
    assert web.people.users["sidorov"]["password"] == was and "link" not in web.people.users["sidorov"]
    assert (web.cfg.inbox / "Стройка/вкс.mp4").is_file() and len(key_file(web)) == 2

    # в журнале у каждого действия ключом — пометка, имя ключа и его вид
    events = [e for e in web.trail.read() if e.get("via") == "key"]
    assert [e["kind"] for e in events] == ["user_create", "access", "user_change", "folder_rename", "speech_model"]
    assert {(e["by"], e["key"], e["key_kind"]) for e in events} == {("admin", "Claude Code", "manage")}
    # разжалованный администратор с ключом «с управлением» сервисом уже не управляет
    admin.post("/api/admin/users/save", json={"login": "ivanov", "name": "Иванов", "admin": True})
    left, _ = keyed(web, ivanov, "остался", 7, manage=True)
    admin.post("/api/admin/users/save", json={"login": "ivanov", "name": "Иванов", "admin": False})
    assert left.get("/api/admin/status").status_code == 403 and left.get("/api/v1/whoami").json()["manage"] is False


def test_manage_key_approves_a_request_but_gets_no_password_link(site):
    web, admin, _, _ = staffed(site)
    steward, _ = keyed(web, admin, "Claude Code", 30, manage=True)
    assert site[1].post("/api/request", json={"name": "Сидоров", "email": "", "login": "sidorov"}).status_code == 200
    claim, = steward.get("/api/admin/requests").json()["requests"]
    done = steward.post("/api/admin/requests/approve", json={"id": claim["id"], "login": "sidorov", "name": "Сидоров"})
    assert done.status_code == 200 and done.json()["link"] is None and done.json()["link_on_page"] is True
    assert "sidorov" in web.people.users and "link" not in web.people.users["sidorov"]
    shown = admin.post("/api/admin/users/link", json={"login": "sidorov"})         # ссылку выпускает человек
    assert shown.status_code == 200 and shown.json()["link"]["token"]


def test_admin_sees_all_keys_and_revokes_any(site):
    web, admin, ivanov, petrov = staffed(site)
    robot, issued = keyed(web, ivanov, "Cursor", 90)
    keyed(web, petrov, "Codex")
    steward, _ = keyed(web, admin, "управление", 7, manage=True)
    with web.base.write() as con:
        con.execute("UPDATE keys SET created = created - ? WHERE owner = 'petrov'", (200 * 86400,))    # давно лежит без дела
        con.execute("UPDATE keys SET expires = ? WHERE owner = 'ivanov'", (int(time.time()) - 5,))     # срок вышел
    assert ivanov.get("/api/admin/keys").status_code == 403
    listed = admin.get("/api/admin/keys").json()
    assert listed["stale_days"] == 90
    assert [(k["owner"], k["owner_name"], k["name"], k["kind"], k["mark"]) for k in listed["keys"]] == [
        ("admin", "Администратор", "управление", "manage", ""),
        ("ivanov", "Иванов", "Cursor", "plain", "expired"),
        ("petrov", "Петров", "Codex", "plain", "stale")]
    assert all("hash" not in k and set(k) >= {"id", "prefix", "created", "used", "expires"} for k in listed["keys"])
    assert issued["key"] not in admin.get("/api/admin/keys").text

    codex = listed["keys"][2]["id"]
    assert ivanov.post("/api/admin/keys/revoke", json={"id": codex}).status_code == 403
    assert steward.post("/api/admin/keys/revoke", json={"id": codex}).json()["detail"] == web.PAGE_ONLY
    gone = admin.post("/api/admin/keys/revoke", json={"id": codex})
    assert gone.status_code == 200 and [k["name"] for k in gone.json()["keys"]] == ["управление", "Cursor"]
    assert admin.post("/api/admin/keys/revoke", json={"id": codex}).status_code == 404
    assert petrov.get("/api/keys").json()["keys"] == []
    event = [e for e in web.trail.read() if e["kind"] == "key_revoke"][-1]
    assert (event["by"], event["key"], event["owner"], event["key_kind"]) == ("admin", "Codex", "petrov", "plain")
    assert "via" not in event


def test_ordinary_key_gets_no_costs_and_changes_nothing(site):
    web, admin, ivanov, _ = staffed(site)
    admin.post("/api/folder", json={"dir": "Проекты"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "manage": "tree"})
    recorded(web, "Проекты/вкс.mp4")
    robot, _ = keyed(web, ivanov)
    for path in COSTS:                                # администратор папки — не администратор сервиса
        refused = robot.get(path)
        assert refused.status_code == 403 and refused.json()["detail"] == web.NO_COSTS, path
    for path, body in (("/api/record/delete", {"file": "Проекты/вкс.mp4"}),
                       ("/api/access", {"dir": "Проекты", "login": "petrov", "read": "tree"}),
                       ("/api/password", {"old": PASSWORD, "new": "новый-пароль-1"}),
                       ("/api/keys", {"name": "второй"})):
        refused = robot.post(path, json=body)
        assert refused.status_code == 403 and refused.json()["detail"] == web.BY_KEY_ONLY_READS, path
    assert (web.cfg.inbox / "Проекты/вкс.mp4").is_file() and web.people.users["petrov"]["read"] == {}
    assert enter(web, "ivanov") and len(key_file(web)) == 1 and web.service.queue == []


def test_key_renames_and_moves_a_record_but_does_not_delete(site):
    """Ключом запись переименовывают и переносят — с теми же правами, что у человека, и с пометкой
    в журнале действий, как у загрузки. Удаление остаётся человеку на странице."""
    web, admin, ivanov, _ = staffed(site)
    admin.post("/api/folder", json={"dir": "Проекты"})
    admin.post("/api/folder", json={"dir": "Чужая"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "manage": "tree"})
    admin.post("/api/access", json={"dir": "Чужая", "login": "ivanov", "read": "tree"})
    admin.post("/api/folder", json={"dir": "Проекты/Архив"})
    recorded(web, "Проекты/вкс.mp4")
    recorded(web, "Чужая/план.mp4")
    robot, _ = keyed(web, ivanov, "Cursor")

    renamed = robot.post("/api/v1/rename", params={"file": "Проекты/вкс.mp4", "name": "Итоги"})
    assert renamed.status_code == 200 and renamed.json() == {"ok": True, "file": "Проекты/Итоги.mp4"}
    moved = robot.post("/api/v1/move", params={"file": "Проекты/Итоги.mp4", "dir": "Проекты/Архив"})
    assert moved.status_code == 200 and moved.json()["file"] == "Проекты/Архив/Итоги.mp4"
    assert (web.cfg.inbox / "Проекты/Архив/Итоги.mp4").is_file() and web.service.queue == []
    # адреса страницы ключу тоже открыты: логика одна
    back = robot.post("/api/record/move", json={"file": "Проекты/Архив/Итоги.mp4", "dir": "Проекты"})
    assert back.status_code == 200 and back.json()["file"] == "Проекты/Итоги.mp4"

    # права те же, что у человека: где он только читает, ключ не переименует и туда не перенесёт
    assert robot.post("/api/v1/rename", params={"file": "Чужая/план.mp4", "name": "моё"}).status_code == 403
    assert robot.post("/api/v1/move", params={"file": "Проекты/Итоги.mp4", "dir": "Чужая"}).status_code == 403
    assert robot.post("/api/v1/rename", params={"file": "Нет/такой.mp4", "name": "x"}).status_code == 404
    assert robot.post("/api/v1/move", params={"file": "Проекты/Итоги.mp4"}).status_code == 422      # куда — обязательно
    assert site[1].post("/api/v1/rename", params={"file": "Проекты/Итоги.mp4", "name": "x"}).status_code == 401

    refused = robot.post("/api/record/delete", json={"file": "Проекты/Итоги.mp4"})
    assert refused.status_code == 403 and refused.json()["detail"] == web.BY_KEY_ONLY_READS
    assert (web.cfg.inbox / "Проекты/Итоги.mp4").is_file()

    events = [e for e in web.trail.read() if e["kind"] in ("record_rename", "record_move")]
    assert [(e["kind"], e["by"], e["via"], e["key"]) for e in events] == [
        ("record_rename", "ivanov", "key", "Cursor"), ("record_move", "ivanov", "key", "Cursor"),
        ("record_move", "ivanov", "key", "Cursor")]
    # сам человек со страницы — без пометки
    ivanov.post("/api/record/rename", json={"file": "Проекты/Итоги.mp4", "name": "Решения"})
    assert "via" not in [e for e in web.trail.read() if e["kind"] == "record_rename"][-1]


# ---------- /api/v1 для ИИ-клиентов ----------

TASKS_MD = """# Протокол совещания

## 1. Кратко

Обсудили бюджет склада.

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Иванов Иван** | | | |
| Согласовать смету | до пятницы | [00:10:05] | — |
| **Ответственный не назван** | | | |
| Проверить остатки | — | — | — |
"""


def filled(site):
    """Иванов читает «Проекты» с подпапками и загружает туда; у Петрова своя запись в личной папке."""
    web, admin, ivanov, petrov = staffed(site)
    for path in ("Проекты", "Проекты/Сметы", "Проекты/Сметы/Архив", "Снабжение"):
        admin.post("/api/folder", json={"dir": path})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "tree", "upload": "tree"})
    for rel, title, day in (("Проекты/вкс.mp4", "Планёрка", "2026-10-09"),
                            ("Проекты/Сметы/Архив/смета.mp4", "Смета по складу", "2026-09-01"),
                            ("Снабжение/итоги.mp4", "Итоги недели", "2026-10-08"),
                            (f"{HOME}/petrov/личное.mp4", "Личное", "2026-10-07")):
        recorded(web, rel)
        folder, _, name = rel.rpartition("/")
        stem = name.rsplit(".", 1)[0]
        out = web.cfg.outbox / folder / stem
        (out / "summary" / f"{stem}.md").write_text(TASKS_MD.replace("Иванов Иван", "Петров Пётр")
                                                    if "petrov" in rel else TASKS_MD, encoding="utf-8")
        (out / "transcript").mkdir()
        (out / "transcript" / f"{stem}.txt").write_text("[00:00:01] S1: Про бюджет склада и дебиторку.\n", encoding="utf-8")
        web.service.ledger.put({**web.service.ledger.get(rel), "title": title, "audio_duration_sec": 600,
                                "record_date": day, "finished_at": f"{day}T12:00:00+03:00"})
    return web, admin, ivanov, petrov


def walk(node):
    """Все папки дерева с их записями: путь → файлы."""
    found = {node["path"]: [r["file"] for r in node.get("records", [])]}
    for child in node.get("folders", []):
        found.update(walk(child))
    return found


def test_tree_shows_only_open_folders_with_their_records(site):
    web, admin, ivanov, petrov = filled(site)
    robot, _ = keyed(web, ivanov)
    tree = robot.get("/api/v1/tree", params={"depth": 5}).json()
    assert walk(tree) == {"": [], HOME: [], f"{HOME}/ivanov": [], "Проекты": ["Проекты/вкс.mp4"],
                          "Проекты/Сметы": [], "Проекты/Сметы/Архив": ["Проекты/Сметы/Архив/смета.mp4"]}
    projects = next(f for f in tree["folders"] if f["path"] == "Проекты")
    assert projects["name"] == "Проекты" and projects["can_upload"] and not tree["can_upload"]
    assert projects["records"] == [{"file": "Проекты/вкс.mp4", "title": "Планёрка", "date": "2026-10-09",
                                    "duration_sec": 600, "state": "ready", "has_protocol": True}]
    assert not next(f for f in tree["folders"] if f["path"] == HOME)["can_upload"]        # чужие личные папки рядом

    # глубина: по умолчанию два уровня, дальше — счётчики и отдельный запрос
    cut = walk(robot.get("/api/v1/tree", params={"depth": 0}).json())
    assert cut == {"": [], HOME: [], "Проекты": []}
    shallow = robot.get("/api/v1/tree", params={"depth": 0}).json()
    assert next(f for f in shallow["folders"] if f["path"] == "Проекты") == {
        "path": "Проекты", "name": "Проекты", "can_upload": True, "records_inside": 2}
    inner = robot.get("/api/v1/tree", params={"dir": "Проекты/Сметы"}).json()
    assert inner["path"] == "Проекты/Сметы" and walk(inner) == {
        "Проекты/Сметы": [], "Проекты/Сметы/Архив": ["Проекты/Сметы/Архив/смета.mp4"]}

    # чужое не видно ни содержимым, ни именем; для остальных — свои границы
    for foreign in ("Снабжение", f"{HOME}/petrov", "Нет такой"):
        assert robot.get("/api/v1/tree", params={"dir": foreign}).status_code == 404, foreign
    for bad in ("..", "Проекты/../Снабжение", ".trash"):
        assert admin.get("/api/v1/tree", params={"dir": bad}).status_code == 400, bad
    assert "личное" not in robot.get("/api/v1/tree", params={"depth": 8}).text
    assert set(walk(petrov.get("/api/v1/tree").json())) == {"", HOME, f"{HOME}/petrov"}
    everything = walk(admin.get("/api/v1/tree", params={"depth": 8}).json())
    assert everything["Снабжение"] == ["Снабжение/итоги.mp4"] and f"{HOME}/petrov" in everything


def test_tree_and_status_follow_a_record_through_processing(site, monkeypatch):
    web, admin, ivanov, petrov = filled(site)
    new, rebuilt, waiting = "Проекты/новая.mp4", "Проекты/вкс.mp4", f"{HOME}/ivanov/ждёт.mp3"
    (web.cfg.inbox / new).write_bytes(b"video")
    fresh = ivanov.get("/api/v1/status", params={"file": new}).json()
    assert fresh == {"file": new, "state": "uploaded", "done": False, "text": fresh["text"]} and "принят" in fresh["text"]

    web.service.scan_once()                                         # сканер заметил файл и ждёт, пока он утихнет
    seen = ivanov.get("/api/v1/status", params={"file": new}).json()
    assert seen["state"] == "waiting" and seen["needed_sec"] == 3600 and not seen["done"]
    web.service.pending.clear()
    monkeypatch.setattr(web.service, "queue", [])
    web.service.enqueue_manual(waiting, None, False, "ivanov", "manual")
    monkeypatch.setattr(web.service, "running", [
        job(new, step="расшифровка", chunks_done=1, chunks_total=3), job(rebuilt, step="протокол")])

    going = ivanov.get("/api/v1/status", params={"file": new}).json()
    assert (going["state"], going["step"], going["parts_done"], going["parts_total"], going["done"]) == (
        "processing", "расшифровка", 1, 3, False)
    queued = ivanov.get("/api/v1/status", params={"file": waiting}).json()
    assert queued["state"] == "queued" and queued["position"] == 1 and "очеред" in queued["text"]
    states = {r["file"]: r["state"] for rows in [n.get("records", []) for n in flat(ivanov.get("/api/v1/tree", params={"depth": 5}).json())]
              for r in rows}
    assert states == {new: "processing", rebuilt: "processing", waiting: "queued",
                      "Проекты/Сметы/Архив/смета.mp4": "ready"}
    # пересборка готовой записи: в дереве она одна, с названием и новым состоянием
    assert ivanov.get("/api/v1/tree", params={"dir": "Проекты", "depth": 0}).json()["records"][1]["title"] == "Планёрка"

    web.service.running.clear()
    ready = ivanov.get("/api/v1/status", params={"file": rebuilt}).json()
    assert ready["state"] == "ready" and ready["done"] and ready["finished_at"] == "2026-10-09T12:00:00+03:00"
    web.service.ledger.put({"file": new, "status": "error", "error": "нет связи", "finished_at": "2026-10-10T12:00:00+03:00"})
    failed = ivanov.get("/api/v1/status", params={"file": new}).json()
    assert failed["state"] == "error" and failed["error"] == "нет связи" and failed["done"]

    for path in (new, "Проекты/нет.mp4", "Снабжение/итоги.mp4", "../x.mp4"):
        assert petrov.get("/api/v1/status", params={"file": path}).status_code == 404, path
    assert ivanov.get("/api/v1/status", params={"file": "Проекты/нет.mp4"}).status_code == 404
    assert admin.get("/api/v1/status", params={"file": "../users.json"}).status_code == 404


def flat(node):
    return [node] + [n for child in node.get("folders", []) for n in flat(child)]


def test_record_gives_protocol_tasks_links_and_the_html_file(site):
    web, admin, ivanov, petrov = filled(site)
    robot, _ = keyed(web, ivanov)
    query = {"file": "Проекты/вкс.mp4"}
    got = robot.get("/api/v1/record", params=query).json()
    assert got["title"] == "Планёрка" and got["date"] == "2026-10-09" and got["state"] == "ready" and got["duration_sec"] == 600
    assert got["protocol"].startswith("# Протокол совещания") and "transcript" not in got
    assert got["tasks"][0] == {"who": "Иванов Иван", "absent": False, "task": "Согласовать смету", "due_text": "до пятницы",
                               "due": None, "due_by": None, "due_at": None, "due_date": "2026-10-16", "overdue": False, "open": True,
                               "status": None, "at": "00:10:05", "at_sec": 605, "key": marks.key("Иванов Иван", "Согласовать смету"),
                               "done": False, "done_by": None, "done_at": None, "comment": None}
    assert got["can_mark"] is False                                   # Иванов в «Проектах» только читает
    assert got["links"] == {
        "record": "http://testserver/api/v1/record?file=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B%2F%D0%B2%D0%BA%D1%81.mp4",
        "page": "http://testserver/?path=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B&open=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B%2F%D0%B2%D0%BA%D1%81.mp4",
        "html": "http://testserver/api/v1/record.html?file=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B%2F%D0%B2%D0%BA%D1%81.mp4",
        "audio": None}
    assert robot.get(got["links"]["record"]).json()["title"] == "Планёрка"          # ссылки из ответа рабочие

    page = robot.get(got["links"]["html"])
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "attachment" in page.headers["content-disposition"] and ".map.html" in page.headers["content-disposition"]
    assert "Согласовать смету" in page.text and page.content == robot.get("/api/map", params={**query, "download": True}).content

    for path in ("/api/v1/record", "/api/v1/record.html"):
        assert robot.get(path, params={"file": "Снабжение/итоги.mp4"}).status_code == 404, path
        assert robot.get(path, params={"file": f"{HOME}/petrov/личное.mp4"}).status_code == 404, path
    web.service.ledger.put({"file": "Проекты/пусто.mp4", "status": "no_audio", "stem": "пусто"})
    empty = robot.get("/api/v1/record", params={"file": "Проекты/пусто.mp4"}).json()
    assert empty["state"] == "no_audio" and empty["protocol"] is None and empty["tasks"] == [] and empty["links"]["html"] is None
    assert robot.get("/api/v1/record.html", params={"file": "Проекты/пусто.mp4"}).status_code == 404


def test_protocol_is_exported_to_docx_and_pdf_by_the_same_rights(site):
    import docx
    from pypdf import PdfReader
    web, admin, ivanov, petrov = filled(site)
    robot, _ = keyed(web, ivanov)
    query = {"file": "Проекты/вкс.mp4"}
    before = sorted(str(p) for p in web.cfg.outbox.rglob("*") if "_state" not in p.parts and "_logs" not in p.parts)

    word = ivanov.get("/api/export", params={**query, "format": "docx"})
    assert word.status_code == 200
    assert word.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    # имя файла — название записи; кириллица — по RFC 5987, рядом запасное имя латиницей
    assert word.headers["content-disposition"] == (
        "attachment; filename=\"protocol.docx\"; filename*=UTF-8''%D0%9F%D0%BB%D0%B0%D0%BD%D1%91%D1%80%D0%BA%D0%B0.docx")
    doc = docx.Document(io.BytesIO(word.content))
    assert doc.paragraphs[0].text == "Планёрка" and "Дата совещания: 09.10.2026 · Длительность: 10 мин · Папка: Проекты" in doc.paragraphs[1].text
    assert [p.text for p in doc.paragraphs if p.style.name == "Heading 2"] == ["1. Кратко", "5. Задачи"]
    assert len(doc.tables[0].rows) == 5 and len(doc.tables[0].columns) == 4
    # постоянный адрес сервиса не задан — метки времени остаются текстом, адрес запроса в файл не попадает
    assert not [r for r in doc.part.rels.values() if r.reltype.endswith("/hyperlink")]

    pdf = ivanov.get("/api/export", params={**query, "format": "pdf"})
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"].endswith("%D0%9F%D0%BB%D0%B0%D0%BD%D1%91%D1%80%D0%BA%D0%B0.pdf")
    read = PdfReader(io.BytesIO(pdf.content))
    assert read.metadata.title == "Планёрка" and "Согласовать смету" in read.pages[0].extract_text()

    # ключом доступа — те же файлы по адресам для ИИ-клиентов
    for kind, mime in (("docx", word.headers["content-type"]), ("pdf", "application/pdf")):
        got = robot.get(f"/api/v1/record.{kind}", params=query)
        assert got.status_code == 200 and got.headers["content-type"] == mime and len(got.content) > 1000, kind
        assert got.headers["content-disposition"].endswith(f".{kind}")
    assert robot.get("/api/v1/record.pdf", params=query).content[:5] == b"%PDF-"

    assert ivanov.get("/api/export", params={**query, "format": "odt"}).status_code == 400
    assert ivanov.get("/api/export", params=query).status_code == 422
    # чужая запись неотличима от несуществующей; у записи без протокола выгружать нечего
    web.service.ledger.put({"file": "Проекты/пусто.mp4", "status": "no_audio", "stem": "пусто"})
    for file in ("Снабжение/итоги.mp4", f"{HOME}/petrov/личное.mp4", "Проекты/нет.mp4", "Проекты/пусто.mp4"):
        for path, extra in (("/api/export", {"format": "docx"}), ("/api/export", {"format": "pdf"}),
                            ("/api/v1/record.docx", {}), ("/api/v1/record.pdf", {})):
            assert robot.get(path, params={"file": file, **extra}).status_code == 404, (path, file)
    assert petrov.get("/api/export", params={**query, "format": "pdf"}).status_code == 404
    assert site[1].get("/api/export", params={**query, "format": "pdf"}).status_code == 401
    # файлы собираются в памяти: в выходной папке ничего не появилось
    assert before == sorted(str(p) for p in web.cfg.outbox.rglob("*") if "_state" not in p.parts and "_logs" not in p.parts)


def test_exported_file_links_to_the_service_when_its_address_is_set(tmp_path):
    import docx
    with running(tmp_path, extra={"PUBLIC_URL": "https://podyachiy.example"}) as site:
        web, admin, ivanov, petrov = filled(site)
        word = ivanov.get("/api/export", params={"file": "Проекты/вкс.mp4", "format": "docx"})
        links = [r.target_ref for r in docx.Document(io.BytesIO(word.content)).part.rels.values()
                 if r.reltype.endswith("/hyperlink")]
        assert links == ["https://podyachiy.example/?path=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B"
                         "&open=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B%2F%D0%B2%D0%BA%D1%81.mp4&t=605"]


def test_search_finds_only_in_open_folders_and_gives_excerpts(site):
    web, admin, ivanov, petrov = filled(site)
    robot, _ = keyed(web, ivanov)
    found = robot.get("/api/v1/search", params={"q": "бюджет склада"}).json()
    assert [f["file"] for f in found["found"]] == ["Проекты/вкс.mp4", "Проекты/Сметы/Архив/смета.mp4"]
    first = found["found"][0]
    assert first["title"] == "Планёрка" and first["date"] == "2026-10-09" and first["where"] == ["protocol", "transcript"]
    # выдержка — чистый текст: от разметки протокола остались слова, «**» — только вокруг найденного
    assert first["excerpts"][0].startswith("Протокол совещания 1. Кратко Обсудили **бюджет** склада. 5. Задачи "
                                           "Задача · Срок · В записи · Статус Иванов Иван Согласовать смету · ")
    assert first["excerpts"][0].count("**") == 2 and "#" not in first["excerpts"][0] and "|" not in first["excerpts"][0]
    assert first["excerpts"][1] == "[00:00:01] S1: Про **бюджет** склада и дебиторку."
    assert found["scanned"] == 2 and found["more"] == 0
    narrowed = robot.get("/api/v1/search", params={"q": "бюджет", "dir": "Проекты/Сметы"}).json()
    assert [f["file"] for f in narrowed["found"]] == ["Проекты/Сметы/Архив/смета.mp4"]
    assert robot.get("/api/v1/search", params={"q": "бюджет", "dir": "Снабжение"}).status_code == 404      # закрыта
    assert len(admin.get("/api/v1/search", params={"q": "бюджет"}).json()["found"]) == 4
    assert [f["file"] for f in petrov.get("/api/v1/search", params={"q": "бюджет"}).json()["found"]] == [f"{HOME}/petrov/личное.mp4"]
    assert robot.get("/api/v1/search", params={"q": "я"}).status_code == 400
    assert robot.get("/api/v1/search", params={"q": "нетакогослова"}).json()["found"] == []


def test_tasks_are_collected_by_folder_person_and_date(site):
    web, admin, ivanov, petrov = filled(site)
    robot, _ = keyed(web, ivanov)
    everything = robot.get("/api/v1/tasks").json()
    assert everything["records"] == 2 and len(everything["tasks"]) == 4
    assert everything["tasks"][0] == {
        "who": "Иванов Иван", "absent": False, "task": "Согласовать смету", "due_text": "до пятницы", "status": None,
        "due": None, "due_by": None, "due_at": None, "overdue": False,
        "at": "00:10:05", "at_sec": 605, "open": True, "due_date": "2026-10-16", "file": "Проекты/вкс.mp4",
        "key": marks.key("Иванов Иван", "Согласовать смету"), "done": False, "done_by": None, "done_at": None,
        "comment": None, "unsigned": False, "can_mark": False,
        "dir": "Проекты", "title": "Планёрка", "date": "2026-10-09",
        "link": "http://testserver/?path=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B&open="
                "%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B%2F%D0%B2%D0%BA%D1%81.mp4&t=605"}
    assert everything["people"] == ["Иванов Иван"]
    mine = robot.get("/api/v1/tasks", params={"who": "иванов"}).json()["tasks"]
    assert [(t["file"], t["task"]) for t in mine] == [("Проекты/вкс.mp4", "Согласовать смету"),
                                                      ("Проекты/Сметы/Архив/смета.mp4", "Согласовать смету")]
    assert [t["file"] for t in robot.get("/api/v1/tasks", params={"who": "Иванов", "since": "2026-10-01"}).json()["tasks"]] \
        == ["Проекты/вкс.mp4"]
    assert {t["file"] for t in robot.get("/api/v1/tasks", params={"dir": "Проекты/Сметы"}).json()["tasks"]} \
        == {"Проекты/Сметы/Архив/смета.mp4"}
    # чужие папки не дают ни поручений, ни счёта записей
    assert robot.get("/api/v1/tasks", params={"who": "Петров"}).json() == {"tasks": [], "records": 2, "people": ["Иванов Иван"],
                                                                            "total": 0, "open": 0}
    assert robot.get("/api/v1/tasks", params={"dir": "Снабжение"}).status_code == 404
    assert robot.get("/api/v1/tasks", params={"dir": "Проекты/Сметы", "since": "2027-01-01"}).json() == {
        "tasks": [], "records": 0, "people": [], "total": 0, "open": 0}
    assert petrov.get("/api/v1/tasks").json()["tasks"][0]["who"] == "Петров Пётр"
    assert admin.get("/api/v1/tasks").json()["records"] == 4
    assert robot.get("/api/v1/tasks", params={"since": "вчера"}).status_code == 400


def set_protocol(web, rel, text):
    record = web.service.ledger.get(rel)
    folder, _, name = rel.rpartition("/")
    path = web.cfg.outbox / folder / record["stem"] / "summary" / f"{record['stem']}.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_tasks_registry_filters_sorts_and_exports(site):
    """Отборы страницы «Поручения» и /api/v1/tasks — одни и те же: папка, человек, период, срок, слова."""
    web, admin, ivanov, petrov = filled(site)
    set_protocol(web, "Проекты/Сметы/Архив/смета.mp4", TASKS_MD.replace("Согласовать смету | до пятницы", "Сдать отчёт | 05.09")
                 .replace("| [00:10:05] | — |", "| [00:10:05] | выполнено |").replace("Иванов Иван", "Сидорова Анна"))
    tasks = lambda client=ivanov, **params: client.get("/api/v1/tasks", params=params).json()["tasks"]
    names = lambda found: [(t["file"].rpartition("/")[2], t["task"]) for t in found]

    assert names(tasks()) == [("вкс.mp4", "Согласовать смету"), ("вкс.mp4", "Проверить остатки"),
                              ("смета.mp4", "Сдать отчёт"), ("смета.mp4", "Проверить остатки")]
    assert ivanov.get("/api/v1/tasks").json()["people"] == ["Иванов Иван", "Сидорова Анна"]
    assert names(tasks(sort="due")) [:2] == [("смета.mp4", "Сдать отчёт"), ("вкс.mp4", "Согласовать смету")]
    assert names(tasks(due="true")) == [("вкс.mp4", "Согласовать смету"), ("смета.mp4", "Сдать отчёт")]
    assert names(tasks(open="true", due="true")) == [("вкс.mp4", "Согласовать смету")]
    assert names(tasks(q="остатки", dir="Проекты/Сметы")) == [("смета.mp4", "Проверить остатки")]
    assert names(tasks(q="смета по складу отчёт")) == [("смета.mp4", "Сдать отчёт")]        # слова из названия совещания
    assert names(tasks(since="2026-09-01", until="2026-09-30")) == [("смета.mp4", "Сдать отчёт"), ("смета.mp4", "Проверить остатки")]
    assert names(tasks(until="2026-08-31")) == [] and names(tasks(who="сидорова")) == [("смета.mp4", "Сдать отчёт")]
    assert ivanov.get("/api/v1/tasks", params={"sort": "name"}).status_code == 400
    assert ivanov.get("/api/v1/tasks", params={"until": "завтра"}).status_code == 400
    # чужие поручения не видны ни в строках, ни в подсказке имён
    assert all(t["dir"].startswith("Проекты") for t in tasks())
    assert names(tasks(q="остатки", client=petrov)) == [("личное.mp4", "Проверить остатки")]
    assert petrov.get("/api/v1/tasks").json()["people"] == ["Петров Пётр"]
    assert len(tasks(client=admin)) == 8
    assert site[1].get("/api/v1/tasks.csv").status_code == 401

    sheet = ivanov.get("/api/v1/tasks.csv", params={"due": "true", "sort": "due"})
    assert sheet.status_code == 200 and sheet.headers["content-type"].startswith("text/csv")
    assert 'filename="tasks.csv"' in sheet.headers["content-disposition"]
    assert sheet.content.startswith(b"\xef\xbb\xbf")                          # метка порядка байтов — для Excel
    lines = sheet.text.lstrip("\ufeff").splitlines()
    assert lines[0] == ("Ответственный;Поручение;Срок (дата);Срок в протоколе;Срок по словам протокола;Просрочено;Статус;"
                        "Совещание;Дата совещания;Папка;Запись;"
                        "Время в записи;Ссылка;Выполнено;Кто отметил;Когда отмечено;Комментарий;Кто задал срок")
    assert lines[1].split(";")[:12] == ["Сидорова Анна", "Сдать отчёт", "", "05.09", "2026-09-05", "", "выполнено", "Смета по складу",
                                        "2026-09-01", "Проекты/Сметы/Архив", "смета.mp4", "00:10:05"]
    assert lines[1].split(";")[12].startswith("http://testserver/?path=") and lines[1].endswith("&t=605;;;;;")
    assert len(lines) == 3 and lines[2].startswith("Иванов Иван;Согласовать смету;;до пятницы;2026-10-16;;;Планёрка;2026-10-09;Проекты;")
    assert "Петров" not in ivanov.get("/api/v1/tasks.csv").text


def test_tasks_registry_reads_files_once_and_stays_fresh_after_changes(site):
    """Разбор протоколов помнится между запросами, но пересборка, переименование, перенос
    и удаление записи или папки видны сразу."""
    web, admin, ivanov, _ = filled(site)
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "manage": "tree"})
    ivanov = enter(web, "ivanov")
    files = lambda client=ivanov: sorted({t["file"] for t in client.get("/api/v1/tasks").json()["tasks"]})
    assert files(admin) and web.task_cache.reads == 4
    assert files() == ["Проекты/Сметы/Архив/смета.mp4", "Проекты/вкс.mp4"]
    ivanov.get("/api/v1/tasks.csv")
    assert web.task_cache.reads == 4                                    # ни один файл заново не читали

    # протокол пересобрали — файл записан заново
    path = set_protocol(web, "Проекты/вкс.mp4", TASKS_MD.replace("Согласовать смету", "Подписать договор") + " \n")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    fresh = ivanov.get("/api/v1/tasks", params={"dir": "Проекты", "q": "договор"}).json()["tasks"]
    assert [t["task"] for t in fresh] == ["Подписать договор"] and web.task_cache.reads == 5
    assert ivanov.get("/api/v1/tasks", params={"q": "Согласовать", "since": "2026-10-01"}).json()["tasks"] == []

    renamed = ivanov.post("/api/record/rename", json={"file": "Проекты/вкс.mp4", "name": "Итоги"}).json()["file"]
    assert files() == ["Проекты/Итоги.mp4", "Проекты/Сметы/Архив/смета.mp4"]
    moved = ivanov.post("/api/record/move", json={"file": renamed, "dir": "Проекты/Сметы"}).json()["file"]
    assert files() == ["Проекты/Сметы/Архив/смета.mp4", "Проекты/Сметы/Итоги.mp4"]
    assert [t["task"] for t in ivanov.get("/api/v1/tasks", params={"dir": "Проекты/Сметы", "who": "иванов",
                                                                   "since": "2026-10-01"}).json()["tasks"]] == ["Подписать договор"]
    assert ivanov.post("/api/record/delete", json={"file": moved}).status_code == 200
    assert files() == ["Проекты/Сметы/Архив/смета.mp4"]

    assert admin.post("/api/admin/folders/rename", json={"dir": "Проекты/Сметы", "name": "Расчёты"}).status_code == 200
    assert files() == ["Проекты/Расчёты/Архив/смета.mp4"]
    assert admin.post("/api/admin/folders/delete", json={"dir": "Проекты/Расчёты"}).status_code == 200
    assert files() == [] and "Снабжение/итоги.mp4" in files(admin)
    # в памяти остались только протоколы записей, которые есть в журнале обработки
    assert len(web.task_cache) == len(web.service.ledger.records) == 2


def test_block_of_previous_tasks_does_not_double_the_registry(site):
    """Раздел «Поручения прошлого совещания» в протоколе — не поручения этой записи."""
    web, admin, ivanov, _ = filled(site)
    from app import external, registry
    block = registry.block(web.service.ledger.get("Проекты/Сметы/Архив/смета.mp4"), marks.merged(external.tasks(TASKS_MD), {}))
    assert block.count("\n- **") == 2
    set_protocol(web, "Проекты/вкс.mp4", TASKS_MD + "\n" + block)
    assert len(ivanov.get("/api/v1/tasks").json()["tasks"]) == 4          # по два на запись, как и было
    record = ivanov.get("/api/v1/record", params={"file": "Проекты/вкс.mp4"}).json()
    assert len(record["tasks"]) == 2 and "## Поручения прошлого совещания" in record["protocol"]
    # ссылка из раздела и из реестра открывает ту запись на главной — с перемоткой
    assert "&t=605)" in block and "t = parseInt(start.get(\"t\")" in ivanov.get("/").text


def test_records_listing_carries_the_new_fields(site):
    web, admin, ivanov, _ = filled(site)
    listed = ivanov.get("/api/v1/records", params={"dir": "Проекты"}).json()["records"]
    assert [(r["file"], r["state"], r["date"]) for r in listed] == [
        ("Проекты/Сметы/Архив/смета.mp4", "ready", "2026-09-01"), ("Проекты/вкс.mp4", "ready", "2026-10-09")]
    assert listed[0]["links"]["html"].startswith("http://testserver/api/v1/record.html?file=")
    assert ivanov.get("/api/v1/records", params={"since": "2026-10-01T00:00:00+03:00"}).json()["records"][0]["file"] == "Проекты/вкс.mp4"


def test_folders_lists_where_the_person_may_upload(site, monkeypatch):
    web, admin, ivanov, petrov = filled(site)
    answer = ivanov.get("/api/v1/folders").json()
    assert answer["folders"] == [f"{HOME}/ivanov", "Проекты", "Проекты/Сметы", "Проекты/Сметы/Архив"]
    assert answer["upload"]["enabled"] and answer["upload"]["max_bytes"] == 4 * 2 ** 30 and "mp4" in answer["upload"]["extensions"]
    assert petrov.get("/api/v1/folders").json()["folders"] == [f"{HOME}/petrov"]
    assert "Снабжение" in admin.get("/api/v1/folders").json()["folders"] and "" not in admin.get("/api/v1/folders").json()["folders"]
    monkeypatch.setattr(web, "upload_ready", False)
    assert ivanov.get("/api/v1/folders").json()["folders"] == [] and not ivanov.get("/api/v1/folders").json()["upload"]["enabled"]


def test_streamed_upload_needs_a_folder_and_follows_the_rights(site):
    web, admin, ivanov, petrov = filled(site)
    robot, _ = keyed(web, ivanov, "Codex")
    body = b"0123456789" * 30000                                       # 300 КБ: тело идёт не одним куском
    done = robot.put("/api/v1/upload", params={"dir": "Проекты/Сметы", "name": "новая запись.mp3"}, content=body)
    assert done.status_code == 200, done.text
    assert done.json()["file"] == "Проекты/Сметы/новая запись.mp3" and done.json()["size"] == len(body)
    assert done.json()["state"] == "uploaded"
    assert (web.cfg.inbox / "Проекты/Сметы/новая запись.mp3").read_bytes() == body
    assert robot.get(done.json()["status"]).json()["state"] == "uploaded"          # адрес опроса из ответа рабочий
    assert not list((web.cfg.inbox / upload.PARTS_DIR).glob("*.part"))

    # без папки — отказ со списком папок, куда можно
    for params in ({"name": "запись.mp3"}, {"dir": "", "name": "запись.mp3"}, {"dir": " / ", "name": "запись.mp3"},
                   {"dir": "Проекты"}, {}):
        refused = robot.put("/api/v1/upload", params=params, content=b"123")
        assert refused.status_code == 400, params
        assert "Проекты/Сметы" in refused.json()["detail"] and f"{HOME}/ivanov" in refused.json()["detail"]
        assert "Снабжение" not in refused.json()["detail"]
    # в чужую — 403; не запись, плохое имя, повтор, пустой файл — свои отказы
    for folder in ("Снабжение", f"{HOME}/petrov", HOME):
        assert robot.put("/api/v1/upload", params={"dir": folder, "name": "запись.mp3"}, content=b"123").status_code == 403, folder
    for name, code in (("вирус.exe", 400), ("../x.mp3", 400), (".скрытая.mp3", 400), ("новая запись.mp3", 409)):
        answer = robot.put("/api/v1/upload", params={"dir": "Проекты/Сметы", "name": name}, content=b"123")
        assert answer.status_code == code, name
    assert robot.put("/api/v1/upload", params={"dir": "Проекты", "name": "пусто.mp3"}, content=b"").status_code == 400
    assert site[1].put("/api/v1/upload", params={"dir": "Проекты", "name": "без входа.mp3"}, content=b"123").status_code == 401
    files = sorted(p.name for p in web.cfg.inbox.rglob("*.mp3"))
    assert files == ["новая запись.mp3"] and not list((web.cfg.inbox / upload.PARTS_DIR).glob("*.part"))

    # в журнале — кто, каким ключом; сканер берёт запись на заметку, а недогрузку не видит
    event, = [e for e in web.trail.read() if e["kind"] == "upload"]
    assert (event["by"], event["file"], event["size"], event["via"], event["key"]) == (
        "ivanov", "Проекты/Сметы/новая запись.mp3", len(body), "key", "Codex")
    web.service.scan_once()
    assert list(web.service.pending) == ["Проекты/Сметы/новая запись.mp3"]
    # с логином и паролем загрузка та же, но без пометки ключа
    assert ivanov.put("/api/v1/upload", params={"dir": f"{HOME}/ivanov", "name": "сам.mp3"}, content=b"123").status_code == 200
    assert "via" not in [e for e in web.trail.read() if e["kind"] == "upload"][-1]


def test_streamed_upload_is_cut_at_the_size_limit(site, monkeypatch):
    web, admin, ivanov, _ = staffed(site)
    home = f"{HOME}/ivanov"
    monkeypatch.setattr(web, "cfg", dataclasses.replace(web.cfg, upload_max_bytes=1000))
    big = ivanov.put("/api/v1/upload", params={"dir": home, "name": "длинная.mp3"}, content=b"x" * 1001)
    assert big.status_code == 413 and "предела" in big.json()["detail"]

    def pieces():           # клиент размер не назвал — предел ловится по ходу приёма
        for _ in range(3):
            yield b"x" * 400
    assert ivanov.put("/api/v1/upload", params={"dir": home, "name": "поток.mp3"}, content=pieces()).status_code == 413
    assert ivanov.put("/api/v1/upload", params={"dir": home, "name": "в меру.mp3"}, content=b"x" * 1000).status_code == 200
    assert sorted(p.name for p in (web.cfg.inbox / home).iterdir()) == ["в меру.mp3"]
    assert not list((web.cfg.inbox / upload.PARTS_DIR).glob("*.part"))


def test_api_description_is_markdown_for_the_model(site):
    web, client = site
    assert client.get("/api/v1").status_code == 401
    about = client.get("/api/v1", auth=("admin", ADMIN_PASSWORD))
    assert about.status_code == 200 and about.headers["content-type"].startswith("text/markdown")
    for path in ("/api/v1/tree", "/api/v1/record", "/api/v1/record.html", "/api/v1/record.docx", "/api/v1/record.pdf",
                 "/api/v1/search", "/api/v1/tasks",
                 "/api/v1/upload", "/api/v1/status", "/api/v1/folders", "/api/v1/whoami", "/api/v1/records",
                 "/api/v1/costs", "/api/v1/rename", "/api/v1/move", "/api/v1/dictionary"):
        assert path in about.text, path
    # описание не отстаёт от кода: в нём названы все адреса /api/v1
    described = set(re.findall(r"^### (?:GET|PUT|POST) (/api/v1[\w./]*)", about.text, flags=re.M))
    assert described == {p for _, p in api_routes(web) if p.startswith("/api/v1")}
    assert "http://testserver" in about.text and "{{" not in about.text


# ---------- навык для ИИ-клиентов ----------

def front_matter(text):
    """Поля заголовка SKILL.md — как их прочтёт клиент (простые строки и свёрнутый текст «>-»)."""
    assert text.startswith("---\n")
    head = text[4:text.index("\n---\n", 4)]
    fields, key = {}, None
    for line in head.split("\n"):
        if line[:1] not in (" ", ""):
            key, _, value = line.partition(":")
            fields[key] = "" if value.strip() in (">-", "") else value.strip()
        elif key:
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields


def test_skill_is_served_without_login_with_the_address_of_this_install(tmp_path):
    with running(tmp_path, extra={"PUBLIC_URL": "http://192.0.2.10:8082/"}) as (web, client):
        install = client.get("/skill")
        assert install.status_code == 200 and install.headers["content-type"].startswith("text/plain")
        assert install.headers["cache-control"] == "no-cache"
        for must in ("http://192.0.2.10:8082/skill/SKILL.md", "~/.claude/skills/podyachiy/", "~/.agents/skills/podyachiy/",
                     ".podyachiy", "url=http://192.0.2.10:8082", "chmod 600", "http://192.0.2.10:8082/#keys",
                     "не проси человека вставить пароль или ключ в чат", "network_access = true", "sandbox.json",
                     '"allow": ["192.0.2.10"]', "curl.exe", "whoami"):
            assert must in install.text, must

        skill = client.get("/skill/SKILL.md")
        assert skill.status_code == 200 and skill.headers["content-type"].startswith("text/markdown")
        fields = front_matter(skill.text)
        assert fields["name"] == "podyachiy" and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fields["name"])
        assert set(fields) == {"name", "description", "compatibility", "metadata"}      # лишние поля клиенты отвергают
        assert 50 < len(fields["description"]) <= 600 and len(fields["compatibility"]) <= 500     # предел клиентов — 1024
        for word in ("Подьячий", "Podyachiy",
                     "протокол", "поручения", "расшифровк", "загрузить запись", "переименовать", "перенести", "meeting", "action items", "upload"):
            assert word in fields["description"], word
        body = skill.text
        assert len(body.split("\n")) < 500
        for must in ("http://192.0.2.10:8082", "Authorization:", "Bearer ", "Basic ", "curl.exe", "/api/v1/tree", "/api/v1/record",
                     "/api/v1/search", "/api/v1/tasks", "/api/v1/upload", "/api/v1/status", "/api/v1/folders",
                     "transcript=true", "EscapeDataString", "не выводи"):
            assert must in body, must
        for text in (install.text, body):
            assert "{{" not in text and "testserver" not in text and "\r" not in text

        packed = client.get("/skill/podyachiy.zip")
        assert packed.status_code == 200 and packed.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(packed.content)) as z:
            assert z.namelist() == ["podyachiy/SKILL.md"] and z.testzip() is None
            assert z.read("podyachiy/SKILL.md").decode("utf-8") == body
        assert client.get("/api/me").json()["url"] == "http://192.0.2.10:8082"


def test_skill_takes_the_address_from_the_request_when_public_url_is_not_set(site):
    web, client = site
    assert "http://testserver/skill/SKILL.md" in client.get("/skill").text
    assert "url=http://testserver\n" in client.get("/skill/SKILL.md").text
    assert client.get("/api/me").json()["url"] == "http://testserver"
    assert "http://podyachiy.lan:8082/skill" in client.get("/skill/SKILL.md", headers={"Host": "podyachiy.lan:8082"}).text


def test_every_request_of_the_skill_is_a_real_address(site):
    """Навык не должен звать адреса, которых нет: все /api/v1/… из него есть в приложении."""
    web, client = site
    named = set(re.findall(r"/api/v1(?:/[a-z.]+)?", client.get("/skill/SKILL.md").text))
    assert len(named) >= 9 and named <= {p for _, p in api_routes(web)}


def test_personal_mode_serves_the_api_and_the_skill_without_keys(tmp_path):
    """В личном режиме входа нет: API отвечает без ключа, а навык и инструкция ключ не требуют —
    в файле ~/.podyachiy только адрес."""
    with running(tmp_path, mode="personal", extra={"PUBLIC_URL": "http://localhost:8098"}) as (web, client):
        who = client.get("/api/v1/whoami").json()
        assert (who["login"], who["via"], who["manage"]) == ("owner", "login", True)
        assert client.get("/api/v1/tree").json()["path"] == ""
        assert client.get("/api/v1").status_code == 200 and "без ключа и пароля" in client.get("/api/v1").text
        for method, path in (("GET", "/api/keys"), ("POST", "/api/keys"), ("POST", "/api/keys/revoke")):
            off = client.request(method, path, json={"name": "ключ", "id": "нет"})
            assert off.status_code == 404 and off.json()["detail"] == web.NO_KEYS_HERE, path
        assert key_file(web) == []

        install, skill = client.get("/skill").text, client.get("/skill/SKILL.md").text
        assert "printf '# Podyachiy\\nurl=http://localhost:8098\\n' >" in install
        assert '@("# Podyachiy", "url=http://localhost:8098"))' in install
        for text in (install, skill):
            assert "{{" not in text
            for word in ("key=stg_", "key=\n", "login=\n", "password=\n", '"key="', "#keys", "Ключи доступа",
                         "Выпустить ключ"):
                assert word not in text, word
        assert "ключ не нужен" in install and "ключ и пароль не нужны" in skill
        assert "Управление сервисом" not in skill and "## Управление" not in client.get("/api/v1").text
        assert front_matter(skill)["name"] == "podyachiy"


def test_skill_has_one_version_and_tells_what_is_new(site):
    """Версия навыка записана в одном месте — верхнем разделе CHANGELOG.md; её отдают SKILL.md,
    whoami и список изменений, по которым клиент обновляет себя сам."""
    web, client = site
    changes = (web.SKILL / "CHANGELOG.md").read_text(encoding="utf-8")
    version = int(re.search(r"^## (\d+)", changes, flags=re.M).group(1))
    assert version >= 4 and web._skill_version() == version
    assert [v for v, _ in web._skill_changes()] == list(range(version, 0, -1))      # по убыванию, без пропусков

    skill = client.get("/skill/SKILL.md").text
    assert f'version: "{version}"' in skill and f"Версия этого навыка — **{version}**" in skill
    assert f"own=\"\"; n={version}\n" in skill and f'$own = ""; $n = {version};' in skill
    for must in ("skill_version", "/CHANGELOG.md?since=$n", ".podyachiy", "$HOME/.claude/skills/podyachiy",
                 "$HOME/.agents/skills/podyachiy", "SKILL.new", "не трогай"):
        assert must in skill, must
    who = client.get("/api/v1/whoami", auth=("admin", ADMIN_PASSWORD)).json()
    assert who["skill_version"] == version and who["skill_url"] == "http://testserver/skill/SKILL.md"
    assert who["skill_changes"] == "http://testserver/skill/CHANGELOG.md"
    assert f"версия {version}" in client.get("/skill").text

    full = client.get("/skill/CHANGELOG.md")
    assert full.status_code == 200 and full.headers["content-type"].startswith("text/markdown")
    assert f"## {version}\n" in full.text and "## 1\n" in full.text
    fresh = client.get("/skill/CHANGELOG.md", params={"since": version - 1}).text
    assert f"## {version}\n" in fresh and f"## {version - 1}\n" not in fresh
    assert "Новее ничего нет" in client.get("/skill/CHANGELOG.md", params={"since": version}).text
    # папки, которые навык обновляет, — те же, куда его ставит инструкция
    for folder in ("~/.claude/skills/podyachiy/", "~/.agents/skills/podyachiy/"):
        assert folder in client.get("/skill").text


def test_powershell_function_feeds_curl_without_bom(site):
    """Stg отдаёт curl.exe строку с учётными данными через -K -. Где PowerShell пишет в программы
    UTF-8 с меткой BOM (так настроены оболочки ИИ-агентов в Windows), curl принимал метку за
    неизвестный параметр и падал на каждом запросе. Кодировка задаётся в самой функции, до вызова."""
    _, client = site
    skill = client.get("/skill/SKILL.md").text
    stg = re.search(r"```powershell\n(function Stg.*?)```", skill, flags=re.S).group(1)
    fix = "$OutputEncoding = [Console]::InputEncoding = New-Object Text.UTF8Encoding $false"
    assert fix in stg and stg.index(fix) < stg.index("curl.exe -sS -K -")


def test_skill_tells_to_confirm_names_and_lists_the_short_commands(site):
    _, client = site
    skill = client.get("/skill/SKILL.md").text
    for must in ("Не угадывай ФИО без оснований в тексте", "только после того, как человек подтвердил",
                 "платный", "rebuild=true", "/api/v1/speakers", "/podyachiy <команда>", "$podyachiy <команда>",
                 "`поручения просроченные`", "`говорящие <запись>`", "`загрузить <файл> [в <папку>]`",
                 '"manage": true'):
        assert must in skill, must


# ---------- говорящие через /api/v1 ----------

def voiced(web, rel):
    """Запись из двух частей по три голоса: расшифровка с метками и разбор с догадками об именах."""
    recorded(web, rel)
    folder, _, name = rel.rpartition("/")
    stem = name.rsplit(".", 1)[0]
    out = web.cfg.outbox / folder / stem / "transcript"
    out.mkdir(parents=True)
    long = "это довольно длинная реплика про бюджет склада и сроки поставки"
    segments = [{"start": 5, "end": 9, "speaker": "S1", "text": "Коллеги, добрый день, начинаем планёрку, Иван Петрович, вам слово."},
                {"start": 10, "end": 20, "speaker": "S2", "text": "Спасибо. По складу: " + long},
                {"start": 21, "end": 22, "speaker": "S1", "text": "Да."}]
    segments += [{"start": 30 + 10 * n, "end": 35 + 10 * n, "speaker": "S2", "text": f"Пункт {n}: {long}"} for n in range(9)]
    segments += [{"start": 5400 + 3, "end": 5400 + 9, "speaker": "S2", "text": "Во второй части метка S2 — уже другой человек, не Иван Петрович."},
                 {"start": 5400 + 10, "end": 5400 + 12, "speaker": "S3", "text": "Угу."}]
    (out / f"{stem}.json").write_text(json.dumps({"segments": segments, "cuts": [5400]}), encoding="utf-8")
    (out / f"{stem}.triage.json").write_text(json.dumps({
        "type": "meeting", "templates": sorted(web.templates.TEMPLATES),
        "speakers": [{"label": "S1", "name": "Анна", "role": "ведущая"}, {"label": "S2", "name": None, "role": ""}]}),
        encoding="utf-8")
    return rel


def test_speakers_are_read_with_samples_and_signed_through_the_api(site):
    web, admin, ivanov, petrov = staffed(site)
    admin.post("/api/folder", json={"dir": "Проекты"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "manage": "only"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "petrov", "read": "only"})
    file = voiced(web, "Проекты/планёрка.mp4")
    robot, _ = keyed(web, ivanov, "Cursor")
    reader, _ = keyed(web, petrov, "Codex")

    seen = robot.get("/api/v1/speakers", params={"file": file}).json()
    assert seen["file"] == file and seen["can_edit"] is True and seen["rebuilding"] is False
    first, second = seen["parts"]
    assert (first["part"], first["start_sec"], first["end_sec"]) == (1, 0, 5400)
    assert (second["part"], second["start_sec"], second["end_sec"]) == (2, 5400, None)
    s1, s2 = first["speakers"]
    assert (s1["label"], s1["name"], s1["guess"], s1["role"], s1["lines"]) == ("S1", "", "Анна", "ведущая", 2)
    assert (s2["label"], s2["name"], s2["guess"], s2["lines"]) == ("S2", "", "", 10) and s2["share"] > s1["share"]
    assert [x["label"] for x in second["speakers"]] == ["S2", "S3"]
    # реплик — не больше пяти, с временем и ключом строки; содержательные, от начала до конца речи
    assert len(s2["samples"]) == 5 and s2["samples"][0] == {
        "at": "00:00:10", "at_sec": 10, "line": "10.00", "text": segments_text(web, file)[1]}
    assert s2["samples"][-1]["at_sec"] == 110 and all("Пункт" in x["text"] for x in s2["samples"][1:])
    assert [x["at"] for x in s1["samples"]] == ["00:00:05", "00:00:21"]          # у кого реплик мало — все, что есть
    assert second["speakers"][0]["samples"][0]["at"] == "01:30:03"
    assert "Иван Петрович" in s1["samples"][0]["text"]

    # подпись метки в части, одной реплики, снятие подписи
    signed = robot.post("/api/v1/speakers", params={"file": file, "part": 1, "label": "S2", "name": " Иванов  Иван Петрович "})
    assert signed.status_code == 200 and signed.json()["ok"]
    assert signed.json()["parts"][0]["speakers"][1]["name"] == "Иванов Иван Петрович"
    assert signed.json()["parts"][1]["speakers"][0]["name"] == ""                # S2 второй части — другой человек
    assert signed.json()["names_newer"] in (True, False) and web.service.queue == []   # без rebuild протокол не трогается
    assert robot.post("/api/v1/speakers", params={"file": file, "line": "21.00", "name": "Петрова Анна"}).status_code == 200
    saved = json.loads((web.cfg.outbox / "Проекты/планёрка/transcript/планёрка.names.json").read_text(encoding="utf-8"))
    assert saved == {"parts": {"1": {"S2": "Иванов Иван Петрович"}}, "lines": {"21.00": "Петрова Анна"}}
    assert "[Иванов Иван Петрович]" in robot.get("/api/v1/record", params={"file": file, "transcript": True}).json()["transcript"]
    # страница видит те же подписи — логика одна
    assert ivanov.get("/api/result", params={"file": file, "kind": "summary"}).json()["parts"][0]["speakers"][1]["manual"] \
        == "Иванов Иван Петрович"
    cleared = robot.post("/api/v1/speakers", params={"file": file, "line": "21.00", "name": ""})
    assert cleared.status_code == 200 and json.loads(
        (web.cfg.outbox / "Проекты/планёрка/transcript/планёрка.names.json").read_text(encoding="utf-8"))["lines"] == {}

    # пересборка — отдельным запросом; сервер подтверждения человека не проверяет (это правило навыка)
    rebuilt = robot.post("/api/v1/speakers", params={"file": file, "rebuild": True})
    assert rebuilt.status_code == 200 and rebuilt.json()["rebuilding"] is True
    item, = web.service.queue
    assert (item.path, item.force, item.reason) == (file, False, "rebuild")

    # плохие запросы и права
    for params in ({"part": 1, "label": "S9", "name": "Кто-то"}, {"part": 7, "label": "S1", "name": "Кто-то"},
                   {"line": "99.99", "name": "Кто-то"}, {"label": "S1", "name": "без части"}, {"part": 1},
                   {"part": 1, "label": "S1", "line": "5.00", "name": "и то и другое"}, {}):
        assert robot.post("/api/v1/speakers", params={"file": file, **params}).status_code == 400, params
    assert reader.get("/api/v1/speakers", params={"file": file}).json()["can_edit"] is False
    refused = reader.post("/api/v1/speakers", params={"file": file, "part": 1, "label": "S1", "name": "Чужой"})
    assert refused.status_code == 403 and "владелец папки" in refused.json()["detail"]
    assert reader.post("/api/v1/speakers", params={"file": file, "rebuild": True}).status_code == 403
    stranger = enter(web, "petrov")
    admin.post("/api/access", json={"dir": "Проекты", "login": "petrov", "read": "none"})
    assert stranger.get("/api/v1/speakers", params={"file": file}).status_code == 404        # папка закрыта
    assert site[1].get("/api/v1/speakers", params={"file": file}).status_code == 401
    recorded(web, "Проекты/без голосов.mp4")
    assert robot.get("/api/v1/speakers", params={"file": "Проекты/без голосов.mp4"}).status_code == 409

    events = [e for e in web.trail.read() if e["kind"] == "names"]
    assert len(events) == 3 and {(e["by"], e["via"], e["key"], e["key_kind"]) for e in events} == {
        ("ivanov", "key", "Cursor", "plain")}
    # и страница ключу теперь открыта тем же правом — отдельного запрета на подписи нет
    assert robot.post("/api/admin/names", json={"file": file, "parts": {"1": {"S1": "Петрова Анна"}}}).status_code == 200


def segments_text(web, file):
    folder, _, name = file.rpartition("/")
    stem = name.rsplit(".", 1)[0]
    data = json.loads((web.cfg.outbox / folder / stem / "transcript" / f"{stem}.json").read_text(encoding="utf-8"))
    return [seg["text"] for seg in data["segments"]]


# ---------- отметка «выполнено» у поручения ----------

SMETA = marks.key("Иванов Иван", "Согласовать смету")
OSTATKI = marks.key(None, "Проверить остатки")


def test_owner_marks_a_task_done_and_the_reader_only_sees_it(site):
    web, admin, ivanov, petrov = filled(site)
    file = "Проекты/вкс.mp4"
    body = {"file": file, "key": SMETA, "done": True}
    # читатель видит, но не отмечает; чужая запись неотличима от несуществующей; без входа — 401
    assert ivanov.post("/api/record/task", json=body).status_code == 403
    assert ivanov.post("/api/v1/task", params={"file": file, "key": SMETA}).status_code == 403
    assert petrov.post("/api/record/task", json=body).status_code == 404
    assert ivanov.post("/api/record/task", json={**body, "file": "Снабжение/итоги.mp4"}).status_code == 404
    assert site[1].post("/api/record/task", json=body).status_code == 401
    assert web.noted.everything() == {}
    assert all(not t["can_mark"] for t in ivanov.get("/api/v1/tasks").json()["tasks"])

    done = admin.post("/api/record/task", json={**body, "comment": "  подписано \n 10.10 "})
    assert done.status_code == 200
    task = done.json()["task"]
    assert task["done"] and not task["open"] and task["done_by"] == "admin" and task["comment"] == "подписано 10.10"
    assert task["status"] is None and task["key"] == SMETA                      # слова протокола — отдельно
    assert list(web.noted.load("Проекты/вкс.mp4")) == [SMETA] and not list(web.cfg.outbox.rglob("*.tasks.json"))
    protocol = web.cfg.outbox / "Проекты/вкс/summary/вкс.md"
    assert protocol.read_text(encoding="utf-8") == TASKS_MD                     # протокол на диске не переписан

    seen = ivanov.get("/api/v1/tasks", params={"dir": "Проекты", "since": "2026-10-01"}).json()
    assert [(t["task"], t["open"], t["done"], t["done_by"], t["comment"]) for t in seen["tasks"]] == [
        ("Согласовать смету", False, True, "admin", "подписано 10.10"), ("Проверить остатки", True, False, None, None)]
    assert (seen["total"], seen["open"]) == (2, 1)
    record = ivanov.get("/api/v1/record", params={"file": file}).json()
    assert [t["done"] for t in record["tasks"]] == [True, False] and record["can_mark"] is False
    assert admin.get("/api/v1/record", params={"file": file}).json()["can_mark"] is True
    # окно записи на главной: поручения со строкой протокола, у которой стоит флажок
    shown = ivanov.get("/api/result", params={"file": file})
    lines = shown.json()["text"].split("\n")
    assert [(lines[t["line"]].split("|")[1].strip(), t["done"]) for t in shown.json()["tasks"]] == [
        ("Согласовать смету", True), ("Проверить остатки", False)]
    assert shown.json()["can_mark"] is False and admin.get("/api/result", params={"file": file}).json()["can_mark"] is True
    assert "tasks" not in ivanov.get("/api/result", params={"file": file, "kind": "transcript"}).json()

    # отбор «только незакрытые» и счёт «открыто N из M»
    only = ivanov.get("/api/v1/tasks", params={"open": "true"}).json()
    assert [(t["file"], t["task"]) for t in only["tasks"]] == [
        ("Проекты/вкс.mp4", "Проверить остатки"), ("Проекты/Сметы/Архив/смета.mp4", "Согласовать смету"),
        ("Проекты/Сметы/Архив/смета.mp4", "Проверить остатки")]                 # то же поручение другой записи открыто
    assert (only["total"], only["open"]) == (4, 3)
    mine = ivanov.get("/api/v1/tasks", params={"open": "true", "who": "иванов"}).json()
    assert (len(mine["tasks"]), mine["total"], mine["open"]) == (1, 2, 1)

    sheet = ivanov.get("/api/v1/tasks.csv", params={"dir": "Проекты", "since": "2026-10-01"}).text.splitlines()
    assert sheet[0].endswith(";Выполнено;Кто отметил;Когда отмечено;Комментарий;Кто задал срок")
    assert sheet[1].split(";")[-5:] == ["да", "admin", task["done_at"][:16].replace("T", " "), "подписано 10.10", ""]
    assert sheet[2].endswith(";;;;;")

    # выгрузка: отметка в колонке статуса
    import docx
    word = docx.Document(io.BytesIO(ivanov.get("/api/export", params={"file": file, "format": "docx"}).content))
    assert word.tables[0].rows[2].cells[3].text == "выполнено, " + marks.day(task["done_at"])
    assert "выполнено, " + marks.day(task["done_at"]) in ivanov.get("/api/v1/record.html", params={"file": file}).text

    # снятие: отметки нет, событие в журнале действий осталось
    back = admin.post("/api/record/task", json={**body, "done": False}).json()["task"]
    assert not back["done"] and back["open"] and back["done_by"] is None
    assert admin.post("/api/record/task", json={**body, "done": False}).status_code == 200      # повтор ничего не меняет
    assert ivanov.get("/api/v1/tasks", params={"open": "true"}).json()["open"] == 4
    events = [e for e in web.trail.read() if e["kind"].startswith("task_")]
    assert [(e["kind"], e["by"], e["file"], e["task"], e["owner"]) for e in events] == [
        ("task_done", "admin", file, "Согласовать смету", "Иванов Иван"),
        ("task_reopen", "admin", file, "Согласовать смету", "Иванов Иван")]
    assert "via" not in events[0]

    assert admin.post("/api/record/task", json={**body, "key": "нет такого"}).status_code == 404
    assert admin.post("/api/record/task", json={**body, "comment": "я" * 301}).status_code == 400
    assert admin.post("/api/record/task", json={**body, "comment": "я" * 300}).status_code == 200
    web.service.ledger.put({"file": "Проекты/пусто.mp4", "status": "no_audio", "stem": "пусто"})
    assert admin.post("/api/record/task", json={**body, "file": "Проекты/пусто.mp4"}).status_code == 404


def test_folder_admin_and_home_owner_mark_tasks_and_the_key_is_noted_in_the_journal(site):
    web, admin, ivanov, petrov = filled(site)
    admin.post("/api/access", json={"dir": "Проекты/Сметы", "login": "ivanov", "manage": "tree"})
    ivanov = enter(web, "ivanov")
    rows = {t["file"]: t["can_mark"] for t in ivanov.get("/api/v1/tasks").json()["tasks"]}
    assert rows == {"Проекты/вкс.mp4": False, "Проекты/Сметы/Архив/смета.mp4": True}
    robot, _ = keyed(web, ivanov, "Cursor")
    archived = {"file": "Проекты/Сметы/Архив/смета.mp4", "key": OSTATKI}
    # ключом — в рамках тех же прав, адресом /api/v1; done по умолчанию true
    assert robot.post("/api/v1/task", params={**archived, "file": "Проекты/вкс.mp4"}).status_code == 403
    marked = robot.post("/api/v1/task", params={**archived, "comment": "сверено"})
    assert marked.status_code == 200 and marked.json()["task"]["done_by"] == "ivanov"
    assert robot.post("/api/v1/task", params={"file": archived["file"]}).status_code == 422       # какое поручение — обязательно
    left = robot.get("/api/v1/tasks", params={"dir": "Проекты/Сметы", "open": "true"}).json()
    assert [t["task"] for t in left["tasks"]] == ["Согласовать смету"] and (left["total"], left["open"]) == (2, 1)
    assert robot.post("/api/v1/task", params={**archived, "done": "false"}).json()["task"]["done"] is False

    # хозяин личной папки отмечает у себя
    own = {"file": f"{HOME}/petrov/личное.mp4", "key": marks.key("Петров Пётр", "Согласовать смету"), "done": True}
    assert petrov.get("/api/v1/tasks").json()["tasks"][0]["can_mark"] is True
    assert petrov.post("/api/record/task", json=own).json()["task"]["done_by"] == "petrov"
    assert ivanov.post("/api/record/task", json=own).status_code == 404

    events = [(e["kind"], e["by"], e.get("via"), e.get("key")) for e in web.trail.read() if e["kind"].startswith("task_")]
    assert events == [("task_done", "ivanov", "key", "Cursor"), ("task_reopen", "ivanov", "key", "Cursor"),
                      ("task_done", "petrov", None, None)]


def test_marks_stay_with_the_record_and_the_registry_sees_them_at_once(site):
    """Память реестра видит отметку сразу; отметка переезжает с записью и папкой и уходит в корзину."""
    web, admin, ivanov, _ = filled(site)
    state = lambda: {(t["file"], t["task"]): t["done"]
                     for t in admin.get("/api/v1/tasks", params={"dir": "Проекты"}).json()["tasks"]}
    assert not any(state().values())
    reads = web.task_cache.reads
    assert admin.post("/api/record/task", json={"file": "Проекты/вкс.mp4", "key": SMETA, "done": True}).status_code == 200
    assert state()[("Проекты/вкс.mp4", "Согласовать смету")] is True and web.task_cache.reads == reads + 1
    assert state() and web.task_cache.reads == reads + 1                    # дальше — из памяти

    renamed = admin.post("/api/record/rename", json={"file": "Проекты/вкс.mp4", "name": "Итоги"}).json()["file"]
    assert state()[(renamed, "Согласовать смету")] is True
    moved = admin.post("/api/record/move", json={"file": renamed, "dir": "Проекты/Сметы"}).json()["file"]
    assert state()[(moved, "Согласовать смету")] is True and list(web.noted.everything()) == [moved]
    assert admin.post("/api/admin/folders/rename", json={"dir": "Проекты/Сметы", "name": "Расчёты"}).status_code == 200
    assert state() == {("Проекты/Расчёты/Итоги.mp4", "Согласовать смету"): True,
                       ("Проекты/Расчёты/Итоги.mp4", "Проверить остатки"): False,
                       ("Проекты/Расчёты/Архив/смета.mp4", "Согласовать смету"): False,
                       ("Проекты/Расчёты/Архив/смета.mp4", "Проверить остатки"): False}
    # протокол пересобрали с другой формулировкой — отметка осталась при своём поручении
    path = set_protocol(web, "Проекты/Расчёты/Итоги.mp4", TASKS_MD.replace("Согласовать смету", "Согласование сметы"))
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    assert state()[("Проекты/Расчёты/Итоги.mp4", "Согласование сметы")] is True

    held = web.noted.load("Проекты/Расчёты/Итоги.mp4")
    gone = admin.post("/api/record/delete", json={"file": "Проекты/Расчёты/Итоги.mp4"}).json()
    # из базы отметки ушли вместе с записью; в корзине они записаны — на случай возврата руками
    note = json.loads((web.cfg.outbox / gone["trash"] / "удалено.json").read_text(encoding="utf-8"))
    assert held and note["marks"] == {"Проекты/Расчёты/Итоги.mp4": held} and web.noted.everything() == {}


def test_personal_mode_marks_tasks_without_login(tmp_path):
    with running(tmp_path, mode="personal") as (web, client):
        recorded(web, "вкс.mp4")
        (web.cfg.outbox / "вкс/summary/вкс.md").write_text(TASKS_MD, encoding="utf-8")
        row = client.get("/api/v1/tasks").json()["tasks"][0]
        assert row["can_mark"] is True
        done = client.post("/api/record/task", json={"file": "вкс.mp4", "key": row["key"], "done": True}).json()["task"]
        assert done["done_by"] == "owner" and client.get("/api/v1/tasks", params={"open": "true"}).json()["open"] == 1


# ---------- модель распознавания и поставщики на странице администратора ----------

NOVA, SCRIBE, MAI = "deepgram/nova-3", "elevenlabs/scribe-v2", "microsoft/mai-transcribe-2"


def test_admin_picks_the_speech_model_on_the_page_and_can_return_to_env(site):
    """SPEECH_MODEL из .env действует, пока администратор не выбрал на странице; выбор работает без
    перезапуска, записан в папке состояния и в журнале действий."""
    web, admin, ivanov, _ = staffed(site)
    speech = admin.get("/api/admin/status").json()["speech"]
    assert speech["current"] == speech["env"] == NOVA and speech["chosen"] is None
    assert [m["id"] for m in speech["models"]] == [SCRIBE, NOVA, MAI]
    assert all(m["title"] and m["note"] and m["price_hour"] > 0 for m in speech["models"])

    saved = admin.post("/api/admin/speech-model", json={"model": SCRIBE}).json()
    assert saved["current"] == SCRIBE and saved["chosen"]["by"] == "admin" and saved["env"] == NOVA
    assert web.service.speech_model() == SCRIBE and web.cfg.model == NOVA       # обработчик берёт уже новую
    assert settings.Store(db.Database(web.cfg.state_dir)).speech_model()["value"] == SCRIBE     # с диска
    assert admin.get("/api/admin/status").json()["config"]["speech_model"] == SCRIBE
    event = web.trail.read()[-1]
    assert (event["kind"], event["by"], event["model"], event["was"]) == ("speech_model", "admin", SCRIBE, NOVA)

    assert admin.post("/api/admin/speech-model", json={"model": "x-ai/grok-stt-1.0"}).status_code == 400
    assert web.service.speech_model() == SCRIBE
    back = admin.post("/api/admin/speech-model", json={"model": None}).json()
    assert back["current"] == NOVA and back["chosen"] is None and web.trail.read()[-1]["source"] == "env"

    # только администратор сервиса: ни пользователь, ни ключ (даже ключ администратора) модель не меняют
    assert ivanov.post("/api/admin/speech-model", json={"model": MAI}).status_code == 403
    assert web.service.speech_model() == NOVA


def test_choice_made_on_the_page_survives_a_restart(tmp_path):
    with running(tmp_path) as (web, client):
        assert client.post("/api/admin/speech-model", json={"model": MAI},
                           auth=("admin", ADMIN_PASSWORD)).status_code == 200
    with running(tmp_path) as (web, client):
        speech = client.get("/api/admin/status", auth=("admin", ADMIN_PASSWORD)).json()["speech"]
        assert speech["current"] == MAI and speech["env"] == NOVA and web.service.speech_model() == MAI


def test_manual_run_takes_a_model_only_together_with_overwrite(site):
    web, client = site
    (web.cfg.inbox / "запись.mp4").write_bytes(b"video")
    run = lambda **body: client.post("/api/admin/run", json={"file": "запись.mp4", **body},
                                     auth=("admin", ADMIN_PASSWORD))
    refused = run(model=MAI)
    assert refused.status_code == 400 and "перезапис" in refused.json()["detail"]      # без перезаписи модель ни к чему
    assert run(model="x-ai/grok-stt-1.0", force=True).status_code == 400
    assert web.service.queue == []
    assert run(model=MAI, force=True).status_code == 200
    item = web.service.queue[0]
    assert (item.path, item.force, item.manual, item.model, item.by) == ("запись.mp4", True, True, MAI, "admin")
    web.service.queue.clear()
    assert run(force=True).status_code == 200 and web.service.queue[0].model == ""     # «как в настройках»


def test_rebuild_after_signing_names_never_asks_for_recognition(site):
    """«Пересобрать протокол» ставит запись в очередь без перезаписи и без модели — какая бы ни была выбрана."""
    web, admin, ivanov, _ = staffed(site)
    rel = f"{HOME}/ivanov/планёрка.mp4"
    recorded(web, rel, by="ivanov")
    out = web.cfg.outbox / HOME / "ivanov" / "планёрка" / "transcript"
    out.mkdir(parents=True)
    (out / "планёрка.json").write_text(json.dumps({"model": NOVA, "segments": [
        {"start": 1, "end": 2, "speaker": "S1", "text": "Добрый день."}]}), encoding="utf-8")
    admin.post("/api/admin/speech-model", json={"model": SCRIBE})
    signed = ivanov.post("/api/admin/names", json={"file": rel, "parts": {"1": {"S1": "Иванов И. И."}}, "rebuild": True})
    assert signed.status_code == 200
    item = web.service.queue[0]
    assert (item.path, item.force, item.model, item.reason) == (rel, False, "", "rebuild")


def test_status_shows_the_providers_without_their_keys(tmp_path):
    extra = {"OPENAI_BASE_URL": "http://routerai.ru/api/v1", "OPENAI_API_KEY": "sk-main-secret",
             "BACKUP_BASE_URL": "https://openrouter.ai/api/v1", "BACKUP_API_KEY": "sk-spare-secret"}
    with running(tmp_path, extra=extra) as (web, client):
        answer = client.get("/api/admin/status", auth=("admin", ADMIN_PASSWORD))
        shown = answer.json()["providers"]
        assert shown["main"] == {"title": "RouterAI", "currency": "RUB", "host": "routerai.ru", "key_present": True}
        assert shown["backup"]["title"] == "OpenRouter" and shown["backup"]["currency"] == "USD"
        assert shown["text"] is None and shown["last_switch"] is None and shown["resting_sec"] == 0
        assert "secret" not in answer.text
        assert any("основной поставщик: RouterAI, стоимость в RUB; запасной: OpenRouter" in line for line in web.log.tail_from(0)[1])


def test_status_without_a_backup_and_spending_in_two_currencies(site):
    web, client = site
    status = lambda: client.get("/api/admin/status", auth=("admin", ADMIN_PASSWORD)).json()
    assert status()["providers"]["backup"] is None and status()["totals"]["cost"] == {}
    web.service.ledger.put({"file": "а.mp4", "status": "ok", "cost": {"RUB": 12.0, "USD": 0.05},
                            "speech_model": SCRIBE, "finished_at": "2026-10-09T12:00:00+03:00",
                            "cost_models": [{"stage": "speech", "model": SCRIBE, "provider": "RouterAI",
                                             "currency": "RUB", "cost": 12.0},
                                            {"stage": "summary", "model": "deepseek", "provider": "OpenRouter",
                                             "currency": "USD", "cost": 0.05}],
                            "cost_providers": [{"provider": "RouterAI", "currency": "RUB", "cost": 12.0},
                                               {"provider": "OpenRouter", "currency": "USD", "cost": 0.05}]})
    web.trail.run(web.service.ledger.get("а.mp4"), "admin", "manual")
    body = status()
    assert body["totals"]["cost"] == {"RUB": 12.0, "USD": 0.05}
    assert body["history"][0]["cost"] == {"RUB": 12.0, "USD": 0.05} and body["history"][0]["speech_model"] == SCRIBE
    shown = client.get("/api/browse", auth=("admin", ADMIN_PASSWORD)).json()["files"][0]
    assert shown["speech_model"] == SCRIBE and "cost" not in shown          # расходы — только администратору
    report = client.get("/api/v1/costs", auth=("admin", ADMIN_PASSWORD)).json()
    assert report["currencies"] == ["RUB", "USD"] and report["total"]["cost"] == {"RUB": 12.0, "USD": 0.05}
    assert {p["provider"]: p["cost"] for p in report["providers"]} == {"RouterAI": {"RUB": 12.0},
                                                                       "OpenRouter": {"USD": 0.05}}
    sheet = csv_tables(client.get("/api/admin/report.csv", auth=("admin", ADMIN_PASSWORD)).content.decode("utf-8-sig"))
    assert [row[-3:] for row in sheet["Прогоны"][1:]] == [["RouterAI", "RUB", "12.000000"],
                                                          ["OpenRouter", "USD", "0.050000"]]


# ---------- письма о записи: «запись готова» и протокол на почту ----------

MEETING = "Проекты/вкс.mp4"


def desk(office):
    """Иванов — администратор папки «Проекты», Петров её читает и загрузил запись, у Сидорова
    почты нет, Орлова папку не видит. Запись — с протоколом, названием и длительностью."""
    web, guest, admin, post = office
    for login, name, email in (("ivanov", "Иванов Иван", "ivanov@example.test"), ("petrov", "Петров Пётр", "petrov@example.test"),
                               ("sidorov", "Сидоров", ""), ("orlova", "Орлова", "orlova@example.test")):
        saved = admin.post("/api/admin/users/save", json={"login": login, "name": name, "password": PASSWORD, "email": email})
        assert saved.status_code == 200, saved.text
    admin.post("/api/folder", json={"dir": "Проекты"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "manage": "tree"})
    for login in ("petrov", "sidorov"):
        admin.post("/api/access", json={"dir": "Проекты", "login": login, "read": "tree"})
    recorded(web, MEETING, by="petrov")
    set_protocol(web, MEETING, TASKS_MD)
    web.service.ledger.put({**web.service.ledger.get(MEETING), "title": "Планёрка: итоги/план", "audio_duration_sec": 600,
                            "record_date": "2026-10-09"})
    return web, admin, enter(web, "ivanov"), enter(web, "petrov"), post


def send(client, **body):
    return client.post("/api/record/send", json={"file": MEETING, "to": "petrov@example.test", **body})


def test_owner_sends_the_protocol_with_a_pdf_and_the_journal_keeps_the_addresses(office):
    from pypdf import PdfReader
    web, admin, ivanov, petrov, post = desk(office)
    shown = ivanov.get("/api/result", params={"file": MEETING}).json()
    assert shown["can_send"] is True
    offer = ivanov.get("/api/record/recipients", params={"file": MEETING}).json()
    # предлагаются те, кому папка открыта и у кого есть почта: без Сидорова (нет почты) и Орловой (нет доступа)
    assert offer == {"people": [{"name": "Иванов Иван", "email": "ivanov@example.test"},
                                {"name": "Петров Пётр", "email": "petrov@example.test"}],
                     "max": 20, "note_max": 1000, "domains": []}

    note = "Коллеги, <b>срочно</b>:\nсм. задачи"
    done = send(ivanov, to="petrov@example.test, Guest@Partner.test\npetrov@example.test", note=note)
    assert done.status_code == 200, done.text
    assert done.json() == {"ok": True, "to": ["petrov@example.test", "guest@partner.test"], "refused": []}
    # каждому получателю — своё письмо, в «Кому» только он; в остальном письма одинаковые
    letter, other = post.sent
    assert (letter.to, other.to) == (["petrov@example.test"], ["guest@partner.test"])
    assert (other.subject, other.text, other.attachments) == (letter.subject, letter.text, letter.attachments)
    assert letter.reply_to == other.reply_to == "ivanov@example.test"
    assert letter.subject == "протокол — Планёрка: итоги/план"
    assert "Отправитель: Иванов Иван." in letter.text and "Дата: 09.10.2026" in letter.text
    assert "Кратко:\n- Обсудили бюджет склада.\n" in letter.text and "во вложении (PDF)" in letter.text
    assert ("Запись в сервисе: http://podyachiy.test/?path=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B&open=" in letter.text
            and "нужна учётная запись в сервисе" in letter.text)
    assert "&lt;b&gt;срочно&lt;/b&gt;:<br>см. задачи" in letter.html and "<b>срочно</b>" not in letter.html
    (data, kind, name), = letter.attachments
    assert kind == "application/pdf" and name == "Планёрка итоги план.pdf"
    assert "Согласовать смету" in PdfReader(io.BytesIO(data)).pages[0].extract_text()

    event = web.trail.read()[-1]
    assert (event["kind"], event["by"], event["file"], event["format"], event["note"], event["refused"]) == (
        "protocol_sent", "ivanov", MEETING, "pdf", True, [])
    assert event["to"] == ["petrov@example.test", "guest@partner.test"]         # адреса — полностью
    assert kinds(web).count("protocol_sent") == 1                               # писем два, действие в журнале одно
    assert any("протокол отправлен на почту" in line and "guest@partner.test" in line for line in web.log.tail_from(0)[1])


def test_protocol_goes_as_docx_or_as_a_link_only(office):
    import docx
    web, admin, ivanov, petrov, post = desk(office)
    assert send(ivanov, format="docx").status_code == 200
    (data, kind, name), = post.sent[-1].attachments
    assert kind == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert name == "Планёрка итоги план.docx" and "во вложении (DOCX)" in post.sent[-1].text
    assert docx.Document(io.BytesIO(data)).paragraphs[0].text == "Планёрка: итоги/план"

    assert send(ivanov, format="none").status_code == 200
    assert post.sent[-1].attachments == [] and "по ссылке ниже" in post.sent[-1].text
    assert "Приписка" not in post.sent[-1].text
    assert [e["format"] for e in web.trail.read() if e["kind"] == "protocol_sent"] == ["docx", "none"]
    # администратор из .env — хозяин любой папки; почты у него нет, и «Ответить» в письме не подставлен
    assert send(admin).status_code == 200 and post.sent[-1].reply_to == ""
    assert "Отправитель: Администратор." in post.sent[-1].text


def test_only_the_owner_of_the_folder_sends_the_protocol(office):
    web, admin, ivanov, petrov, post = desk(office)
    # читателю кнопка не показывается, и адрес ему закрыт
    assert petrov.get("/api/result", params={"file": MEETING}).json()["can_send"] is False
    assert petrov.get("/api/record/recipients", params={"file": MEETING}).status_code == 403
    refused = send(petrov)
    assert refused.status_code == 403 and "владелец папки" in refused.json()["detail"]
    # чужая запись неотличима от несуществующей
    orlova = enter(web, "orlova")
    assert send(orlova).status_code == 404 and send(ivanov, file="Проекты/нет.mp4").status_code == 404
    assert orlova.get("/api/record/recipients", params={"file": MEETING}).status_code == 404
    # без входа — 401; ключом доступа — запрет, даже у хозяина папки
    assert send(office[1]).status_code == 401
    robot, _ = keyed(web, ivanov)
    by_key = send(robot)
    assert by_key.status_code == 403 and by_key.json()["detail"] == web.BY_KEY_ONLY_READS
    assert "отправку протокола на почту" in web.BY_KEY_ONLY_READS
    assert robot.get("/api/record/recipients", params={"file": MEETING}).status_code == 403
    assert robot.get("/api/result", params={"file": MEETING}).json()["can_send"] is False
    # в личной папке хозяин — сам человек
    recorded(web, f"{HOME}/petrov/личное.mp4", by="petrov")
    assert send(petrov, file=f"{HOME}/petrov/личное.mp4", to="ivanov@example.test").status_code == 200
    assert post.sent[-1].to == ["ivanov@example.test"] and len(post.sent) == 1
    assert "protocol_sent" in kinds(web) and kinds(web).count("protocol_sent") == 1


def test_bad_requests_to_send_are_refused_before_any_letter(office, monkeypatch):
    web, admin, ivanov, petrov, post = desk(office)
    bad = send(ivanov, to="petrov@example.test, не адрес")
    assert bad.status_code == 400 and "«не» не похоже на адрес" in bad.json()["detail"] and bad.headers["x-field"] == "to"
    assert send(ivanov, to=" , ").status_code == 400
    many = send(ivanov, to=", ".join(f"u{n}@example.test" for n in range(21)))
    assert many.status_code == 400 and "не больше 20" in many.json()["detail"]
    assert send(ivanov, to=", ".join(f"u{n}@example.test" for n in range(20)), format="none").status_code == 200
    assert [m.to for m in post.sent] == [[f"u{n}@example.test"] for n in range(20)]
    post.sent.clear()
    assert send(ivanov, format="odt").status_code == 400
    long = send(ivanov, note="я" * 1001)
    assert long.status_code == 400 and long.headers["x-field"] == "note"
    assert send(ivanov, note="я" * 1000, format="none").status_code == 200 and post.sent.pop()
    # у записи без протокола отправлять нечего
    web.service.ledger.put({"file": "Проекты/пусто.mp4", "status": "no_audio", "stem": "пусто"})
    assert send(ivanov, file="Проекты/пусто.mp4").status_code == 404
    # вложение больше предела — отказ с советом отправить ссылкой
    monkeypatch.setattr(web.mail, "ATTACH_MAX_BYTES", 1000)
    big = send(ivanov)
    assert big.status_code == 413 and "без вложения" in big.json()["detail"]
    assert send(ivanov, format="none").status_code == 200 and post.sent.pop()
    assert post.sent == [] and kinds(web).count("protocol_sent") == 3


def test_sending_is_limited_per_person_per_hour(office, monkeypatch):
    web, admin, ivanov, petrov, post = desk(office)
    assert web.SENDS_PER_HOUR == 10
    monkeypatch.setattr(web, "protocol_sends", signup.Throttle(2))
    assert [send(ivanov, format="none").status_code for _ in range(3)] == [200, 200, 429]
    assert "за час" in send(ivanov, format="none").json()["detail"] and len(post.sent) == 2
    # отказ по адресам в предел не засчитывается, и предел у каждого свой
    assert send(admin, to="не адрес").status_code == 400
    # неудачная отправка тоже: почта лежала — человек не остаётся без попыток, когда её починят
    post.broken = mail.MailError("ConnectionRefusedError: [Errno 111] Connection refused", mail.DOWN)
    assert [send(admin, format="none").status_code for _ in range(5)] == [502] * 5
    post.broken = None
    assert [send(admin, format="none").status_code for _ in range(3)] == [200, 200, 429]


def test_smtp_error_is_told_in_plain_words_and_refused_addresses_are_named(office):
    web, admin, ivanov, petrov, post = desk(office)
    # сервера нет на связи: человеку — что случилось и к кому идти; ответ сервера как есть — в журналы
    post.broken = mail.MailError("gaierror: [Errno -2] Name or service not known", mail.DOWN)
    failed = send(ivanov)
    assert failed.status_code == 502
    assert failed.json()["detail"] == "почтовый сервер не отвечает — сообщите администратору сервиса"
    assert "x-field" not in failed.headers
    event = web.trail.read()[-1]
    assert (event["kind"], event["what"], event["by"], event["to"]) == ("mail_failed", "protocol", "ivanov", ["petrov@example.test"])
    assert "gaierror" in event["error"] and "protocol_sent" not in kinds(web)
    assert any("не отправлен" in line and "gaierror" in line for line in web.log.tail_from(0)[1])
    post.broken = mail.MailError("SMTPAuthenticationError: (535, b'5.7.8 bad password')")
    assert send(ivanov).json()["detail"] == "почтовый сервер не принял письмо — сообщите администратору сервиса"
    # сервер отказал всем адресам: каждый адрес с ответом сервера, как при частичной неудаче, — под полем адресов
    post.broken = mail.MailError("сервер отказал всем адресам: …", refused={
        "petrov@example.test": "550 5.1.1 no such user", "guest@partner.test": "552 5.2.2 mailbox full"})
    failed = send(ivanov, to="petrov@example.test, guest@partner.test")
    assert failed.status_code == 502 and failed.headers["x-field"] == "to"
    assert failed.json()["detail"] == ("почтовый сервер не принял ни один адрес: petrov@example.test (550 5.1.1 no such user); "
                                       "guest@partner.test (552 5.2.2 mailbox full). Проверьте адреса")
    assert kinds(web).count("mail_failed") == 3 and "protocol_sent" not in kinds(web)
    # часть адресов сервер не принял — остальным письма ушли, и страница узнаёт, кому нет
    post.broken, post.refused = None, {"guest@partner.test": "550 5.1.1 no such user"}
    partly = send(ivanov, to="petrov@example.test, guest@partner.test, ivanov@example.test").json()
    assert partly == {"ok": True, "to": ["petrov@example.test", "ivanov@example.test"],
                      "refused": [{"email": "guest@partner.test", "why": "550 5.1.1 no such user"}]}
    assert [m.to for m in post.sent] == [["petrov@example.test"], ["ivanov@example.test"]]
    event = web.trail.read()[-1]
    assert (event["kind"], event["refused"]) == ("protocol_sent", ["guest@partner.test"])
    assert event["to"] == ["petrov@example.test", "guest@partner.test", "ivanov@example.test"]
    assert kinds(web).count("protocol_sent") == 1
    assert any("не ушло: guest@partner.test (550 5.1.1 no such user)" in line for line in web.log.tail_from(0)[1])


def test_mail_domains_limit_the_recipients(tmp_path, monkeypatch):
    with running(tmp_path, extra={**MAIL_ENV, "MAIL_DOMAINS": "Example.test, @corp.example"}) as (web, client):
        post = Post()
        monkeypatch.setattr(web.mail, "send", post.send)
        monkeypatch.setattr(web.mail, "send_each", post.send_each)
        assert web.cfg.mail_domains == ("example.test", "corp.example")
        admin = enter(web, "admin", ADMIN_PASSWORD)
        web_, admin, ivanov, petrov, post = desk((web, client, admin, post))
        admin.post("/api/admin/users/save", json={"login": "sidorov", "name": "Сидоров", "email": "sidorov@partner.test"})
        offer = ivanov.get("/api/record/recipients", params={"file": MEETING}).json()
        # Сидорову папка открыта, но его почта — в чужом домене: в подсказке его нет
        assert offer["domains"] == ["example.test", "corp.example"]
        assert [p["email"] for p in offer["people"]] == ["ivanov@example.test", "petrov@example.test"]
        outside = send(ivanov, to="petrov@example.test, guest@partner.test")
        assert outside.status_code == 400 and outside.headers["x-field"] == "to"
        assert "example.test, corp.example" in outside.json()["detail"] and "guest@partner.test" in outside.json()["detail"]
        assert post.sent == []
        assert send(ivanov, to="petrov@example.test, boss@CORP.example").status_code == 200
        assert [m.to for m in post.sent] == [["petrov@example.test"], ["boss@corp.example"]]
        assert any("только на адреса доменов: example.test, corp.example" in line for line in web.log.tail_from(0)[1])


def test_without_mail_nothing_is_sent_and_the_button_is_hidden(site):
    web, admin, ivanov, petrov = staffed(site)
    recorded(web, f"{HOME}/ivanov/вкс.mp4", by="ivanov")
    file = f"{HOME}/ivanov/вкс.mp4"
    assert ivanov.get("/api/result", params={"file": file}).json()["can_send"] is False
    assert ivanov.post("/api/record/send", json={"file": file, "to": "a@example.test"}).status_code == 503
    assert ivanov.get("/api/record/recipients", params={"file": file}).status_code == 503
    # и письмо о готовности без почты никуда не идёт
    web.people.put("ivanov", "Иванов", email="ivanov@example.test")
    assert web._tell(web.service.ledger.get(file), "ready", "ivanov") is None
    shown = ivanov.get("/api/me").json()
    assert shown["mail"] is False and shown["email"] == "ivanov@example.test" and shown["notify_ready"] is True


def test_person_switches_ready_letters_off_and_on(office):
    web, admin, ivanov, petrov, post = desk(office)
    shown = petrov.get("/api/me").json()
    assert (shown["mail"], shown["email"], shown["notify_ready"]) == (True, "petrov@example.test", True)
    assert admin.get("/api/me").json()["email"] == "" and office[1].get("/api/me").json()["notify_ready"] is False
    assert petrov.post("/api/notify", json={"ready": False}).json() == {"ok": True, "notify_ready": False}
    assert petrov.get("/api/me").json()["notify_ready"] is False
    assert web.trail.read()[-1]["kind"] == "notify" and web.trail.read()[-1]["ready"] is False
    record = web.service.ledger.get(MEETING)
    assert web._tell(record, "ready", "petrov") is None and post.sent == []
    # правка учётной записи администратором выбор человека не сбрасывает; перезапуск — тоже
    admin.post("/api/admin/users/save", json={"login": "petrov", "name": "Петров П. П."})
    assert web.people.users["petrov"]["notify_ready"] is False
    assert users.Users(db.Database(web.cfg.state_dir)).users["petrov"]["notify_ready"] is False
    petrov.post("/api/notify", json={"ready": True})
    web._tell(record, "ready", "petrov").join(5)
    assert [m.to for m in post.sent] == [["petrov@example.test"]]
    # у администратора из .env учётной записи нет — настраивать нечего; ключом настройку не меняют
    assert admin.post("/api/notify", json={"ready": False}).status_code == 400
    robot, _ = keyed(web, petrov)
    assert robot.post("/api/notify", json={"ready": False}).status_code == 403
    assert office[1].post("/api/notify", json={"ready": False}).status_code == 401


def test_users_file_from_before_the_letters_gets_them_switched_on(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "users.json").write_text(json.dumps({"users": {"ivanov": {
        "name": "Иванов", "email": "i@example.test", "password": "", "home": f"{HOME}/ivanov", "admin": False,
        "read": {}, "manage": {}, "keys": []}}}), encoding="utf-8")
    cfg = types.SimpleNamespace(state_dir=state, inbox=tmp_path / "inbox", outbox=tmp_path / "outbox")
    people = users.Users(legacy.start(cfg))
    assert people.mailbox("ivanov") == {"name": "Иванов", "email": "i@example.test", "notify_ready": True}
    assert people.mailbox("nobody") is None


def test_ready_letter_goes_only_to_a_person_with_mail_who_wants_it(office):
    web, admin, ivanov, petrov, post = desk(office)
    record = web.service.ledger.get(MEETING)
    # без почты, без учётной записи (admin из .env, удалённый пользователь, пустой автор) — письма нет
    for login in ("sidorov", "admin", "nobody", ""):
        assert web._tell(record, "ready", login) is None, login
    web._tell(record, "ready", "petrov").join(5)
    letter, = post.sent
    assert letter.to == ["petrov@example.test"] and letter.subject == "запись готова — Планёрка: итоги/план"
    assert "Здравствуйте, Петров Пётр!" in letter.text and "Длительность: 10 мин" in letter.text
    assert "Поручений в протоколе: 2" in letter.text and "- Обсудили бюджет склада." in letter.text
    assert "http://podyachiy.test/?path=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B&open=" in letter.text
    assert letter.attachments == []
    event = web.trail.read()[-1]
    assert (event["kind"], event["by"], event["file"], event["to"], event["outcome"]) == (
        "ready_mail", "", MEETING, "petrov", "ready")

    # сбой: человеческим языком, без текста ошибки из журнала обработки
    broken = {"file": "Проекты/сбой.mp4", "status": "error", "failures": 3, "stem": "сбой",
              "error": "Traceback: httpx.ReadTimeout at 0x7f", "finished_at": "2026-10-09T12:00:00+03:00"}
    web._tell(broken, "failed", "petrov").join(5)
    assert post.sent[-1].subject == "запись не обработана — сбой.mp4"
    assert "Автоматических попыток больше не будет" in post.sent[-1].text and "Traceback" not in post.sent[-1].text
    assert "ReadTimeout" not in post.sent[-1].html
    web._tell({**broken, "failures": 1}, "failed", "petrov").join(5)        # ручной запуск, автоматика ещё повторит
    assert "попробует ещё раз сам" in post.sent[-1].text
    # тому, кто запустил запись сам, не пишут «обратитесь к администратору»: причина — в разделе «Обработка»
    web._tell(broken, "failed", "petrov", True).join(5)
    assert "в разделе «Обработка»" in post.sent[-1].text and "Обратитесь к администратору" not in post.sent[-1].text
    assert "запустите запись ещё раз" in post.sent[-1].text
    web._tell({"file": "Проекты/тихо.mp4", "status": "no_audio", "stem": "тихо"}, "failed", "petrov").join(5)
    assert post.sent[-1].subject == "в записи нет звука — тихо.mp4"
    assert "http://podyachiy.test/?path=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B\n" in post.sent[-1].text


def test_broken_mail_leaves_the_record_ready_and_is_written_down(office):
    web, admin, ivanov, petrov, post = desk(office)
    post.broken = mail.MailError("ConnectionRefusedError: [Errno 111] Connection refused")
    web._tell(web.service.ledger.get(MEETING), "ready", "petrov").join(5)
    assert post.sent == [] and web.service.ledger.get(MEETING)["status"] == "ok"
    event = web.trail.read()[-1]
    assert (event["kind"], event["what"], event["file"], event["to"]) == ("mail_failed", "ready", MEETING, "petrov")
    assert "Connection refused" in event["error"]
    assert any("письмо о записи" in line and "не ушло" in line and "Connection refused" in line
               for line in web.log.tail_from(0)[1])
    assert petrov.get("/api/result", params={"file": MEETING}).status_code == 200


def test_worker_tells_the_author_once_and_slow_mail_does_not_hold_it(office, monkeypatch):
    """Сквозной путь: обработчик закончил запись — автору ушло письмо; пересборка письма не шлёт.
    Почтовый сервер отвечает медленно, а обработчик тем временем берёт следующую запись."""
    web, admin, ivanov, petrov, post = desk(office)
    release, sent = threading.Event(), post.send

    def slow_send(*args, **kw):
        release.wait(10)
        return sent(*args, **kw)
    monkeypatch.setattr(web.mail, "send", slow_send)

    async def process(cfg, rel, stem, template, force, manual, log, on_progress=None, neighbours=None, words=None, marked=None):
        return {"file": rel, "stem": stem, "status": "ok", "cost": {}, "title": "Новая планёрка",
                "finished_at": "2026-10-10T12:00:00+03:00"}
    monkeypatch.setattr(web.pipeline, "process", process)

    def wait(done):
        deadline = time.time() + 10
        while not done():
            assert time.time() < deadline, "обработчик не дошёл до конца"
            time.sleep(0.05)

    for name in ("новая.mp4", "вторая.mp4"):
        (web.cfg.inbox / "Проекты" / name).write_bytes(b"video")
        web.trail.write("upload", "petrov", file=f"Проекты/{name}", size=5)
        web.service.queue.append(watcher.QueueItem(path=f"Проекты/{name}"))
    # обе записи обработаны, хотя первое письмо всё ещё висит на почтовом сервере
    wait(lambda: all(web.service.ledger.get(f"Проекты/{n}") for n in ("новая.mp4", "вторая.mp4")) and not web.service.running)
    assert post.sent == [] and web.service.ledger.get("Проекты/новая.mp4")["announced"] == "ready"
    release.set()
    wait(lambda: len(post.sent) == 2)
    assert {m.subject for m in post.sent} == {"запись готова — Новая планёрка"}
    assert all(m.to == ["petrov@example.test"] for m in post.sent)

    # хозяин папки пересобирает протокол — письма нет ни ему, ни автору
    web.service.enqueue_manual("Проекты/новая.mp4", None, False, "ivanov", "rebuild")
    wait(lambda: web.service.ledger.get("Проекты/новая.mp4")["runs"] == 2 and not web.service.running)
    # администратор запускает вручную — письмо было бы ему, но у admin из .env почты нет
    web.service.enqueue_manual("Проекты/новая.mp4", None, False, "admin", "manual")
    wait(lambda: web.service.ledger.get("Проекты/новая.mp4")["runs"] == 3 and not web.service.running)
    # вручную запускает администратор сервиса с почтой — письмо ему
    admin.post("/api/admin/users/save", json={"login": "orlova", "name": "Орлова", "admin": True})
    web.service.enqueue_manual("Проекты/новая.mp4", None, False, "orlova", "manual")
    wait(lambda: len(post.sent) == 3)
    assert post.sent[-1].to == ["orlova@example.test"]
    told = lambda: [e["to"] for e in web.trail.read() if e["kind"] == "ready_mail"]
    wait(lambda: len(told()) == 3)                  # строка в журнал действий пишется следом за отправкой, в её же потоке
    assert told() == ["petrov", "petrov", "orlova"]


# ---------- заявка на доступ без почты ----------

def test_request_to_login_chain_without_mail(site, monkeypatch):
    """Почты в сервисе нет: заявка → список у администратора → одобрение → ссылка в ответе → пароль → вход."""
    web, guest = site
    monkeypatch.setattr(web.mail, "send", lambda *a, **kw: pytest.fail("без почты писем быть не должно"))
    admin = enter(web, "admin", ADMIN_PASSWORD)
    assert guest.get("/api/me").json()["signup"] is True and web.cfg.public_url == ""

    # почта в заявке необязательна, но если указана — проверяется
    assert guest.post("/api/request", json={**CLAIM, "email": "не почта"}).headers["x-field"] == "email"
    assert guest.post("/api/request", json={**CLAIM, "email": ""}).json() == {"ok": True}
    assert guest.post("/api/request", json={"name": "Орлова Анна", "email": "", "login": "orlova"}).status_code == 200
    assert guest.post("/api/request", json={**CLAIM, "email": ""}).status_code == 409          # тот же логин уже ждёт
    assert admin.get("/api/me").json()["requests"] == 2 and guest.get("/api/me").json()["requests"] == 0
    listed = admin.get("/api/admin/requests").json()
    assert [(c["login"], c["email"]) for c in listed["requests"]] == [("sidorov", ""), ("orlova", "")]
    assert listed["mail"]["on"] is False

    claim = listed["requests"][0]
    approved = admin.post("/api/admin/requests/approve", json={"id": claim["id"], "login": "sidorov", "name": "Сидоров Пётр"}).json()
    link = approved["link"]
    assert approved["ok"] and "mail_error" not in approved and [c["login"] for c in approved["requests"]] == ["orlova"]
    assert link["path"] == "/set-password" and link["hours"] == 48 and len(link["token"]) > 20
    user, = approved["users"]
    assert (user["login"], user["email"], user["has_password"]) == ("sidorov", "", False)
    assert link["token"] not in on_disk(web) and users._link_hash(link["token"]) in on_disk(web)     # на диске только хеш
    assert admin.get("/api/me").json()["requests"] == 1

    assert guest.post("/api/set-password/check", json={"token": link["token"]}).json()["login"] == "sidorov"
    assert guest.post("/api/set-password", json={"token": link["token"], "password": "пароль-по-ссылке"}).status_code == 200
    assert guest.post("/api/set-password", json={"token": link["token"], "password": "пароль-по-ссылке"}).status_code == 400
    sidorov = enter(web, "sidorov", "пароль-по-ссылке")
    assert sidorov.get("/api/me").json()["home"] == f"{HOME}/sidorov"

    # человек забыл пароль: администратор выпускает новую ссылку из строки пользователя, прежняя гаснет
    first = admin.post("/api/admin/users/link", json={"login": "sidorov"}).json()["link"]["token"]
    second = admin.post("/api/admin/users/link", json={"login": "sidorov"}).json()["link"]["token"]
    assert guest.post("/api/set-password/check", json={"token": first}).status_code == 404
    assert guest.post("/api/set-password", json={"token": second, "password": "ещё-один-пароль"}).status_code == 200
    again = enter(web, "sidorov", "ещё-один-пароль")
    assert again.post("/api/admin/users/link", json={"login": "sidorov"}).status_code == 403      # себе ссылку не выпустить
    assert kinds(web) == ["login", "request_new", "request_new", "request_approve", "link_shown", "password_link", "login",
                          "link_shown", "link_shown", "password_link", "login"]
    assert [e["why"] for e in web.trail.read() if e["kind"] == "link_shown"] == ["invite", "admin", "admin"]
    assert link["token"] not in "\n".join(web.log.lines) and second not in "\n".join(web.log.lines)


def test_with_mail_the_request_still_needs_an_address(office):
    web, guest, admin, post = office
    refused = guest.post("/api/request", json={**CLAIM, "email": ""})
    assert refused.status_code == 400 and refused.headers["x-field"] == "email"
    assert guest.get("/api/me").json()["signup"] is True


# ---------- срок поручения датой ----------

def test_owner_sets_a_due_date_and_the_reader_only_sees_it(site):
    web, admin, ivanov, petrov = filled(site)
    file = "Проекты/вкс.mp4"
    late = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
    soon = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    body = {"file": file, "key": SMETA, "due": late}
    # права — те же, что у отметки «выполнено»: читатель не может, чужая запись — 404, без входа — 401
    assert ivanov.post("/api/record/task", json=body).status_code == 403
    assert ivanov.post("/api/v1/task", params={"file": file, "key": SMETA, "due": late}).status_code == 403
    assert petrov.post("/api/record/task", json=body).status_code == 404
    assert site[1].post("/api/record/task", json=body).status_code == 401
    assert web.noted.everything() == {}
    for bad in ("завтра", "20.10.2026", "2026-13-01", "2026-1-5"):
        assert admin.post("/api/record/task", json={**body, "due": bad}).status_code == 400, bad
    assert admin.post("/api/record/task", json={"file": file, "key": SMETA}).status_code == 400    # ни done, ни due

    protocol = pipeline.protocol_path(web.cfg, file, web.service.ledger.get(file))
    before = protocol.read_bytes()
    task = admin.post("/api/record/task", json=body).json()["task"]
    assert (task["due"], task["due_text"], task["due_by"], task["due_date"], task["overdue"], task["done"], task["open"]) == (
        late, "до пятницы", "admin", late, True, False, True)
    assert protocol.read_bytes() == before                                   # Markdown протокола не переписан

    # читатель видит срок в реестре, в записи и на странице, отборы и порядок работают
    row = next(t for t in ivanov.get("/api/v1/tasks").json()["tasks"] if t["file"] == file and t["key"] == SMETA)
    assert (row["due"], row["due_text"], row["overdue"], row["can_mark"]) == (late, "до пятницы", True, False)
    assert [(t["file"], t["task"]) for t in ivanov.get("/api/v1/tasks", params={"overdue": "true"}).json()["tasks"]] == [
        (file, "Согласовать смету")]
    assert ivanov.get("/api/v1/tasks", params={"sort": "due"}).json()["tasks"][0]["key"] == SMETA
    assert ivanov.get("/api/v1/record", params={"file": file}).json()["tasks"][0]["due"] == late
    shown = ivanov.get("/api/result", params={"file": file}).json()
    assert (shown["tasks"][0]["due"], shown["tasks"][0]["due_col"], shown["can_mark"]) == (late, 1, False)

    sheet = ivanov.get("/api/v1/tasks.csv", params={"overdue": "true"}).text.lstrip("\ufeff").splitlines()
    assert len(sheet) == 2 and sheet[1].split(";")[:6] == ["Иванов Иван", "Согласовать смету", marks.day(late), "до пятницы", "", "да"]
    assert sheet[1].endswith(";admin")
    import docx
    word = docx.Document(io.BytesIO(ivanov.get("/api/export", params={"file": file, "format": "docx"}).content))
    assert word.tables[0].rows[2].cells[1].text == marks.day(late)
    assert marks.day(late) in ivanov.get("/api/v1/record.html", params={"file": file}).text

    # перенос срока вперёд — уже не просрочено; выполненное с прошедшим сроком — тоже
    moved = admin.post("/api/v1/task", params={"file": file, "key": SMETA, "due": soon}).json()["task"]
    assert (moved["due"], moved["overdue"], moved["done"]) == (soon, False, False)     # due без done отметку не ставит
    assert ivanov.get("/api/v1/tasks", params={"overdue": "true"}).json()["tasks"] == []
    admin.post("/api/record/task", json=body)
    both = admin.post("/api/record/task", json={"file": file, "key": SMETA, "done": True}).json()["task"]
    assert (both["done"], both["due"], both["overdue"]) == (True, late, False)

    # снятие ручного срока возвращает срок из протокола; отметка остаётся
    back = admin.post("/api/record/task", json={**body, "due": ""}).json()["task"]
    assert (back["due"], back["due_text"], back["due_date"], back["due_by"], back["done"]) == (None, "до пятницы", "2026-10-16", None, True)
    assert admin.post("/api/record/task", json={**body, "due": ""}).status_code == 200            # повтор ничего не меняет
    events = [e for e in web.trail.read() if e["kind"] == "task_due"]
    assert [(e["by"], e["file"], e["task"], e["owner"], e["was"], e["now"]) for e in events] == [
        ("admin", file, "Согласовать смету", "Иванов Иван", "до пятницы", late),
        ("admin", file, "Согласовать смету", "Иванов Иван", late, soon),
        ("admin", file, "Согласовать смету", "Иванов Иван", soon, late),
        ("admin", file, "Согласовать смету", "Иванов Иван", late, "до пятницы")]


def test_key_of_the_folder_owner_sets_a_due_date_and_it_moves_with_the_record(site):
    web, admin, ivanov, petrov = filled(site)
    robot, _ = keyed(web, petrov)
    file, soon = f"{HOME}/petrov/личное.mp4", (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
    key = marks.key("Петров Пётр", "Согласовать смету")
    set_ = robot.post("/api/v1/task", params={"file": file, "key": key, "due": soon})
    assert set_.status_code == 200 and set_.json()["task"]["due"] == soon and set_.json()["task"]["done"] is False
    event = [e for e in web.trail.read() if e["kind"] == "task_due"][-1]
    assert event["by"] == "petrov" and event["via"] == "key"
    # срок лежит рядом с протоколом и переезжает вместе с записью
    moved = petrov.post("/api/record/rename", json={"file": file, "name": "итоги недели"}).json()["file"]
    assert robot.get("/api/v1/record", params={"file": moved}).json()["tasks"][0]["due"] == soon
    assert robot.post("/api/v1/task", params={"file": moved, "key": key, "due": ""}).json()["task"]["due"] is None


# ---------- раздельные права: чтение и загрузка ----------
# Обработка записи платная, поэтому всё, что к ней ведёт, — загрузка и пересборка протокола —
# открыто только с правом загрузки. Ступени на папку: чтение → загрузка → администрирование.

def tiers(site):
    """«Проекты» с подпапкой и записью. Иванов — читатель, Петров — читатель с загрузкой,
    Сидоров — администратор папки; личной загрузки нет ни у кого из троих."""
    web, _ = site
    admin = enter(web, "admin", ADMIN_PASSWORD)
    for login, name in (("ivanov", "Иванов"), ("petrov", "Петров"), ("sidorov", "Сидоров")):
        assert admin.post("/api/admin/users/save", json={"login": login, "name": name,
                                                         "password": PASSWORD}).status_code == 200
    for path in ("Проекты", "Проекты/Сметы", "Снабжение"):
        admin.post("/api/folder", json={"dir": path})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "tree"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "petrov", "upload": "tree"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "sidorov", "manage": "tree"})
    voiced(web, "Проекты/планёрка.mp4")
    return web, admin, enter(web, "ivanov"), enter(web, "petrov"), enter(web, "sidorov")


def test_reader_sees_everything_in_the_folder_but_the_page_refuses_his_upload(site):
    web, admin, ivanov, petrov, sidorov = tiers(site)
    file = "Проекты/планёрка.mp4"
    # бесплатное читателю открыто, как и раньше
    inside = ivanov.get("/api/browse", params={"path": "Проекты"}).json()
    assert [f["file"] for f in inside["files"]] == [file]
    assert ivanov.get("/api/result", params={"file": file, "kind": "summary"}).status_code == 200
    assert ivanov.get("/api/search", params={"q": "Решили"}).json()["files"][0]["file"] == file
    assert ivanov.get("/api/v1/tasks").status_code == 200
    assert ivanov.get("/api/export", params={"file": file, "format": "docx"}).status_code == 200
    # а загрузки нет: кнопки не будет (anywhere), причина — словами
    assert inside["upload"]["enabled"] is False and inside["upload"]["anywhere"] is False
    assert "право загрузки" in inside["upload"]["denied"] and inside["home_upload"] is False
    for folder in ("Проекты", "Проекты/Сметы"):
        chunk = {"name": "запись.mp3", "size": 5, "dir": folder}
        asked = ivanov.get("/api/upload", params=chunk)
        assert asked.status_code == 403 and "право загрузки" in asked.json()["detail"], folder
        assert "администратору" in asked.json()["detail"]
        assert ivanov.put("/api/upload", params={**chunk, "offset": 0}, content=b"12345").status_code == 403, folder
        assert ivanov.delete("/api/upload", params=chunk).status_code == 403, folder
    assert not list(web.cfg.inbox.rglob("запись.mp3")) and not [e for e in web.trail.read() if e["kind"] == "upload"]


def test_reader_with_upload_and_the_folder_admin_upload_on_the_page(site):
    web, admin, ivanov, petrov, sidorov = tiers(site)
    for client, folder, name in ((petrov, "Проекты", "от петрова.mp3"), (petrov, "Проекты/Сметы", "смета.mp3"),
                                 (sidorov, "Проекты/Сметы", "от сидорова.mp3"), (admin, "Снабжение", "админ.mp3")):
        inside = client.get("/api/browse", params={"path": folder}).json()["upload"]
        assert inside["enabled"] is True and inside["anywhere"] is True and inside["denied"] == "", name
        sent = client.put("/api/upload", params={"name": name, "size": 5, "dir": folder, "offset": 0}, content=b"12345")
        assert sent.status_code == 200 and sent.json()["done"], name
    assert [(e["by"], e["file"]) for e in web.trail.read() if e["kind"] == "upload"] == [
        ("petrov", "Проекты/от петрова.mp3"), ("petrov", "Проекты/Сметы/смета.mp3"),
        ("sidorov", "Проекты/Сметы/от сидорова.mp3"), ("admin", "Снабжение/админ.mp3")]
    # право загрузки — на свою папку: в закрытую по-прежнему нельзя
    there = petrov.get("/api/upload", params={"name": "чужое.mp3", "size": 5, "dir": "Снабжение"})
    assert there.status_code == 403 and "не открыта" in there.json()["detail"]
    # читатель с загрузкой остаётся читателем: записью не распоряжается, доступ не раздаёт
    seen = petrov.get("/api/result", params={"file": "Проекты/планёрка.mp4", "kind": "summary"}).json()
    assert (seen["can_edit"], seen["can_act"], seen["can_rebuild"]) == (False, False, False)
    assert petrov.post("/api/record/rename", json={"file": "Проекты/планёрка.mp4", "name": "новое"}).status_code == 403
    assert petrov.get("/api/access", params={"dir": "Проекты"}).status_code == 403


def test_upload_right_follows_its_scope_and_is_taken_back(site):
    web, admin, ivanov, petrov, sidorov = tiers(site)
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "upload": "only"})
    put = lambda folder: ivanov.put("/api/upload", params={"name": f"{len(folder)}.mp3", "size": 5, "dir": folder,
                                                           "offset": 0}, content=b"12345").status_code
    assert put("Проекты") == 200 and put("Проекты/Сметы") == 403           # «только эта папка» — без подпапок
    below = ivanov.get("/api/browse", params={"path": "Проекты/Сметы"}).json()["upload"]
    assert (below["enabled"], below["anywhere"], below["denied"]) == (False, True, web.NO_UPLOAD)
    # из общего списка читателю с загрузкой, но без личной, говорят открыть папку
    flat = ivanov.get("/api/browse", params={"flat": True}).json()
    assert flat["upload"]["enabled"] is False and flat["home_upload"] is False
    assert flat["upload"]["denied"] == web.UPLOAD_ELSEWHERE
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "upload": "tree"})
    assert put("Проекты/Сметы") == 200                                     # наследуется на подпапки
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "upload": "none"})
    assert ivanov.get("/api/upload", params={"name": "ещё.mp3", "size": 5, "dir": "Проекты"}).status_code == 403
    assert "Проекты" in dir_names(ivanov)                                  # чтение при этом осталось


def test_folder_admin_grants_upload_and_the_grant_is_in_the_journal(site):
    web, admin, ivanov, petrov, sidorov = tiers(site)
    granted = sidorov.post("/api/access", json={"dir": "Проекты/Сметы", "login": "ivanov", "upload": "only"})
    assert granted.status_code == 200
    row = next(u for u in granted.json()["users"] if u["login"] == "ivanov")
    assert (row["upload"], row["upload_via"], row["read"], row["read_via"]) == ("only", None, "none", "Проекты")
    assert next(u for u in granted.json()["users"] if u["login"] == "petrov")["upload_via"] == "Проекты"
    # читатель и читатель с загрузкой прав не раздают; на все папки загрузку не даёт и администратор
    for client in (ivanov, petrov):
        assert client.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "upload": "tree"}).status_code == 403
    assert admin.post("/api/access", json={"dir": "", "login": "ivanov", "upload": "tree"}).status_code == 400
    assert admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "upload": "всё"}).status_code == 400
    sidorov.post("/api/access", json={"dir": "Проекты/Сметы", "login": "ivanov", "upload": "none"})
    events = [e for e in admin.get("/api/admin/journal").json()["events"] if e["kind"] == "access"
              and e["folder"] == "Проекты/Сметы"]
    assert [(e["by"], e["login"], e["upload"], e["read"], e["manage"]) for e in reversed(events)] == [
        ("sidorov", "ivanov", "only", None, None), ("sidorov", "ivanov", "none", None, None)]
    # раздел «Папки и доступ» показывает три ступени раздельно
    listed = {f["path"]: f for f in admin.get("/api/admin/folders").json()["folders"]}
    assert (listed["Проекты"]["viewers"], listed["Проекты"]["uploaders"], listed["Проекты"]["managers"]) == (
        ["Иванов"], ["Петров"], ["Сидоров"])
    shown = {u["login"]: u for u in admin.post("/api/admin/users").json()["users"]}
    assert shown["petrov"]["upload"] == {"Проекты": "tree"} and shown["petrov"]["home_upload"] is False


def test_home_folder_takes_uploads_only_when_the_admin_allowed(site):
    web, admin, ivanov, petrov, sidorov = tiers(site)
    home = f"{HOME}/ivanov"
    chunk = {"name": "личная.mp3", "size": 5, "dir": home}
    refused = ivanov.get("/api/upload", params=chunk)
    assert refused.status_code == 403 and refused.json()["detail"] == web.NO_HOME_UPLOAD
    assert ivanov.put("/api/upload", params={**chunk, "offset": 0}, content=b"12345").status_code == 403
    inside = ivanov.get("/api/browse", params={"path": home}).json()
    assert inside["upload"]["enabled"] is False and inside["mkdir"] is True        # папка его, подпапки заводит
    assert ivanov.get("/api/browse", params={"flat": True}).json()["upload"]["denied"] == web.NO_UPLOAD_AT_ALL
    personal = {p["login"]: p["upload"] for p in admin.get("/api/admin/folders").json()["personal"]}
    assert personal == {"ivanov": False, "petrov": False, "sidorov": False}

    saved = admin.post("/api/admin/users/save", json={"login": "ivanov", "name": "Иванов", "home_upload": True})
    assert saved.status_code == 200 and next(u for u in saved.json()["users"] if u["login"] == "ivanov")["home_upload"]
    flat = ivanov.get("/api/browse", params={"flat": True}).json()
    assert flat["home_upload"] is True and flat["upload"]["anywhere"] is True and flat["upload"]["denied"] == ""
    assert ivanov.put("/api/upload", params={**chunk, "offset": 0}, content=b"12345").json()["done"]
    # правка без признака право не трогает; снятие закрывает загрузку снова
    admin.post("/api/admin/users/save", json={"login": "ivanov", "name": "Иванов И."})
    assert web.people.users["ivanov"]["home_upload"] is True
    admin.post("/api/admin/users/save", json={"login": "ivanov", "name": "Иванов И.", "home_upload": False})
    assert ivanov.get("/api/upload", params={**chunk, "name": "вторая.mp3"}).status_code == 403
    changes = [e for e in web.trail.read() if e["kind"] == "user_change" and e["login"] == "ivanov"]
    assert [e.get("home_upload") for e in changes] == [True, None, False]          # выдача и снятие — в журнале
    # администратору папки «Проекты» личная загрузка тоже нужна отдельно
    assert sidorov.get("/api/upload", params={**chunk, "dir": f"{HOME}/sidorov"}).status_code == 403


def test_rebuild_is_paid_and_needs_the_upload_right(site):
    web, admin, ivanov, petrov, sidorov = tiers(site)
    shared, own = "Проекты/планёрка.mp4", voiced(web, f"{HOME}/ivanov/личная.mp4")
    names = {"parts": {"1": {"S1": "Петрова Анна"}}}
    # хозяин личной папки без права загрузки: подписывает, но не пересобирает — и страница это знает
    seen = ivanov.get("/api/result", params={"file": own, "kind": "summary"}).json()
    assert (seen["can_edit"], seen["can_rebuild"]) == (True, False)
    refused = ivanov.post("/api/admin/names", json={"file": own, **names, "rebuild": True})
    assert refused.status_code == 403 and refused.json()["detail"] == web.NO_REBUILD
    assert not (web.cfg.outbox / HOME / "ivanov/личная/transcript/личная.names.json").exists()     # не наполовину
    assert ivanov.post("/api/admin/names", json={"file": own, **names}).status_code == 200         # подпись бесплатна
    assert ivanov.post("/api/admin/names", json={"file": own, "rebuild": True}).status_code == 403
    assert web.service.queue == []
    # читатель и читатель с загрузкой в общей папке не хозяева: прежний отказ
    for client in (ivanov, petrov):
        assert client.post("/api/admin/names", json={"file": shared, "rebuild": True}).status_code == 403
    assert web.service.queue == []
    # администратор папки пересобирает: администрирование включает загрузку
    assert sidorov.get("/api/result", params={"file": shared, "kind": "summary"}).json()["can_rebuild"] is True
    assert sidorov.post("/api/admin/names", json={"file": shared, "rebuild": True}).json()["rebuilding"] is True
    # личную — после того как администратор сервиса разрешил загрузку
    admin.post("/api/admin/users/save", json={"login": "ivanov", "name": "Иванов", "home_upload": True})
    assert ivanov.get("/api/result", params={"file": own, "kind": "summary"}).json()["can_rebuild"] is True
    assert ivanov.post("/api/admin/names", json={"file": own, "rebuild": True}).status_code == 200
    assert sorted((i.path, i.reason) for i in web.service.queue) == [(own, "rebuild"), (shared, "rebuild")]
    # ручной запуск и перераспознавание — только администратору сервиса, как и было
    for client in (ivanov, petrov, sidorov):
        assert client.post("/api/admin/run", json={"file": shared, "force": True}).status_code == 403


def test_key_uploads_and_rebuilds_only_within_its_owners_upload_right(site):
    web, admin, ivanov, petrov, sidorov = tiers(site)
    reader, _ = keyed(web, ivanov, "читатель")
    loader, _ = keyed(web, petrov, "с загрузкой")
    lead, _ = keyed(web, sidorov, "администратор папки")
    shared = "Проекты/планёрка.mp4"

    who = reader.get("/api/v1/whoami").json()
    assert who["upload"] is True and who["can_upload"] is False             # сервис принимает, а этому человеку нельзя
    assert loader.get("/api/v1/whoami").json()["can_upload"] is True
    assert reader.get("/api/v1/folders").json()["folders"] == []
    assert loader.get("/api/v1/folders").json()["folders"] == ["Проекты", "Проекты/Сметы"]
    assert lead.get("/api/v1/folders").json()["folders"] == ["Проекты", "Проекты/Сметы"]
    tree = reader.get("/api/v1/tree").json()
    assert not next(f for f in tree["folders"] if f["path"] == "Проекты")["can_upload"]
    assert next(f for f in loader.get("/api/v1/tree").json()["folders"] if f["path"] == "Проекты")["can_upload"]
    assert reader.get("/api/v1/record", params={"file": shared}).status_code == 200       # читать ключ читателя может
    assert reader.get("/api/v1/search", params={"q": "бюджет"}).status_code == 200

    refused = reader.put("/api/v1/upload", params={"dir": "Проекты", "name": "запись.mp3"}, content=b"123")
    assert refused.status_code == 403 and refused.json()["detail"] == web.NO_UPLOAD
    assert reader.put("/api/v1/upload", params={"dir": f"{HOME}/ivanov", "name": "запись.mp3"},
                      content=b"123").json()["detail"] == web.NO_HOME_UPLOAD
    nowhere = reader.put("/api/v1/upload", params={"name": "запись.mp3"}, content=b"123")
    assert nowhere.status_code == 400 and "право загрузки выдаёт" in nowhere.json()["detail"]
    assert not list(web.cfg.inbox.rglob("запись.mp3"))
    assert loader.put("/api/v1/upload", params={"dir": "Проекты/Сметы", "name": "запись.mp3"}, content=b"123").status_code == 200
    assert lead.put("/api/v1/upload", params={"dir": "Проекты", "name": "запись.mp3"}, content=b"123").status_code == 200
    assert loader.put("/api/v1/upload", params={"dir": "Снабжение", "name": "запись.mp3"}, content=b"123").status_code == 403
    assert [(e["by"], e["key"]) for e in web.trail.read() if e["kind"] == "upload"] == [
        ("petrov", "с загрузкой"), ("sidorov", "администратор папки")]

    # пересборка ключом: читателю и читателю с загрузкой — нет, администратору папки — да
    for robot in (reader, loader):
        assert robot.get("/api/v1/speakers", params={"file": shared}).json()["can_rebuild"] is False
        assert robot.post("/api/v1/speakers", params={"file": shared, "rebuild": True}).status_code == 403
    assert lead.get("/api/v1/speakers", params={"file": shared}).json()["can_rebuild"] is True
    own = voiced(web, f"{HOME}/ivanov/личная.mp4")
    denied = reader.post("/api/v1/speakers", params={"file": own, "rebuild": True})
    assert denied.status_code == 403 and denied.json()["detail"] == web.NO_REBUILD
    assert reader.post("/api/v1/speakers", params={"file": own, "part": 1, "label": "S1", "name": "Анна"}).status_code == 200
    assert web.service.queue == []
    assert lead.post("/api/v1/speakers", params={"file": shared, "rebuild": True}).json()["rebuilding"] is True
    # право выдали владельцу — тот же ключ начал загружать, без перевыпуска
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "upload": "only"})
    assert reader.put("/api/v1/upload", params={"dir": "Проекты", "name": "теперь можно.mp3"}, content=b"123").status_code == 200
    assert reader.get("/api/v1/whoami").json()["can_upload"] is True


def test_service_started_over_an_old_users_file_gives_reading_only(tmp_path):
    """Установка до раздельных прав: чтение включало загрузку, у каждого была загрузка в личную
    папку. После обновления те же права — только чтение, пока загрузку не выдадут заново."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "users.json").write_text(json.dumps({"users": {"ivanov": {
        "name": "Иванов", "email": "", "password": users.hash_password(PASSWORD), "home": f"{HOME}/ivanov",
        "admin": False, "read": {"Проекты": "tree"}, "manage": {}, "notify_ready": True, "source": "local"}}}),
        encoding="utf-8")
    with running(tmp_path) as (web, _):
        admin, ivanov = enter(web, "admin", ADMIN_PASSWORD), enter(web, "ivanov")
        admin.post("/api/folder", json={"dir": "Проекты"})
        recorded(web, "Проекты/вкс.mp4")
        assert [f["file"] for f in ivanov.get("/api/browse", params={"path": "Проекты"}).json()["files"]] == ["Проекты/вкс.mp4"]
        for folder in ("Проекты", f"{HOME}/ivanov"):
            sent = ivanov.put("/api/upload", params={"name": "з.mp3", "size": 5, "dir": folder, "offset": 0}, content=b"12345")
            assert sent.status_code == 403, folder
            assert ivanov.put("/api/v1/upload", params={"dir": folder, "name": "з.mp3"}, content=b"123").status_code == 403
        assert ivanov.get("/api/v1/whoami").json()["can_upload"] is False
        admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "upload": "tree"})
        assert ivanov.put("/api/v1/upload", params={"dir": "Проекты", "name": "з.mp3"}, content=b"123").status_code == 200


def test_claim_is_approved_with_or_without_home_upload(office):
    web, guest, admin, post = office
    for login, allow in (("reader", False), ("loader", True)):
        assert guest.post("/api/request", json={**CLAIM, "login": login, "email": f"{login}@example.test"}).status_code == 200
        claim = next(c for c in admin.get("/api/admin/requests").json()["requests"] if c["login"] == login)
        body = {"id": claim["id"], "login": login, "name": login.title(), **({"home_upload": True} if allow else {})}
        assert admin.post("/api/admin/requests/approve", json=body).status_code == 200
        assert web.people.users[login]["home_upload"] is allow and web.people.access(login).uploader is allow
    seen = [e.get("home_upload") for e in web.trail.read() if e["kind"] == "request_approve"]
    assert seen == [None, True]


# ---------- проверка ключа поставщика ----------

def checker(monkeypatch, states=None):
    """Заглушка поставщика для проверки ключа: кого спросили и о каких моделях. Ставится до
    запуска сервиса — проверка идёт и при запуске."""
    asked = []

    async def check(provider, models=()):
        asked.append((provider.title, provider.key_env, tuple(models)))
        state = (states or {}).get(provider.key_env, providers.CHECK_OK)
        return {"state": state, "text": "ключ принят" if state == providers.CHECK_OK else "ключ не принят (HTTP 401)",
                "hint": "" if state == providers.CHECK_OK else f"проверьте {provider.key_env} в .env"}

    monkeypatch.setattr(providers, "check", check)
    return asked


def wait_log(web, word):
    for _ in range(200):
        lines = [line for line in web.log.tail_from(0)[1] if word in line]
        if lines:
            return lines
        time.sleep(0.02)
    return []


def test_key_check_button_asks_every_configured_provider(tmp_path, monkeypatch):
    asked = checker(monkeypatch, {"BACKUP_API_KEY": providers.CHECK_REFUSED})
    extra = {"OPENAI_BASE_URL": "https://routerai.ru/api/v1", "BACKUP_BASE_URL": "https://openrouter.ai/api/v1",
             "BACKUP_API_KEY": "sk-spare-not-a-key", "SUMMARY_MODEL": "deepseek/deepseek-v4.1-flash",
             "ACTIONS_MODELS": "deepseek/deepseek-v4.1-flash,openai/gpt-6-luna"}
    with running(tmp_path, extra=extra) as (web, guest):
        assert guest.post("/api/admin/providers/check").status_code == 401
        asked.clear()
        done = enter(web, "admin", ADMIN_PASSWORD).post("/api/admin/providers/check")
        body = done.json()
        assert done.status_code == 200 and body["ok"] is False
        assert [(c["role"], c["title"], c["state"]) for c in body["checks"]] == [
            ("основной поставщик", "RouterAI", "ok"), ("запасной поставщик", "OpenRouter", "refused")]
        assert body["checks"][1]["hint"] == "проверьте BACKUP_API_KEY в .env"
        text_models = ("deepseek/deepseek-v4.1-flash", "openai/gpt-6-luna")
        assert asked == [("RouterAI", "OPENAI_API_KEY", text_models), ("OpenRouter", "BACKUP_API_KEY", text_models)]
        assert "sk-test-not-a-key" not in done.text and "sk-spare-not-a-key" not in done.text


def test_key_check_covers_the_separate_text_address(tmp_path, monkeypatch):
    asked = checker(monkeypatch)
    extra = {"SUMMARY_BASE_URL": "http://lm.local:1234/v1", "SUMMARY_API_KEY": "lm-studio",
             "SUMMARY_MODEL": "qwen3-14b", "ACTIONS_MODELS": "qwen3-14b"}
    with running(tmp_path, mode="personal", extra=extra) as (web, client):
        asked.clear()
        body = client.post("/api/admin/providers/check").json()         # личный режим: кнопка работает без входа
        assert body["ok"] is True and [c["role"] for c in body["checks"]] == ["основной поставщик", "текстовые этапы"]
        # текстовые модели спрашивают у того, кто их обслуживает
        assert [(key, models) for _, key, models in asked] == [("OPENAI_API_KEY", ()), ("SUMMARY_API_KEY", ("qwen3-14b",))]


def test_key_is_checked_once_on_start_and_a_refusal_is_only_a_warning(tmp_path, monkeypatch):
    for mode in ("good", "bad"):
        (tmp_path / mode).mkdir()
    asked = checker(monkeypatch)
    with running(tmp_path / "good") as (web, client):
        assert wait_log(web, "проверка ключа")[0].endswith("проверка ключа: основной поставщик 127.0.0.1 — ключ принят")
        assert len(asked) == 1
    checker(monkeypatch, {"OPENAI_API_KEY": providers.CHECK_REFUSED})
    with running(tmp_path / "bad") as (web, client):
        line = wait_log(web, "проверка ключа")[0]
        assert "ВНИМАНИЕ: проверка ключа: основной поставщик 127.0.0.1 — ключ не принят (HTTP 401)" in line
        assert "Что делать: проверьте OPENAI_API_KEY в .env" in line and "sk-test-not-a-key" not in line
        assert client.get("/api/status").json()["ok"] is True           # сервис при этом работает


# ---------- личный режим: загрузка в корень запросом, страницы входа ----------

def test_personal_mode_uploads_to_the_root_by_request_like_the_button(tmp_path):
    for mode in ("personal", "corporate"):
        (tmp_path / mode).mkdir()
    with running(tmp_path / "personal", mode="personal") as (web, client):
        sent = client.put("/api/v1/upload", params={"name": "планёрка.mp3"}, content=b"123")
        assert sent.status_code == 200 and sent.json()["file"] == "планёрка.mp3"
        assert (web.cfg.inbox / "планёрка.mp3").read_bytes() == b"123"
        # то же самое делает кнопка на странице
        page = client.put("/api/upload", params={"name": "с кнопки.mp3", "size": 3, "offset": 0}, content=b"123")
        assert page.json()["done"] and page.json()["file"] == "с кнопки.mp3"
        again = client.put("/api/v1/upload", params={"name": "планёрка.mp3"}, content=b"123")
        assert again.status_code == 409
        nameless = client.put("/api/v1/upload", content=b"123")
        assert nameless.status_code == 400 and "нужно имя файла" in nameless.json()["detail"]
        assert "администратор" not in nameless.json()["detail"]
        api = client.get("/api/v1").text
        assert "без него запись ложится в корень записей" in api and "`dir` и `name` обязательны" not in api
    with running(tmp_path / "corporate") as (web, _):
        admin = enter(web, "admin", ADMIN_PASSWORD)
        refused = admin.put("/api/v1/upload", params={"name": "планёрка.mp3"}, content=b"123")
        assert refused.status_code == 400 and "нужны папка и имя файла" in refused.json()["detail"]
        assert not (web.cfg.inbox / "планёрка.mp3").exists()
        assert "`dir` и `name` обязательны" in admin.get("/api/v1").text


def test_personal_mode_sends_the_login_pages_to_the_main_one(tmp_path):
    with running(tmp_path, mode="personal") as (web, client):
        for path in ("/login", "/set-password", "/login?next=%2Ftasks", "/login#request"):
            moved = client.get(path, follow_redirects=False)
            assert moved.status_code == 302 and moved.headers["location"] == "/", path
        assert client.get("/").status_code == 200


# ---------- отказ на неразобранное тело запроса — по-русски ----------

def test_unreadable_request_body_is_refused_in_russian(site):
    _, client = site
    json_type = {"Content-Type": "application/json"}
    # так тело уходит из PowerShell: кириллица в кодировке консоли, кавычки внутри строки потеряны
    for body in ('{"login": "админ", "password": "пароль"}'.encode("cp1251"), b"{login:admin,password:x}"):
        refused = client.post("/api/login", content=body, headers=json_type)
        assert refused.status_code in (400, 422), body
        detail = refused.json()["detail"]
        assert detail.startswith("тело запроса не разобрано: ожидается JSON в кодировке UTF-8") and "PowerShell" in detail
        assert "error parsing" not in refused.text and "JSON decode" not in refused.text
    # обычный отказ на нехватку поля остаётся как был
    assert client.post("/api/login", json={"login": "admin"}).status_code == 422


# ---------- справка: локальный адрес в навыке ----------

def test_me_tells_whether_the_service_address_is_set(tmp_path):
    for mode in ("unset", "set"):
        (tmp_path / mode).mkdir()
    with running(tmp_path / "unset") as (web, client):
        me = client.get("/api/me").json()
        assert me["url_set"] is False and me["url"] == "http://testserver"
        assert "http://testserver/skill" in client.get("/skill").text
    with running(tmp_path / "set", extra={"PUBLIC_URL": "http://192.0.2.10:8081"}) as (web, client):
        me = client.get("/api/me").json()
        assert me["url_set"] is True and me["url"] == "http://192.0.2.10:8081"
    help_page = (ROOT / "app" / "static" / "help.html").read_text(encoding="utf-8")
    assert 'id="ailocal"' in help_page and "me.url_set" in help_page and "PUBLIC_URL" in help_page


# ---------- образец настроек не отстаёт от кода, compose и инструкции ----------

def test_env_example_lists_every_setting_and_nothing_else():
    names = lambda text, pattern: set(re.findall(pattern, text, flags=re.M))
    example = names((ROOT / "install" / ".env.example").read_text(encoding="utf-8"), r"^#?([A-Z][A-Z0-9_]+)=")
    code = set()
    for path in (ROOT / "app").glob("*.py"):
        code |= names(path.read_text(encoding="utf-8"), r"(?:environ\.get|_env)\(\s*\"([A-Z][A-Z0-9_]+)\"")
    code -= {"APP_BUILD_DATE", "APP_COMMIT"}            # задаются в образе при сборке, не в .env
    code -= {"OPENROUTER_API_KEY"}                      # второе имя ключа — в образце упомянуто словами
    compose = set()
    for path in (ROOT / "compose.yml", ROOT / "install" / "compose.yml"):
        compose |= names(path.read_text(encoding="utf-8"), r"\$\{([A-Z][A-Z0-9_]+)")
    table = set()
    for row in re.findall(r"^\| ((?:`[A-Z][A-Z0-9_]+`(?:, )?)+) \|", (ROOT / "install" / "README.md").read_text(encoding="utf-8"),
                          flags=re.M):
        table |= set(re.findall(r"`([A-Z][A-Z0-9_]+)`", row))
    assert len(code) > 40 and len(compose) > 8 and len(table) > 50
    assert not (code | compose | table) - example, "нет в образце"
    assert not example - (code | compose | table), "в образце лишнее"
    assert not example - table, "нет в таблице настроек инструкции"


# ---------- словарь папки и записи: участники и сокращения ----------

PEOPLE_TEXT = "Ершова Лидия Марковна (Лида) — главный бухгалтер\n2. Громов Пётр Андреевич, начальник склада"
TERMS_TEXT = "КС-2 — акт о приёмке выполненных работ\nСКУД"
ERSHOVA = {"name": "Ершова Лидия Марковна", "role": "главный бухгалтер", "aka": ["Лида"]}
PLAN = "Проекты/планёрка.mp4"


def test_folder_admin_edits_the_dictionary_and_a_reader_only_reads_it(site):
    web, admin, ivanov, petrov, sidorov = tiers(site)
    # пусто, пока никто не вписал; читателю видно, править — хозяину папки
    empty = ivanov.get("/api/dictionary", params={"path": "Проекты"}).json()
    assert (empty["scope"], empty["path"], empty["people"], empty["terms"]) == ("folder", "Проекты", [], [])
    assert empty["can_edit"] is False and empty["inherited"] == {"people": [], "terms": []}
    assert empty["limits"] == {"people": 200, "terms": 300} and empty["common"] is False
    assert sidorov.get("/api/dictionary", params={"path": "Проекты"}).json()["can_edit"] is True

    saved = sidorov.post("/api/dictionary", json={"path": "Проекты", "people": PEOPLE_TEXT, "terms": TERMS_TEXT})
    assert saved.status_code == 200 and saved.json()["ok"]
    assert saved.json()["people"] == [ERSHOVA, {"name": "Громов Пётр Андреевич", "role": "начальник склада", "aka": []}]
    assert saved.json()["terms"] == [{"term": "КС-2", "meaning": "акт о приёмке выполненных работ"},
                                     {"term": "СКУД", "meaning": ""}]
    assert saved.json()["changed_by"] == "Сидоров" and saved.json()["changed_at"]
    # лежит в базе — вместе с правами
    assert [(r["scope"], r["path"]) for r in web.base.rows("SELECT scope, path FROM dictionaries")] == [("folders", "Проекты")]

    # читатель и читатель с загрузкой видят тот же список, но не правят
    for client in (ivanov, petrov):
        seen = client.get("/api/dictionary", params={"path": "Проекты"}).json()
        assert seen["people"][0] == ERSHOVA and seen["can_edit"] is False
        refused = client.post("/api/dictionary", json={"path": "Проекты", "people": "Чужой Человек"})
        assert refused.status_code == 403 and refused.json()["detail"] == web.NO_DICT_EDIT
    assert web.words.get("folders", "Проекты")["people"][0] == ERSHOVA

    # подпапка получает словарь папки выше; свой раздел меняется отдельно от другого
    sidorov.post("/api/dictionary", json={"path": "Проекты/Сметы", "terms": "кс-2 — акт\nТСД"})
    sub = ivanov.get("/api/dictionary", params={"path": "Проекты/Сметы"}).json()
    assert sub["people"] == [] and [t["term"] for t in sub["terms"]] == ["кс-2", "ТСД"]
    assert [(p["name"], p["path"]) for p in sub["inherited"]["people"]] == [
        ("Ершова Лидия Марковна", "Проекты"), ("Громов Пётр Андреевич", "Проекты")]
    assert web.words.effective("Проекты/Сметы/смета.mp3")["terms"][0] == {
        "term": "кс-2", "meaning": "акт", "scope": "folder", "path": "Проекты/Сметы"}           # ближний победил
    sidorov.post("/api/dictionary", json={"path": "Проекты/Сметы", "people": []})                # сокращения не тронуты
    assert len(web.words.get("folders", "Проекты/Сметы")["terms"]) == 2

    # чужая, несуществующая и служебная папки неотличимы; у корня словаря нет
    stranger = enter(web, "ivanov")
    assert stranger.get("/api/dictionary", params={"path": "Снабжение"}).status_code == 404
    for path in ("Нет такой", "../state", ".trash"):
        assert admin.get("/api/dictionary", params={"path": path}).status_code == 404, path
        assert admin.post("/api/dictionary", json={"path": path, "people": "Кто-то"}).status_code == 404, path
    assert admin.get("/api/dictionary").status_code == 400
    assert admin.get("/api/dictionary", params={"path": "Проекты", "file": PLAN}).status_code == 400
    assert admin.post("/api/dictionary", json={"path": "Проекты"}).status_code == 400            # менять нечего
    assert admin.post("/api/dictionary", json={"path": "Проекты", "rebuild": True}).status_code == 400
    too_many = admin.post("/api/dictionary", json={"path": "Проекты", "people": [f"Человек {n}" for n in range(201)]})
    assert too_many.status_code == 400 and "не больше 200 строк" in too_many.json()["detail"]
    assert len(web.words.get("folders", "Проекты")["people"]) == 2                               # отказ ничего не стёр
    assert site[1].get("/api/dictionary", params={"path": "Проекты"}).status_code == 401

    # журнал действий: кто, где, сколько строк — без самих имён
    events = [e for e in web.trail.read() if e["kind"] == "dictionary"]
    assert [(e["by"], e["folder"], e["people"], e["terms"]) for e in events] == [
        ("sidorov", "Проекты", 2, 2), ("sidorov", "Проекты/Сметы", 0, 2), ("sidorov", "Проекты/Сметы", 0, 2)]
    assert "Ершова" not in str(web.trail.read())                # в журнале действий — только счёт строк словаря


def test_record_dictionary_adds_to_the_folder_and_suggests_names_to_the_speakers_panel(site):
    web, admin, ivanov, petrov, sidorov = tiers(site)
    sidorov.post("/api/dictionary", json={"path": "Проекты", "people": PEOPLE_TEXT, "terms": TERMS_TEXT})
    own = sidorov.post("/api/dictionary", json={"file": PLAN, "people": "Юдина Анна Сергеевна — ведёт планёрку\n"
                                                                         "ершова лидия марковна — докладчик"})
    assert own.status_code == 200 and (own.json()["scope"], own.json()["path"]) == ("record", PLAN)
    assert [p["name"] for p in own.json()["people"]] == ["Юдина Анна Сергеевна", "ершова лидия марковна"]
    assert [p["name"] for p in own.json()["inherited"]["people"]] == ["Ершова Лидия Марковна", "Громов Пётр Андреевич"]
    assert own.json()["processed"] is True and own.json()["can_rebuild"] is True
    # в обработку идёт словарь записи поверх словаря папки
    words = web.service.words(PLAN)
    assert [(p["name"], p["role"]) for p in words["people"]] == [
        ("Юдина Анна Сергеевна", "ведёт планёрку"), ("ершова лидия марковна", "докладчик"),
        ("Громов Пётр Андреевич", "начальник склада")]
    assert [t["term"] for t in words["terms"]] == ["КС-2", "СКУД"]
    # панель говорящих получает эти ФИО для подсказки — и читателю тоже
    for client in (sidorov, ivanov):
        seen = client.get("/api/result", params={"file": PLAN, "kind": "summary"}).json()
        assert seen["roster"] == [{"name": "Юдина Анна Сергеевна", "role": "ведёт планёрку"},
                                  {"name": "ершова лидия марковна", "role": "докладчик"},
                                  {"name": "Громов Пётр Андреевич", "role": "начальник склада"}]
    reader = ivanov.get("/api/dictionary", params={"file": PLAN}).json()
    assert reader["can_edit"] is False and reader["can_rebuild"] is False and len(reader["people"]) == 2
    for client in (ivanov, petrov):                             # читатель и читатель с загрузкой записи не хозяева
        assert client.post("/api/dictionary", json={"file": PLAN, "people": "Чужой"}).status_code == 403
    assert enter(web, "ivanov").get("/api/dictionary", params={"file": "Снабжение/нет.mp4"}).status_code == 404

    # словарь переезжает вместе с записью и с папкой, а с удалённой записью уходит
    moved = sidorov.post("/api/record/move", json={"file": PLAN, "dir": "Проекты/Сметы"}).json()["file"]
    renamed = sidorov.post("/api/record/rename", json={"file": moved, "name": "смета"}).json()["file"]
    assert renamed == "Проекты/Сметы/смета.mp4" and web.words.get("records", PLAN)["people"] == []
    assert len(sidorov.get("/api/dictionary", params={"file": renamed}).json()["people"]) == 2
    assert admin.post("/api/admin/folders/rename", json={"dir": "Проекты", "name": "Стройка"}).status_code == 200
    after = admin.get("/api/dictionary", params={"file": "Стройка/Сметы/смета.mp4"}).json()
    assert len(after["people"]) == 2 and [p["path"] for p in after["inherited"]["people"]] == ["Стройка", "Стройка"]
    assert admin.get("/api/dictionary", params={"path": "Проекты"}).status_code == 404
    assert admin.post("/api/record/delete", json={"file": "Стройка/Сметы/смета.mp4"}).status_code == 200
    assert web.words.get("records", "Стройка/Сметы/смета.mp4")["people"] == []
    assert admin.post("/api/admin/folders/delete", json={"dir": "Стройка"}).status_code == 200
    assert web.base.rows("SELECT * FROM dictionaries") == []


def test_rebuild_with_the_new_dictionary_is_paid_and_never_recognizes_again(site):
    web, admin, ivanov, petrov, sidorov = tiers(site)
    own = voiced(web, f"{HOME}/ivanov/личная.mp4")
    # хозяин личной папки без права загрузки: словарь правит, но не пересобирает — и не наполовину
    refused = ivanov.post("/api/dictionary", json={"file": own, "people": "Юдина Анна", "rebuild": True})
    assert refused.status_code == 403 and refused.json()["detail"] == web.NO_REBUILD
    assert web.words.get("records", own)["people"] == [] and web.service.queue == []
    kept = ivanov.post("/api/dictionary", json={"file": own, "people": "Юдина Анна"})
    assert kept.status_code == 200 and kept.json()["can_rebuild"] is False and web.service.queue == []
    # протокол собран раньше словаря — страница говорит, что он с ним расходится
    assert kept.json()["dict_newer"] is True
    assert ivanov.get("/api/result", params={"file": own, "kind": "summary"}).json()["dict_newer"] is True
    assert sidorov.get("/api/result", params={"file": PLAN, "kind": "summary"}).json()["dict_newer"] is False

    # администратор папки сохраняет и пересобирает одним запросом; распознавание заново не заказывается
    done = sidorov.post("/api/dictionary", json={"file": PLAN, "terms": "КС-2", "rebuild": True})
    assert done.status_code == 200 and done.json()["rebuilding"] is True
    item, = web.service.queue
    assert (item.path, item.force, item.model, item.reason, item.by) == (PLAN, False, "", "rebuild", "sidorov")
    # пересборка без правки — тоже можно: словарь папки меняют отдельно
    web.service.queue.clear()
    assert sidorov.post("/api/dictionary", json={"file": PLAN, "rebuild": True}).status_code == 200
    assert [i.path for i in web.service.queue] == [PLAN]
    assert len([e for e in web.trail.read() if e["kind"] == "dictionary" and e.get("file") == PLAN]) == 1

    # только что загруженная запись: словарь можно вписать до обработки, пересобирать ещё нечего
    (web.cfg.inbox / "Проекты" / "новая.mp3").write_bytes(b"x")
    fresh = sidorov.post("/api/dictionary", json={"file": "Проекты/новая.mp3", "people": "Громов Пётр"})
    assert fresh.status_code == 200 and fresh.json()["processed"] is False and fresh.json()["can_rebuild"] is False
    assert web.service.words("Проекты/новая.mp3")["people"][0]["name"] == "Громов Пётр"
    assert sidorov.post("/api/dictionary", json={"file": "Проекты/новая.mp3", "rebuild": True}).status_code == 409
    for file in ("Проекты/нет такой.mp3", "Проекты/../Проекты/новая.mp3", "Проекты//новая.mp3", "Проекты/заметка.txt"):
        assert sidorov.post("/api/dictionary", json={"file": file, "people": "Кто-то"}).status_code == 404, file


def test_dictionary_is_read_and_written_through_the_api_with_the_owners_rights(site):
    web, admin, ivanov, petrov, sidorov = tiers(site)
    robot, _ = keyed(web, sidorov, "Cursor")
    reader, _ = keyed(web, ivanov, "Codex")
    body = {"people": ["Ершова Лидия Марковна (Лида) — главный бухгалтер", {"name": "Громов Пётр Андреевич", "role": "склад"}],
            "terms": [{"term": "КС-2", "meaning": "акт"}, "СКУД"]}
    put = robot.put("/api/v1/dictionary", params={"dir": "Проекты"}, json=body)
    assert put.status_code == 200 and put.json()["people"][0] == ERSHOVA and len(put.json()["terms"]) == 2
    # add=true дополняет: совпавший человек заменяется, остальные остаются; раздела нет в теле — не тронут
    added = robot.put("/api/v1/dictionary", params={"dir": "Проекты", "add": True},
                      json={"people": ["громов пётр андреевич — начальник склада", "Юдина Анна"]})
    assert [(p["name"], p["role"]) for p in added.json()["people"]] == [
        ("Ершова Лидия Марковна", "главный бухгалтер"), ("громов пётр андреевич", "начальник склада"), ("Юдина Анна", "")]
    assert len(added.json()["terms"]) == 2
    # без add раздел заменяется целиком
    assert [p["name"] for p in robot.put("/api/v1/dictionary", params={"dir": "Проекты"},
                                         json={"people": ["Юдина Анна"]}).json()["people"]] == ["Юдина Анна"]

    seen = reader.get("/api/v1/dictionary", params={"dir": "Проекты"})
    assert seen.status_code == 200 and seen.json()["can_edit"] is False and seen.json()["people"][0]["name"] == "Юдина Анна"
    assert reader.put("/api/v1/dictionary", params={"dir": "Проекты"}, json={"people": []}).status_code == 403
    assert reader.get("/api/v1/dictionary", params={"dir": "Снабжение"}).status_code == 404

    # словарь записи и пересборка — отдельным запросом без тела; читателю и то и другое закрыто
    assert robot.put("/api/v1/dictionary", params={"file": PLAN}, json={"people": ["Ершова Лидия Марковна"]}).status_code == 200
    record = robot.get("/api/v1/dictionary", params={"file": PLAN}).json()
    assert record["scope"] == "record" and record["can_rebuild"] is True and record["inherited"]["people"][0]["path"] == "Проекты"
    rebuilt = robot.put("/api/v1/dictionary", params={"file": PLAN, "rebuild": True})
    assert rebuilt.status_code == 200 and rebuilt.json()["rebuilding"] is True and web.service.queue[0].force is False
    assert reader.put("/api/v1/dictionary", params={"file": PLAN, "rebuild": True}).status_code == 403
    for params in ({}, {"dir": "Проекты", "file": PLAN}):
        assert robot.put("/api/v1/dictionary", params=params, json=body).status_code == 400
    assert robot.put("/api/v1/dictionary", params={"dir": "Проекты"}, json={"people": 5}).status_code == 400
    assert robot.put("/api/v1/dictionary", params={"dir": "Проекты"}).status_code == 400             # менять нечего

    # в журнале действий видно, что правил ключ
    events = [e for e in web.trail.read() if e["kind"] == "dictionary"]
    assert len(events) == 4 and {(e["by"], e["via"], e["key"], e["key_kind"]) for e in events} == {
        ("sidorov", "key", "Cursor", "plain")}
    # навык знает про словарь и требует согласия на пересборку
    skill = site[1].get("/skill/SKILL.md").text
    assert "/api/v1/dictionary" in skill and "добавь в словарь папки участников из этого списка" in skill
    assert "## 12" in site[1].get("/skill/CHANGELOG.md").text


def test_personal_mode_has_dictionaries_without_any_rights(tmp_path):
    with running(tmp_path, mode="personal") as (web, client):
        (web.cfg.inbox / "Планёрки").mkdir()
        saved = client.post("/api/dictionary", json={"path": "Планёрки", "people": PEOPLE_TEXT})
        assert saved.status_code == 200 and saved.json()["can_edit"] is True and saved.json()["changed_by"] == ""
        recorded(web, "Планёрки/вкс.mp4")
        own = client.put("/api/v1/dictionary", params={"file": "Планёрки/вкс.mp4", "rebuild": True}, json={"terms": ["КС-2"]})
        assert own.status_code == 200 and own.json()["can_rebuild"] is True and web.service.queue[0].reason == "rebuild"
        assert client.get("/api/browse", params={"path": "Планёрки"}).json()["owns"] is True


# ---------- сверка описания API: единые имена, отказы и умолчания ----------

def test_saving_a_user_changes_only_what_the_request_names(site):
    """Правка существующего пользователя без поля это поле не трогает: раньше запрос без admin
    снимал роль администратора, а без name — заменял ФИО логином."""
    web, admin, _, _ = staffed(site)
    made = admin.post("/api/admin/users/save", json={"login": "sidorov", "name": "Сидоров Пётр", "password": PASSWORD,
                                                     "admin": True, "email": "sidorov@example.test", "home_upload": True})
    assert made.status_code == 200
    kept = lambda: {k: web.people.users["sidorov"][k] for k in ("name", "admin", "email", "home_upload")}
    full = {"name": "Сидоров Пётр", "admin": True, "email": "sidorov@example.test", "home_upload": True}
    assert kept() == full
    assert admin.post("/api/admin/users/save", json={"login": "sidorov"}).status_code == 200
    assert kept() == full and enter(web, "sidorov")                  # и пароль прежний
    assert admin.post("/api/admin/users/save", json={"login": "sidorov", "name": "Сидоров П. А."}).status_code == 200
    assert kept() == {**full, "name": "Сидоров П. А."}
    assert admin.post("/api/admin/users/save", json={"login": "sidorov", "admin": False}).status_code == 200
    assert kept() == {**full, "name": "Сидоров П. А.", "admin": False}
    assert admin.post("/api/admin/users/save", json={"login": "sidorov", "home_upload": False, "email": ""}).status_code == 200
    assert kept() == {"name": "Сидоров П. А.", "admin": False, "email": "", "home_upload": False}
    # у нового умолчания прежние: ФИО — логин, не администратор, без почты и без загрузки
    assert admin.post("/api/admin/users/save", json={"login": "novikov", "password": PASSWORD}).status_code == 200
    fresh = web.people.users["novikov"]
    assert (fresh["name"], fresh["admin"], fresh["email"], fresh["home_upload"]) == ("novikov", False, "", False)


def test_refused_parameters_are_named_in_russian_like_any_other_refusal(site):
    """Отказ проверки параметров — {"detail": "строка"} по-русски: какой параметр или поле и что
    с ним не так. Страницы показывают detail как есть."""
    web, admin, ivanov, _ = staffed(site)
    for answer, text in (
            (ivanov.get("/api/v1/record"), "не указан параметр file"),
            (ivanov.get("/api/v1/tree", params={"depth": "глубоко"}), "параметр depth должен быть целым числом"),
            (ivanov.get("/api/v1/tasks", params={"open": "может быть"}), "параметр open должен быть true или false"),
            (ivanov.post("/api/v1/rename"), "не указан параметр file; не указан параметр name"),
            (admin.post("/api/access", json={"read": "tree"}), "не указано поле dir; не указано поле login"),
            (admin.post("/api/access", json={"dir": "Проекты", "login": 5}), "поле login должно быть строкой"),
            (admin.post("/api/admin/users/save", json={"login": "sidorov", "admin": "да нет"}),
             "поле admin должно быть true или false"),
            (admin.post("/api/access", json=["Проекты"]), "тело запроса должно быть объектом JSON"),
            (admin.post("/api/access"), "нет тела запроса: ожидается JSON")):
        assert answer.status_code == 422 and answer.json() == {"detail": text}, answer.text
    broken = admin.post("/api/access", content=b"{not json", headers={"Content-Type": "application/json"})
    assert broken.status_code == 422 and broken.json()["detail"] == web.BODY_NOT_PARSED


def test_folder_is_dir_and_record_is_file_in_every_request_and_old_names_are_not_taken(site):
    web, admin, ivanov, _ = staffed(site)
    home = f"{HOME}/ivanov"
    recorded(web, f"{home}/вкс.mp4")
    assert ivanov.post("/api/folder", json={"dir": f"{home}/Архив"}).json() == {"ok": True, "path": f"{home}/Архив"}
    moved = ivanov.post("/api/v1/move", params={"file": f"{home}/вкс.mp4", "dir": f"{home}/Архив"})
    assert moved.json() == {"ok": True, "file": f"{home}/Архив/вкс.mp4"}
    admin.post("/api/folder", json={"dir": "Проекты"})
    assert admin.get("/api/access", params={"dir": "Проекты"}).json()["path"] == "Проекты"
    assert admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "tree"}).status_code == 200
    assert admin.post("/api/admin/folders/rename", json={"dir": "Проекты", "name": "Стройка"}).json()["path"] == "Стройка"
    assert admin.post("/api/admin/run", json={"file": f"{home}/Архив/вкс.mp4"}).json()["queued"] == f"{home}/Архив/вкс.mp4"
    # прежние имена не принимаются: отказ называет нужное
    for answer, text in (
            (ivanov.post("/api/v1/move", params={"file": f"{home}/Архив/вкс.mp4", "folder": home}), "не указан параметр dir"),
            (ivanov.post("/api/record/move", json={"file": f"{home}/Архив/вкс.mp4", "folder": home}), "не указано поле dir"),
            (ivanov.post("/api/folder", json={"path": f"{home}/Ещё"}), "не указано поле dir"),
            (admin.get("/api/access", params={"path": "Стройка"}), "не указан параметр dir"),
            (admin.post("/api/access", json={"path": "Стройка", "login": "ivanov", "read": "none"}), "не указано поле dir"),
            (admin.post("/api/admin/folders/rename", json={"path": "Стройка", "name": "Проекты"}), "не указано поле dir"),
            (admin.post("/api/admin/folders/delete", json={"path": "Стройка"}), "не указано поле dir"),
            (admin.post("/api/admin/run", json={"path": f"{home}/Архив/вкс.mp4"}), "не указано поле file")):
        assert answer.status_code == 422 and answer.json() == {"detail": text}, answer.text
    assert (web.cfg.inbox / "Стройка").is_dir() and not (web.cfg.inbox / home / "Ещё").exists()


def test_unknown_or_closed_folder_is_404_everywhere_and_an_empty_one_is_an_empty_answer(site):
    """Папки нет или она человеку закрыта — один и тот же отказ во всех методах, принимающих
    папку; раньше tree и dictionary отвечали 404, а search, tasks и records — пустым ответом."""
    web, admin, ivanov, _ = filled(site)
    admin.post("/api/folder", json={"dir": "Проекты/Пусто"})
    robot, _ = keyed(web, ivanov)
    asks = (("/api/v1/tree", {}), ("/api/v1/records", {}), ("/api/v1/search", {"q": "бюджет"}), ("/api/v1/tasks", {}),
            ("/api/v1/tasks.csv", {}), ("/api/v1/dictionary", {}))
    for folder in ("Нет такой", "Снабжение", "Проекты/Нет такой", f"{HOME}/petrov"):       # нет, закрыта, нет, чужая
        for path, extra in asks:
            refused = robot.get(path, params={"dir": folder, **extra})
            assert refused.status_code == 404, (path, folder, refused.text)
            assert refused.json() == {"detail": "такой папки нет или она вам не открыта"}, (path, folder)
    for path, extra in asks:
        assert robot.get(path, params={"dir": "Проекты/Пусто", **extra}).status_code == 200, path
    assert robot.get("/api/v1/records", params={"dir": "Проекты/Пусто"}).json()["records"] == []
    assert robot.get("/api/v1/search", params={"dir": "Проекты/Пусто", "q": "бюджет"}).json()["found"] == []
    assert robot.get("/api/v1/tasks", params={"dir": "Проекты/Пусто"}).json()["tasks"] == []
    empty = robot.get("/api/v1/tree", params={"dir": "Проекты/Пусто"}).json()
    assert empty["records"] == [] and empty["folders"] == []
    # тому, кому папка открыта, она отвечает; папка выше открытой — путь к ней, а не отказ
    assert len(admin.get("/api/v1/tasks", params={"dir": "Снабжение"}).json()["tasks"]) == 2
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "none", "upload": "none"})
    admin.post("/api/access", json={"dir": "Проекты/Сметы/Архив", "login": "ivanov", "read": "only"})
    assert [f["path"] for f in robot.get("/api/v1/tree", params={"dir": "Проекты"}).json()["folders"]] == ["Проекты/Сметы"]
    assert robot.get("/api/v1/tree", params={"dir": "Проекты/Пусто"}).status_code == 404
    # перенос в папку, которой нет, — тот же текст
    gone = admin.post("/api/v1/move", params={"file": "Проекты/вкс.mp4", "dir": "Нет такой"})
    assert gone.status_code == 404 and gone.json()["detail"] == "такой папки нет или она вам не открыта"
    # глубина дерева вне 0…8 — не отказ: приводится к границе
    assert "records_inside" in robot.get("/api/v1/tree", params={"depth": -3}).json()["folders"][0]
    assert robot.get("/api/v1/tree", params={"depth": 99}).status_code == 200


def test_admin_status_holds_no_disk_paths(site, tmp_path):
    web, client = site
    recorded(web, "Проекты/вкс.mp4")
    assert web.service.ledger.get("Проекты/вкс.mp4")["output"].startswith(str(tmp_path))
    summary = client.get("/api/admin/status", auth=("admin", ADMIN_PASSWORD))
    body = summary.json()
    assert "inbox" not in body["config"] and "outbox" not in body["config"]
    assert body["history"][0]["file"] == "Проекты/вкс.mp4" and "output" not in body["history"][0]
    # журнал работы в этой проверке не участвует: в нём сервис сам пишет, куда разложил шаблоны
    assert str(tmp_path) not in json.dumps({k: v for k, v in body.items() if k != "log"}, ensure_ascii=False)
    page = client.get("/admin").text
    assert "c.inbox" not in page and "c.outbox" not in page


def test_costs_are_for_the_service_admin_only(site):
    """Расходы — только администратору сервиса: встроенному admin и назначенному им, самому
    (кукой или логином с паролем) и ключу «с управлением». Обычный ключ администратора,
    администратор папки, читатель и любые их ключи получают 403 со словами."""
    web, admin, ivanov, petrov = staffed(site)
    saved = admin.post("/api/admin/users/save", json={"login": "orlova", "name": "Орлова", "password": PASSWORD,
                                                      "admin": True})
    assert saved.status_code == 200
    orlova = enter(web, "orlova")
    admin.post("/api/folder", json={"dir": "Проекты"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "tree", "upload": "tree",
                                    "manage": "tree"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "petrov", "read": "tree"})
    for by, file, cost in (("ivanov", f"{HOME}/ivanov/вкс.mp4", 0.25), ("admin", "Проекты/итоги.mp4", 0.5),
                           ("petrov", f"{HOME}/petrov/личное.mp4", 1.0)):
        web.trail.write("run", "", file=file, uploaded_by=by, reason="new", status="ok", audio_sec=600, cost=cost)

    def basic(login, password=PASSWORD):
        client = TestClient(web.app)
        client.auth = (login, password)
        return client

    allowed = {"встроенный admin": admin, "admin логином и паролем": basic("admin", ADMIN_PASSWORD),
               "ключ admin с управлением": keyed(web, admin, "с управлением", 30, manage=True)[0],
               "назначенный администратор": orlova, "назначенный логином и паролем": basic("orlova"),
               "ключ назначенного с управлением": keyed(web, orlova, "с управлением", 30, manage=True)[0]}
    for who, client in allowed.items():
        full = client.get("/api/v1/costs").json()
        assert "scope" not in full and full["total"]["runs"] == 3 and len(full["users"]) == 3, who
        assert full["total"]["cost"] == {"USD": 1.75} and full["names"]["ivanov"] == "Иванов", who
        assert client.get("/api/admin/report").json() == full, who
        assert len(csv_tables(client.get("/api/admin/report.csv").content.decode("utf-8-sig"))["Прогоны"]) == 4, who
        assert client.get("/api/admin/report.xlsx").content.startswith(b"PK"), who

    by_key = {"обычный ключ admin": keyed(web, admin, "обычный")[0],
              "обычный ключ назначенного администратора": keyed(web, orlova, "обычный")[0]}
    others = {"администратор папки": ivanov, "администратор папки логином и паролем": basic("ivanov"),
              "ключ администратора папки": keyed(web, ivanov)[0],
              "читатель": petrov, "читатель логином и паролем": basic("petrov"), "ключ читателя": keyed(web, petrov)[0]}
    for group, detail in ((by_key, web.BY_KEY_ONLY_READS), (others, web.NO_COSTS)):
        for who, client in group.items():
            for path in COSTS:
                refused = client.get(path, params={"bucket": "month"})
                assert refused.status_code == 403 and refused.json() == {"detail": detail}, f"{who}: {path}"
    assert web.NO_COSTS == "расходы видит только администратор сервиса"
    assert TestClient(web.app).get("/api/v1/costs").status_code == 401
    # ключа «с управлением» не-администратору не выпустить: другого пути к расходам у него нет
    assert ivanov.post("/api/keys", json={"name": "с управлением", "days": 30, "manage": True}).status_code == 403
    assert admin.get("/api/v1/costs", params={"bucket": "hour"}).status_code == 400


def test_costs_are_open_in_personal_mode(tmp_path):
    """Личный режим: входа нет, владелец один — расходы открыты, как и всё остальное."""
    with running(tmp_path, mode="personal") as (web, client):
        web.trail.write("run", "", file="вкс.mp4", reason="new", status="ok", audio_sec=600, cost=0.25)
        for path in COSTS:
            assert client.get(path).status_code == 200, path
        report = client.get("/api/v1/costs").json()
        assert "scope" not in report and report["total"]["cost"] == {"USD": 0.25}


def money_in(value, where=""):
    """Где в ответе названы деньги: поля с суммами и суммы в тексте."""
    found = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if "cost" in key or "price" in key or key in ("totals", "paid_audio"):
                found.append(f"{where}.{key}")
            found += money_in(inner, f"{where}.{key}")
    elif isinstance(value, list):
        for inner in value:
            found += money_in(inner, where + "[]")
    elif isinstance(value, str) and re.search(r"\d\s?[$₽]|\b(USD|RUB)\b|cost", value):
        found.append(f"{where}: {value[:60]}")
    return found


def test_nobody_but_the_service_admin_sees_what_a_record_cost(site):
    """Суммы не утекают мимо отчёта: всё, что отвечает пользователю, администратору папки и их
    ключам, — запись, списки, дерево, состояние, файлы для человека, «кто я» — и письмо о
    готовности стоимости обработки не называют. У записи в журнале обработки она при этом есть."""
    web, admin, ivanov, petrov = staffed(site)
    admin.post("/api/folder", json={"dir": "Проекты"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "tree", "upload": "tree",
                                    "manage": "tree"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "petrov", "read": "tree"})
    file = "Проекты/вкс.mp4"
    voiced(web, file)
    web.service.ledger.put({**web.service.ledger.get(file), "title": "Итоги квартала", "speech_model": "elevenlabs/scribe-v2",
                            "cost": {"RUB": 12.5, "USD": 0.25},
                            "cost_details": [{"stage": "speech", "currency": "RUB", "cost": 12.5}],
                            "cost_models": [{"stage": "speech", "model": "elevenlabs/scribe-v2", "currency": "RUB",
                                             "cost": 12.5}],
                            "cost_providers": [{"provider": "RouterAI", "currency": "RUB", "cost": 12.5}]})
    record = web.service.ledger.get(file)
    web.trail.run(record, "ivanov", "manual")
    assert record["runs"] == 2 and record["cost_total"] == {"RUB": 12.5, "USD": 0.5}
    # администратору суммы видны — и проверка их ловит
    assert money_in(admin.get("/api/admin/status").json()) and money_in(admin.get("/api/v1/costs").json())

    asked = {"/api/v1/record": {"file": file, "transcript": "true"}, "/api/v1/status": {"file": file},
             "/api/v1/tree": {"depth": 3}, "/api/v1/search": {"q": "бюджет"}, "/api/v1/speakers": {"file": file},
             "/api/v1/dictionary": {"file": file}, "/api/v1/record.html": {"file": file},
             "/api/v1/record.docx": {"file": file}, "/api/v1/record.pdf": {"file": file},
             "/api/browse": {"path": "Проекты"}, "/api/result": {"file": file}, "/api/map": {"file": file},
             "/api/export": {"file": file, "format": "docx"}, "/api/search": {"q": "бюджет"},
             "/api/access": {"dir": "Проекты"}, "/api/dictionary": {"file": file}, "/api/audio": {"file": file},
             "/api/record/recipients": {"file": file}, "/api/record/folders": {"file": file}}
    callers = {"администратор папки": ivanov, "читатель": petrov,
               "ключ администратора папки": keyed(web, ivanov)[0], "ключ читателя": keyed(web, petrov)[0]}
    for who, client in callers.items():
        answered = set()
        for method, path in api_routes(web):
            if method != "GET" or path in ("/api/v1/openapi.json", "/api/v1"):
                continue                # описание API одно на всех: в нём пример ответа с суммами, а не суммы
            answer = client.get(path, params=asked.get(path))
            if answer.status_code != 200:
                continue
            answered.add(path)
            kind = answer.headers["content-type"]
            body = answer.json() if "json" in kind else answer.text if kind.startswith("text/") else None
            if path == "/api/v1/record.docx":
                body = zipfile.ZipFile(io.BytesIO(answer.content)).read("word/document.xml").decode("utf-8")
            assert not money_in(body), f"{who}: {path}: {money_in(body)}"
        assert answered >= {"/api/v1/record", "/api/v1/status", "/api/v1/records", "/api/v1/tree", "/api/v1/whoami",
                            "/api/v1/record.html", "/api/v1/record.docx", "/api/v1/record.pdf", "/api/v1/search", "/api/v1/tasks", "/api/v1/speakers",
                            "/api/browse", "/api/result", "/api/search", "/api/me"}, who
        assert answered.isdisjoint(COSTS) and "/api/admin/status" not in answered, who
        everything = client.get("/api/browse", params={"flat": "true"}).json()
        assert everything["files"][0]["runs"] == 2 and not money_in(everything), who
        page = client.get("/api/v1/record.html", params={"file": file}).text
        assert "Решили" in page or "Протокол" in page, who          # страница отдана, а не пустышка
    assert ivanov.get("/api/v1/record", params={"file": file}).json()["title"] == "Итоги квартала"

    # письмо о готовности — автору загрузки: суммы нет ни в одном его поле
    letter = notify.about(record, "## 1. Кратко\n\n- Решили.\n", "http://podyachiy.test")
    assert letter and not money_in(letter)
    # «Расходы» в меню — только при me.admin; остальные страницы отчёт не спрашивают и сумм не рисуют
    assert ivanov.get("/api/me").json()["admin"] is False and petrov.get("/api/me").json()["admin"] is False
    static = ROOT / "app" / "static"
    menu = (static / "common.js").read_text(encoding="utf-8")
    assert re.search(r'\(me && me\.admin \? `<h3>Управление</h3>`[^;]*manage\("costs", "Расходы", "coin"\) \+\s*manage\("templates", "Настройки", "gear", "", \["brand"\]\) : ""\);', menu)
    assert menu.count('"costs"') == 1
    for name in ("index.html", "tasks.html", "about.html"):
        text = (static / name).read_text(encoding="utf-8")
        assert "/api/admin/" not in text.replace("/api/admin/names", "") and "money(" not in text, name
        assert "#costs" not in text, name


def test_broken_streamed_upload_is_refused_in_words(site, monkeypatch):
    """Соединение оборвалось посреди файла: отказ — как у остальных, с текстом, а обрывок стёрт."""
    web, admin, ivanov, _ = staffed(site)

    def cut(*_):
        raise web.ClientDisconnect()

    monkeypatch.setattr(web.upload, "stream_finish", cut)
    refused = ivanov.put("/api/v1/upload", params={"dir": f"{HOME}/ivanov", "name": "вкс.mp4"}, content=b"video")
    assert refused.status_code == 400 and refused.json()["detail"].startswith("загрузка оборвалась: ")
    assert list((web.cfg.inbox / upload.PARTS_DIR).glob("*.part")) == []
    assert not (web.cfg.inbox / HOME / "ivanov" / "вкс.mp4").exists()


def test_results_trash_is_emptied_even_when_the_inbox_is_read_only(tmp_path, monkeypatch):
    """Входная папка только для чтения: загрузки нет, но корзина результатов чистится и при
    запуске, и раз в сутки; почему не чистится другая корзина — одной строкой, а не каждый раз."""
    long_ago = time.time() - (folders.TRASH_DAYS + 1) * 86400
    olds = [tmp_path / side / folders.TRASH / "20260101-000000" for side in ("inbox", "outbox")]
    for old in olds:
        old.mkdir(parents=True)
        os.utime(old, (long_ago, long_ago))
    monkeypatch.setattr(upload, "writable", lambda inbox: False)
    real = folders.shutil.rmtree

    def rmtree(path):
        if tmp_path / "inbox" in path.parents:
            raise OSError(30, "Read-only file system")
        real(path)

    monkeypatch.setattr(folders.shutil, "rmtree", rmtree)
    with running(tmp_path) as (web, client):
        assert not web.upload_ready and client.get("/api/status").status_code == 200
        assert olds[0].is_dir() and not olds[1].exists()             # чистка при запуске дошла до корзины результатов
        why = [line for line in web.log.tail() if "не чистится" in line]
        assert len(why) == 1 and why[0].endswith("корзина входной папки не чистится: Read-only file system")
        again = tmp_path / "outbox" / folders.TRASH / "20260102-000000"
        again.mkdir()
        os.utime(again, (long_ago, long_ago))
        web._clean()
        web._clean()
        assert not again.exists()
        assert len([line for line in web.log.tail() if "не чистится" in line]) == 1      # причина прежняя — строка одна
        monkeypatch.setattr(folders.shutil, "rmtree", real)
        web._clean()
        assert not olds[0].exists()
        calls = []
        monkeypatch.setattr(web, "_clean", lambda: calls.append(1))
        monkeypatch.setattr(web, "CLEAN_EVERY_SEC", 0.01)
        with TestClient(web.app):               # перезапуск: чистка при запуске и дальше по кругу, а не один раз
            time.sleep(0.3)
        assert len(calls) > 2


def test_pages_keep_the_order_protocol_map_transcript_and_one_download_group():
    """Порядок на страницах, который сервер не проверяет: «Карта» в окне записи — вкладкой между
    протоколом и расшифровкой; в «Расходах» — одна группа «Скачать отчёт целиком» из трёх форматов."""
    static = ROOT / "app" / "static"
    common = (static / "common.js").read_text(encoding="utf-8")
    kinds = re.search(r"const KINDS = \[(.*?)\];", common, re.S).group(1)
    assert re.findall(r'\["(\w+)"', kinds) == ["summary", "transcript", "srt"]
    tabs = common[common.index('<div class="tabs" role="tablist">${KINDS.map'):]
    tabs = tabs[:tabs.index("</div>")]
    assert 'k === "summary" && ready && viewData && viewData.has_map ? `<a class="tablink"' in tabs and ">Карта</a>" in tabs
    assert common.count(">Карта</a>") == 1 and 'target="_blank"' not in tabs      # возврат — «назад» и кнопка на карте
    admin = (static / "admin.html").read_text(encoding="utf-8")
    group = admin[admin.index('<label id="filelabel">Скачать отчёт целиком</label>'):admin.index('id="costprint"')]
    assert re.findall(r"/api/admin/report\.(\w+)\?\$\{file\}", group) == ["xlsx", "csv"]
    assert admin.count("/api/admin/report.") == 2 and "Скачать CSV" not in admin
    assert 'new URLSearchParams({ start: cost.start, end: cost.end, bucket: cost.bucket })' in admin


# ---------- база и резервная копия на странице «Обработка» ----------

def test_admin_sees_the_base_and_the_last_backup_and_a_warning_about_a_network_folder(site, tmp_path):
    web, admin, ivanov, _ = staffed(site)
    stored = admin.get("/api/admin/status").json()["storage"]
    assert stored["schema"] == db.SCHEMA_VERSION and stored["bytes"] > 0 and stored["backup"] is None
    assert stored["kind"] in ("local", "desktop", "network") and stored["warnings"] == []
    assert ivanov.get("/api/admin/status").status_code == 403
    # копию делает отдельная команда рядом с работающим сервисом — страница узнаёт о ней из базы
    cfg = dataclasses.replace(web.cfg, backup_dir=tmp_path / "backup")
    cfg.backup_dir.mkdir()
    made = backup.create(cfg, with_inbox=False, kept=lambda path: True, say=lambda line: None)
    noted = admin.get("/api/admin/status").json()["storage"]["backup"]
    assert noted["file"] == made["archive"].rsplit("/", 1)[-1] and noted["inbox"] is False and noted["skipped"] == 0
    assert admin.get("/api/admin/journal", params={"kind": "backup"}).json()["events"][0]["file"] == noted["file"]
    assert backup.verify(made["archive"], lambda line: None)["base"]["counts"]["users"] == 2
    # сетевая папка под базой — предупреждение на странице: без путей сервера, с тем, что делать
    web.storage_warnings[:] = migrate.storage_warnings(web.cfg, {"type": "cifs", "source": "//server/share", "kind": db.NETWORK})
    warning, = admin.get("/api/admin/status").json()["storage"]["warnings"]
    assert "сетевой папке (cifs)" in warning and "server" not in warning and "локальный диск" in warning
    page = (ROOT / "app" / "static" / "admin.html").read_text(encoding="utf-8")
    assert 'id="storagewarn"' in page and 'role="alert"' in page and "резервная копия не делалась" in page
