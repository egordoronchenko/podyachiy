"""Шаблоны протокола, задания модели и словарь: проверка текста, прошлые версии, свои типы
и выбор своего типа разбором записи; раздел «Настройки» целиком через сервис."""
import asyncio
import json
import shutil
import types
from pathlib import Path

import pytest

from app import actions, dictionary, llm, pipeline, speech, templates, triage
from tests.test_web import (ADMIN_PASSWORD, enter, keyed, kinds, recorded, running, seed, site,  # noqa: F401
                            staffed)

ROOT = Path(__file__).resolve().parent.parent
STANDARD = ROOT / "prompts"
WHEN = "Разбирают один сбой от начала до конца: что случилось, почему и как не допустить снова."


@pytest.fixture
def prompts(tmp_path):
    target = tmp_path / "prompts"
    shutil.copytree(STANDARD, target)
    return target


def text_of(name: str) -> str:
    return (STANDARD / name).read_text(encoding="utf-8")


# ---------- проверка текста ----------

def test_standard_files_pass_the_check():
    """Тексты из поставки сохраняются без отказа. У совещания и созвона нет ни одного замечания;
    лекция и постановка задачи устроены иначе — про них страница честно говорит то же, что
    сказала бы про свой шаблон."""
    for key, meta in templates.TEMPLATES.items():
        warnings = templates.check("type", text_of(meta["file"]))
        assert (warnings == []) == (key in ("meeting", "call")), (key, warnings)
    assert any("Нет раздела «Задачи»" in w for w in templates.check("type", text_of("lecture.md")))
    for item in ("triage", "actions", "glossary", "common"):
        assert templates.check(item, text_of(templates.SERVICE[item]["file"])) == [], item


@pytest.mark.parametrize("kind, text, why", [
    ("type", "", "текст пустой"),
    ("type", "   \n\n", "текст пустой"),
    ("type", "Составь протокол совещания по расшифровке.", "не описаны разделы протокола"),
    ("type", "## Раздел\n" + "я" * templates.MAX_CHARS, "длиннее 60000 знаков"),
    ("type", "## Раздел\n\x00\x01двоичное", "не текстовый файл"),
    ("triage", "", "текст пустой"),
    ("triage", "Определи тип записи и верни его.", "вернуть JSON с полем type"),
    ("actions", "\n", "текст пустой"),
    ("glossary", "СКУД\x00", "не текстовый файл"),
])
def test_what_breaks_the_pipeline_is_refused(kind, text, why):
    with pytest.raises(templates.TemplateError) as refused:
        templates.check(kind, text)
    assert why in str(refused.value)


def test_what_other_parts_of_the_service_read_gives_warnings():
    meeting = text_of("meeting.md")
    assert templates.check("type", meeting) == []
    no_tasks = meeting.replace("## 5. Задачи", "## 5. Что делать дальше")
    assert ["реестр поручений по таким записям" in w for w in templates.check("type", no_tasks)] == [True]
    listed = meeting.replace("| Задача | Срок |", "| Что сделать | Срок |")
    assert ["нет образца таблицы с колонкой «Задача»" in w for w in templates.check("type", listed)] == [True]
    no_brief = meeting.replace("## 1. Кратко", "## 1. Суть")
    assert ["в письме о готовности" in w for w in templates.check("type", no_brief)] == [True]
    no_people = meeting.replace("## 2. Участники", "## 2. Кто был")
    assert ["Нет раздела «Участники»" in w for w in templates.check("type", no_people)] == [True]
    assert any("метки времени" in w for w in templates.check("type", meeting.replace("чч:мм:сс", "время")))
    assert any("ничего не подставляет" in w for w in templates.check("type", meeting + "\n{{transcript}}\n"))
    assert any("ничего не подставляет" in w for w in templates.check("type", meeting + "\nТекст: {transcript}\n"))
    bare = templates.check("type", "## Итог\n\nНапиши итог.")
    assert len(bare) == 4                                           # задачи, кратко, участники, метки времени
    assert any("title" in w and "speakers" in w for w in templates.check("triage", "Верни JSON с полем type."))
    assert templates.check("glossary", "# только пояснения\n\n") == [
        "В словаре нет ни одного термина (строки с # — пояснения): подсказок модели не будет."]
    assert any("образца таблицы" in w for w in templates.check("actions", "Выпиши поручения списком."))


def test_the_checks_use_the_same_patterns_as_the_readers():
    """Раздел, который проверка считает «Задачами» и «Кратко», действительно читают реестр и письма."""
    from app import external, notify
    protocol = "# Протокол\n\n## 1. Кратко\n\n- Решили.\n\n## 5. Задачи\n\n| Задача | Срок |\n|---|---|\n| Согласовать смету | — |\n"
    assert templates.check("type", protocol + "[чч:мм:сс]\n\n## Участники\n") == []
    assert [t["task"] for t in external.tasks(protocol)] == ["Согласовать смету"]
    assert notify.brief(protocol) == ["Решили."]


# ---------- файлы и прошлые версии ----------

def test_save_keeps_the_previous_text_and_only_the_last_versions(prompts):
    first = templates.read(prompts / "meeting.md")
    assert templates.save(prompts, "meeting.md", first) is False            # тот же текст — нечего сохранять
    assert templates.backups(prompts, "meeting.md") == []
    for n in range(4):
        assert templates.save(prompts, "meeting.md", f"## Раздел {n}\r\nтекст\r\n\r\n") is True
    assert templates.read(prompts / "meeting.md") == "## Раздел 3\nтекст\n"
    kept = templates.backups(prompts, "meeting.md")
    assert len(kept) == templates.KEEP == 2 and kept[0]["at"] >= kept[1]["at"]
    assert templates.backup_text(prompts, "meeting.md", kept[0]["id"]) == "## Раздел 2\nтекст\n"
    assert templates.backup_text(prompts, "meeting.md", kept[1]["id"]) == "## Раздел 1\nтекст\n"
    with pytest.raises(templates.TemplateError):
        templates.backup_text(prompts, "meeting.md", "20200101-000000")
    assert not list(prompts.glob("*.tmp")) and templates.backups(prompts, "call.md") == []
    # прошлые версии не мешают обработке: в списке типов и шаблонов их нет
    assert sorted(templates.types(prompts)) == sorted(templates.TEMPLATES)


