"""Большая установка: 100 000 событий и 20 000 записей. Страницы «Расходы», журнал действий,
список записей и реестр поручений отвечают за разумное время.

Пределы взяты с запасом в несколько раз от замеренного (в скобках — замер на машине разработки):
тест ловит возврат к чтению всего журнала на каждый запрос, а не разницу между машинами.
"""
import json
import time

import pytest

from app import db, journal, marks
from tests.test_web import ADMIN_PASSWORD, enter, running

RECORDS, ACTIONS = 20_000, 60_000
MD = """# Протокол

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Иванов Иван** | | | |
| Согласовать смету по объекту {n} | до пятницы | [00:10:05] | в работе |
| Закрыть акт {n} | 15.10 | [00:12:00] | выполнено |
| **Петров Пётр** | | | |
| Проверить остатки по складу {n} | — | — | — |
"""


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    """Установка, проработавшая полтора года: запись раз в сорок минут, у каждой два прогона."""
    root = tmp_path_factory.mktemp("big")
    base = db.Database(root / "state")
    stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S+03:00", time.gmtime(t))
    day0 = 1_735_722_000                                            # 01.01.2025
    events, rows, noted = [], [], []
    for n in range(RECORDS):
        folder, stem = f"Отдел {n % 20:02d}/Серия {n % 200 // 20:02d}", f"Совещание {n:05d}"
        file, t, loader = f"{folder}/{stem}.mp3", day0 + n * 2400, f"user{n % 300:03d}"
        out = root / "outbox" / folder / stem / "summary"
        out.mkdir(parents=True)
        (out / f"{stem}.md").write_text(MD.format(n=n), encoding="utf-8")
        stages = [{"stage": "speech", "model": "scribe", "provider": "RouterAI", "currency": "RUB", "cost": 20.0 + n % 7},
                  {"stage": "summary", "model": "deepseek", "provider": "RouterAI", "currency": "RUB", "cost": 1.5}]
        events.append((t, {"at": stamp(t), "kind": "upload", "by": loader, "file": file, "size": 50_000_000}))
        for k, (reason, by, spent) in enumerate((("new", "", stages), ("rebuild", loader, stages[1:]))):
            total = sum(s["cost"] for s in spent)
            events.append((t + 600 + k * 3600, {
                "at": stamp(t + 600 + k * 3600), "kind": "run", "by": by, "file": file, "uploaded_by": loader,
                "reason": reason, "status": "ok", "started_at": stamp(t + 60), "audio_sec": 3000.0, "cost": {"RUB": total},
                "stages": spent, "providers": [{"provider": "RouterAI", "currency": "RUB", "cost": total}]}))
        record = {"file": file, "stem": stem, "status": "ok", "finished_at": stamp(t + 4200), "runs": 2,
                  "title": f"Совещание отдела {n % 20} № {n}", "record_date": stamp(t)[:10], "summary": f"summary/{stem}.md",
                  "audio_duration_sec": 3000.0, "cost": {"RUB": 1.5}, "cost_total": {"RUB": 23.0 + n % 7},
                  "gist": "Обсудили план и сроки. " * 4, "topics": ["план", "сроки", "бюджет"]}
        rows.append((file, "ok", record["finished_at"], json.dumps(record, ensure_ascii=False)))
        if n % 10 == 0:
            who, task = "Иванов Иван", f"Согласовать смету по объекту {n}"
            noted.append((file, marks.key(who, task), who, task, "done", loader, stamp(t + 86400)))
    for n in range(ACTIONS):
        t = day0 + (n * 7919) % (RECORDS * 2400)
        kind, who = ("login", "names", "task_done", "access", "dictionary")[n % 5], f"user{n % 300:03d}"
        more = {"login": {}, "names": {"file": rows[n % RECORDS][0]}, "task_done": {"file": rows[n % RECORDS][0], "task": "т"},
                "access": {"folder": f"Отдел {n % 20:02d}", "login": who, "read": "tree"},
                "dictionary": {"folder": f"Отдел {n % 20:02d}", "people": 3, "terms": 5}}[kind]
        events.append((t, {"at": stamp(t), "kind": kind, "by": who, **more}))
    events.sort(key=lambda pair: pair[0])
    journal.Journal(base).replace([event for _, event in events])
    with base.write() as con:
        con.executemany("INSERT INTO records (file, status, finished_at, data, marks_at) VALUES (?, ?, ?, ?, 0)", rows)
        con.executemany("INSERT INTO marks (file, key, who, task, status, done_by, done_at) VALUES (?, ?, ?, ?, ?, ?, ?)", noted)
        con.execute("UPDATE records SET marks_at = 1760000000 WHERE file IN (SELECT file FROM marks)")
    base.close()
    assert len(events) == 120_000
    with running(root) as (web, _):
        admin = enter(web, "admin", ADMIN_PASSWORD)
        saved = admin.post("/api/admin/users/save", json={"login": "reader", "name": "Читатель", "password": "пароль-1234"})
        assert saved.status_code == 200
        admin.post("/api/access", json={"dir": "Отдел 00", "login": "reader", "read": "tree"})
        yield web, admin, enter(web, "reader", "пароль-1234")


