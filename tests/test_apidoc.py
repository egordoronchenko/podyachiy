"""Описание внешнего API: openapi.json, страница «API» и текст для модели — из одного источника
(app/apidoc.py), и этот источник не расходится с кодом.

Проверяется на сервисе, поднятом как в tests/test_web.py: перечень методов — против маршрутов
приложения и списков уровней доступа, параметры — против самих маршрутов, примеры запросов —
настоящими запросами к тестовым данным, поля примеров ответов — против настоящих ответов,
тексты отказов — против строк в коде. Платные вызовы доходят только до постановки в очередь:
обработчик в тесте заданий не берёт, поставщик — закрытый порт.
"""
import ast
import json
import re

import pytest

from app import apidoc
from tests import test_web as T
from tests.test_web import ADMIN_PASSWORD, ROOT, enter, keyed, running

MEETING = "Проекты/планёрка.mp4"
# Вне /api/v1 и управления, но описано: без них не обойтись при переносе записи и раздаче прав.
ALSO_OUTSIDE = {("GET", "/api/record/folders"), ("POST", "/api/folder")}
PAID = {"put-upload", "post-speakers", "put-dictionary", "post-admin-run"}
# Примеры идут в порядке описания; этим нужна запись, которую загружает пример upload.
AFTER_UPLOAD = ["post-rename", "post-move"]


def operations(spec):
    return [(method.upper(), path, item) for path, methods in spec["paths"].items() for method, item in methods.items()]


@pytest.fixture
def stand(tmp_path):
    """Сервис с данными, на которых написаны примеры описания: папки «Проекты» и «Проекты/Архив»,
    пользователь ivanov, обработанная запись с поручениями и двумя голосами, расход, две заявки
    на доступ. Отдаёт модуль, клиента без входа и двух клиентов администратора: с обычным ключом
    и с ключом «с управлением»."""
    with running(tmp_path) as (web, guest):
        T.seed(web)
        web.service._take = lambda: None            # очередь никто не разбирает: платное до поставщика не дойдёт
        admin = enter(web, "admin", ADMIN_PASSWORD)
        admin.post("/api/admin/users/save", json={"login": "ivanov", "name": "Иванов Иван", "password": T.PASSWORD})
        for path in ("Проекты", "Проекты/Архив"):
            admin.post("/api/folder", json={"dir": path})
        admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "tree"})
        T.recorded(web, MEETING, by="ivanov")
        out = web.cfg.outbox / "Проекты" / "планёрка"
        (out / "summary" / "планёрка.md").write_text(T.TASKS_MD, encoding="utf-8")
        (out / "transcript").mkdir()
        (out / "transcript" / "планёрка.json").write_text(json.dumps({"cuts": [], "segments": [
            {"start": 5, "end": 9, "speaker": "S1", "text": "Коллеги, добрый день, начинаем планёрку."},
            {"start": 10, "end": 20, "speaker": "S2", "text": "Спасибо. По складу: бюджет согласован, стеллажи привезут."}]}),
            encoding="utf-8")
        web.service.ledger.put({**web.service.ledger.get(MEETING), "title": "Планёрка по складу",
                                "audio_duration_sec": 600, "record_date": "2026-03-12"})
        for login, name in (("orlova", "Орлова Анна"), ("gromov", "Громов Пётр")):
            assert guest.post("/api/request", json={"name": name, "email": "", "login": login}).status_code == 200
        plain, _ = keyed(web, admin, "обычный")
        manage, _ = keyed(web, admin, "с управлением", 30, manage=True)
        yield web, guest, plain, manage


# ---------- что отдаётся ----------