def test_uploaded_file_must_be_utf8_text():
    assert templates.decode("﻿## Раздел\n".encode("utf-8")) == "## Раздел\n"
    with pytest.raises(templates.TemplateError) as refused:
        templates.decode("## Раздел: кратко".encode("cp1251"))
    assert "не в кодировке UTF-8" in str(refused.value)
    with pytest.raises(templates.TemplateError) as refused:
        templates.decode(b"x" * (templates.UPLOAD_MAX_BYTES + 1))
    assert "больше 256 КБ" in str(refused.value)


def test_file_edited_by_hand_in_another_encoding_is_shown_and_not_fatal(prompts):
    (prompts / "glossary.txt").write_bytes("СКУД, БСП\n".encode("cp1251"))
    assert "�" in templates.read(prompts / "glossary.txt")
    assert templates.read(prompts / "нет.md") is None


# ---------- свои типы ----------

def test_key_is_made_from_the_title():
    assert templates.slug("Разбор инцидента") == "razbor_incidenta"
    assert templates.slug("  1:1 с руководителем!  ") == "s_rukovoditelem"
    assert templates.slug("Щи & Ёжик — 2") == "schi_ezhik_2"
    assert templates.slug("!!!") == "" and len(templates.slug("очень длинное название " * 5)) <= 30


def test_own_type_is_added_changed_and_removed(prompts):
    key = templates.add_type(prompts, "", " Разбор   инцидента ", WHEN, text_of("meeting.md"), True)
    assert key == "razbor_incidenta"
    kinds_now = templates.types(prompts)
    assert list(kinds_now)[:4] == list(templates.TEMPLATES) and kinds_now[key] == {
        "title": "Разбор инцидента", "file": "razbor_incidenta.md", "custom": True, "when": WHEN, "actions": True}
    # текст — копия шаблона совещания, со строками общих правил: собирается так же, как он
    assert templates.load_template(prompts, key) == ("Разбор инцидента", templates.load_template(prompts, "meeting")[1])
    assert json.loads((prompts / "types.json").read_text(encoding="utf-8")) == {
        key: {"title": "Разбор инцидента", "when": WHEN, "actions": True}}

    for bad, why in ((("", "", WHEN), "укажите название"), (("", "Разбор инцидента", WHEN), "уже есть"),
                     (("meeting", "Планёрка", WHEN), "уже есть"), (("glossary", "Планёрка", WHEN), "уже есть"),
                     (("", "Совещание", WHEN), "с названием «Совещание» уже есть"),
                     (("Плохой-ключ", "Планёрка", WHEN), "ключ типа — латинские буквы"),
                     (("", "!!!", WHEN), "ключ составить не смог"), (("", "Планёрка", "коротко"), "когда применять"),
                     (("", "я" * 61, WHEN), "до 60 знаков")):
        with pytest.raises(templates.TemplateError) as refused:
            templates.add_type(prompts, bad[0], bad[1], bad[2], "## Раздел", False)
        assert why in str(refused.value), bad
    assert sorted(templates.read_custom(prompts)[0]) == [key]

    templates.change_type(prompts, key, "Разбор сбоя", WHEN + " Виноватых не ищут.", False)
    assert templates.types(prompts)[key]["title"] == "Разбор сбоя" and not templates.types(prompts)[key]["actions"]
    for standard in ("meeting", "triage", "нет"):
        with pytest.raises(templates.TemplateError):
            templates.change_type(prompts, standard, "Другое", WHEN, False)
        with pytest.raises(templates.TemplateError) as refused:
            templates.remove_type(prompts, standard)
        assert "удалить можно только свой тип" in str(refused.value)

    templates.remove_type(prompts, key)
    assert sorted(templates.types(prompts)) == sorted(templates.TEMPLATES) and not (prompts / f"{key}.md").exists()
    assert len(templates.backups(prompts, f"{key}.md")) == 1               # текст не пропал — он в прошлых версиях
    with pytest.raises(ValueError):
        templates.load_template(prompts, key)


def test_types_file_edited_by_hand_never_breaks_the_service(prompts):
    file = prompts / "types.json"
    file.write_text("{не json", encoding="utf-8")
    custom, problem = templates.read_custom(prompts)
    assert custom == {} and "не читается" in problem and sorted(templates.types(prompts)) == sorted(templates.TEMPLATES)
    with pytest.raises(templates.TemplateError) as refused:               # и новый тип поверх испорченного файла не пишется
        templates.add_type(prompts, "", "Планёрка", WHEN, "## Раздел", False)
    assert "Исправьте или удалите файл types.json" in str(refused.value) and file.read_text(encoding="utf-8") == "{не json"
    file.write_text("[1, 2]", encoding="utf-8")
    assert templates.read_custom(prompts) == ({}, "в файле types.json должен быть объект «ключ: описание» — "
                                                  "свои типы шаблонов не действуют")
    file.write_text(json.dumps({"incident": {"title": "Разбор инцидента", "when": WHEN, "file": "../../etc/passwd"},
                                "meeting": {"title": "Подмена стандартного"}, "Плохой ключ": {"title": "x"},
                                "empty": {"title": "  "}, "str": "не объект", "lost": {"title": "Без файла"}}),
                    encoding="utf-8")
    custom, problem = templates.read_custom(prompts)
    assert sorted(custom) == ["incident", "lost"] and custom["incident"]["file"] == "incident.md"   # путь из файла не берётся
    assert "пропущены неверные записи: meeting, Плохой ключ, empty, str" in problem
    assert templates.TEMPLATES["meeting"]["title"] == "Совещание" and templates.types(prompts)["meeting"]["title"] == "Совещание"
    # типа без файла шаблона разбор не видит: выбрать его значило бы оставить запись без протокола
    assert "incident" not in templates.types(prompts) and "lost" not in templates.types(prompts)
    (prompts / "incident.md").write_text("## Что случилось\n", encoding="utf-8")
    assert "incident" in templates.types(prompts) and "lost" in templates.types_listed(prompts)


def test_triage_task_lists_own_types_with_their_descriptions(prompts):
    plain = templates.load_triage(prompts)
    assert plain.endswith("- meeting — Совещание\n- lecture — Лекция\n- call — Созвон с заказчиком\n- spec — Постановка задачи")
    templates.add_type(prompts, "incident", "Разбор инцидента", WHEN, "## Что случилось\n", False)
    task = templates.load_triage(prompts)
    assert f"- spec — Постановка задачи\n- incident — Разбор инцидента: {WHEN}\n" in task
    assert task.endswith("лучше, чем под признаки остальных типов.") and task.startswith(plain[:200])


# ---------- разбор выбирает свой тип ----------

