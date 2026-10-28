"""Реестр поручений: строки из нескольких протоколов, отбор, сортировка, память разбора
и раздел «Поручения прошлого совещания» в новом протоколе."""
import asyncio
import json
import os
import types
from pathlib import Path

from app import actions, external, ledger, marks, mindmap, pipeline, registry, summarize, triage
from app.config import Settings

MD = """# Протокол совещания

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Иванов Иван** | | | |
| Согласовать смету | до пятницы | [00:10:05] | в работе |
| Закрыть акт | 15.10 | [00:12:00] | выполнено |
| **Ответственный не назван** | | | |
| Проверить остатки | — | — | — |
| **Не участвовали в совещании** | | | |
| Петрова: прислать договор | не назван | [00:20:00] | ждёт |
"""
UNMARKED = lambda text: marks.merged(external.tasks(text), {})       # поручения протокола, отметок нет
OTHER = ("## 5. Задачи\n\n| Задача | Срок |\n|---|---|\n| **Пётр Сидоров** | |\n| Проверить смету | завтра |\n"
         "| **Семён** | |\n| Собрать замечания | — |\n")


def protocols(tmp_path, **texts):
    """Записи одной папки с протоколами на диске: имя → текст."""
    records = []
    for n, (name, text) in enumerate(texts.items()):
        path = tmp_path / name / "summary" / f"{name}.md"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
        records.append({"file": f"Планёрки/{name}.mp4", "stem": name, "status": "ok", "title": name.capitalize(),
                        "finished_at": f"2026-10-0{n + 1}T10:00:00+03:00"})
    return records, lambda rec: tmp_path / rec["stem"] / "summary" / f"{rec['stem']}.md"


def test_rows_join_protocols_and_carry_the_meeting_and_the_link(tmp_path):
    records, path_of = protocols(tmp_path, первая=MD, вторая=OTHER)
    records.append({"file": "Планёрки/пустая.mp4", "stem": "пустая", "status": "ok"})       # протокола на диске нет
    found, scanned = registry.rows(records, path_of, registry.Cache(), "http://s.test")
    assert scanned == 2 and len(found) == 6
    assert found[0] == {"who": "Иванов Иван", "absent": False, "task": "Согласовать смету", "due_text": "до пятницы",
                        "due": None, "due_by": None, "due_at": None, "overdue": False,
                        "status": "в работе", "at": "00:10:05", "at_sec": 605, "open": True, "due_date": "2026-10-02",
                        "key": marks.key("Иванов Иван", "Согласовать смету"), "done": False, "done_by": None,
                        "done_at": None, "comment": None, "unsigned": False,
                        "file": "Планёрки/первая.mp4", "dir": "Планёрки", "title": "Первая", "date": "2026-10-01",
                        "link": "http://s.test/?path=%D0%9F%D0%BB%D0%B0%D0%BD%D1%91%D1%80%D0%BA%D0%B8&open="
                                "%D0%9F%D0%BB%D0%B0%D0%BD%D1%91%D1%80%D0%BA%D0%B8%2F%D0%BF%D0%B5%D1%80%D0%B2%D0%B0%D1%8F.mp4&t=605"}
    assert found[2]["link"].endswith(".mp4") and found[2]["at_sec"] is None           # без метки — просто запись
    assert registry.people(found) == ["Иванов Иван", "Пётр Сидоров", "Петрова", "Семён"]


