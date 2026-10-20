"""Первый запуск версии с базой над установкой прежнего вида: перенос файлов состояния в базу,
сверка, папка «до-базы-…», повторный запуск, сбой посередине, пустая установка."""
import json
import os

import pytest

from app import db, dictionary, journal, ledger, marks, migrate, settings, signup, usage, users
from tests import legacy

HOME = users.HOME_ROOT
AT = "2026-10-0{}T10:00:00+03:00"
TASK = {"status": "done", "by": "ivanov", "at": AT.format(5), "comment": "сдано", "who": "Иванов Иван",
        "task": "Согласовать смету по складу"}
DUE = {"due": "2026-10-20", "due_by": "ivanov", "due_at": AT.format(6), "who": None, "task": "Проверить остатки"}
STAGES = [{"stage": "speech", "model": "scribe", "provider": "RouterAI", "currency": "RUB", "cost": 12.5},
          {"stage": "summary", "model": "deepseek", "provider": "OpenRouter", "currency": "USD", "cost": 0.041234}]


def old_install(tmp_path):
    """Установка прежней версии, в которой есть всё: пользователи с правами, ключи, словари, настройки,
    оформление, заявки, журнал событий с переименованиями, журнал обработки и отметки поручений."""
    cfg = legacy.setup(tmp_path)
    account = {"email": "", "password": users.hash_password("пароль-1234"), "admin": False, "read": {}, "upload": {},
               "manage": {}, "home_upload": False, "notify_ready": True, "source": "local"}
    legacy.put(cfg.state_dir / "users.json", {"users": {
        "ivanov": {**account, "name": "Иванов Иван", "email": "ivanov@example.test", "home": f"{HOME}/ivanov",
                   "read": {"Проекты": "tree", "": "tree"}, "upload": {"Проекты": "only"}, "manage": {"Снабжение": "tree"},
                   "home_upload": True, "link": {"hash": "f" * 64, "expires": 1790000000}},
        "petrov": {**account, "name": "Петров", "home": f"{HOME}/petrov", "admin": True, "notify_ready": False},
        "sidorov": {**account, "name": "Сидоров", "home": f"{HOME}/sidorov", "password": "", "source": "domain",
                    "checked": 1760000000}}})
    legacy.put(cfg.state_dir / "keys.json", {"keys": [
        {"id": "0a1b2c3d", "hash": "a" * 64, "prefix": "stg_aaaaaaaa", "owner": "ivanov", "name": "Cursor",
         "kind": "plain", "created": 1000, "expires": None, "used": 2000},
        {"id": "4e5f6a7b", "hash": "b" * 64, "prefix": "stg_bbbbbbbb", "owner": "admin", "name": "управление",
         "kind": "manage", "created": 3000, "expires": 3000 + 90 * 86400, "used": None}]})
    people = [{"name": "Ершова Лидия Марковна", "role": "главный бухгалтер", "aka": ["Лидия Марковна"]}]
    legacy.put(cfg.state_dir / "dictionaries.json", {
        "folders": {"Проекты": {"people": people, "terms": [{"term": "КС-2", "meaning": "акт"}], "by": "ivanov",
                                "at": AT.format(1), "changed": 1759300000.25}},
        "records": {"Проекты/план.mp4": {"people": [], "terms": [{"term": "СКУД", "meaning": ""}], "by": "petrov",
                                         "at": AT.format(2), "changed": 1759400000.5}}})
    legacy.put(cfg.state_dir / "settings.json", {
        "speech_model": {"value": "elevenlabs/scribe-v2", "by": "petrov", "at": AT.format(3)},
        "templates": {"meeting": {"by": "petrov", "at": AT.format(3), "digest": "0123abcd"}}})
    legacy.put(cfg.state_dir / "brand" / "brand.json", {"name": "ООО Ромашка", "logo": "0a1b2c3d4e5f", "by": "petrov",
                                                        "at": AT.format(3), "source": {"format": "PNG", "width": 512,
                                                                                       "height": 512, "bytes": 999}})
    for name in ("logo.png", "logo-small.png", "favicon.png"):
        (cfg.state_dir / "brand" / name).write_bytes(b"png")
    legacy.put(cfg.state_dir / "requests.json", {"requests": [
        {"id": "aa11", "at": AT.format(1), "name": "Орлова", "email": "orlova@example.test", "login": "orlova",
         "comment": "отдел кадров", "status": "pending"},
        {"id": "bb22", "at": AT.format(1), "name": "Сидоров", "email": "", "login": "sidorov", "comment": "",
         "status": "approved", "decided_at": AT.format(2), "decided_by": "petrov"}]})
    (cfg.state_dir / "session.key").write_bytes(b"k" * 32)
    (cfg.state_dir / "ldap-ca.pem").write_text("сертификат", encoding="utf-8")

    run = lambda day, file, by="", **more: {"at": AT.format(day), "kind": "run", "by": by, "file": file,
                                            "uploaded_by": "ivanov", "reason": "new", "status": "ok",
                                            "started_at": AT.format(day), "audio_sec": 1800.5,
                                            "cost": {"RUB": 12.5, "USD": 0.041234}, "stages": STAGES,
                                            "providers": [{"provider": "RouterAI", "currency": "RUB", "cost": 12.5},
                                                          {"provider": "OpenRouter", "currency": "USD", "cost": 0.041234}],
                                            **more}
    legacy.put(cfg.state_dir / "journal.jsonl", [
        {"at": AT.format(1), "kind": "login", "by": "ivanov"},
        {"at": AT.format(1), "kind": "upload", "by": "ivanov", "file": "Старая/план.mp4", "size": 10},
        run(1, "Старая/план.mp4"),
        # строка времён одного поставщика: число без валюты, этапы без поставщика
        {"at": AT.format(2), "kind": "run", "by": "", "file": "Старая/смета.mp4", "cost": 0.25, "audio_sec": 600,
         "stages": [{"stage": "speech", "model": "deepgram/nova-3", "cost": 0.2},
                    {"stage": "summary", "model": "deepseek", "cost": 0.05}]},
        {"at": AT.format(3), "kind": "folder_rename", "by": "petrov", "folder": "Старая", "to": "Проекты"},
        run(4, "Проекты/план.mp4", by="petrov", reason="rebuild"),
        {"at": AT.format(5), "kind": "record_rename", "by": "ivanov", "file": "Проекты/смета.mp4", "to": "Проекты/итоги.mp4"},
        {"at": AT.format(6), "kind": "record_delete", "by": "ivanov", "file": "Проекты/итоги.mp4", "trash": ".trash/x"},
        {"at": AT.format(7), "kind": "access", "by": "admin", "folder": "Проекты", "login": "ivanov", "read": "tree",
         "upload": None, "manage": None, "via": "key", "key": "управление", "key_kind": "manage"}])

    record = lambda file, stem, **more: {"file": file, "stem": stem, "status": "ok", "finished_at": AT.format(4),
                                         "output": str(cfg.outbox / "Проекты" / stem), "runs": 2,
                                         "cost": {"RUB": 12.5, "USD": 0.041234}, "cost_total": {"RUB": 25.0, "USD": 0.082468},
                                         "audio_duration_sec": 1800.5, "summary": f"summary/{stem}.md", **more}
    legacy.put(cfg.outbox / "_state" / "processed.json", {"updated_at": "…", "totals": {}, "files": [
        record("Проекты/план.mp4", "план"), record("Проекты/без отметок.mp4", "без отметок"),
        record("Проекты/сняты.mp4", "сняты", status="no_summary", failures=1)]})
    legacy.put(cfg.outbox / "Проекты" / "план" / "план.tasks.json", {"0123456789abcdef": TASK, "fedcba9876543210": DUE})
    legacy.put(cfg.outbox / "Проекты" / "сняты" / "сняты.tasks.json", {})          # все отметки сняты — файл остался
    os.utime(cfg.outbox / "Проекты" / "сняты" / "сняты.tasks.json", (1759500000, 1759500000))
    legacy.put(cfg.outbox / ".trash" / "x" / "итоги" / "итоги.tasks.json", {"k": TASK})   # в корзине — не трогается
    (cfg.outbox / "Проекты" / "план" / "summary").mkdir()
    (cfg.outbox / "Проекты" / "план" / "summary" / "план.md").write_text("# Протокол", encoding="utf-8")
    return cfg