def answer(kind):
    return json.dumps({"type": kind, "type_reason": "разбирают один сбой", "confidence": "высокая",
                       "title": "Сбой обмена со складом", "date": "", "gist": "Разобрали сбой", "topics": [],
                       "same_person": [], "speakers": [{"label": "S1", "name": "Иван", "role": ""}]}, ensure_ascii=False)


def test_parse_takes_own_types_only_when_they_are_given():
    with pytest.raises(ValueError):
        triage.parse(answer("incident"))                                    # стандартный набор его не знает
    kinds_now = {**templates.TEMPLATES, "incident": {"title": "Разбор инцидента"}}
    assert triage.parse(answer("incident"), kinds_now)["type"] == "incident"
    with pytest.raises(ValueError):
        triage.parse(answer("выдуманный"), kinds_now)


class Model:
    """Вместо текстовой модели: разбору отвечает типом, протоколу — текстом. Проверку ответа
    (check) вызывает так же, как настоящий llm.chat."""

    def __init__(self, kind):
        self.kind, self.asked = kind, []

    async def chat(self, cfg, stage, system, user, *, budget=0, response_format=None, check=None, log=lambda s: None):
        self.asked.append((stage, user))
        raw = answer(self.kind) if stage == "triage" else "# Разбор инцидента\n\n## 1. Что случилось\n\nСбой обмена [00:10:05].\n"
        if check is not None:
            check(raw)
        return raw, {}


def staged(tmp_path, prompts):
    cfg = types.SimpleNamespace(inbox=tmp_path / "in", outbox=tmp_path / "out", prompts_dir=prompts,
                                model="deepgram/nova-3", summary_model="тест", actions_models=("тест",),
                                summary_max_tokens=0, prev_tasks=False, public_url="")
    (cfg.inbox / "Сбои").mkdir(parents=True)
    (cfg.inbox / "Сбои" / "обмен.mp3").write_bytes(b"x")
    out = cfg.outbox / "Сбои" / "обмен" / "transcript"
    out.mkdir(parents=True)
    segments = [{"start": 605, "end": 606, "speaker": "S1", "text": "Обмен со складом упал в ночь на вторник."}]
    (out / "обмен.json").write_text(json.dumps({"segments": segments}), encoding="utf-8")
    (out / "обмен.txt").write_text(speech.to_text_timed(segments, None)[0], encoding="utf-8")
    (out / "обмен.meta.json").write_text(json.dumps({"success": True, "model": cfg.model, "audio_duration_sec": 700.0}),
                                         encoding="utf-8")
    return cfg


def process(cfg, template=None):
    lines = []
    record = asyncio.run(pipeline.process(cfg, "Сбои/обмен.mp3", "обмен", template, False, False, lines.append))
    return record, lines


def test_triage_picks_the_own_type_and_the_protocol_goes_by_its_template(tmp_path, prompts, monkeypatch):
    templates.add_type(prompts, "incident", "Разбор инцидента", WHEN, "ШАБЛОН ИНЦИДЕНТА\n\n## 1. Что случилось\n", False)
    cfg, model = staged(tmp_path, prompts), Model("incident")
    monkeypatch.setattr(llm, "chat", model.chat)

    async def no_actions(*a, **kw):
        raise AssertionError("у этого типа шаг поручений выключен")

    monkeypatch.setattr(actions, "extract_file", no_actions)
    record, lines = process(cfg)
    assert record["status"] == "ok", record.get("error")
    assert record["template"] == "incident" and record["template_title"] == "Разбор инцидента" and record["template_auto"]
    stages = [stage for stage, _ in model.asked]
    assert stages == ["triage", "summary"]
    assert f"- incident — Разбор инцидента: {WHEN}" in model.asked[0][1]            # разбор знал про тип и когда его брать
    assert model.asked[1][1].startswith("ШАБЛОН ИНЦИДЕНТА")                            # протокол — по шаблону этого типа
    saved = json.loads((cfg.outbox / "Сбои/обмен/transcript/обмен.triage.json").read_text(encoding="utf-8"))
    assert saved["type"] == "incident" and saved["templates"] == sorted([*templates.TEMPLATES, "incident"])
    assert "шаблон incident" in (cfg.outbox / "Сбои/обмен/summary/обмен.md").read_text(encoding="utf-8")
    assert any("«Разбор инцидента»" in line for line in lines)

    # пересборка без изменений: разбор взят готовый, модель спрошена только про протокол
    model.asked.clear()
    assert process(cfg)[0]["reused_triage"] and [s for s, _ in model.asked] == ["summary"]

    # тип удалили: записи остаются как есть, а при пересборке разбор выбирает заново из оставшихся
    templates.remove_type(prompts, "incident")
    before = (cfg.outbox / "Сбои/обмен/summary/обмен.md").read_text(encoding="utf-8")
    assert "шаблон incident" in before
    model.asked.clear()
    model.kind = "meeting"

    async def one_list(*a, **kw):
        return ""

    monkeypatch.setattr(actions, "extract_file", one_list)
    record, lines = process(cfg)
    assert record["status"] == "ok" and record["template"] == "meeting" and not record["reused_triage"]
    assert [s for s, _ in model.asked] == ["triage", "summary"] and "incident" not in model.asked[0][1]
    # модель по старой памяти назвала удалённый тип — ответ не принят, запись остаётся с расшифровкой
    model.kind = "incident"
    (cfg.outbox / "Сбои/обмен/transcript/обмен.triage.json").unlink()
    record, _ = process(cfg)
    assert record["status"] == "no_summary" and "не из списка" in record["error"]
    # вручную заданный удалённый тип — понятная ошибка, а не молчаливая подмена
    assert "неизвестный шаблон: incident" in process(cfg, "incident")[0]["error"]


def test_own_type_with_the_actions_step_gets_the_list_of_tasks(tmp_path, prompts, monkeypatch):
    templates.add_type(prompts, "retro", "Ретроспектива", WHEN, text_of("meeting.md"), True)
    cfg, model = staged(tmp_path, prompts), Model("retro")
    monkeypatch.setattr(llm, "chat", model.chat)
    called = []

    async def listed(*a, **kw):
        called.append(1)
        return "\n\nСПИСОК ПОРУЧЕНИЙ"

    monkeypatch.setattr(actions, "extract_file", listed)
    record, _ = process(cfg)
    assert record["status"] == "ok" and record["template"] == "retro" and called == [1]
    assert "СПИСОК ПОРУЧЕНИЙ" in model.asked[1][1]