def test_pick_filters_by_person_due_state_and_words(tmp_path):
    records, path_of = protocols(tmp_path, первая=MD, вторая=OTHER)
    found, _ = registry.rows(records, path_of, registry.Cache())
    tasks = lambda **kw: [r["task"] for r in registry.pick(found, **kw)]
    assert tasks(who=" иванов ") == ["Согласовать смету", "Закрыть акт"]
    assert tasks(who="семен") == ["Собрать замечания"]                                # ё и е — одно
    assert tasks(who="никто") == []
    assert tasks(due=True) == ["Согласовать смету", "Закрыть акт", "Проверить смету"]   # «не назван» — не срок
    assert "Закрыть акт" not in tasks(only_open=True) and len(tasks(only_open=True)) == 5
    assert tasks(q="СМЕТУ") == ["Согласовать смету", "Проверить смету"]
    assert tasks(q="смету вторая") == ["Проверить смету"]                             # слова ищутся и в названии совещания
    assert tasks(q="смету", who="сидоров", due=True, only_open=True) == ["Проверить смету"]


def test_order_by_meeting_and_by_due(tmp_path):
    records, path_of = protocols(tmp_path, первая=MD, вторая=OTHER)
    found, _ = registry.rows(records, path_of, registry.Cache())
    by_date = registry.order(found, "date")
    assert [r["date"] for r in by_date] == ["2026-10-02"] * 2 + ["2026-10-01"] * 4
    assert [r["task"] for r in by_date[2:]] == ["Согласовать смету", "Закрыть акт", "Проверить остатки", "прислать договор"]
    by_due = registry.order(found, "due")
    assert [(r["task"], r["due_date"]) for r in by_due[:3]] == [
        ("Согласовать смету", "2026-10-02"), ("Проверить смету", "2026-10-03"), ("Закрыть акт", "2026-10-15")]
    assert all(r["due_date"] is None for r in by_due[3:])


def test_due_is_counted_from_the_meeting_day_when_it_can_be():
    thursday = "2026-10-01"
    for said, day in (("сегодня вечером", "2026-10-01"), ("завтра", "2026-10-02"), ("послезавтра", "2026-10-03"),
                      ("в пятницу", "2026-10-02"), ("до вторника включительно", "2026-10-06"),
                      ("четверг", "2026-10-08"),                      # сказано в четверг — значит, следующий
                      ("до конца недели", "2026-10-04"), ("на этой неделе", "2026-10-04"),
                      ("на следующей неделе", "2026-10-11"), ("15.10", "2026-10-15"), ("до 3.11.2026", "2026-11-03"),
                      ("к 20 октября", "2026-10-20"), ("до 15 января", "2027-01-15"), ("5 мая", "2027-05-05"),
                      ("после отпуска", None), ("не назван", None), ("Не назван («по горячим следам»)", None),
                      ("31.02", None), (None, None)):
        assert registry.due_day(said, thursday) == day, said
    assert registry.due_day("завтра", "") is None


def test_open_is_read_from_the_status_words():
    for status in (None, "в работе", "ждёт", "приоритетная", "не выполнено", "задача в работе, частично выполнена"):
        assert registry.is_open(status), status
    for status in ("выполнено", "Сделано", "закрыта", "готово", "отменено", "решено на совещании"):
        assert not registry.is_open(status), status


