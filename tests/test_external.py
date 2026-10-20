import datetime

from app import external

MD = """# Протокол совещания

## 4. Решения

| Решение | В записи |
|---|---|
| Перейти на недельный план | [00:54:12] |

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Пётр Петрович** | | | |
| Подготовить заявку на стеллажи | на этой неделе | [00:36:59] | — |
| Сверить остатки на складе | — | [00:26:34]; [00:27:10] | приоритетная |
| **Ответственный не назван** | | | |
| Обновить список поставщиков | — | — | — |
| **Не участвовали в совещании** | | | |
| Иван: напомнить всем про график отпусков | — | [00:22:31] | в работе |

Прочерк в колонке «Срок» — срок на совещании не назвали.

## 6. Открытые вопросы

| Вопрос | В записи |
|---|---|
| Кто оплачивает доставку | [01:02:03] |
"""


def test_tasks_carry_owner_due_place_and_status():
    got = external.tasks(MD)
    assert got == [
        {"who": "Пётр Петрович", "absent": False, "task": "Подготовить заявку на стеллажи",
         "due": "на этой неделе", "status": None, "at": "00:36:59", "at_sec": 2219},
        {"who": "Пётр Петрович", "absent": False, "task": "Сверить остатки на складе",
         "due": None, "status": "приоритетная", "at": "00:26:34", "at_sec": 1594},
        {"who": None, "absent": False, "task": "Обновить список поставщиков", "due": None, "status": None,
         "at": None, "at_sec": None},
        {"who": "Иван", "absent": True, "task": "напомнить всем про график отпусков", "due": None,
         "status": "в работе", "at": "00:22:31", "at_sec": 1351},
    ]


def test_older_protocols_keep_the_owner_in_a_column():
    md = ("## 5. Задачи\n\n| Задача (что сделать) | Ответственный (кто) | Срок (когда) | Приоритет/статус (если назван) |\n"
          "|---|---|---|---|\n| Протестировать релиз | Петрова | до четверга | В работе |\n")
    assert external.tasks(md) == [{"who": "Петрова", "absent": False, "task": "Протестировать релиз",
                                   "due": "до четверга", "status": "В работе", "at": None, "at_sec": None}]


def test_protocol_without_tasks_section_gives_nothing():
    assert external.tasks("# Конспект лекции\n\n## 1. Тема\n\n| Термин | Пояснение |\n|---|---|\n| Кэш | память |\n") == []


def rec(file, finished, status="ok"):
    return {"file": file, "status": status, "finished_at": finished, "title": "Т"}


def test_listing_returns_only_newer_records_oldest_first():
    records = [rec("Совещания/б.mp4", "2026-10-09T12:00:00+03:00"),
               rec("Совещания/а.mp4", "2026-10-09T10:00:00+03:00"),
               rec("Обучение/в.mp4", "2026-10-09T11:00:00+03:00", status="no_audio")]
    assert [r["name"] for r in external.listing(records, None, "")] == ["а.mp4", "в.mp4", "б.mp4"]
    since = external.parse_since("2026-10-09T10:00:00+03:00")
    assert [r["name"] for r in external.listing(records, since, "")] == ["в.mp4", "б.mp4"]
    assert [r["name"] for r in external.listing(records, None, "Совещания")] == ["а.mp4", "б.mp4"]
    # другой часовой пояс — тот же момент
    assert [r["name"] for r in external.listing(records, external.parse_since("2026-10-09T08:00:00+00:00"), "")] == ["б.mp4"]


def test_since_survives_a_plus_turned_into_space_by_the_address():
    assert external.parse_since("2026-10-09T10:00:00 03:00") == datetime.datetime(
        2026, 10, 9, 10, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=3)))


def test_links_are_encoded_and_html_only_for_ready_records():
    ready = external.brief(rec("Совещания/Проекты отдела/запись 1.mp4", "2026-10-09T10:00:00+03:00"), "http://s.test")
    assert ready["dir"] == "Совещания/Проекты отдела" and ready["state"] == "ready" and ready["has_protocol"]
    assert ready["links"]["record"] == ("http://s.test/api/v1/record?file=%D0%A1%D0%BE%D0%B2%D0%B5%D1%89%D0%B0%D0%BD"
                                        "%D0%B8%D1%8F%2F%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B%20%D0%BE%D1%82%D0%B4%D0%B5%D0%BB%D0%B0%2F"
                                        "%D0%B7%D0%B0%D0%BF%D0%B8%D1%81%D1%8C%201.mp4")
    assert ready["links"]["html"].startswith("http://s.test/api/v1/record.html?file=")
    failed = external.brief(rec("а.mp4", "2026-10-09T10:00:00+03:00", "error"))
    assert failed["links"]["html"] is None and failed["state"] == "error" and not failed["has_protocol"]
    assert failed["links"]["page"] == "/?path=&open=%D0%B0.mp4"
    assert external.page_url("П/а.mp4", "http://s.test", 605) == "http://s.test/?path=%D0%9F&open=%D0%9F%2F%D0%B0.mp4&t=605"


def test_date_is_the_meeting_day_when_named_and_the_processing_day_otherwise():
    done = "2026-10-09T10:00:00+03:00"
    assert external.day_of({"record_date": "2026-01-14", "finished_at": done}) == "2026-01-14"
    for named in ("", None, "14.01.2026", "январь"):
        assert external.day_of({"record_date": named, "finished_at": done}) == "2026-10-09", named
    assert external.day_of({}) == ""
    # пересборка протокола дату не сдвигает: день — когда запись обработана впервые
    assert external.day_of({"first_done_at": "2026-09-01T10:00:00+03:00", "finished_at": done}) == "2026-09-01"
    assert external.short({"file": "а.mp4", "status": "no_summary", "finished_at": done, "audio_duration_sec": 61}) == {
        "file": "а.mp4", "title": None, "date": "2026-10-09", "duration_sec": 61, "state": "no_protocol",
        "has_protocol": False}


def test_inside_means_the_folder_itself_or_any_folder_below():
    assert external.inside("Проекты/Сметы", "Проекты") and external.inside("Проекты", "") and not external.inside("Проекты отдела", "Проекты")
