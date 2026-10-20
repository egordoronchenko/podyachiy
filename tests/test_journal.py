import sqlite3

import pytest

from app import db, journal, usage

usd = lambda v: {"USD": v}


def run(j, file, cost, by="", reason="new", audio=600.0, stages=None):
    return j.run({"file": file, "status": "ok", "cost": cost, "audio_duration_sec": audio,
                  "cost_models": stages or [{"stage": "speech", "model": "nova", "cost": cost}]}, by, reason)


def test_run_is_charged_to_the_uploader_or_to_whoever_started_it(tmp_path):
    j = journal.Journal(db.Database(tmp_path))
    j.write("upload", "ivanov", file="Проекты/Сметы/а.mp4", size=10)
    run(j, "Проекты/Сметы/а.mp4", 0.5)
    run(j, "Проекты/Сметы/а.mp4", 0.1, by="admin", reason="rebuild")
    run(j, "Проекты/б.mp4", 0.2)                              # положили мимо страницы
    rep = journal.report(journal.Journal(db.Database(tmp_path)).read())    # читается с диска заново
    assert rep["total"] == {"cost": {"USD": 0.8}, "runs": 3, "records": 2, "audio_sec": 1200.0}
    assert rep["currencies"] == ["USD"]
    assert {u["login"]: u["cost"] for u in rep["users"]} == {"ivanov": usd(0.5), "admin": usd(0.1), "": usd(0.2)}
    assert [(f["folder"], f["cost"], f["own"]) for f in rep["folders"]] == [
        ("Проекты", usd(0.8), usd(0.2)), ("Проекты/Сметы", usd(0.6), usd(0.6))]
    assert rep["records"][0] == {"file": "Проекты/Сметы/а.mp4", "cost": usd(0.6), "runs": 2,
                                 "records": 1, "audio_sec": 600.0}


def test_rename_keeps_spending_under_the_new_name_and_delete_keeps_it_at_all(tmp_path):
    j = journal.Journal(db.Database(tmp_path))
    j.write("upload", "ivanov", file="Старая/а.mp4", size=10)
    run(j, "Старая/а.mp4", 0.5)
    j.write("folder_rename", "admin", folder="Старая", to="Новая")
    run(j, "Новая/а.mp4", 0.1)
    j.write("folder_delete", "admin", folder="Новая", records=1, trash=".trash/x")
    rep = journal.report(j.read())
    assert [(f["folder"], f["cost"]) for f in rep["folders"]] == [("Новая", usd(0.6))]
    assert rep["users"] == [{"login": "ivanov", "cost": usd(0.6), "runs": 2, "records": 1, "audio_sec": 600.0}]


def test_periods_and_buckets(tmp_path):
    events = [{"at": f"{day}T10:00:00+03:00", "kind": "run", "by": "", "file": "П/а.mp4", "cost": 1.0}
              for day in ("2026-09-28", "2026-10-04", "2026-10-05", "2027-01-01")]
    series = lambda bucket: [(s["period"], s["cost"]["USD"]) for s in journal.report(events, bucket=bucket)["series"]]
    assert series("week") == [("2026-09-28", 2.0), ("2026-10-05", 1.0), ("2026-12-28", 1.0)]
    assert series("month") == [("2026-09", 1.0), ("2026-10", 2.0), ("2027-01", 1.0)]
    assert series("year") == [("2026", 3.0), ("2027", 1.0)]
    assert journal.report(events, "2026-10-04", "2026-10-05")["total"]["cost"] == usd(2.0)


def test_stages_and_models(tmp_path):
    j = journal.Journal(db.Database(tmp_path))
    run(j, "П/а.mp4", 0.6, stages=[{"stage": "speech", "model": "nova", "cost": 0.5},
                                   {"stage": "summary", "model": "deepseek", "cost": 0.1}])
    assert journal.report(j.read())["stages"] == [
        {"stage": "speech", "model": "nova", "cost": usd(0.5), "runs": 1},
        {"stage": "summary", "model": "deepseek", "cost": usd(0.1), "runs": 1}]