def test_cache_reads_a_protocol_once_and_again_only_when_it_changes(tmp_path):
    records, path_of = protocols(tmp_path, первая=MD, вторая=OTHER)
    cache = registry.Cache()
    first, _ = registry.rows(records, path_of, cache)
    assert cache.reads == 2 and len(cache) == 2
    again, _ = registry.rows(records, path_of, cache)
    assert again == first and cache.reads == 2                        # второй запрос файлы не читал
    path = path_of(records[1])
    path.write_text(OTHER + "| Отправить отчёт | — |\n", encoding="utf-8")           # протокол пересобрали
    fresh, _ = registry.rows(records, path_of, cache)
    assert cache.reads == 3 and [r["task"] for r in fresh][-1] == "Отправить отчёт"
    # тот же размер, другое время изменения — тоже новый файл
    path.write_text(path.read_text(encoding="utf-8").replace("Отправить", "Проверить"), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    assert [r["task"] for r in registry.rows(records, path_of, cache)[0]][-1] == "Проверить отчёт" and cache.reads == 4

    path.unlink()                                                     # запись удалили
    assert registry.rows(records, path_of, cache)[1] == 1 and len(cache) == 1
    cache.prune(7, lambda: [])                                        # журнал изменился: записей больше нет
    assert len(cache) == 0
    registry.rows(records[:1], path_of, cache)
    cache.prune(7, lambda: [])                                        # та же версия журнала — ничего не делает
    assert len(cache) == 1


def rec(file, day="", first="", status="ok", **more):
    return {"file": file, "status": status, "record_date": day, "first_done_at": first,
            "finished_at": more.pop("finished", first), **more}


def test_previous_is_the_record_just_before_this_one_in_the_same_folder():
    folder = [rec("П/а.mp4", "2026-09-01", "2026-09-20T10:00:00+03:00"),         # дату назвал разбор
              rec("П/б.mp4", "", "2026-09-10T10:00:00+03:00"),                   # дата — день первой обработки
              rec("П/в.mp4", "", "2026-09-10T15:00:00+03:00"),
              rec("П/сбой.mp4", "", "2026-09-12T10:00:00+03:00", status="no_summary"),
              rec("П/Архив/г.mp4", "", "2026-09-13T10:00:00+03:00"),             # подпапка — другая серия
              rec("Другая/д.mp4", "", "2026-09-14T10:00:00+03:00")]
    prev = lambda file, day, first: (registry.previous(folder, file, registry.order_key(rec(file, day, first))) or {}).get("file")
    assert prev("П/новая.mp4", "", "2026-09-15T10:00:00+03:00") == "П/в.mp4"
    assert prev("П/новая.mp4", "2026-09-10", "2026-09-15T10:00:00+03:00") == "П/в.mp4"   # в один день — по времени обработки
    assert prev("П/новая.mp4", "2026-09-05", "2026-09-15T10:00:00+03:00") == "П/а.mp4"   # старое совещание обработали позже
    assert prev("П/новая.mp4", "2026-08-01", "2026-09-15T10:00:00+03:00") is None
    assert prev("П/в.mp4", "", "2026-09-10T15:00:00+03:00") == "П/б.mp4"                 # сама себе не предыдущая
    assert prev("Третья/е.mp4", "", "2026-09-15T10:00:00+03:00") is None


def test_rebuilt_record_keeps_its_place_in_the_row():
    """Пересборка старого протокола не делает запись «новейшей»: место в ряду — по первой обработке."""
    folder = [rec("П/а.mp4", "", "2026-09-01T10:00:00+03:00"),
              rec("П/б.mp4", "", "2026-09-08T10:00:00+03:00", finished="2026-10-09T10:00:00+03:00"),   # пересобрана
              rec("П/в.mp4", "", "2026-09-15T10:00:00+03:00")]
    cfg = types.SimpleNamespace(inbox=Path("нет"), outbox=Path("нет"), public_url="")
    said = []
    assert pipeline.previous_tasks(cfg, "П/б.mp4", folder, "", "2026-10-10T10:00:00+03:00", said.append, series=True) == ""
    assert said == []                         # предыдущая — «а», её протокола на диске нет: молча без раздела
    key = registry.order_key({"record_date": "", "finished_at": "2026-10-10T10:00:00+03:00",
                              "first_done_at": folder[1]["first_done_at"]})
    assert registry.previous(folder, "П/б.mp4", key)["file"] == "П/а.mp4"
    assert external.day_of(folder[1]) == "2026-09-08"


def test_block_lists_open_tasks_with_links_and_is_not_parsed_as_tasks_of_the_new_record():
    prev = {"file": "Планёрки/вкс 1.mp4", "title": "Планёрка [вкс]", "finished_at": "2026-10-01T10:00:00+03:00"}
    block = registry.block(prev, UNMARKED(MD), "http://s.test")
    link = "http://s.test/?path=%D0%9F%D0%BB%D0%B0%D0%BD%D1%91%D1%80%D0%BA%D0%B8&open=%D0%9F%D0%BB%D0%B0%D0%BD%D1%91%D1%80%D0%BA%D0%B8%2F%D0%B2%D0%BA%D1%81%201.mp4"
    assert block.splitlines() == [
        "## Поручения прошлого совещания", "",
        f"Незакрытые поручения из протокола записи [«Планёрка (вкс)» от 01.10.2026]({link}). "
        "Раздел добавлен сервисом: эта папка — серия совещаний; статус — каким он был на том совещании. "
        "Убрать раздел можно кнопкой у его заголовка на странице записи.", "",
        f"- **Иванов Иван:** Согласовать смету; срок: до пятницы; статус: в работе; [в записи 00:10:05]({link}&t=605)",
        "- **Ответственный не назван:** Проверить остатки",
        f"- **Петрова (не участвовал в совещании):** прислать договор; статус: ждёт; [в записи 00:20:00]({link}&t=1200)"]
    assert "Закрыть акт" not in block                                  # выполненное не напоминаем
    assert summarize.MARK_RE.findall(block) == []                      # меток [чч:мм:сс] нет: они вели бы на эту запись

    new = "# Протокол\n\n## 5. Задачи\n\n| Задача | Срок |\n|---|---|\n| **Сидоров** | |\n| Сверить склад | — |\n"
    assert external.tasks(new + "\n" + block) == external.tasks(new)   # поручения не задваиваются
    assert [t["task"] for t in external.tasks(new + "\n" + block)] == ["Сверить склад"]
    # и когда раздела «Задачи» в новом протоколе нет вовсе
    assert external.tasks("# Конспект\n\n## 1. Тема\n\nТекст.\n\n" + block) == []
    assert registry.block(prev, [t for t in UNMARKED(MD) if t["status"] == "выполнено"]) == ""
    assert registry.block(prev, []) == ""


def test_map_and_plain_text_understand_the_links_of_the_block():
    block = registry.block({"file": "П/а.mp4", "title": "Планёрка"}, UNMARKED(MD))
    page = mindmap.render(block)
    assert '<a href="/?path=%D0%9F&amp;open=%D0%9F%2F%D0%B0.mp4&amp;t=605">в записи 00:10:05</a>' in page
    assert '<span class="ts">' not in page
    assert mindmap.plain("см. [«Планёрка»](/?open=a) и [00:10:05]") == "см. «Планёрка» и [00:10:05]"
    assert mindmap.inline("[плохая](javascript:alert(1))") == "[плохая](javascript:alert(1))"
    tree = mindmap.parse("# Протокол\n\n" + block)
    assert [s["t"] for s in tree["c"]] == ["Поручения прошлого совещания"]


TRI = {"type": "meeting", "title": "Планёрка", "gist": "О складе", "date": "", "topics": [], "speakers": [],
       "confidence": "высокая", "type_reason": ""}


def processed(tmp_path, monkeypatch, name, neighbours, prev_tasks=True, protocol=MD, series=True):
    """Настоящий pipeline.process над готовой расшифровкой; модели подменены."""
    root = Path(__file__).resolve().parent.parent
    cfg = types.SimpleNamespace(inbox=tmp_path / "in", outbox=tmp_path / "out", prompts_dir=root / "prompts",
                                model="deepgram/nova-3", summary_model="тест", actions_models=("тест",),
                                summary_max_tokens=0, prev_tasks=prev_tasks, public_url="")
    (cfg.inbox / "Планёрки").mkdir(parents=True, exist_ok=True)
    (cfg.inbox / "Планёрки" / f"{name}.mp3").write_bytes(b"x")
    out = cfg.outbox / "Планёрки" / name / "transcript"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(
        {"segments": [{"start": sec, "end": sec + 1, "speaker": "S1", "text": "Согласуйте смету."}
                      for sec in (605, 720, 1200)]}), encoding="utf-8")       # метки протокола MD — из расшифровки
    (out / f"{name}.meta.json").write_text(json.dumps(
        {"success": True, "model": cfg.model, "audio_duration_sec": 700.0}), encoding="utf-8")

    async def model(*a, **kw):
        return protocol, {}

    async def no_actions(*a, **kw):
        return ""

    monkeypatch.setattr(triage, "load_saved", lambda *a, **kw: dict(TRI))
    monkeypatch.setattr(summarize, "summarize", model)
    monkeypatch.setattr(actions, "extract_file", no_actions)
    lines = []
    record = asyncio.run(pipeline.process(cfg, f"Планёрки/{name}.mp3", name, None, False, False, lines.append,
                                          neighbours=neighbours, series=series))
    assert record["status"] == "ok", record.get("error")
    return cfg, record, (cfg.outbox / "Планёрки" / name / "summary" / f"{name}.md").read_text(encoding="utf-8"), lines