def test_openapi_is_served_without_login_and_holds_no_secrets(stand, tmp_path):
    web, guest, _, _ = stand
    answer = guest.get("/api/v1/openapi.json")
    assert answer.status_code == 200 and answer.headers["content-type"] == "application/json"
    spec = answer.json()
    assert spec["openapi"].startswith("3.1") and spec["servers"] == [{"url": "http://testserver"}]
    assert spec["info"]["version"] == (ROOT / "VERSION").read_text().strip()
    # ни данных, ни настроек установки: только адрес сервиса и описание запросов
    for secret in (ADMIN_PASSWORD, "sk-test-not-a-key", str(tmp_path), "Громов", "stg_" + "A"):
        assert secret not in answer.text, secret
    # встроенное описание FastAPI по-прежнему выключено
    for path in T.FASTAPI_DOCS:
        assert guest.get(path).status_code == 404, path


def test_openapi_takes_the_address_from_public_url(tmp_path):
    with running(tmp_path, extra={"PUBLIC_URL": "https://podyachiy.example/"}) as (_, client):
        answer = client.get("/api/v1/openapi.json", headers={"Host": "localhost:8081"})
        spec = answer.json()
        assert spec["servers"] == [{"url": "https://podyachiy.example"}] and "localhost" not in answer.text
        sample = spec["paths"]["/api/v1/tree"]["get"]["x-codeSamples"][0]["source"]
        assert '"https://podyachiy.example/api/v1/tree"' in sample


def test_openapi_is_structurally_valid(stand):
    """Проверка формы без внешних сервисов: обязательные части OpenAPI 3.1, ссылки на схемы,
    уникальные имена методов, у параметров — место, схема и описание."""
    _, guest, _, _ = stand
    spec = guest.get("/api/v1/openapi.json").json()
    assert set(spec) >= {"openapi", "info", "servers", "tags", "paths", "components", "security"}
    assert spec["info"]["title"] and len(spec["info"]["description"]) > 1000
    tags = [t["name"] for t in spec["tags"]]
    assert tags == list(apidoc.TAGS) and all(t["description"] for t in spec["tags"])
    schemes = spec["components"]["securitySchemes"]
    assert {(s["type"], s["scheme"]) for s in schemes.values()} == {("http", "bearer"), ("http", "basic")}
    assert all(set(need) <= set(schemes) for need in spec["security"])

    refs = set(re.findall(r'"\$ref": "#/components/schemas/(\w+)"', json.dumps(spec)))
    assert refs == set(spec["components"]["schemas"])        # все ссылки ведут к схеме, лишних схем нет

    ids = [item["operationId"] for _, _, item in operations(spec)]
    assert len(ids) == len(set(ids)) > 40 and all(re.fullmatch(r"[a-z]+(-[a-z0-9]+)+", i) for i in ids)
    for method, path, item in operations(spec):
        where = f"{method} {path}"
        assert path.startswith("/api/") and method in ("GET", "POST", "PUT"), where
        assert item["tags"][0] in tags and len(item["tags"]) == 1, where
        for p in item.get("parameters", []):
            assert p["in"] == "query" and p["schema"] and p["description"], f"{where}: {p['name']}"
            assert isinstance(p["required"], bool) and "title" not in p["schema"], where
        for code, answer in item["responses"].items():
            assert code.isdigit() and answer["description"] and answer["content"], f"{where}: {code}"


def test_every_method_has_words_rights_and_examples(stand):
    _, guest, _, _ = stand
    spec = guest.get("/api/v1/openapi.json").json()
    for method, path, item in operations(spec):
        where = f"{method} {path}"
        assert len(item["summary"]) > 5 and not item["summary"].endswith("."), where
        assert len(item["description"]) > 40, where
        assert item["x-rights"]["level"] in spec["x-rights-levels"] and item["x-rights"]["text"], where
        assert item["x-key"] in spec["x-key-kinds"] and isinstance(item["x-paid"], bool), where
        assert bool(item.get("x-paid-when")) == item["x-paid"], where
        (media, good), = item["responses"]["200"]["content"].items()
        assert good["example"] not in (None, "", {}), where
        if media == "application/json":
            # у каждого поля примера есть описание, и наоборот
            assert set(good["schema"]["properties"]) == set(good["example"]), where
            assert all(p["description"] for p in good["schema"]["properties"].values()), where
        bash, shell = item["x-codeSamples"]
        assert (bash["lang"], shell["lang"]) == ("bash", "powershell") and path in bash["source"], where
        assert bash["source"].startswith("curl -sS ") and "curl.exe -sS " in shell["source"], where
        # кириллица в команде PowerShell — только внутри EscapeDataString и в файле тела запроса
        assert shell["source"].splitlines()[-1].isascii(), where
        assert ("Authorization: Bearer" in bash["source"]) == (item["x-key"] != "none"), where
        if method != "GET":
            assert f"-X {method}" in bash["source"] or " -T " in bash["source"], where
    assert {item["operationId"] for _, _, item in operations(spec) if item["x-paid"]} == PAID