def test_glossary_goes_to_the_text_stages_and_to_the_engine_that_takes_terms(prompts):
    (prompts / "glossary.txt").write_text("# пояснение\nСКУД, 1С:ЗУП\nрегистр сведений\n", encoding="utf-8")
    block = templates.load_glossary(prompts)
    assert block.endswith("СКУД, 1С:ЗУП\nрегистр сведений") and "пояснение" not in block
    assert speech.glossary_terms(prompts) == ["СКУД", "1С:ЗУП", "регистр сведений"]
    assert [m for m, e in speech.ENGINES.items() if e.takes_terms] == ["microsoft/mai-transcribe-2"]
    # комментарий в файле из поставки больше не говорит, что подсказать распознаванию нельзя вовсе
    shipped = text_of("glossary.txt")
    assert "и подсказать\n# ему термины нельзя" not in shipped and "MAI-Transcribe 2" in shipped


# ---------- раздел «Настройки» через сервис ----------

def items(client):
    return {t["id"]: t for t in client.get("/api/admin/templates").json()["items"]}


def text(client, item, version="current"):
    return client.get("/api/admin/templates/text", params={"id": item, "version": version})


def test_page_shows_what_is_really_in_the_files(site):
    web, admin, ivanov, _ = staffed(site)
    seed(web)
    seen = admin.get("/api/admin/templates").json()
    assert [t["id"] for t in seen["items"]] == ["meeting", "lecture", "call", "spec", "triage", "actions", "glossary",
                                               "common"]
    assert seen["problem"] == "" and seen["writable"] and seen["limits"]["chars"] == templates.MAX_CHARS
    meeting = items(admin)["meeting"]
    assert (meeting["kind"], meeting["title"], meeting["file"], meeting["state"]) == ("type", "Совещание", "meeting.md", "standard")
    assert meeting["actions"] and meeting["has_default"] and not meeting["custom"] and meeting["warnings"] == []
    assert not meeting["outside"] and meeting["by"] == "" and meeting["backups"] == [] and "договариваются" in meeting["when"]
    assert [items(admin)[k]["kind"] for k in ("triage", "actions", "glossary")] == ["triage", "actions", "glossary"]
    assert text(admin, "meeting").json()["text"] == text_of("meeting.md")
    assert text(admin, "meeting", "default").json()["text"] == text_of("meeting.md")

    # файл поправили на сервере, в обход страницы: страница показывает то, что лежит в файле
    path = web.cfg.prompts_dir / "meeting.md"
    path.write_text(text_of("meeting.md").replace("## 5. Задачи", "## 5. Дела"), encoding="utf-8")
    meeting = items(admin)["meeting"]
    assert meeting["state"] == "changed" and meeting["outside"] and meeting["by"] == ""
    assert len(meeting["warnings"]) == 1 and "реестр поручений" in meeting["warnings"][0]
    assert "## 5. Дела" in text(admin, "meeting").json()["text"] and "## 5. Задачи" in text(admin, "meeting", "default").json()["text"]
    # испорченный руками файл страницу не ломает: показан как есть, с объяснением
    path.write_bytes("Без разделов, в другой кодировке".encode("cp1251"))
    (web.cfg.prompts_dir / "call.md").unlink()
    (web.cfg.prompts_dir / "types.json").write_text("{", encoding="utf-8")
    seen = admin.get("/api/admin/templates")
    assert seen.status_code == 200 and "не читается" in seen.json()["problem"]
    broken = items(admin)
    assert "не описаны разделы" in broken["meeting"]["problem"] and broken["call"]["state"] == "missing"
    assert "файла нет" in broken["call"]["problem"] and text(admin, "call").status_code == 404
    assert "�" in text(admin, "meeting").json()["text"]

    # скачать файлом; чужим — ничего
    got = admin.get("/api/admin/templates/text", params={"id": "triage", "download": True})
    assert got.text == text_of("triage.md") and got.headers["content-disposition"] == 'attachment; filename="triage.md"'
    assert got.headers["x-content-type-options"] == "nosniff" and got.headers["content-type"].startswith("text/plain")
    for unknown in ("нет", "../.env", "types", "history"):
        assert text(admin, unknown).status_code == 404, unknown
    assert ivanov.get("/api/admin/templates").status_code == 403 and text(ivanov, "meeting").status_code == 403
    assert site[1].get("/api/admin/templates").status_code == 401


