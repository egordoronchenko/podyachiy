"""Правка протокола человеком: текст встаёт в файл протокола без модели, прежний уходит в версии
с автором и временем, правка поверх чужой — отказ 409. Всё, что собирается из протокола (карта,
выгрузки, поиск, реестр поручений), видит правку сразу; отметки «выполнено» и сроки не теряются;
пересборка моделью правленый текст не стирает, а оставляет в версиях.

Сервис поднимается так же, как в tests/test_web.py.
"""
import io
import json
import re

import docx

from app import summarize
from tests import test_web as T
from tests.test_web import enter, keyed, recorded, set_protocol, site, staffed  # noqa: F401 — site: фикстура

REL = "Планёрки/планёрка.mp4"
HEADER = "<!-- протокол: модель тест, шаблон meeting, источник transcript/планёрка.json -->\n\n"
LONG_TASK = "Согласовать смету на стеллажи для склада"
PROTOCOL = T.TASKS_MD.replace("Согласовать смету", LONG_TASK)


def desk(site):
    """Папка «Планёрки» с готовой записью: Иванов — её администратор, Петров только читает."""
    web, admin, ivanov, petrov = staffed(site)
    admin.post("/api/folder", json={"dir": "Планёрки"})
    admin.post("/api/access", json={"dir": "Планёрки", "login": "ivanov", "read": "tree", "manage": "tree"})
    admin.post("/api/access", json={"dir": "Планёрки", "login": "petrov", "read": "tree"})
    recorded(web, REL)
    path = set_protocol(web, REL, HEADER + PROTOCOL)
    return web, admin, ivanov, petrov, path


def version(client):
    return client.get("/api/v1/versions", params={"file": REL}).json()["version"]


def save(client, text, base):
    return client.post("/api/v1/protocol", json={"file": REL, "text": text, "base": base})


def test_owner_edits_the_protocol_and_everything_built_from_it_sees_the_edit(site):
    web, admin, ivanov, petrov, path = desk(site)
    shown = ivanov.get("/api/result", params={"file": REL}).json()
    assert shown["can_edit"] and shown["edited"] is None
    assert shown["version"] == summarize.digest(path.read_bytes()) == version(ivanov)

    # читатель папки протокол не правит
    refused = save(petrov, "# Чужое", shown["version"])
    assert refused.status_code == 403 and refused.json()["detail"] == web.protocol_edit.NO_PROTOCOL_EDIT
    assert not petrov.get("/api/result", params={"file": REL}).json()["can_edit"]

    fixed = PROTOCOL.replace("Обсудили бюджет склада.", "Обсудили бюджет склада и поставку стеллажей.")
    done = save(ivanov, fixed, shown["version"])
    assert done.status_code == 200, done.text
    answer = done.json()
    assert answer["changed"] and answer["version"] == summarize.digest(path.read_bytes()) != shown["version"]
    assert answer["edited"]["by"] == "ivanov" and answer["edited"]["how"] == "edit"
    # служебная пометка сервиса — на месте, текст — правленый; модель не звали, в очередь ничего не встало
    assert path.read_text(encoding="utf-8") == HEADER + fixed and web.core.service.queue == []

    # карта, выгрузка, поиск, запись для ИИ-клиентов и реестр поручений видят правку сразу
    assert "поставку стеллажей" in ivanov.get("/api/map", params={"file": REL}).text
    assert "поставку стеллажей" in path.with_name("планёрка.map.html").read_text(encoding="utf-8")
    exported = docx.Document(io.BytesIO(ivanov.get("/api/export", params={"file": REL, "format": "docx"}).content))
    assert any("поставку стеллажей" in p.text for p in exported.paragraphs)
    assert [f["file"] for f in ivanov.get("/api/search", params={"q": "поставку стеллажей"}).json()["files"]] == [REL]
    record = ivanov.get("/api/v1/record", params={"file": REL}).json()
    assert "поставку стеллажей" in record["protocol"] and record["version"] == answer["version"]
    assert record["edited"]["by"] == "ivanov"
    assert LONG_TASK in [t["task"] for t in ivanov.get("/api/v1/tasks", params={"dir": "Планёрки"}).json()["tasks"]]

    # прежний текст — в версиях: его собрала модель
    listed = ivanov.get("/api/v1/versions", params={"file": REL}).json()
    assert listed["version"] == answer["version"] and listed["can_edit"]
    (old,) = listed["versions"]
    assert old["by"] is None and old["how"] == "model" and old["chars"] == len(HEADER + PROTOCOL)
    seen = petrov.get("/api/v1/version", params={"file": REL, "stamp": old["stamp"]}).json()
    assert seen["text"] == PROTOCOL and seen["stamp"] == old["stamp"]           # читать версии может и читатель

    # правка — в журнале действий; тот же текст ещё раз новой версии не даёт
    events = [e for e in admin.get("/api/admin/journal").json()["events"] if e["kind"].startswith("protocol_")]
    assert [(e["kind"], e["by"], e["file"]) for e in events] == [("protocol_edit", "ivanov", REL)]
    # признак ручной правки виден и там, откуда пересобирают: у подписей говорящих и в словаре записи
    # (оба отдают его вместе с names_newer и dict_newer — web.common._rebuild_state)
    assert ivanov.get("/api/dictionary", params={"file": REL}).json()["edited"]["by"] == "ivanov"
    again = save(ivanov, fixed, answer["version"]).json()
    assert not again["changed"] and len(ivanov.get("/api/v1/versions", params={"file": REL}).json()["versions"]) == 1


