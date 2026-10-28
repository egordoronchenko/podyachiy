"""Пути в параметрах всех методов API: «..» не выводит из открытой папки, а путь записи, присланный
в разложенной форме Unicode (macOS), находит ту же запись, что и в составной.

Методы берутся из самого приложения (app.routes): новый метод с параметром file, dir или path попадает
под проверку сам. Его обязательным параметрам нужен образец в SAMPLE — без него тест остановится
и скажет, какого не хватает.
"""
import unicodedata

import pytest

from app import watcher
from tests.test_web import MAIL_ENV, TASKS_MD, Post, filled, job, keyed, recorded, running, set_protocol

PATHS = {"file", "dir", "path"}
# Образцы параметров, кроме путей: обязательных и тех, без которых метод до проверки пути не дойдёт (имя
# файла загрузки). Функция — от пути записи, которой касается запрос: имя после переименования у каждой
# записи своё, а расширение переименование отбрасывает само.
SAMPLE = {
    "name": lambda file: " итоги.".join(file.rpartition("/")[2].rsplit(".", 1)),
    "size": lambda file: 5, "stamp": lambda file: "1", "offset": lambda file: 0,
    "format": lambda file: "docx", "key": lambda file: "нет-такого", "on": lambda file: False,
    "to": lambda file: "ivanov@example.test", "login": lambda file: "petrov", "text": lambda file: TASKS_MD,
    "base": lambda file: "",
}
# Отбор журнала действий: путь в нём только сравнивается со строками журнала и ничего не открывает.
FILTERS = {("GET", "/api/admin/journal")}


@pytest.fixture
def site(tmp_path, monkeypatch):
    """Сервис с почтой — письма никуда не уходят: методы отправки протокола без почты до пути не доходят."""
    with running(tmp_path, extra=MAIL_ENV) as (web, client):
        post = Post()
        monkeypatch.setattr(web.core.mail, "send", post.send)
        monkeypatch.setattr(web.core.mail, "send_each", post.send_each)
        yield web, client


def path_methods(web, wanted: set[str]) -> list[tuple[str, str, dict[str, tuple[str, bool]]]]:
    """Методы API, у которых среди параметров есть один из wanted: метод, адрес и параметры —
    имя → (где: query или body, обязателен ли)."""
    found = []
    for route in web.app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None or not route.path.startswith("/api/"):
            continue
        params: dict[str, tuple[str, bool]] = {}

        def collect(dep):
            for p in dep.query_params:
                params[p.name] = ("query", p.field_info.is_required())
            model = dep.body_params[0].field_info.annotation if len(dep.body_params) == 1 else None
            if hasattr(model, "model_fields"):              # одно тело-модель: её поля — на верхнем уровне JSON
                params.update({name: ("body", f.is_required()) for name, f in model.model_fields.items()})
            else:
                params.update({p.name: ("body", p.field_info.is_required()) for p in dep.body_params})
            for sub in dep.dependencies:
                collect(sub)

        collect(dependant)
        if params.keys() & wanted:
            found += [(method, route.path, params) for method in sorted(route.methods - {"HEAD", "OPTIONS"})]
    return found


def ask(client, method: str, path: str, params: dict, given: dict):
    """Запрос с параметрами given; прочие обязательные — из SAMPLE (по пути записи из given)."""
    file = given.get("file") or "Проекты/вкс.mp4"
    values = dict(given)
    for name, (_, required) in params.items():
        if name not in values and (required or name in SAMPLE):
            assert name in SAMPLE, f"{method} {path}: впишите в SAMPLE образец обязательного параметра {name}"
            values[name] = SAMPLE[name](file)
    query = {k: v for k, v in values.items() if params[k][0] == "query"}
    body = {k: v for k, v in values.items() if params[k][0] == "body"}
    has_body = any(where == "body" for where, _ in params.values())
    return client.request(method, path, params=query, json=body if has_body else None,
                          content=None if has_body or method not in ("PUT", "POST") else b"12345")


# ---------- «..» ----------