def timed(call, limit: float, what: str):
    """Ответ запроса и проверка, что лучший из трёх уложился в предел (секунды)."""
    best, answer = None, None
    for _ in range(3):
        started = time.perf_counter()
        answer = call()
        spent = time.perf_counter() - started
        best = spent if best is None else min(best, spent)
        assert answer.status_code == 200, answer.text[:300]
    assert best < limit, f"{what}: {best:.2f} с при пределе {limit} с"
    return answer.json() if answer.headers["content-type"].startswith("application/json") else answer.text


def test_costs_for_a_month_are_picked_by_the_index_and_for_all_time_stay_bearable(big):
    web, admin, _ = big
    month = timed(lambda: admin.get("/api/admin/report", params={"start": "2026-03-01", "end": "2026-03-31", "bucket": "day"}),
                  1.5, "расходы за месяц")                             # (0,11 с; прежде 1,08 с)
    assert month["total"]["runs"] == 2232 and len(month["series"]) == 31
    whole = timed(lambda: admin.get("/api/admin/report", params={"bucket": "month"}), 15, "расходы за всё время")   # (2,1 с; прежде 2,5 с)
    assert whole["total"]["runs"] == 2 * RECORDS and whole["total"]["records"] == RECORDS
    assert whole["total"]["cost"] == {"RUB": float(sum(23.0 + n % 7 for n in range(RECORDS)))}      # до копейки
    # запрос за месяц читает прогоны месяца, а не весь журнал: план запроса идёт по индексу дня
    plan = " ".join(row[3] for row in web.base.rows(
        "EXPLAIN QUERY PLAN SELECT runs.id FROM runs WHERE runs.day >= ? AND runs.day <= ?", "2026-03-01", "2026-03-31"))
    assert "runs_day" in plan and "SCAN" not in plan
    book = timed(lambda: admin.get("/api/admin/report.csv", params={"start": "2026-03-01", "end": "2026-03-31"}), 4, "отчёт файлом")
    assert len(book.splitlines()) > 2232 and "Прогоны" in book


def test_action_journal_reads_the_last_events_without_the_whole_journal(big):
    web, admin, _ = big
    last = timed(lambda: admin.get("/api/admin/journal", params={"limit": 200}), 0.5, "журнал действий")   # (0,004 с; прежде 0,78 с)
    assert len(last["events"]) == 200 and all(e["kind"] != "run" for e in last["events"])
    assert last["events"][0]["at"] >= last["events"][-1]["at"]
    one = timed(lambda: admin.get("/api/admin/journal", params={"dir": "Отдел 08", "kind": "access"}), 0.5, "журнал по папке")
    assert len(one["events"]) == 200 and {e["folder"] for e in one["events"]} == {"Отдел 08"}
    by = timed(lambda: admin.get("/api/admin/journal", params={"by": "user123", "since": "2026-01-01", "until": "2026-01-31"}),
               0.5, "журнал по человеку за месяц")
    assert by["events"] and all(e["by"] == "user123" and e["at"].startswith("2026-01") for e in by["events"])
    for query, index in (("kind <> 'run' ORDER BY id DESC LIMIT 200", "events_actions"),
                         ("actor = 'user123' ORDER BY id DESC LIMIT 200", "events_actor"),
                         ("file = 'x' ORDER BY id DESC LIMIT 200", "events_file")):
        plan = " ".join(row[3] for row in web.base.rows(f"EXPLAIN QUERY PLAN SELECT at FROM events WHERE {query}"))
        assert index in plan, plan


def test_list_of_records_and_the_status_page_do_not_slow_down(big):
    web, admin, reader = big
    folder = timed(lambda: admin.get("/api/browse", params={"path": "Отдел 03/Серия 02"}), 2, "список записей папки")   # (0,09 с)
    assert len(folder["files"]) == 100
    timed(lambda: admin.get("/api/browse"), 2, "обзор корня")                                                           # (0,10 с)
    status = timed(lambda: admin.get("/api/admin/status"), 2, "состояние обработки")                                    # (0,07 с)
    assert status["totals"]["files"] == RECORDS and status["storage"]["bytes"] > 50 * 2 ** 20


def test_task_registry_answers_for_a_reader_at_once_and_for_everything_in_seconds(big):
    web, admin, reader = big
    mine = timed(lambda: reader.get("/api/v1/tasks", params={"open": "true"}), 5, "реестр читателя")       # (0,26 с)
    assert (mine["records"], mine["total"], mine["open"]) == (1000, 3000, 1000)                             # из трёх поручений записи одно закрыто словом протокола
    everything = timed(lambda: admin.get("/api/v1/tasks"), 40, "реестр целиком")                           # (4,4 с; первый раз 9 с — читаются 20 000 протоколов)
    assert (everything["records"], everything["total"]) == (RECORDS, 3 * RECORDS)
    assert everything["open"] == 2 * RECORDS - RECORDS // 10                                                # отметки из базы учтены