def test_event_is_kept_whole_and_is_found_by_what_it_is_about(tmp_path):
    j = journal.Journal(db.Database(tmp_path))
    j.write("login", "ivanov")
    j.write("upload", "ivanov", file="Проекты/Сметы/а.mp4", size=10)
    j.write("access", "admin", folder="Проекты", login="petrov", read="tree", upload=None, manage=None)
    j.write("folder_create", "petrov", folder="Проекты отдела")
    run(j, "Проекты/Сметы/а.mp4", 0.5)
    again = journal.Journal(db.Database(tmp_path))                  # с диска, другим соединением
    kinds = lambda **how: [e["kind"] for e in again.actions(**how)]
    assert kinds() == ["folder_create", "access", "upload", "login"]            # свежие первыми, прогонов здесь нет
    assert again.actions(limit=1)[0] == {**again.actions()[0], "kind": "folder_create", "by": "petrov",
                                         "folder": "Проекты отдела"}
    assert again.actions(kind="access")[0]["read"] == "tree" and again.actions(kind="access")[0]["upload"] is None
    assert kinds(by="ivanov") == ["upload", "login"] and kinds(login="petrov") == ["access"]
    assert kinds(file="Проекты/Сметы/а.mp4") == ["upload"]
    # папка — с подпапками; «Проекты отдела» в «Проекты» не входит
    assert kinds(folder="Проекты") == ["access", "upload"] and kinds(folder="Проекты отдела") == ["folder_create"]
    day = again.actions()[0]["at"][:10]
    assert kinds(start=day, end=day) == kinds() and kinds(end="2000-01-01") == [] and kinds(start="2999-01-01") == []
    assert again.count() == 5 and [e["kind"] for e in again.read()][-1] == "run"


def test_report_from_the_base_is_the_same_as_the_events_replayed_in_order(tmp_path):
    """Пути прогонов база ведёт при записи событий о переименовании, переносе и удалении;
    тот же отчёт, сосчитанный проигрыванием событий по порядку, обязан совпасть."""
    j = journal.Journal(db.Database(tmp_path))
    j.write("upload", "ivanov", file="Проекты/а.mp4", size=10)
    run(j, "Проекты/а.mp4", 0.5)
    run(j, "Проекты/Сметы/б.mp4", 0.3, by="petrov")
    j.write("folder_rename", "admin", folder="Проекты", to="Проекты отдела")
    run(j, "Проекты отдела/а.mp4", 0.1, reason="rebuild")
    j.write("record_move", "ivanov", file="Проекты отдела/а.mp4", to="Архив/а.mp4")
    j.write("record_delete", "ivanov", file="Проекты отдела/Сметы/б.mp4", trash=".trash/x")
    j.write("folder_rename", "admin", folder="Проекты отдела", to="Старое")      # удалённая запись уже не переезжает
    run(j, "Проекты_2/в.mp4", 0.7)                                              # похожее имя папки — не подпапка
    j.write("folder_rename", "admin", folder="Проекты", to="Нет такой")
    for bucket in journal.BUCKETS:
        assert j.report(bucket=bucket) == journal.report(j.read(), bucket=bucket)
    assert {r["file"]: r["cost"] for r in j.report()["records"]} == {
        "Архив/а.mp4": usd(0.6), "Проекты отдела/Сметы/б.mp4": usd(0.3), "Проекты_2/в.mp4": usd(0.7)}
    assert [r["file"] for r in j.read() if r["kind"] == "run"] == [
        "Проекты/а.mp4", "Проекты/Сметы/б.mp4", "Проекты отдела/а.mp4", "Проекты_2/в.mp4"]   # под каким путём шёл
    day = j.read()[0]["at"][:10]
    assert j.report(day, day) == journal.report(j.read(), day, day)
    assert j.report("2999-01-01")["total"] == {"cost": {}, "runs": 0, "records": 0, "audio_sec": 0}