def tree(cfg) -> dict:
    """Все файлы установки с содержимым — чтобы сравнить «до» и «после»."""
    return {p.relative_to(cfg.state_dir.parent).as_posix(): p.read_bytes()
            for root in (cfg.state_dir, cfg.outbox) for p in sorted(root.rglob("*")) if p.is_file()}


def test_everything_moves_into_the_base_and_reads_back_the_same(tmp_path):
    cfg = old_install(tmp_path)
    before = tree(cfg)
    lines = []
    base = legacy.start(cfg, lines)

    people = users.Users(base, "секрет")
    assert sorted(people.users) == ["ivanov", "petrov", "sidorov"]
    ivanov = people.users["ivanov"]
    assert (ivanov["read"], ivanov["upload"], ivanov["manage"]) == ({"Проекты": "tree", "": "tree"}, {"Проекты": "only"},
                                                                    {"Снабжение": "tree"})
    assert ivanov["link"] == {"hash": "f" * 64, "expires": 1790000000} and ivanov["home_upload"] is True
    assert people.verify("ivanov", "пароль-1234") and people.access("petrov").admin
    assert people.users["petrov"]["notify_ready"] is False and people.checked("sidorov") == 1760000000
    assert [(k["id"], k["owner"], k["kind"], k["used"]) for k in people.all_keys(now=4000)] == [
        ("4e5f6a7b", "admin", "manage", None), ("0a1b2c3d", "ivanov", "plain", 2000)]

    words = dictionary.Store(base)
    assert words.get("folders", "Проекты")["changed"] == 1759300000.25 and words.get("folders", "Проекты")["by"] == "ivanov"
    assert words.effective("Проекты/план.mp4")["terms"][0] == {"term": "СКУД", "meaning": "", "scope": "record",
                                                               "path": "Проекты/план.mp4"}
    options = settings.Store(base)
    assert options.speech_model() == {"value": "elevenlabs/scribe-v2", "by": "petrov", "at": AT.format(3)}
    assert options.template_notes() == {"meeting": {"by": "petrov", "at": AT.format(3), "digest": "0123abcd"}}
    look = __import__("app.brand", fromlist=["Brand"]).Brand(base).view()
    assert (look["name"], look["logo"], look["by"], look["source"]["width"]) == ("ООО Ромашка", "0a1b2c3d4e5f", "petrov", 512)
    claims = signup.Requests(base)
    assert [c["login"] for c in claims.pending()] == ["orlova"] and claims.all()[1]["decided_by"] == "petrov"

    book = ledger.Ledger(base)
    assert sorted(book.records) == ["Проекты/без отметок.mp4", "Проекты/план.mp4", "Проекты/сняты.mp4"]
    assert book.get("Проекты/план.mp4") == json.loads(before["outbox/_state/processed.json"])["files"][0]
    assert book.totals()["cost"] == {"RUB": 75.0, "USD": 0.247404} and book.retry_due("Проекты/сняты.mp4") is True
    noted = marks.Store(base)
    assert noted.everything() == {"Проекты/план.mp4": {"0123456789abcdef": TASK, "fedcba9876543210": DUE}}
    # время правки отметок — время прежнего файла, и там, где все отметки сняты
    assert noted.changed("Проекты/сняты.mp4") == 1759500000 and noted.changed("Проекты/без отметок.mp4") == 0
    assert noted.changed("Проекты/план.mp4") == pytest.approx(os.path.getmtime(
        cfg.state_dir / next(cfg.state_dir.glob("до-базы-*")).name / "outbox/Проекты/план/план.tasks.json"))

    trail = journal.Journal(base)
    events = trail.read()
    assert [e["kind"] for e in events] == ["login", "upload", "run", "run", "folder_rename", "run", "record_rename",
                                           "record_delete", "access"]
    assert events[-1] == {"at": AT.format(7), "kind": "access", "by": "admin", "folder": "Проекты", "login": "ivanov",
                          "read": "tree", "upload": None, "manage": None, "via": "key", "key": "управление",
                          "key_kind": "manage"}
    # прежняя строка без валюты легла как OpenRouter в долларах
    assert events[3]["cost"] == {"USD": 0.25} and events[3]["providers"] == [
        {"provider": "OpenRouter", "currency": "USD", "cost": 0.25}]
    report = trail.report(bucket="month")
    assert report["total"] == {"cost": {"RUB": 25.0, "USD": 0.332468}, "runs": 3, "records": 2, "audio_sec": 2400.5}
    # пути — какими они стали: папку переименовали, запись переименовали и удалили
    assert {r["file"]: r["cost"] for r in report["records"]} == {
        "Проекты/план.mp4": {"RUB": 25.0, "USD": 0.082468}, "Проекты/итоги.mp4": {"USD": 0.25}}
    assert trail.uploaded == {"Проекты/план.mp4": "ivanov"} and trail.actions(login="ivanov")[0]["kind"] == "access"

    # прежние файлы — в папке «до-базы-<дата>», на прежних местах их нет; чужое и документы не тронуты
    after = tree(cfg)
    keep = next(cfg.state_dir.glob("до-базы-*")).name
    moved = {"state/users.json", "state/keys.json", "state/dictionaries.json", "state/settings.json",
             "state/requests.json", "state/journal.jsonl", "state/brand/brand.json", "outbox/_state/processed.json",
             "outbox/Проекты/план/план.tasks.json", "outbox/Проекты/сняты/сняты.tasks.json"}
    assert not moved & set(after) and not (cfg.outbox / "_state").exists()
    for name in moved:
        kept = f"state/{keep}/" + (name[len("state/"):] if name.startswith("state/") else name)
        assert after[kept] == before[name], name
    untouched = set(before) - moved
    assert untouched == {"state/session.key", "state/ldap-ca.pem", "state/brand/logo.png", "state/brand/logo-small.png",
                         "state/brand/favicon.png", "outbox/Проекты/план/summary/план.md",
                         "outbox/.trash/x/итоги/итоги.tasks.json"}
    assert all(after[name] == before[name] for name in untouched)
    assert "Сервис их больше не читает" in after[f"state/{keep}/ПРОЧТИ.txt"].decode("utf-8")

    done = next(line for line in lines if line.startswith("перенос в базу завершён"))
    for part in ("сверка сошлась", "пользователей — 3", "прав на папки — 4", "ключей доступа — 2", "словарей — 2",
                 "заявок — 2", "записей в журнале обработки — 3", "отметок и сроков поручений — 2", "событий — 9",
                 "из них прогонов — 3", "25.00 ₽ + 0.33 $"):
        assert part in done, part
    assert any(keep in line and "копия на случай отката" in line for line in lines)