def test_edit_over_someone_elses_edit_or_a_rebuild_is_refused(site, monkeypatch):
    web, admin, ivanov, _, path = desk(site)
    opened = version(ivanov)                        # оба открыли один и тот же протокол
    assert save(admin, PROTOCOL + "\nДобавил администратор.\n", opened).status_code == 200
    late = save(ivanov, PROTOCOL + "\nДобавил Иванов.\n", opened)
    assert late.status_code == 409 and late.json()["detail"] == web.protocol_edit.PROTOCOL_CHANGED
    assert "Добавил администратор." in path.read_text(encoding="utf-8")
    assert "Добавил Иванов." not in path.read_text(encoding="utf-8")
    # без отпечатка правку не принимают
    assert ivanov.post("/api/v1/protocol", json={"file": REL, "text": "# Протокол"}).status_code == 422

    # пока протокол собирается, править его нельзя: пересборка запишет поверх
    monkeypatch.setattr(web.core.service, "running", [T.job(REL)])
    busy = save(ivanov, "# Протокол\n", version(ivanov))
    assert busy.status_code == 409 and busy.json()["detail"] == web.protocol_edit.PROTOCOL_BUSY
    monkeypatch.setattr(web.core.service, "running", [])

    # у записи без протокола и у чужой записи — 404
    path.unlink()
    assert save(ivanov, "# Протокол\n", opened).json()["detail"] == web.protocol_edit.NO_PROTOCOL_HERE
    assert enter(web, "petrov").get("/api/v1/versions", params={"file": "Нет/такой.mp4"}).status_code == 404


def test_text_of_an_edit_is_checked(site):
    web, _, ivanov, _, path = desk(site)
    base = version(ivanov)
    long = save(ivanov, "а" * (summarize.EDIT_MAX_CHARS + 1), base)
    assert long.status_code == 413 and long.json()["detail"] == web.protocol_edit.PROTOCOL_TOO_LONG
    for text in ("", "  \n\n", HEADER):
        empty = save(ivanov, text, base)
        assert empty.status_code == 400 and empty.json()["detail"] == web.protocol_edit.PROTOCOL_EMPTY, repr(text)
    control = save(ivanov, "# Протокол\x00\n", base)
    assert control.status_code == 400 and control.json()["detail"] == web.protocol_edit.PROTOCOL_NOT_TEXT
    # половинка суррогатной пары в JSON — не текст UTF-8
    broken = ivanov.post("/api/v1/protocol", content=json.dumps({"file": REL, "base": base}, ensure_ascii=False)
                         .replace("}", ', "text": "\\ud800"}').encode("utf-8"), headers={"Content-Type": "application/json"})
    assert broken.status_code == 400 and broken.json()["detail"] == web.protocol_edit.PROTOCOL_NOT_TEXT
    assert path.read_text(encoding="utf-8") == HEADER + PROTOCOL
    # перевод строки Windows и служебная пометка в начале самой правки — не беда: пометка сервиса одна
    done = save(ivanov, "<!-- своя пометка -->\r\n# Протокол\r\n\r\nРешили.\r\n", base)
    assert done.status_code == 200 and path.read_text(encoding="utf-8") == HEADER + "# Протокол\n\nРешили.\n"