def test_event_inside_a_cancelled_action_is_not_written_and_a_lone_one_does_not_break_the_action(tmp_path):
    lines = []
    base = db.Database(tmp_path)
    j = journal.Journal(base, lines.append)
    with pytest.raises(RuntimeError):
        with base.write():
            j.write("folder_rename", "admin", folder="А", to="Б")
            raise RuntimeError("действие сорвалось")
    assert j.read() == []
    base.con.execute("DROP TABLE uploads")                          # база не принимает событие о загрузке
    assert j.write("upload", "ivanov", file="а.mp4")["kind"] == "upload" and "не пишется" in lines[0]
    with pytest.raises(sqlite3.Error):
        with base.write():                                          # а внутри действия сбой события отменяет действие
            j.write("upload", "ivanov", file="а.mp4")


def test_record_rename_and_move_carry_spending_and_the_uploader(tmp_path):
    j = journal.Journal(db.Database(tmp_path))
    j.write("upload", "ivanov", file="Проекты/а.mp4", size=10)
    run(j, "Проекты/а.mp4", 0.5)
    run(j, "Проекты/б.mp4", 0.3)
    j.write("record_rename", "ivanov", file="Проекты/а.mp4", to="Проекты/итоги.mp4")
    j.write("record_move", "admin", file="Проекты/итоги.mp4", to="Архив/итоги.mp4")
    assert j.uploaded == {"Архив/итоги.mp4": "ivanov"}
    assert run(j, "Архив/итоги.mp4", 0.1)["uploaded_by"] == "ivanov"        # автор загрузки не потерялся
    again = journal.Journal(db.Database(tmp_path))                                        # и после перезапуска тоже
    assert again.uploaded == {"Архив/итоги.mp4": "ivanov"}
    rep = journal.report(again.read())
    assert {r["file"]: (r["cost"], r["runs"]) for r in rep["records"]} == {
        "Архив/итоги.mp4": (usd(0.6), 2), "Проекты/б.mp4": (usd(0.3), 1)}
    assert [(f["folder"], f["cost"]) for f in rep["folders"]] == [("Архив", usd(0.6)), ("Проекты", usd(0.3))]
    assert {u["login"]: u["cost"] for u in rep["users"]} == {"ivanov": usd(0.6), "": usd(0.3)}


def test_deleted_record_keeps_its_spending_under_the_last_path(tmp_path):
    j = journal.Journal(db.Database(tmp_path))
    j.write("upload", "ivanov", file="Проекты/а.mp4", size=10)
    run(j, "Проекты/а.mp4", 0.5)
    j.write("record_rename", "ivanov", file="Проекты/а.mp4", to="Проекты/итоги.mp4")
    j.write("record_delete", "ivanov", file="Проекты/итоги.mp4", trash=".trash/x")
    assert j.uploaded == {}
    # по тому же пути легла другая запись, её переименовали: расходы удалённой остаются где были
    run(j, "Проекты/итоги.mp4", 0.2, by="petrov")
    j.write("record_rename", "petrov", file="Проекты/итоги.mp4", to="Проекты/новая.mp4")
    rep = journal.report(j.read())
    assert {r["file"]: r["cost"] for r in rep["records"]} == {"Проекты/итоги.mp4": usd(0.5),
                                                              "Проекты/новая.mp4": usd(0.2)}
    assert rep["total"]["cost"] == usd(0.7)


TWO = [{"stage": "speech", "model": "scribe", "provider": "RouterAI", "currency": "RUB", "cost": 12.0},
       {"stage": "summary", "model": "deepseek", "provider": "OpenRouter", "currency": "USD", "cost": 0.04}]


def paid(j, file, stages, by="", audio=600.0):
    """Прогон, как его пишет обработчик: суммы по валютам и по поставщикам."""
    cost, spent = {}, {}
    for s in stages:
        cost[s["currency"]] = cost.get(s["currency"], 0) + s["cost"]
        spent[s["provider"], s["currency"]] = spent.get((s["provider"], s["currency"]), 0) + s["cost"]
    return j.run({"file": file, "status": "ok", "cost": cost, "audio_duration_sec": audio, "cost_models": stages,
                  "cost_providers": [{"provider": p, "currency": c, "cost": v} for (p, c), v in spent.items()]},
                 by, "new")