def test_second_start_does_not_repeat_the_move_and_old_files_dropped_later_are_not_read(tmp_path):
    cfg = old_install(tmp_path)
    legacy.start(cfg).close()
    first = tree(cfg)
    lines = []
    base = legacy.start(cfg, lines)
    assert users.Users(base).count() == 3 and journal.Journal(base).count() == 9
    assert not any("перенос" in line or "переношу" in line for line in lines)
    assert len(list(cfg.state_dir.glob("до-базы-*"))) == 1
    assert {k: v for k, v in tree(cfg).items() if "podyachiy.db" not in k} == {
        k: v for k, v in first.items() if "podyachiy.db" not in k}
    base.close()
    # кто-то вернул в папку состояния прежний файл: сервис его не читает и говорит об этом
    legacy.put(cfg.state_dir / "users.json", {"users": {"чужой": {"name": "Чужой", "password": "", "home": "x"}}})
    base = legacy.start(cfg, lines)
    assert sorted(users.Users(base).users) == ["ivanov", "petrov", "sidorov"]
    assert (cfg.state_dir / "users.json").is_file() and any("сервис их не читает" in line and "users.json" in line for line in lines)


def test_fresh_install_gets_an_empty_base_and_no_folder_of_old_files(tmp_path):
    cfg = legacy.setup(tmp_path)
    lines = []
    base = legacy.start(cfg, lines)
    assert sorted(p.name for p in cfg.state_dir.iterdir() if not p.name.startswith(db.FILE + "-")) == [db.FILE]
    assert users.Users(base).users == {} and journal.Journal(base).read() == [] and base.noted("legacy") is None
    assert base.noted("created") and [line for line in lines if "перенос" in line] == []
    assert any(line.startswith("база: ") and "схема 1" in line for line in lines)