def test_admin_edits_a_template_gets_warnings_and_rolls_back(site):
    web, admin, ivanov, _ = staffed(site)
    seed(web)
    original = text_of("meeting.md")
    path = web.cfg.prompts_dir / "meeting.md"

    # обязательного нет — отказ, файл не тронут
    for bad, why in (("", "текст пустой"), ("Просто напиши протокол.", "не описаны разделы протокола"),
                     ("## Раздел\n" + "я" * 60001, "длиннее 60000 знаков")):
        refused = admin.post("/api/admin/templates/save", json={"id": "meeting", "text": bad})
        assert refused.status_code == 400 and why in refused.json()["detail"], why
    assert path.read_text(encoding="utf-8") == original and items(admin)["meeting"]["backups"] == []
    assert admin.post("/api/admin/templates/save", json={"id": "нет", "text": "## Раздел"}).status_code == 404
    assert ivanov.post("/api/admin/templates/save", json={"id": "meeting", "text": original}).status_code == 403

    # желательного нет — сохраняется с предупреждением; прежний текст — в прошлых версиях
    edited = original.replace("## 5. Задачи", "## 5. Что делать дальше")
    saved = admin.post("/api/admin/templates/save", json={"id": "meeting", "text": edited.replace("\n", "\r\n")}).json()
    assert saved["ok"] and saved["changed"] and len(saved["warnings"]) == 1 and "реестр поручений" in saved["warnings"][0]
    assert path.read_text(encoding="utf-8") == edited and "\r" not in path.read_bytes().decode("utf-8")
    meeting = items(admin)["meeting"]
    assert meeting["state"] == "changed" and meeting["by"] == "admin" and not meeting["outside"]
    assert len(meeting["backups"]) == 1 and meeting["changed_at"][:2] == "20"
    assert text(admin, "meeting", meeting["backups"][0]["id"]).json()["text"] == original
    assert admin.post("/api/admin/templates/save", json={"id": "meeting", "text": edited}).json()["changed"] is False

    # обработка берёт уже новый текст — без перезапуска
    assert "## 5. Что делать дальше" in templates.load_template(web.cfg.prompts_dir, "meeting")[1]

    # откат к прошлой версии: текущий текст сам уходит в прошлые версии
    back = admin.post("/api/admin/templates/restore", json={"id": "meeting", "version": meeting["backups"][0]["id"]}).json()
    assert back["changed"] and back["warnings"] == [] and path.read_text(encoding="utf-8") == original
    meeting = items(admin)["meeting"]
    assert meeting["state"] == "standard" and len(meeting["backups"]) == 2
    assert text(admin, "meeting", meeting["backups"][0]["id"]).json()["text"] == edited

    # вернуть стандартный после правки
    admin.post("/api/admin/templates/save", json={"id": "meeting", "text": edited})
    assert items(admin)["meeting"]["state"] == "changed"
    assert admin.post("/api/admin/templates/restore", json={"id": "meeting", "version": "default"}).json()["changed"]
    assert path.read_text(encoding="utf-8") == original and len(items(admin)["meeting"]["backups"]) == templates.KEEP
    for version in ("current", "20200101-000000"):
        refused = admin.post("/api/admin/templates/restore", json={"id": "meeting", "version": version})
        assert refused.status_code in (400, 404), version

    # файлом: UTF-8 принимается, другая кодировка и слишком большой файл — нет
    loaded = admin.put("/api/admin/templates/upload", params={"id": "meeting"}, content=("﻿" + edited).encode("utf-8"))
    assert loaded.status_code == 200 and loaded.json()["changed"] and path.read_text(encoding="utf-8") == edited
    for body, code, why in ((edited.encode("cp1251"), 400, "не в кодировке UTF-8"),
                            (b"\x89PNG\r\n\x1a\n\x00\x00", 400, "не в кодировке UTF-8"),
                            (b"## x\n\x00", 400, "не текстовый файл"),
                            (b"x" * (templates.UPLOAD_MAX_BYTES + 1), 413, "больше 256 КБ")):
        refused = admin.put("/api/admin/templates/upload", params={"id": "meeting"}, content=body)
        assert refused.status_code == code and why in refused.json()["detail"], why
    assert path.read_text(encoding="utf-8") == edited

    events = [e for e in web.trail.read() if e["kind"] == "template_save"]
    assert [e["how"] for e in events] == ["page", "backup", "page", "default", "file"]
    assert events[0]["by"] == "admin" and events[0]["file"] == "meeting.md" and events[0]["warnings"] == 1
    assert admin.get("/api/admin/journal").status_code == 200


def test_glossary_and_the_tasks_for_the_model_are_edited_on_the_same_page(site):
    web, admin, _, _ = staffed(site)
    seed(web)
    done = admin.post("/api/admin/templates/save", json={"id": "glossary", "text": "# наши слова\nСКУД, Ромашка\n"}).json()
    assert done["changed"] and done["warnings"] == []
    assert templates.load_glossary(web.cfg.prompts_dir).endswith("СКУД, Ромашка")
    assert speech.glossary_terms(web.cfg.prompts_dir) == ["СКУД", "Ромашка"]
    empty = admin.post("/api/admin/templates/save", json={"id": "glossary", "text": ""}).json()
    assert empty["changed"] and "нет ни одного термина" in empty["warnings"][0]
    assert templates.load_glossary(web.cfg.prompts_dir) == ""
    refused = admin.post("/api/admin/templates/save", json={"id": "triage", "text": "Определи тип записи."})
    assert refused.status_code == 400 and "JSON с полем type" in refused.json()["detail"]
    assert admin.post("/api/admin/templates/restore", json={"id": "glossary", "version": "default"}).json()["changed"]
    assert items(admin)["glossary"]["state"] == "standard"


def test_admin_adds_an_own_type_and_the_service_offers_it_everywhere(site):
    web, admin, ivanov, _ = staffed(site)
    seed(web)
    recorded(web, "запись.mp4")
    body = {"title": "Разбор инцидента", "when": WHEN, "base": "call"}
    assert ivanov.post("/api/admin/templates/type", json=body).status_code == 403
    for bad, why in (({**body, "title": ""}, "укажите название"), ({**body, "when": "мало"}, "когда применять"),
                     ({**body, "key": "meeting"}, "уже есть"), ({**body, "base": "нет"}, "такого шаблона нет"),
                     ({**body, "text": "без разделов"}, "не описаны разделы")):
        refused = admin.post("/api/admin/templates/type", json=bad)
        assert refused.status_code in (400, 404) and why in refused.json()["detail"], why
    assert sorted(items(admin)) == sorted([*templates.TEMPLATES, *templates.SERVICE])

    made = admin.post("/api/admin/templates/type", json=body).json()
    key = made["key"]
    assert key == "razbor_incidenta" and made["warnings"] == []
    mine = items(admin)[key]
    assert (mine["state"], mine["custom"], mine["has_default"], mine["by"], mine["actions"]) == ("custom", True, False, "admin", False)
    assert mine["when"] == WHEN and text(admin, key).json()["text"] == text_of("call.md")      # начат с копии созвона
    assert text(admin, key, "default").status_code == 404
    # ручной запуск и страницы показывают новый тип; разбор получает его в задании
    assert admin.get("/api/admin/status").json()["templates"][key] == "Разбор инцидента"
    assert admin.get("/api/browse").json()["templates"][key] == "Разбор инцидента"
    assert f"- {key} — Разбор инцидента: {WHEN}" in templates.load_triage(web.cfg.prompts_dir)
    assert admin.post("/api/admin/run", json={"file": "запись.mp4", "template": key}).status_code == 200
    assert web.service.queue[0].template == key
    web.service.queue.clear()
    assert admin.post("/api/admin/run", json={"file": "запись.mp4", "template": "выдуманный"}).status_code == 400

    # правка шаблона своего типа и его описания
    own = "ШАБЛОН ИНЦИДЕНТА\n\n## 1. Что случилось\n\n## 2. Задачи\n\n| Задача | Срок |\n"
    assert admin.post("/api/admin/templates/save", json={"id": key, "text": own}).json()["changed"]
    changed = admin.post("/api/admin/templates/type", json={"new": False, "key": key, "title": "Разбор сбоя",
                                                           "when": WHEN, "actions": True}).json()
    assert changed["ok"] and items(admin)[key]["title"] == "Разбор сбоя" and items(admin)[key]["actions"]
    refused = admin.post("/api/admin/templates/type", json={"new": False, "key": "meeting", "title": "Иное", "when": WHEN})
    assert refused.status_code == 400 and "только у типов, добавленных на странице" in refused.json()["detail"]

    # удаление: стандартный — нельзя; свой — уходит из списков, текст остаётся в прошлых версиях
    refused = admin.post("/api/admin/templates/type/delete", json={"key": "meeting"})
    assert refused.status_code == 400 and "удалить можно только свой тип" in refused.json()["detail"]
    assert ivanov.post("/api/admin/templates/type/delete", json={"key": key}).status_code == 403
    assert admin.post("/api/admin/templates/type/delete", json={"key": key}).json()["ok"]
    assert key not in items(admin) and key not in admin.get("/api/admin/status").json()["templates"]
    assert admin.post("/api/admin/run", json={"file": "запись.mp4", "template": key}).status_code == 400
    assert not (web.cfg.prompts_dir / f"{key}.md").exists()
    assert own.strip() in templates.backup_text(web.cfg.prompts_dir, f"{key}.md",
                                                templates.backups(web.cfg.prompts_dir, f"{key}.md")[0]["id"])
    assert key not in web.options.template_notes()
    assert [k for k in kinds(web) if k.startswith("type_")] == ["type_create", "type_change", "type_delete"]