def test_report_keeps_roubles_and_dollars_apart(tmp_path):
    j = journal.Journal(db.Database(tmp_path))
    j.write("upload", "ivanov", file="Проекты/а.mp4", size=10)
    paid(j, "Проекты/а.mp4", TWO)                                       # основной лёг посреди записи
    paid(j, "Проекты/Сметы/б.mp4", [dict(TWO[0], cost=30.0)], by="petrov")
    paid(j, "Архив/в.mp4", [dict(TWO[1], stage="speech", model="scribe", cost=0.11)], by="petrov")
    event = j.read()[1]
    assert event["cost"] == {"RUB": 12.0, "USD": 0.04}
    assert event["providers"] == [{"provider": "RouterAI", "currency": "RUB", "cost": 12.0},
                                  {"provider": "OpenRouter", "currency": "USD", "cost": 0.04}]

    rep = journal.report(journal.Journal(db.Database(tmp_path)).read(), bucket="month")
    assert rep["currencies"] == ["RUB", "USD"]
    assert rep["total"] == {"cost": {"RUB": 42.0, "USD": 0.15}, "runs": 3, "records": 3, "audio_sec": 1800.0}
    assert {f["folder"]: (f["cost"], f["own"]) for f in rep["folders"]} == {
        "Проекты": ({"RUB": 42.0, "USD": 0.04}, {"RUB": 12.0, "USD": 0.04}),
        "Проекты/Сметы": ({"RUB": 30.0}, {"RUB": 30.0}),
        "Архив": ({"USD": 0.11}, {"USD": 0.11})}
    assert {u["login"]: u["cost"] for u in rep["users"]} == {
        "ivanov": {"RUB": 12.0, "USD": 0.04}, "petrov": {"RUB": 30.0, "USD": 0.11}}
    assert {p["provider"]: (p["cost"], p["runs"], p["records"]) for p in rep["providers"]} == {
        "RouterAI": ({"RUB": 42.0}, 2, 2), "OpenRouter": ({"USD": 0.15}, 2, 2)}
    # одна модель у двух поставщиков — одна строка с двумя суммами
    assert {(s["stage"], s["model"]): s["cost"] for s in rep["stages"]} == {
        ("speech", "scribe"): {"RUB": 42.0, "USD": 0.11}, ("summary", "deepseek"): {"USD": 0.04}}
    assert rep["series"] == [{"period": event["at"][:7], "cost": {"RUB": 42.0, "USD": 0.15}, "runs": 3,
                              "records": 3, "audio_sec": 1800.0}]
    # порядок «от дорогих» — по доле в своей валюте: 30 из 42 ₽ и 0,11 из 0,15 $ больше, чем 12 ₽ и 0,04 $
    assert [u["login"] for u in rep["users"]] == ["petrov", "ivanov"]


def test_run_written_without_currency_or_provider_is_kept_as_openrouter_dollars(tmp_path):
    """Прогон без валюты и поставщика (так писала версия с одним поставщиком) ложится в базу
    в полном виде: OpenRouter, доллары."""
    j = journal.Journal(db.Database(tmp_path))
    j.replace([{"at": "2026-10-01T10:00:00+03:00", "kind": "run", "by": "", "file": "П/а.mp4", "uploaded_by": "",
                "reason": "new", "status": "ok", "audio_sec": 600, "cost": 0.25,
                "stages": [{"stage": "speech", "model": "deepgram/nova-3", "cost": 0.2},
                           {"stage": "summary", "model": "deepseek", "cost": 0.05}]},
               {"at": "2026-10-01T11:00:00+03:00", "kind": "run", "by": "", "file": "П/б.mp4", "reason": "earlier",
                "cost": 1.3, "runs": 13, "stages": [{"stage": "earlier", "model": "", "cost": 1.3}]},
               {"at": "2026-10-01T12:00:00+03:00", "kind": "run", "by": "", "file": "П/в.mp4", "cost": 0.0}])
    paid(j, "П/г.mp4", [TWO[0]])
    runs = journal.runs(j.read())
    assert runs[0]["cost"] == {"USD": 0.25} and runs[2]["cost"] == {} and runs[2]["providers"] == []
    assert runs[0]["providers"] == [{"provider": "OpenRouter", "currency": "USD", "cost": 0.25}]
    assert runs[0]["stages"][0] == {"provider": "OpenRouter", "currency": "USD", "stage": "speech",
                                    "model": "deepgram/nova-3", "cost": 0.2}
    rep = j.report()
    assert rep == journal.report(j.read())
    assert rep["total"]["cost"] == {"RUB": 12.0, "USD": 1.55} and rep["total"]["runs"] == 16
    assert {p["provider"]: p["cost"] for p in rep["providers"]} == {"OpenRouter": {"USD": 1.55},
                                                                    "RouterAI": {"RUB": 12.0}}