def test_any_version_comes_back_as_an_edit_and_can_itself_be_undone(site):
    web, admin, ivanov, _, path = desk(site)
    robot, _ = keyed(web, ivanov)                   # ключу — в тех же правах, что у страницы
    edited = save(robot, PROTOCOL + "\nПравка ключом.\n", version(ivanov)).json()
    assert edited["edited"]["by"] == "ivanov" and edited["edited"]["key"] == "Cursor"
    (model,) = ivanov.get("/api/v1/versions", params={"file": REL}).json()["versions"]

    back = ivanov.post("/api/v1/protocol/restore", json={"file": REL, "stamp": model["stamp"], "base": edited["version"]})
    assert back.status_code == 200, back.text
    assert path.read_text(encoding="utf-8") == HEADER + PROTOCOL
    assert back.json()["edited"]["how"] == "restore" and back.json()["edited"]["from"] == model["stamp"]
    listed = ivanov.get("/api/v1/versions", params={"file": REL}).json()["versions"]
    assert [(v["how"], v["by"]) for v in listed] == [("edit", "ivanov"), ("model", None)]
    # и возврат можно отменить: правка ключом — тоже версия
    assert ivanov.get("/api/v1/version", params={"file": REL, "stamp": listed[0]["stamp"]}).json()["text"].endswith(
        "Правка ключом.\n")
    events = [(e["kind"], e.get("stamp"), e.get("via")) for e in admin.get("/api/admin/journal").json()["events"]
              if e["kind"].startswith("protocol_")]
    assert events == [("protocol_restore", model["stamp"], None), ("protocol_edit", None, "key")]

    # версии, которой нет, и метки, которые не метки, — 404
    for stamp in ("20000101-000000", "../планёрка", ""):
        gone = ivanov.post("/api/v1/protocol/restore", json={"file": REL, "stamp": stamp, "base": back.json()["version"]})
        assert gone.status_code == 404 and gone.json()["detail"] == web.protocol_edit.NO_VERSION, stamp
        assert ivanov.get("/api/v1/version", params={"file": REL, "stamp": stamp}).status_code == 404
    # возвращает тот же хозяин папки: читателю — 403
    refused = enter(web, "petrov").post("/api/v1/protocol/restore",
                                        json={"file": REL, "stamp": model["stamp"], "base": back.json()["version"]})
    assert refused.status_code == 403


def test_marks_and_due_dates_stay_with_their_tasks_after_edits(site):
    web, _, ivanov, _, path = desk(site)
    tasks = {t["task"]: t for t in ivanov.get("/api/v1/record", params={"file": REL}).json()["tasks"]}
    assert ivanov.post("/api/v1/task", params={"file": REL, "key": tasks[LONG_TASK]["key"]}).json()["task"]["done"]
    assert ivanov.post("/api/v1/task", params={"file": REL, "key": tasks["Проверить остатки"]["key"],
                                               "due": "2026-11-02"}).status_code == 200

    # поручение переписали — отметка пошла за ним; нетронутое поручение сохранило срок
    step = PROTOCOL.replace(LONG_TASK, "Согласовать смету на стеллажи для нового склада")
    save(ivanov, step, version(ivanov))
    # вторая правка того же поручения: от первоначальной формулировки она уже далеко, от первой правки — нет
    step = step.replace("на стеллажи для нового склада", "на стеллажи и полки для нового склада к понедельнику")
    save(ivanov, step, version(ivanov))
    now = {t["task"]: t for t in ivanov.get("/api/v1/record", params={"file": REL}).json()["tasks"]}
    assert now["Согласовать смету на стеллажи и полки для нового склада к понедельнику"]["done"] is True
    assert now["Проверить остатки"]["due"] == "2026-11-02" and not now["Проверить остатки"]["done"]
    rows = {r["task"]: r for r in ivanov.get("/api/v1/tasks", params={"dir": "Планёрки"}).json()["tasks"]}
    assert rows["Согласовать смету на стеллажи и полки для нового склада к понедельнику"]["done"] is True
    # отметка — и в карте, и в выгрузке
    assert "выполнено" in ivanov.get("/api/map", params={"file": REL}).text


def test_versions_move_with_the_record(site):
    web, _, ivanov, _, path = desk(site)
    save(ivanov, PROTOCOL + "\nПравка.\n", version(ivanov))
    moved = ivanov.post("/api/record/rename", json={"file": REL, "name": "Итоги"})
    assert moved.status_code == 200, moved.text
    new = moved.json()["file"]
    listed = ivanov.get("/api/v1/versions", params={"file": new}).json()
    assert listed["edited"]["by"] == "ivanov" and [v["how"] for v in listed["versions"]] == ["model"]
    assert ivanov.get("/api/v1/version", params={"file": new, "stamp": listed["versions"][0]["stamp"]}).json()[
        "text"] == PROTOCOL


def test_page_edits_the_protocol_and_warns_before_a_rebuild():
    page = T.page_source("index")
    for must in ("/api/v1/protocol", "/api/v1/versions", "/api/v1/version?", "/api/v1/protocol/restore",
                 "beforeunload", 'aria-live="polite"', 'role="alert"', "status === 409",
                 "Править", "Версии", "Вернуть эту версию", "Выбросить правку"):
        assert must in page, must
    common = (T.ROOT / "app" / "static" / "common.js").read_text(encoding="utf-8")
    # предупреждение о ручной правке — у каждой кнопки, которая пересобирает протокол: «Пересобрать
    # протокол», «Сохранить и пересобрать» у подписей и у словаря; признак edited приходит с каждой вкладки
    assert "ручные правки уйдут в версии протокола" in common
    assert len(re.findall(r"handEdited\((?:viewData|d)\)", common)) == 4          # определение и три кнопки
    assert "protocolPane.body()" in common and 'matches("input, textarea")' in common