def test_spending_from_before_the_journal_is_carried_by_a_line_per_record(tmp_path):
    """Установка, в которой журнала событий ещё не было: расходы — только итогом в журнале обработки.
    В базу они ложатся по строке на запись, датой последнего прогона, — как и делала прежняя версия."""
    cfg = legacy.setup(tmp_path)
    legacy.put(cfg.outbox / "_state" / "processed.json", {"files": [
        {"file": "П/а.mp4", "status": "ok", "cost": 0.1, "cost_total": 1.3, "runs": 13,
         "finished_at": "2026-10-01T10:00:00+03:00", "audio_duration_sec": 3600},
        {"file": "П/б.mp4", "status": "ok", "cost_total": {"RUB": 40.0, "USD": 0.2}, "runs": 2,
         "finished_at": "2026-09-01T10:00:00+03:00"},
        {"status": "ok"}]})                                         # строка без пути записи — такой журнал её и не читал
    lines = []
    trail = journal.Journal(legacy.start(cfg, lines))
    report = trail.report()
    assert report["total"] == {"cost": {"RUB": 40.0, "USD": 1.5}, "runs": 15, "records": 2, "audio_sec": 3600.0}
    assert [s["period"] for s in report["series"]] == ["2026-09-01", "2026-10-01"]
    assert {s["stage"] for s in report["stages"]} == {journal.EARLIER}
    assert [(e["file"], e["reason"], e["runs"]) for e in trail.read()] == [("П/б.mp4", "earlier", 2), ("П/а.mp4", "earlier", 13)]
    done = next(line for line in lines if line.startswith("перенос в базу завершён"))
    assert "журнала расходов не было" in done and "пропущено строк журнала обработки без пути записи — 1" in done
    legacy.start(cfg).close()                                       # второй запуск расходы не удваивает
    assert journal.Journal(db.Database(cfg.state_dir)).report()["total"]["runs"] == 15