def test_rebuild_of_a_record_made_by_a_removed_type_lets_triage_choose_again(site):
    web, admin, _, _ = staffed(site)
    seed(web)
    from tests.test_web import voiced
    key = admin.post("/api/admin/templates/type", json={"title": "Разбор инцидента", "when": WHEN}).json()["key"]
    file = voiced(web, "Проекты/планёрка.mp4")
    web.service.ledger.put({**web.service.ledger.get(file), "template": key, "template_auto": False,
                            "template_title": "Разбор инцидента"})
    admin.post("/api/admin/templates/type/delete", json={"key": key})
    # запись осталась как была: тип и его название — в журнале обработки
    assert web.service.ledger.get(file)["template_title"] == "Разбор инцидента"
    done = admin.post("/api/admin/names", json={"file": file, "parts": {}, "lines": {}, "rebuild": True})
    assert done.status_code == 200 and web.service.queue[0].template is None       # шаблон выберет разбор
    assert any("типа «razbor_incidenta» больше нет" in line for line in web.log.tail_from(0)[1])


def test_templates_are_read_by_a_manage_key_and_changed_only_on_the_page(site):
    web, admin, _, _ = staffed(site)
    seed(web)
    steward, _ = keyed(web, admin, "с управлением", 30, manage=True)
    robot, _ = keyed(web, admin, "обычный")
    assert steward.get("/api/admin/templates").status_code == 200 and text(steward, "meeting").status_code == 200
    assert robot.get("/api/admin/templates").json()["detail"] == web.BY_KEY_ONLY_READS
    original = text_of("meeting.md")
    for client, detail in ((steward, web.PAGE_ONLY), (robot, web.BY_KEY_ONLY_READS)):
        for answer in (client.post("/api/admin/templates/save", json={"id": "meeting", "text": "## Раздел"}),
                       client.put("/api/admin/templates/upload", params={"id": "meeting"}, content=b"## x"),
                       client.post("/api/admin/templates/restore", json={"id": "meeting", "version": "default"}),
                       client.post("/api/admin/templates/type", json={"title": "Разбор инцидента", "when": WHEN}),
                       client.post("/api/admin/templates/type/delete", json={"key": "incident"})):
            assert answer.status_code == 403 and answer.json()["detail"] == detail
    assert (web.cfg.prompts_dir / "meeting.md").read_text(encoding="utf-8") == original
    assert not (web.cfg.prompts_dir / "types.json").exists()


def test_personal_mode_edits_templates_without_login(tmp_path):
    """Шаблоны нужны и одному человеку за своим компьютером."""
    with running(tmp_path, mode="personal") as (web, client):
        seed(web)
        assert len(items(client)) == 8
        edited = text_of("lecture.md") + "\n\n## 8. Домашнее задание\n"
        assert client.post("/api/admin/templates/save", json={"id": "lecture", "text": edited}).json()["changed"]
        assert items(client)["lecture"]["by"] == "owner"
        made = client.post("/api/admin/templates/type", json={"title": "Собеседование", "when": WHEN})
        assert made.status_code == 200 and made.json()["key"] == "sobesedovanie"
        assert client.post("/api/admin/templates/restore", json={"id": "lecture", "version": "default"}).json()["changed"]
        assert client.post("/api/admin/templates/type/delete", json={"key": "sobesedovanie"}).json()["ok"]


# ---------- словарь папки и записи в обработке ----------

WORDS = dictionary.merge([{"scope": "folder", "path": "Сбои", "people": dictionary.parse_people(
    "Ершова Лидия Марковна (Лида) — главный бухгалтер"), "terms": dictionary.parse_terms("КС-2 — акт о приёмке работ")}])


def test_dictionary_goes_to_every_text_stage_after_the_glossary(prompts, tmp_path, monkeypatch):
    """Разбор, поручения и протокол получают список участников и сокращений — следом за общим словарём."""
    (prompts / "glossary.txt").write_text("СКУД\n", encoding="utf-8")
    cfg, model = staged(tmp_path, prompts), Model("meeting")
    monkeypatch.setattr(llm, "chat", model.chat)

    async def listed(file_dir, stem, prompt, cfg, log, notes="", glossary="", who=None):
        model.asked.append(("actions", glossary + "\n\n---\nРАСШИФРОВКА:"))       # шаг поручений получает тот же блок
        return ""
    monkeypatch.setattr(actions, "extract_file", listed)
    lines = []
    record = asyncio.run(pipeline.process(cfg, "Сбои/обмен.mp3", "обмен", None, False, False, lines.append, words=WORDS))
    assert record["status"] == "ok", record["error"]
    assert [stage for stage, _ in model.asked] == ["triage", "actions", "summary"]
    for stage, user in model.asked:
        assert "СПИСОК УЧАСТНИКОВ И СОКРАЩЕНИЙ" in user, stage
        assert "- Ершова Лидия Марковна — главный бухгалтер (ещё называют: Лида)" in user and "- КС-2 — акт о приёмке работ" in user
        # общий словарь — раньше, расшифровка — позже; при расхождении побеждает список
        assert user.index("СЛОВАРЬ (термины") < user.index("СПИСОК УЧАСТНИКОВ") < user.index("РАСШИФРОВКА:"), stage
        assert "верь этому списку" in user
    assert "словарь папки и записи: участников 1, сокращений 1" in lines[0]
    # без словаря блока нет вовсе
    model.asked.clear()
    (cfg.outbox / "Сбои/обмен/transcript/обмен.triage.json").unlink()
    asyncio.run(pipeline.process(cfg, "Сбои/обмен.mp3", "обмен", None, False, False, lines.append))
    assert model.asked and not any("СПИСОК УЧАСТНИКОВ" in user for _, user in model.asked)