def test_new_protocol_gets_the_open_tasks_of_the_previous_record(tmp_path, monkeypatch):
    cfg, first, text, _ = processed(tmp_path, monkeypatch, "первая", [])
    assert registry.BLOCK_TITLE not in text                            # первая запись папки: напоминать нечего
    first["first_done_at"] = "2026-10-01T10:00:00+03:00"           # первая обработана раньше

    new = MD.replace("Согласовать смету", "Сверить склад")
    cfg, second, text, lines = processed(tmp_path, monkeypatch, "вторая", [first], protocol=new)
    assert text.startswith("<!-- протокол:") and text.count("## Поручения прошлого совещания") == 1
    assert text.rstrip().endswith("[в записи 00:20:00](/?path=%D0%9F%D0%BB%D0%B0%D0%BD%D1%91%D1%80%D0%BA%D0%B8&open="
                                  "%D0%9F%D0%BB%D0%B0%D0%BD%D1%91%D1%80%D0%BA%D0%B8%2F%D0%BF%D0%B5%D1%80%D0%B2%D0%B0%D1%8F.mp3&t=1200)")
    assert "**Иванов Иван:** Согласовать смету" in text and "«Планёрка» от 01.10.2026" in text
    assert any("поручения прошлого совещания: предыдущая запись папки — Планёрки/первая.mp3" in ln for ln in lines)
    # поручения самой записи — только её собственные; сверка меток времени раздел не считает
    assert [t["task"] for t in external.tasks(text)] == ["Сверить склад", "Закрыть акт", "Проверить остатки", "прислать договор"]
    assert second["timestamps"] == 3 and second["summary_chars"] == len(new)
    assert "Поручения прошлого совещания" in (cfg.outbox / "Планёрки/вторая/summary/вторая.map.html").read_text(encoding="utf-8")
    assert (cfg.outbox / "Планёрки/первая/summary/первая.md").read_text(encoding="utf-8").count("Поручения прошлого") == 0

    # пересборка второй: прежний протокол уходит в prev/, раздел в новом — один, а не два
    second["first_done_at"] = second["finished_at"]
    _, _, again, _ = processed(tmp_path, monkeypatch, "вторая", [first, second], protocol=new)
    assert again.count("## Поручения прошлого совещания") == 1 and "Согласовать смету" in again