def test_torn_line_of_the_journal_is_skipped_and_counted(tmp_path):
    cfg = legacy.setup(tmp_path)
    good = {"at": AT.format(1), "kind": "run", "by": "", "file": "П/а.mp4", "cost": {"USD": 0.5}}
    (cfg.state_dir / "journal.jsonl").write_text(
        json.dumps(good) + "\n\n" + '{"at": "2026-10-' + "\n" + json.dumps(dict(good, file="П/б.mp4")) + "\n", encoding="utf-8")
    lines = []
    trail = journal.Journal(legacy.start(cfg, lines))
    assert [e["file"] for e in trail.read()] == ["П/а.mp4", "П/б.mp4"]
    assert any("пропущено оборванных строк журнала событий — 1" in line for line in lines)


# ---------- сбой переноса: базы нет, прежние файлы на месте ----------

def refused(cfg, match):
    """Запуск отказан с этой причиной — и на диске ничего не изменилось."""
    before = tree(cfg)
    with pytest.raises(db.Refusal, match=match):
        legacy.start(cfg)
    assert tree(cfg) == before and not (cfg.state_dir / db.FILE).exists()
    assert not (cfg.state_dir / migrate.WORK).exists() and not list(cfg.state_dir.glob("до-базы-*"))


@pytest.mark.parametrize("name, text", [
    ("state/users.json", "{оборвано"), ("state/keys.json", "[]"), ("state/dictionaries.json", "{не json"),
    ("state/settings.json", "{"), ("state/requests.json", '{"requests": [{"id": "1", "чужое поле": 1}]}'),
    ("state/brand/brand.json", "мусор"), ("outbox/_state/processed.json", "{оборвано"),
    ("outbox/Проекты/план/план.tasks.json", '{"k": {"status": "done"}}'),
    ("state/journal.jsonl", '{"kind": "run", "file": "а.mp4"}\n'),              # событие без времени
    ("state/users.json", '{"users": {"ivanov": {"name": "И", "password": "", "home": "x", "неизвестное": 1}}}'),
])
def test_unreadable_old_file_stops_the_move_and_changes_nothing(tmp_path, name, text):
    """Нечитаемый файл — не повод начать с пустого: за пустым журналом обработки стоит повторная
    оплата всех записей, за пустыми пользователями — потерянные права."""
    cfg = old_install(tmp_path)
    (tmp_path / name).write_text(text, encoding="utf-8")
    refused(cfg, "Перенос в базу остановлен, ничего не изменено|перенос состояния в базу не удался")


def test_failure_in_the_middle_of_the_move_leaves_no_base_and_the_next_start_does_it_all(tmp_path, monkeypatch):
    cfg = old_install(tmp_path)
    real = journal.Journal._insert
    seen = []

    def breaks_on_the_fifth(self, con, event):
        seen.append(event["kind"])
        if len(seen) == 5:
            raise OSError(28, "No space left on device")            # пользователи, права, ключи, записи уже вставлены
        real(self, con, event)

    monkeypatch.setattr(journal.Journal, "_insert", breaks_on_the_fifth)
    refused(cfg, "перенос состояния в базу не удался.*No space left.*прежние файлы на месте")
    monkeypatch.undo()
    base = legacy.start(cfg)                                        # причину устранили — перенос прошёл целиком
    assert users.Users(base).count() == 3 and journal.Journal(base).count() == 9