def test_series_is_split_inside_the_period_by_folder_person_and_stage(tmp_path):
    at = lambda day: f"{day}T10:00:00+03:00"
    events = [
        {"at": at("2026-09-30"), "kind": "run", "by": "ivanov", "file": "Проекты/Сметы/а.mp4", "audio_sec": 3600,
         "cost": {"RUB": 12.0, "USD": 0.04}, "stages": TWO},
        {"at": at("2026-10-02"), "kind": "run", "by": "", "uploaded_by": "petrov", "file": "Проекты/б.mp4",
         "audio_sec": 1800, "cost": {"RUB": 30.0}, "stages": [dict(TWO[0], cost=30.0)]},
        {"at": at("2026-10-03"), "kind": "run", "by": "", "file": "в.mp4", "audio_sec": 600,     # запись вне папок
         "cost": {"USD": 0.1}, "stages": [dict(TWO[1], cost=0.1)]},
        # пересборка той же записи в том же месяце: деньги прибавились, звук — нет
        {"at": at("2026-10-09"), "kind": "run", "by": "ivanov", "file": "Проекты/б.mp4", "audio_sec": 1800,
         "cost": {"RUB": 3.0}, "stages": [dict(TWO[0], stage="summary", cost=3.0)]},
    ]
    rep = journal.report(events, bucket="month")
    assert rep["split"] == {
        "folders": {"2026-09": {"Проекты": {"RUB": 12.0, "USD": 0.04}},
                    "2026-10": {"Проекты": {"RUB": 33.0}, "": {"USD": 0.1}}},
        "users": {"2026-09": {"ivanov": {"RUB": 12.0, "USD": 0.04}},
                  "2026-10": {"petrov": {"RUB": 30.0}, "": {"USD": 0.1}, "ivanov": {"RUB": 3.0}}},
        "stages": {"2026-09": {"speech": {"RUB": 12.0}, "summary": {"USD": 0.04}},
                   "2026-10": {"speech": {"RUB": 30.0}, "summary": {"RUB": 3.0, "USD": 0.1}}}}
    # части складываются в сумму периода
    for row in rep["series"]:
        for parts in (rep["split"][group][row["period"]] for group in rep["split"]):
            total = {}
            for cost in parts.values():
                total = usage.plus(total, cost)
            assert total == row["cost"]
    # звук — отдельно по валюте, в которой за запись платили: час в рублях не делится на долларовый звук
    assert rep["paid_audio"] == {"total": {"RUB": 5400.0, "USD": 4200.0},
                                 "series": {"2026-09": {"RUB": 3600.0, "USD": 3600.0},
                                            "2026-10": {"RUB": 1800.0, "USD": 600.0}}}
    assert rep["total"]["audio_sec"] == 6000.0
    # границы периода действуют и на разбивку
    assert list(journal.report(events, "2026-10-01", "2026-10-31", "month")["split"]["folders"]) == ["2026-10"]
    assert journal.report([], bucket="month")["split"] == {"folders": {}, "users": {}, "stages": {}}
    assert journal.report([], bucket="month")["paid_audio"] == {"total": {}, "series": {}}