def test_openapi_lists_the_external_api_and_nothing_internal(stand):
    """Все адреса /api/v1 и всё управление, открытое ключу «с управлением», — и ни одного адреса
    страницы: новый маршрут /api/v1 без описания или описанный внутренний адрес сюда попадут."""
    web, guest, _, _ = stand
    spec = guest.get("/api/v1/openapi.json").json()
    described = {(m, p) for m, p, _ in operations(spec)}
    v1 = {(m, p) for m, p in T.api_routes(web) if p.startswith("/api/v1")}
    assert len(v1) == 23 and described == v1 | T.MANAGE_KEY | ALSO_OUTSIDE
    assert described.isdisjoint(T.PAGE_ONLY) and described <= set(T.api_routes(web))
    for method, path, item in operations(spec):
        assert (item["x-key"] == "manage") == ((method, path) in T.MANAGE_KEY), f"{method} {path}"
        assert (item["x-key"] == "none") == (path == "/api/v1/openapi.json"), path
        if path.startswith("/api/admin/"):
            assert item["x-rights"]["level"] == apidoc.ADMIN and item["tags"] == [apidoc.MANAGE], path


def test_parameters_and_body_fields_come_from_the_routes(stand):
    """Имена, обязательность и значения по умолчанию — из маршрутов; в источнике описан каждый
    параметр и каждое поле тела, и нет описанных, которых маршрут не принимает."""
    web, guest, _, _ = stand
    spec = guest.get("/api/v1/openapi.json").json()
    by_address = {(m, p): item for m, p, item in operations(spec)}
    for op in apidoc._ops("4"):
        item = by_address[op.method, op.path]
        assert set(op.params) == {p["name"] for p in item.get("parameters", [])}, op.id
        assert set(op.query) <= set(op.params), op.id
        body = item.get("requestBody")
        if op.binary:
            assert "application/octet-stream" in body["content"], op.id
            continue
        assert bool(op.body) == bool(body), op.id
        if body:
            model = spec["components"]["schemas"][body["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[1]]
            assert set(op.body) == set(model["properties"]), op.id
            assert set(model.get("required", [])) <= set(op.send), op.id
    tree = {p["name"]: p for p in by_address["GET", "/api/v1/tree"]["parameters"]}
    assert tree["depth"]["schema"] == {"type": "integer", "default": web.TREE_DEPTH} and not tree["depth"]["required"]
    record = {p["name"]: p for p in by_address["GET", "/api/v1/record"]["parameters"]}
    assert record["file"]["required"] and record["file"]["example"] == MEETING
    assert {p["name"] for p in by_address["GET", "/api/v1/tasks"]["parameters"]} >= {"who", "open"}      # имена из адреса


# ---------- примеры — настоящие ----------

def request_of(item):
    """Запрос из примера метода: параметры адреса, тело JSON или файл."""
    kw = {"params": {p["name"]: p["example"] for p in item.get("parameters", []) if "example" in p}}
    for p in item.get("parameters", []):
        assert not p["required"] or "example" in p, f"{item['operationId']}: у обязательного {p['name']} нет примера"
    body = item.get("requestBody")
    if body and "application/octet-stream" in body["content"]:
        kw["content"] = b"video"
    elif body and "example" in body["content"]["application/json"]:
        kw["json"] = body["content"]["application/json"]["example"]
    return kw


def test_every_example_request_is_accepted_and_the_answer_has_the_described_fields(stand):
    """Каждый пример запроса из описания уходит в сервис как есть и получает 200; ответ — того
    типа и с теми полями, что в примере ответа. Единственная подстановка — номер заявки на
    доступ: он случайный. Изменяющие примеры идут в порядке описания, переименование и перенос —
    после загрузки. Платные доходят до очереди и там остаются."""
    web, guest, plain, manage = stand
    spec = guest.get("/api/v1/openapi.json").json()
    todo = sorted(operations(spec), key=lambda o: o[2]["operationId"] in AFTER_UPLOAD)
    assert [o[2]["operationId"] for o in todo[-2:]] == AFTER_UPLOAD
    done = 0
    for method, path, item in todo:
        name, kw = item["operationId"], request_of(item)
        client = {"none": guest, "any": plain, "manage": manage}[item["x-key"]]
        if "id" in kw.get("json", {}):
            kw["json"]["id"] = manage.get("/api/admin/requests").json()["requests"][0]["id"]
        if name == "post-admin-mail-test":
            continue                # без почты отвечает 503: пример проверяет отдельный тест, с почтой
        answer = client.request(method, path, **kw)
        assert answer.status_code == 200, f"{name}: {answer.status_code} {answer.text[:300]}"
        (media, good), = item["responses"]["200"]["content"].items()
        assert answer.headers["content-type"].split(";")[0] == media, name
        if media == "application/json":
            assert set(good["example"]) <= set(answer.json()), f"{name}: {set(good['example']) - set(answer.json())}"
        # в очередь ставит только ручной запуск; подпись и словарь в примерах — без пересборки
        assert [i.path for i in web.service.queue] == ([MEETING] if name == "post-admin-run" else []), name
        web.service.queue.clear()
        if name == "put-upload":
            T.recorded(web, answer.json()["file"])              # вместо обработки: запись готова
        done += 1
    assert done == len(todo) - 1 and web.service.running == []
    # примеры сделали то, что обещали: запись загружена, переименована и перенесена
    assert web.service.ledger.get("Проекты/Архив/Планёрка 12.03.mp4") and (web.cfg.inbox / "Проекты/Договоры 2026").is_dir()
    assert "sidorov" not in web.people.users and "orlova" in web.people.users
    assert plain.get("/api/v1/record", params={"file": MEETING}).json()["tasks"][0]["done"] is True
    # платная часть подписи и словаря — rebuild=true: до очереди, не дальше
    for method, path in (("POST", "/api/v1/speakers"), ("PUT", "/api/v1/dictionary")):
        rebuilt = plain.request(method, path, params={"file": MEETING, "rebuild": True})
        assert rebuilt.status_code == 200 and rebuilt.json()["rebuild"] == "queued" and rebuilt.json()["rebuilding"]
        assert [(i.path, i.force) for i in web.service.queue] == [(MEETING, False)]
        web.service.queue.clear()


def test_trial_letter_example_is_accepted_when_mail_is_on(tmp_path, monkeypatch):
    with running(tmp_path, extra=T.MAIL_ENV) as (web, client):
        post = T.Post()
        monkeypatch.setattr(web.mail, "send", post.send)
        item = client.get("/api/v1/openapi.json").json()["paths"]["/api/admin/mail/test"]["post"]
        answer = client.post("/api/admin/mail/test", auth=("admin", ADMIN_PASSWORD), **request_of(item))
        example = item["responses"]["200"]["content"]["application/json"]["example"]
        assert answer.status_code == 200 and set(example) == set(answer.json()) and len(post.sent) == 1


def strings_in_code():
    """Все строки из кода приложения: куски текста, из которых складываются отказы."""
    found = []
    for path in (ROOT / "app").glob("*.py"):
        if path.name != "apidoc.py":
            found += [node.value for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                      if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    return found


def test_refusal_texts_are_the_ones_written_in_the_code(stand):
    """Текст отказа в описании — строка из кода (целиком или её начало до подставляемого числа):
    поменяли текст в коде, а в описании забыли — тест покажет."""
    web, guest, plain, _ = stand
    spec = guest.get("/api/v1/openapi.json").json()
    code = strings_in_code()
    texts = {r["detail"] for _, _, item in operations(spec) for a in item["responses"].values()
             for r in a.get("x-refusals", []) if r["detail"]}
    assert len(texts) > 60
    for text in texts:
        assert any(text in line for line in code), text
    for _, _, item in operations(spec):
        codes = set(item["responses"]) - {"200"}
        assert ("401" in codes) == (item["x-key"] != "none"), item["operationId"]
        assert all(r["when"] for c in codes for r in item["responses"][c]["x-refusals"]), item["operationId"]

    # и несколько отказов вживую — с тем же кодом и текстом, что в описании
    def described(path, method, code):
        return [r["detail"] for r in spec["paths"][path][method]["responses"][code]["x-refusals"]]

    refused = plain.get("/api/v1/record", params={"file": "Проекты/нет.mp4"})
    assert refused.status_code == 404 and refused.json()["detail"] in described("/api/v1/record", "get", "404")
    refused = plain.get("/api/v1/search", params={"q": "я"})
    assert refused.status_code == 400 and refused.json()["detail"] in described("/api/v1/search", "get", "400")
    refused = plain.post("/api/admin/users")
    assert refused.status_code == 403 and refused.json()["detail"] in described("/api/admin/users", "post", "403")
    refused = guest.get("/api/v1/whoami")
    assert refused.status_code == 401 and refused.json()["detail"].startswith(described("/api/v1/whoami", "get", "401")[0])
    missing = plain.get("/api/v1/record")          # нет обязательного параметра: тот же вид, текст по-русски
    assert missing.status_code == 422 and missing.json() == {"detail": "не указан параметр file"}
    assert missing.json()["detail"].startswith(described("/api/v1/record", "get", "422")[0])
    closed = plain.get("/api/v1/tasks", params={"dir": "Нет такой"})
    assert closed.status_code == 404 and closed.json()["detail"] in described("/api/v1/tasks", "get", "404")
    # расходы — только администратору сервиса: описание называет право, вид ключа и оба отказа
    ivanov = enter(web, "ivanov", T.PASSWORD)
    for path in T.COSTS:
        item = spec["paths"][path]["get"]
        assert (item["x-rights"]["level"], item["x-key"]) == (apidoc.ADMIN, "manage"), path
        assert described(path, "get", "403") == [apidoc.PLAIN_KEY, web.NO_COSTS], path
        for client in (plain, ivanov):
            refused = client.get(path)
            assert refused.status_code == 403 and refused.json()["detail"] in described(path, "get", "403"), path
        assert "scope" not in json.dumps(item, ensure_ascii=False), path
    assert "свои расходы" not in guest.get("/api/v1/openapi.json").text


# ---------- текст для модели и навык ----------

def test_markdown_for_the_model_is_built_from_the_same_description(stand):
    """GET /api/v1 — тот же перечень методов, параметров и прав, что в openapi.json: файл
    app/skill/API.md — только рамка, сами методы подставляются из описания."""
    web, guest, plain, _ = stand
    spec = guest.get("/api/v1/openapi.json").json()
    text = plain.get("/api/v1").text
    frame = (web.SKILL / "API.md").read_text(encoding="utf-8")
    assert "{{" not in text and "{{API}}" in frame and "### " not in frame        # методов в самом файле нет
    heads = re.findall(r"^### (GET|POST|PUT) (/api/[\w./-]+)(?:\?(\S+))?$", text, flags=re.M)
    assert [(m, p) for m, p, _ in heads] == [(m, p) for m, p, _ in operations(spec)]
    for (method, path, query), (_, _, item) in zip(heads, operations(spec)):
        assert [q.rstrip("=") for q in query.split("&") if q] == [p["name"] for p in item.get("parameters", [])], path
        own = re.split(rf"^### {method} {re.escape(path)}(?:\?\S+)?$", text, flags=re.M)[1]
        section = re.split(r"^##+ ", own, maxsplit=1, flags=re.M)[0]
        assert f"**{item['summary']}.**" in section and f"Право: {item['x-rights']['text']}" in section, path
        assert ("платный — " in section) == item["x-paid"] and item["x-codeSamples"][0]["source"] in section, path
        for p in item.get("parameters", []):
            assert f"- `{p['name']}`" in section, f"{path}: {p['name']}"
    for must in ("http://testserver/api/v1/openapi.json", "http://testserver/api.", "## Управление", "## Что стоит денег",
                 "EscapeDataString", "один раз"):
        assert must in text, must


def test_skill_points_to_the_full_description_and_asks_for_one_rebuild(stand):
    _, guest, _, _ = stand
    skill = " ".join(guest.get("/skill/SKILL.md").text.split())
    for must in ("http://testserver/api/v1/openapi.json", "http://testserver/api.",
                 "Подписи — сразу все, пересборка — один раз в конце", "не передавай `rebuild=true` в каждом запросе",
                 "спроси, будут ли ещё правки", "Правки — сразу все, пересборка — один раз в конце", "`already`"):
        assert must in skill, must
    changes = guest.get("/skill/CHANGELOG.md").text
    assert "## 13\n" in changes and "/api/v1/openapi.json" in changes and "один раз в конце" in changes
    spec = guest.get("/api/v1/openapi.json").json()
    for path, method in (("/api/v1/speakers", "post"), ("/api/v1/dictionary", "put")):
        described = spec["paths"][path][method]["description"]
        assert "один раз отправьте `rebuild=true`" in described and "`already`" in described, path


def test_personal_mode_describes_the_api_without_keys_and_users(tmp_path):
    with running(tmp_path, mode="personal") as (web, client):
        spec = client.get("/api/v1/openapi.json").json()
        assert "security" not in spec and spec["components"]["securitySchemes"] == {}
        described = {(m, p) for m, p, _ in operations(spec)}
        assert {(m, p) for m, p in T.api_routes(web) if p.startswith("/api/v1")} <= described
        assert not [p for _, p in described if p.startswith(("/api/admin/", "/api/access"))]      # управления нет
        assert [t["name"] for t in spec["tags"]] == [t for t in apidoc.TAGS if t != apidoc.MANAGE]
        for _, _, item in operations(spec):
            assert item["x-key"] == "none" and item["security"] == [] and "401" not in item["responses"]
            assert "Authorization" not in json.dumps(item["x-codeSamples"]) and "{{" not in json.dumps(item["description"])
        upload = spec["paths"]["/api/v1/upload"]["put"]["description"]
        assert "без него запись ложится в корень записей" in upload and "ключ доступа" not in spec["info"]["description"]
        text = client.get("/api/v1").text
        assert "личном режиме" in text and "/api/admin/users" not in text and "{{" not in text
        # примеры чтения принимаются и без входа
        for path in ("/api/v1/whoami", "/api/v1/folders", "/api/v1/costs"):
            assert client.get(path).status_code == 200, path


# ---------- страница ----------

def test_api_page_is_served_without_login_and_needs_no_internet(stand):
    _, guest, _, _ = stand
    page = guest.get("/api")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert page.headers["cache-control"] == "no-cache"
    html = page.text
    assert "/api/v1/openapi.json" in html and "/static/common.js" in html
    # ни одного чужого адреса: сценарии, стили и шрифты — только свои
    assert not re.findall(r"""(?:src|href)=["'](?:https?:)?//""", html) and "@import" not in html
    # ссылки на раздел: справка (оглавление и «Интеграция с ИИ») и окно «Ключи доступа»
    assert guest.get("/help").text.count('href="/api"') >= 2
    assert 'href="/api"' in guest.get("/static/common.js").text