def test_triage_is_redone_when_the_dictionary_changed_and_only_then(prompts, tmp_path, monkeypatch):
    """Разбор сводил имена со списком участников того раза: список поменяли — разбор пересчитывается;
    не меняли — берётся готовый, как и раньше."""
    cfg, model = staged(tmp_path, prompts), Model("meeting")
    monkeypatch.setattr(llm, "chat", model.chat)
    monkeypatch.setattr(actions, "extract_file", lambda *a, **kw: asyncio.sleep(0, ""))
    run = lambda words=None: (model.asked.clear(), asyncio.run(pipeline.process(
        cfg, "Сбои/обмен.mp3", "обмен", None, False, False, lambda s: None, words=words)),
        [stage for stage, _ in model.asked])[2]
    saved = lambda: json.loads((cfg.outbox / "Сбои/обмен/transcript/обмен.triage.json").read_text(encoding="utf-8"))

    assert "triage" in run() and saved()["dictionary"] == ""
    assert "triage" not in run()                                    # словаря нет и не было
    assert "triage" in run(WORDS) and saved()["dictionary"] == dictionary.fingerprint(WORDS)
    assert "triage" not in run(WORDS)                               # тот же словарь
    assert "triage" in run()                                        # словарь убрали
    # разбор, сделанный до появления словарей, годится, пока словаря нет
    old = saved()
    del old["dictionary"]
    (cfg.outbox / "Сбои/обмен/transcript/обмен.triage.json").write_text(json.dumps(old), encoding="utf-8")
    assert "triage" not in run() and "triage" in run(WORDS)


def test_recognition_gets_the_hints_record_first_and_the_glossary_last(prompts, tmp_path, monkeypatch):
    """При распознавании заново модели уходят подсказки: словарь записи и папки, затем общий словарь."""
    (prompts / "glossary.txt").write_text("СКУД, КС-2\n", encoding="utf-8")
    cfg, model, got = staged(tmp_path, prompts), Model("meeting"), {}
    monkeypatch.setattr(llm, "chat", model.chat)
    monkeypatch.setattr(actions, "extract_file", lambda *a, **kw: asyncio.sleep(0, ""))
    monkeypatch.setattr(pipeline.media, "ensure_audio", lambda src, out_dir, stem, force, log: src)

    async def transcribe(audio_path, out_dir, stem, cfg, work_dir, on_progress=None, log=None, terms=()):
        got["terms"] = list(terms)
        return pipeline._load_stage(out_dir, stem, pipeline.MODE)
    monkeypatch.setattr(speech, "transcribe_one", transcribe)
    asyncio.run(pipeline.process(cfg, "Сбои/обмен.mp3", "обмен", None, True, True, lambda s: None, words=WORDS))
    assert got["terms"] == ["КС-2", "Ершова Лидия Марковна", "Лидия Марковна", "Ершова", "Лида", "СКУД", "КС-2"]
    assert speech.fit_terms("microsoft/mai-transcribe-2", got["terms"]) == got["terms"][:-1]        # повтор уходит один раз


def test_shipped_prompts_tell_to_treat_the_list_as_a_hint():
    """Правила о списке участников стоят в задании на разбор, на поручения и во всех четырёх шаблонах
    (в шаблонах — частью из общих правил: проверяется собранный текст, тот, что уходит модели)."""
    assert "6. Список участников — подсказка, а не перечень присутствующих" in text_of("triage.md")
    for name in templates.TEMPLATES:
        body = templates.load_template(STANDARD, name)[1]
        assert "подсказка, а не перечень присутствующих" in body and "Ершова Лидия Марковна" in body, name
        assert "не попадает вовсе" in body and "пиши в точности как в списке" in " ".join(body.split()), name
    assert "исполнителем\n   по списку не назначай" in text_of("actions.md")


# ---------- общие правила шаблонов ----------

# Отпечатки собранных заданий модели из поставки. Сняты до выноса общих правил в common.md — модель
# получала ровно тот же текст, что и раньше, — и переснимаются только с намеренной правкой текста
# (1.3.0: голоса в «Участниках», факт один раз, фамилии, метки времени).
ASSEMBLED = {
    "meeting": "b605db9dbd227feb4703d333eec20554cf32dd45e2b11e4897241bdd74b531b6",
    "lecture": "efb9460316ee18fa36c43d533ec0e8749fea82a9457d4bb4b3c2dc92b6a11acd",
    "call": "44d399e3198ce2d4e37808438a5ba153ff43bd71e6e509c2c78011fb2f8ba6d9",
    "spec": "ce47234be537b6c7816fc1781b71553230b562819fb214259e3540f5a255f612",
    "triage": "684ca88474f65129dc264e38266eb41a0cfc4c5f15bc8e32ae6e40c27df1547f",
    "actions": "4a4187c34e394b2c1ab14312f5bc88cafa76d6bb7a648a9632cc2b009168a6a5",
}


def sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assembled(prompts_dir) -> dict:
    out = {key: templates.load_template(prompts_dir, key)[1] for key in templates.TEMPLATES}
    return {**out, "triage": templates.load_triage(prompts_dir), "actions": templates.load_actions(prompts_dir)}


def test_shipped_templates_give_the_model_the_same_text_after_the_common_rules_moved_out():
    assert {key: sha(text) for key, text in assembled(STANDARD).items()} == ASSEMBLED
    # одинаковое действительно вынесено: каждый кусок общего файла подключают хотя бы два шаблона
    found = templates.pieces(text_of(templates.COMMON_FILE))
    used = {key: templates.includes(text_of(meta["file"])) for key, meta in templates.TEMPLATES.items()}
    assert found and set(found) == {name for names in used.values() for name in names}
    for name in found:
        assert sum(name in names for names in used.values()) >= 2, name
    assert all(used.values())                                       # общие правила есть в каждом шаблоне
    assert "Ершова Лидия Марковна" in text_of(templates.COMMON_FILE) and "Ершова" not in text_of("meeting.md")