def test_prev_tasks_setting_switches_the_block_off(tmp_path, monkeypatch):
    _, first, _, _ = processed(tmp_path, monkeypatch, "первая", [])
    first["first_done_at"] = "2026-10-01T10:00:00+03:00"           # первая обработана раньше
    _, _, text, lines = processed(tmp_path, monkeypatch, "вторая", [first], prev_tasks=False)
    assert registry.BLOCK_TITLE not in text and not any("прошлого совещания" in ln for ln in lines)

    monkeypatch.delenv("PREV_TASKS", raising=False)
    assert Settings.from_env().prev_tasks                              # по умолчанию включено
    for value, on in (("1", True), ("0", False), ("нет", False), ("off", False), ("", True)):
        monkeypatch.setenv("PREV_TASKS", value)
        assert Settings.from_env().prev_tasks is on, value


def test_block_is_built_only_in_a_series_folder_and_not_after_a_person_removed_it(tmp_path, monkeypatch):
    _, first, _, _ = processed(tmp_path, monkeypatch, "первая", [])
    first["first_done_at"] = "2026-10-01T10:00:00+03:00"           # первая обработана раньше
    # папка не отмечена как серия совещаний — раздела нет, и в журнале работы сказано почему
    _, second, text, lines = processed(tmp_path, monkeypatch, "вторая", [first], series=False)
    assert registry.BLOCK_TITLE not in text
    assert "папка не отмечена как серия совещаний — поручения прошлого совещания не ищу" in lines
    # серия, но у этой записи раздел убрали кнопкой — пересборка его не возвращает
    second.update(first_done_at=second["finished_at"], **{ledger.REMOVED: True})
    _, _, text, lines = processed(tmp_path, monkeypatch, "вторая", [first, second])
    assert registry.BLOCK_TITLE not in text
    assert "раздел «Поручения прошлого совещания» у этой записи убран вручную — не добавляю" in lines
    # а без отметки — раздел на месте
    del second[ledger.REMOVED]
    _, _, text, _ = processed(tmp_path, monkeypatch, "вторая", [first, second])
    assert text.count("## Поручения прошлого совещания") == 1