def test_dots_in_any_path_parameter_of_any_method_lead_nowhere(site):
    """«..» и «.» в file, dir или path любого метода — отказ, а не ответ про чужую папку и не действие над
    ней. Администратор сервиса видит всё — у него путь проверяется сам по себе; читатель «Проектов» по
    «Проекты/../Снабжение» не попадает в закрытую ему папку и не узнаёт, что в ней. Отказ — 4xx, но не 422:
    422 — это непринятые прочие параметры, до пути метод тогда не дошёл. Ничего при этом не меняется."""
    web, admin, ivanov, _ = filled(site)
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "manage": "tree"})
    steward, _ = keyed(web, admin, "Claude Code", 30, manage=True)
    web.core.service._take = lambda: None                      # очередь не разбирается: прогон ничего не тронет
    (web.core.cfg.inbox / "Снабжение" / "Тайное").mkdir()
    good = {"file": "Проекты/вкс.mp4", "dir": "Проекты", "path": "Проекты"}
    bad = {"file": ["Проекты/../Снабжение/итоги.mp4", "../users.json", "Проекты/./вкс.mp4"],
           "dir": ["Проекты/../Снабжение", "..", "Проекты/../../outbox", "Проекты/./Сметы"]}
    bad["path"] = bad["dir"]
    before = (sorted(web.core.service.ledger.records), sorted(p.as_posix() for p in web.core.cfg.inbox.rglob("*")))
    methods = [m for m in path_methods(web, PATHS) if m[:2] not in FILTERS]
    assert len(methods) > 40
    for method, path, params in methods:
        for name in params.keys() & PATHS:
            for value in bad[name]:
                given = {**{k: v for k, v in good.items() if k in params}, name: value}
                for client in (admin, steward, ivanov):
                    got = ask(client, method, path, params, given)
                    assert 400 <= got.status_code < 500 and got.status_code != 422, (
                        method, path, name, value, got.status_code, got.text[:200])
                    if client is ivanov:
                        assert "Тайное" not in got.text and "Снабжение/итоги" not in got.text.replace(value, ""), (
                            method, path, name, value, got.text[:200])
    assert web.core.service.queue == [] and web.core.service.running == []
    assert (sorted(web.core.service.ledger.records), sorted(p.as_posix() for p in web.core.cfg.inbox.rglob("*"))) == before


# ---------- путь записи в разложенной форме ----------

def nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


def test_record_path_in_the_decomposed_form_finds_the_record_in_every_method_with_file(site):
    """macOS присылает «ё» и «й» разложенными: буква и знак над ней отдельно. Записи и папки сервис
    хранит в составной форме — и путь записи в любом методе находит ту же запись в обеих формах:
    ответ на путь в разложенной форме тот же, что на путь в составной. Каждому методу — своя пара
    одинаковых записей: переименование и удаление одной не мешают проверке другой."""
    web, admin, _, _ = filled(site)
    admin.post("/api/folder", json={"dir": "Проекты/Архив"})
    web.core.service._take = lambda: None                      # ручной запуск только ставит в очередь
    lost = {web.access.NO_RECORD, "такой записи нет или её папка вам не открыта", "файл не найден", web.processing.NOT_QUEUED,
            web.processing.NOT_RUNNING}
    methods = [m for m in path_methods(web, {"file"}) if m[:2] not in FILTERS]
    assert len(methods) >= 30
    for n, (method, path, params) in enumerate(methods):
        pair = [f"Проекты/{n:02d}{tag} ёмкий йод.mp4" for tag in ("а", "б")]
        for file in pair:
            recorded(web, file)
            set_protocol(web, file, TASKS_MD)
            if path == "/api/admin/queue/remove":
                web.core.service.queue.append(watcher.QueueItem(path=file))
            elif path == "/api/admin/queue/cancel":
                web.core.service.running.append(job(file))
        extra = {"dir": "Проекты/Архив"} if "dir" in params else {}
        composed = ask(admin, method, path, params, {"file": pair[0], **extra})
        decomposed = ask(admin, method, path, params, {"file": nfd(pair[1]), **extra})
        shown = lambda got: (got.status_code, got.json().get("detail") if got.status_code >= 400 else None)
        # в составной форме метод запись нашёл: проверка не пустая, дальше — тот же ответ
        assert composed.status_code != 422 and shown(composed)[1] not in lost, (method, path, composed.text[:200])
        assert shown(decomposed) == shown(composed), (method, path, decomposed.text[:200])
    web.core.service.running.clear()


def test_record_known_only_in_the_decomposed_form_is_not_taken_for_its_composed_twin(site):
    """Запись, которую журнал обработки знает под именем в разложенной форме (рядом лежит её двойник
    в составной — сервис оставил её как есть и сказал об этом), находится по своему имени: иначе
    удаление одной унесло бы другую."""
    web, admin, _, _ = filled(site)
    composed = "Проекты/Встреча ё.mp4"
    recorded(web, composed)
    recorded(web, nfd(composed))
    gone = admin.post("/api/record/delete", json={"file": nfd(composed)})
    assert gone.status_code == 200, gone.text
    assert web.core.service.ledger.get(composed) is not None and web.core.service.ledger.get(nfd(composed)) is None
    assert admin.get("/api/result", params={"file": composed}).status_code == 200