def test_common_piece_reaches_every_template_that_includes_it(prompts):
    common = text_of(templates.COMMON_FILE)
    rule = "Если после шаблона дан список участников — это подсказка, а не перечень присутствующих."
    assert rule in common
    templates.save(prompts, templates.COMMON_FILE, common.replace(rule, rule + " Новое общее правило."))
    for key, text in assembled(prompts).items():
        assert ("Новое общее правило." in text) == (key in templates.TEMPLATES), key

    # свой текст без строк «@@» — как раньше: собирается как есть
    own = assembled(STANDARD)["meeting"] + "\n\n## 8. Своё\n"
    (prompts / "meeting.md").write_text(own, encoding="utf-8")
    assert templates.load_template(prompts, "meeting")[1] == own.strip()
    # подключён кусок, которого нет, — шаблон не годится, и обработка говорит, чего не хватает
    (prompts / "call.md").write_text(text_of("call.md") + "\n@@ нет-такого\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="нет-такого.*common.md"):
        templates.load_template(prompts, "call")
    (prompts / templates.COMMON_FILE).unlink()
    with pytest.raises(RuntimeError, match="common.md"):
        templates.load_template(prompts, "lecture")
    assert templates.load_template(prompts, "meeting")[1] == own.strip()            # без «@@» общий файл не нужен


def test_common_file_and_includes_are_checked_before_saving(prompts):
    meeting = text_of("meeting.md")
    # шаблон проверяется в том виде, в каком уйдёт модели: правила из общего файла в нём есть
    assert templates.check_file(prompts, "type", meeting) == []
    for bad, why in ((meeting + "\n@@ нет-такого\n", "«нет-такого»"),
                     (meeting + "\n@@ два слова\n", "имени куска в ней не разобрать")):
        with pytest.raises(templates.TemplateError, match=why):
            templates.check_file(prompts, "type", bad)
    common = text_of(templates.COMMON_FILE)
    assert templates.check_file(prompts, "common", common) == []
    assert templates.kind_of("common") == "common" and templates.file_of(prompts, "common") == templates.COMMON_FILE
    piece = "@@ список-участников"
    without = common.replace(piece, "@@ другое-имя")
    with pytest.raises(templates.TemplateError, match="список-участников.*meeting.md"):
        templates.check_file(prompts, "common", without)
    for bad, why in ((common + "\n" + piece + "\nещё раз\n", "дважды"), (common + "\n@@ пустой\n\n", "пустой")):
        with pytest.raises(templates.TemplateError, match=why):
            templates.check_file(prompts, "common", bad)
    assert "ни одного куска" in templates.check("common", "только пояснение")[0]


def test_common_rules_are_one_more_file_on_the_templates_page(site):
    web, admin, _, _ = staffed(site)
    seed(web)
    seen = items(admin)
    common = seen["common"]
    assert (common["kind"], common["file"], common["state"], common["problem"], common["warnings"]) == (
        "common", templates.COMMON_FILE, "standard", "", [])
    assert "@@ имя" in common["when"] and common["includes"] == []
    assert seen["meeting"]["includes"] == templates.includes(text_of("meeting.md")) and seen["call"]["includes"]
    assert seen["triage"]["includes"] == [] and seen["meeting"]["warnings"] == []
    assert text(admin, "common").json()["text"] == text_of(templates.COMMON_FILE)

    # кусок, который подключают шаблоны, из общего файла не убрать: обработка встала бы
    original = text_of(templates.COMMON_FILE)
    refused = admin.post("/api/admin/templates/save", json={
        "id": "common", "text": original.replace("@@ список-участников", "@@ переименован")})
    assert refused.status_code == 400 and "meeting.md" in refused.json()["detail"]
    # правка куска действует во всех шаблонах, которые его подключают, со следующей записи
    edited = original.replace("а не перечень присутствующих.", "а не перечень присутствующих. Новое правило.", 1)
    saved = admin.post("/api/admin/templates/save", json={"id": "common", "text": edited}).json()
    assert saved["changed"] and saved["warnings"] == [] and len(items(admin)["common"]["backups"]) == 1
    for key in templates.TEMPLATES:
        assert "Новое правило." in templates.load_template(web.cfg.prompts_dir, key)[1], key
    assert items(admin)["common"]["state"] == "changed" and items(admin)["meeting"]["state"] == "standard"
    # шаблон, подключающий то, чего нет, не сохраняется
    bad = admin.post("/api/admin/templates/save", json={"id": "meeting", "text": text_of("meeting.md") + "\n@@ нет-такого\n"})
    assert bad.status_code == 400 and "нет-такого" in bad.json()["detail"]
    assert admin.post("/api/admin/templates/restore", json={"id": "common", "version": "default"}).json()["changed"]
    assert {key: sha(t) for key, t in assembled(web.cfg.prompts_dir).items()} == ASSEMBLED


def test_untouched_templates_of_an_older_version_move_to_the_common_rules(tmp_path):
    """Установка до общего файла: в томе шаблонов лежат тексты целиком. Нетронутые сервис при запуске
    заменяет новыми, с подключением общих правил, — модель получает тот же текст, а правка общего
    файла дальше действует и в них. Правленные руками остаются как есть и собираются как раньше."""
    old = assembled(STANDARD)
    with running(tmp_path) as (web, client):
        prompts = web.cfg.prompts_dir
        prompts.mkdir(parents=True, exist_ok=True)
        for key in ("meeting", "lecture"):
            (prompts / f"{key}.md").write_text(old[key] + "\n", encoding="utf-8")
        (prompts / "call.md").write_text(old["call"] + "\n\n## 8. Наш раздел\n", encoding="utf-8")
        seed(web)
        assert (prompts / "meeting.md").read_text(encoding="utf-8") == text_of("meeting.md")
        assert (prompts / "lecture.md").read_text(encoding="utf-8") == text_of("lecture.md")
        assert (prompts / "call.md").read_text(encoding="utf-8") == old["call"] + "\n\n## 8. Наш раздел\n"
        assert {key: sha(t) for key, t in assembled(prompts).items() if key != "call"} == {
            key: d for key, d in ASSEMBLED.items() if key != "call"}
        assert templates.load_template(prompts, "call")[1] == old["call"] + "\n\n## 8. Наш раздел"
        admin = enter(web, "admin", ADMIN_PASSWORD)
        seen = items(admin)
        assert (seen["meeting"]["state"], seen["meeting"]["outside"], seen["meeting"]["backups"]) == ("standard", False, [])
        assert seen["call"]["state"] == "changed" and seen["call"]["includes"] == []
        # второй запуск ничего не меняет
        before = {p.name: p.read_bytes() for p in prompts.iterdir() if p.is_file()}
        seed(web)
        assert {p.name: p.read_bytes() for p in prompts.iterdir() if p.is_file()} == before