def test_check_after_the_move_that_does_not_match_cancels_it(tmp_path, monkeypatch):
    """Сверка: всё читается из новой базы обратно и сравнивается с прочитанным из файлов. Не сошлось
    хоть что-то (здесь — один прогон потерял три копейки) — базы нет, файлы на месте."""
    cfg = old_install(tmp_path)
    real = journal.Journal._insert_run

    def loses_money(self, con, event):
        real(self, con, dict(event, cost={cur: v - 0.03 for cur, v in event["cost"].items()}))

    monkeypatch.setattr(journal.Journal, "_insert_run", loses_money)
    refused(cfg, "сверка после переноса не сошлась: события журнала")
    monkeypatch.undo()
    for what, attr, match in (("права", "users", "пользователи и права"), ("отметки", "everything", "отметки и сроки")):
        owner = users.Users if attr == "users" else marks.Store
        monkeypatch.setattr(owner, attr, property(lambda self: {}) if attr == "users" else lambda self, folder="": {})
        refused(cfg, f"сверка после переноса не сошлась: {match}")
        monkeypatch.undo()


def test_move_cut_after_the_base_took_its_place_is_finished_by_the_next_start(tmp_path, monkeypatch):
    """База собрана и встала на место, а убрать прежние файлы сервис не успел (или не смог): следующий
    запуск не переносит заново — он доделывает уборку по списку, записанному в базе."""
    cfg = old_install(tmp_path)
    real = migrate.shutil.move
    moved = []

    def disk_fails(source, target):
        if len(moved) == 3:
            raise OSError(5, "Input/output error")
        moved.append(source)
        real(source, target)

    monkeypatch.setattr(migrate.shutil, "move", disk_fails)
    with pytest.raises(db.Refusal, match="не удалось убрать.*База уже собрана и цела.*доделает"):
        legacy.start(cfg)
    assert (cfg.state_dir / db.FILE).is_file() and (cfg.outbox / "_state" / "processed.json").is_file()
    monkeypatch.undo()
    with db.Database(cfg.state_dir).write() as con:                 # между запусками в базе уже работали
        con.execute("INSERT INTO meta (key, value) VALUES ('работали', 'да')")
    lines = []
    base = legacy.start(cfg, lines)
    assert base.noted("работали") == "да" and users.Users(base).count() == 3 and journal.Journal(base).count() == 9
    assert not any("переношу состояние" in line for line in lines) and any("убраны в" in line for line in lines)
    assert not (cfg.outbox / "_state").exists() and not (cfg.state_dir / "users.json").exists()
    assert len(list(next(cfg.state_dir.glob("до-базы-*")).rglob("*.json*"))) == 10


def test_lost_base_is_not_replaced_by_an_empty_one(tmp_path):
    """Файл базы пропал, а папка «до-базы-…» говорит, что он был: с пустой базой сервис счёл бы все
    записи новыми и обработал их заново за деньги. Запуск отказан."""
    cfg = old_install(tmp_path)
    legacy.start(cfg).close()
    for path in cfg.state_dir.glob(db.FILE + "*"):
        path.unlink()
    with pytest.raises(db.Refusal, match="файла базы .* нет, хотя раньше он был.*Восстановите базу из резервной копии"):
        legacy.start(cfg)
    assert not (cfg.state_dir / db.FILE).exists()


def test_state_folder_inside_the_container_is_refused_before_anything_is_touched(tmp_path):
    cfg = old_install(tmp_path)
    before = tree(cfg)
    with pytest.raises(db.Refusal, match="не подключена с хоста.*за деньги"):
        migrate.open_base(cfg, lambda line: None, False)
    assert tree(cfg) == before


def test_network_folder_is_a_warning_and_a_desktop_folder_is_not(tmp_path):
    cfg = legacy.setup(tmp_path)
    warning, = migrate.storage_warnings(cfg, {"type": "nfs4", "source": "nas:/export", "kind": db.NETWORK})
    assert "сетевой папке (nfs4)" in warning and "nas:" not in warning and "HOST_STATE" in warning and "допустимы" in warning
    assert migrate.storage_warnings(cfg, {"type": "9p", "source": "C:\\", "kind": db.DESKTOP}) == []
    assert migrate.storage_warnings(cfg, {"type": "ext4", "source": "/dev/sda1", "kind": db.LOCAL}) == []


def test_totals_before_and_after_are_counted_two_ways(tmp_path):
    cfg = old_install(tmp_path)
    old = migrate.read_legacy(cfg)
    base = legacy.start(cfg)
    assert migrate.counts(old) == migrate.counted(base)
    assert migrate.counted(base)["расходы"] == {"RUB": 25.0, "USD": 0.332468} == migrate.spent(old["events"])
    assert usage.text(migrate.counted(base)["расходы"]) == "25.00 ₽ + 0.33 $"