def test_records_in_the_root_never_get_the_block(tmp_path):
    """Две несвязанные записи в корне — не серия: раздел не собирается, даже если признак передан.
    Те же две записи в папке — серия, и раздел есть."""
    cfg = types.SimpleNamespace(inbox=tmp_path / "in", outbox=tmp_path / "out", public_url="")
    for folder in ("", "П"):
        path = cfg.outbox / folder / "а" / "summary" / "а.md"
        path.parent.mkdir(parents=True)
        path.write_text(MD, encoding="utf-8")
    pair = lambda folder: [rec(f"{folder}а.mp4", "2026-10-01", "2026-10-01T10:00:00+03:00", stem="а"),
                           rec(f"{folder}б.mp4", "2026-10-08", "2026-10-08T10:00:00+03:00", stem="б")]
    said = []
    assert pipeline.previous_tasks(cfg, "б.mp4", pair(""), "2026-10-08", "2026-10-08T10:00:00+03:00", said.append,
                                   series=True) == ""
    assert said == ["запись в корне — поручения прошлого совещания не ищу"]
    tail = pipeline.previous_tasks(cfg, "П/б.mp4", pair("П/"), "2026-10-08", "2026-10-08T10:00:00+03:00",
                                   lambda s: None, series=True)
    assert tail.startswith("## Поручения прошлого совещания") and "Согласовать смету" in tail


def test_block_is_cut_out_and_put_back_where_the_service_puts_it():
    tail = registry.block({"file": "П/а.mp4", "title": "Планёрка"}, UNMARKED(MD))
    head = "<!-- протокол: модель тест -->\n\n# Протокол\n\n## 1. Кратко\n\nРешили.\n"
    whole = head + "\n" + tail                                          # так его дописывает summarize_file
    assert registry.has_block(whole) and not registry.has_block(head)
    assert registry.without_block(whole) == head and registry.with_block(head, tail) == whole
    assert registry.without_block(head) == head                         # раздела нет — текст как был
    # после раздела дописали свой раздел руками — вырезается только раздел сервиса
    assert registry.without_block(whole + "\n## 9. Приписка\n\nТекст.\n") == head + "\n## 9. Приписка\n\nТекст.\n"
    # похожий заголовок, написанный не сервисом, разделом не считается
    assert not registry.has_block("# Протокол\n\n## 6. Поручения прошлого совещания и прочее\n\nТекст.\n")
