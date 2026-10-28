"""Отметки «выполнено» у поручений: ключ, строки в базе по пути записи, пересборка протокола,
реестр и его память, раздел прошлых поручений, выгрузка, переезд вместе с записью и папкой."""
import json
import threading
import types

import pytest

from app import db, export, external, folders, marks, mindmap, pipeline, records, registry, users
from app.ledger import Ledger

MD = """<!-- протокол: модель x -->
# Протокол совещания

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Иванов Иван** | | | |
| Согласовать смету по складу | до пятницы | [00:10:05] | в работе |
| Закрыть акт | 15.10 | [00:12:00] | выполнено |
| **S2 (технический специалист)** | | | |
| Проверить остатки | — | — | — |
"""
OLD = "## Задачи\n\n| Ответственный | Задача |\n|---|---|\n| Петров | Прислать договор |\n"


class Noted:
    """Отметки одной записи: то же, что marks.Store, но путь записи уже подставлен."""

    def __init__(self, base, file):
        self.store, self.file = marks.Store(base), file

    def put(self, *args):
        return self.store.put(self.file, *args)

    def put_due(self, *args):
        return self.store.put_due(self.file, *args)

    def load(self):
        return self.store.load(self.file)

    def changed(self):
        return self.store.changed(self.file)


def saved(tmp_path, text=MD, name="вкс", base=None, file=None):
    """Протокол записи на диске и её строка в журнале обработки; отдаёт путь протокола и отметки записи."""
    protocol = tmp_path / name / "summary" / f"{name}.md"
    protocol.parent.mkdir(parents=True)
    protocol.write_text(text, encoding="utf-8")
    base = base or db.Database(tmp_path / "state")
    file = file or f"Планёрки/{name}.mp4"
    Ledger(base).put({"file": file, "stem": name, "status": "ok", "finished_at": "2026-10-01T10:00:00+03:00"})
    return protocol, Noted(base, file)


def key_of(text):
    return next(marks.key(t["who"], t["task"]) for t in external.tasks(MD) if t["task"].startswith(text))


def test_key_ignores_case_punctuation_and_spaces_but_not_the_person():
    key = marks.key("Иванов Иван", "Согласовать смету по складу")
    assert len(key) == 16 and key == marks.key(" иванов  иван ", "согласовать смету, по складу.")
    assert key == marks.key("Иванов Иван", "Согласовать смёту — по складу!")           # ё и е — одно
    assert key != marks.key("Петров Пётр", "Согласовать смету по складу")
    assert key != marks.key("Иванов Иван", "Согласовать смету по офису")
    assert marks.key(None, "Проверить остатки") == marks.key("", "проверить остатки")


def test_mark_is_kept_in_the_base_by_the_path_of_the_record(tmp_path):
    protocol, file = saved(tmp_path)
    assert file.load() == {} and file.changed() == 0 and not list(tmp_path.rglob("*.tasks.json"))
    tasks = external.tasks(MD)
    key = key_of("Согласовать")
    task, changed = file.put(tasks, key, True, "ivanov", "подписано")
    assert changed and task["done"] and task["done_by"] == "ivanov" and task["comment"] == "подписано"
    assert task["done_at"].startswith("20") and task["key"] == key and task["task"] == "Согласовать смету по складу"
    on_disk = Noted(db.Database(tmp_path / "state"), file.file).load()          # с диска, другим соединением
    assert on_disk == {key: {"status": "done", "by": "ivanov", "at": task["done_at"], "who": "Иванов Иван",
                             "task": "Согласовать смету по складу", "comment": "подписано"}}
    marked_at = file.changed()
    assert marked_at > 0
    assert [t["done"] for t in marks.merged(tasks, file.load())] == [True, False, False]
    assert protocol.read_text(encoding="utf-8") == MD                       # протокол не тронут

    # снятие: строка удаляется, а время правки отметок записи растёт — по нему видно, что отметку сняли
    task, changed = file.put(tasks, key, False, "petrov")
    assert file.changed() > marked_at
    assert changed and task == {**tasks[0], "key": key, **marks.BLANK, "due": None, "due_text": "до пятницы",
                                "due_by": None, "due_at": None}
    assert file.load() == {}
    unmarked_at = file.changed()
    assert file.put(tasks, key, False, "petrov")[1] is False         # снимать нечего — ничего не изменилось
    with pytest.raises(KeyError):
        file.put(tasks, "0" * 16, True, "ivanov")
    assert file.changed() == unmarked_at


def test_mark_survives_a_rebuild_when_the_wording_is_almost_the_same(tmp_path):
    _, file = saved(tmp_path)
    file.put(external.tasks(MD), key_of("Согласовать"), True, "ivanov")
    file.put(external.tasks(MD), key_of("Проверить"), True, "ivanov")
    stored = file.load()

    done = lambda text: {t["task"]: t["done"] for t in marks.merged(external.tasks(text), stored)}
    # та же строка в другом месте таблицы и с другим сроком — ключ тот же
    moved = MD.replace("| Закрыть акт | 15.10 | [00:12:00] | выполнено |\n", "").replace("до пятницы", "к 16.10")
    assert done(moved) == {"Согласовать смету по складу": True, "Проверить остатки": True}
    # слова в другой форме: исполнитель тот же, почти все слова общие
    assert done(MD.replace("Согласовать смету по складу", "Согласование сметы по складу"))["Согласование сметы по складу"]
    # другое дело того же человека — не оно
    assert not done(MD.replace("Согласовать смету по складу", "Согласовать договор аренды"))["Согласовать договор аренды"]
    # тот же текст у другого исполнителя — не оно
    assert not done(MD.replace("**Иванов Иван**", "**Сидоров**"))["Согласовать смету по складу"]
    # два похожих поручения вместо одного: к какому относится отметка, неизвестно — ни к какому
    twice = MD.replace("| Согласовать смету по складу |", "| Согласование сметы по складу | — | — | — |\n| Согласовали смету по складам |")
    assert [d for name, d in done(twice).items() if name.startswith("Согласов")] == [False, False]

    # снять отметку можно и у переписанного поручения: уходит старая строка
    rebuilt = external.tasks(MD.replace("Согласовать смету по складу", "Согласование сметы по складу"))
    new_key = marks.key("Иванов Иван", "Согласование сметы по складу")
    task, changed = file.put(rebuilt, new_key, False, "ivanov")
    assert changed and not task["done"] and list(file.load()) == [key_of("Проверить")]


def test_registry_takes_the_mark_over_the_words_of_the_protocol(tmp_path):
    protocol, file = saved(tmp_path)
    rec = {"file": "Планёрки/вкс.mp4", "stem": "вкс", "status": "ok", "finished_at": "2026-10-01T10:00:00+03:00"}
    cache = registry.Cache()
    rows = lambda: registry.rows([rec], lambda r: protocol, cache, marked=file.store)[0]
    assert [(r["open"], r["done"], r["status"], r["unsigned"]) for r in rows()] == [
        (True, False, "в работе", False), (False, False, "выполнено", False), (True, False, None, True)]
    assert cache.reads == 1 and rows() and cache.reads == 1

    # отметка видна сразу: время правки отметок входит в проверку свежести
    file.put(external.tasks(MD), key_of("Согласовать"), True, "ivanov", "готово")
    first = rows()[0]
    assert cache.reads == 2 and not first["open"] and first["done"] and first["status"] == "в работе"
    assert (first["done_by"], first["comment"], first["key"]) == ("ivanov", "готово", key_of("Согласовать"))
    assert [r["task"] for r in registry.pick(rows(), only_open=True)] == ["Проверить остатки"]
    assert cache.reads == 2

    file.put(external.tasks(MD), key_of("Согласовать"), False, "ivanov")
    assert rows()[0]["open"] and cache.reads == 3
    # без отметок (реестр собирают из одних протоколов) строки — как в протоколе
    assert [r["done"] for r in registry.rows([rec], lambda r: protocol, registry.Cache())[0]] == [False] * 3


def test_unsigned_speaker_is_a_label_at_the_start_of_the_name():
    for who, unsigned in (("S2", True), ("S13 (технический специалист)", True), ("Иван (S2)", False),
                          ("Sergey", False), ("S", False), ("Иванов", False), (None, False)):
        assert bool(who and registry.UNSIGNED.match(who)) is unsigned, who


def test_block_of_previous_tasks_skips_what_a_person_closed(tmp_path):
    cfg = types.SimpleNamespace(inbox=tmp_path / "in", outbox=tmp_path / "out", public_url="")
    protocol, file = saved(cfg.outbox / "Планёрки")
    prev = {"file": "Планёрки/вкс.mp4", "stem": "вкс", "status": "ok", "first_done_at": "2026-10-01T10:00:00+03:00",
            "finished_at": "2026-10-01T10:00:00+03:00"}
    block = lambda: pipeline.previous_tasks(cfg, "Планёрки/новая.mp4", [prev], "", "2026-10-08T10:00:00+03:00",
                                            lambda s: None, file.store, series=True)
    assert block().count("\n- **") == 2 and "Согласовать смету" in block() and "Закрыть акт" not in block()
    file.put(external.tasks(MD), key_of("Согласовать"), True, "ivanov")
    assert block().count("\n- **") == 1 and "Согласовать смету" not in block() and "Проверить остатки" in block()
    file.put(external.tasks(MD), key_of("Проверить"), True, "ivanov")
    assert block() == ""                                                   # всё закрыто — раздела нет


def test_exported_files_show_the_mark_in_the_status_column(tmp_path):
    protocol, file = saved(tmp_path)
    tasks = external.tasks(MD)
    assert marks.stamp(MD, {}) == MD
    file.put(tasks, key_of("Согласовать"), True, "ivanov")
    file.put(tasks, key_of("Проверить"), True, "ivanov")
    day = marks.day(file.load()[key_of("Проверить")]["at"])
    text = marks.stamp(MD, file.load())
    assert f"| Согласовать смету по складу | до пятницы | [00:10:05] | в работе; выполнено, {day} |" in text
    assert f"| Проверить остатки | — | — | выполнено, {day} |" in text
    assert "| Закрыть акт | 15.10 | [00:12:00] | выполнено |" in text and text.count("\n") == MD.count("\n")
    assert [t["task"] for t in external.tasks(text)] == [t["task"] for t in tasks]      # таблица осталась таблицей
    assert protocol.read_text(encoding="utf-8") == MD

    # старый протокол без колонки статуса — отметка в самом поручении
    old_key = marks.key("Петров", "Прислать договор")
    stamped = marks.stamp(OLD, {old_key: {"status": "done", "by": "a", "at": "2026-10-10T12:00:00+03:00",
                                          "who": "Петров", "task": "Прислать договор"}})
    assert "| Петров | Прислать договор (выполнено, 10.10.2026) |" in stamped

    table = next(b for b in export.parse(text) if isinstance(b, export.TableBlock))
    assert export.text_of(table.rows[1].cells[3]) == f"в работе; выполнено, {day}"

    # скачиваемый HTML: карта пересобирается, когда отметку поставили и когда сняли
    page = mindmap.ensure(protocol, file.load(), file.changed())
    assert f"выполнено, {day}" in page.read_text(encoding="utf-8")
    file.put(tasks, key_of("Согласовать"), False, "ivanov")
    file.put(tasks, key_of("Проверить"), False, "ivanov")
    assert f"выполнено, {day}" not in mindmap.ensure(protocol, file.load(), file.changed()).read_text(encoding="utf-8")


def record_on_disk(tmp_path):
    cfg = types.SimpleNamespace(inbox=tmp_path / "in", outbox=tmp_path / "out", outbox_inside_inbox=None, stable_sec=0)
    file = "Проекты/вкс.mp4"
    (cfg.inbox / "Проекты").mkdir(parents=True)
    (cfg.inbox / "Архив").mkdir()
    (cfg.inbox / file).write_bytes(b"video")
    base = db.Database(tmp_path / "state")
    protocol, noted = saved(cfg.outbox / "Проекты", base=base, file=file)
    book = Ledger(base)
    book.put({"file": file, "stem": "вкс", "status": "ok", "output": str(cfg.outbox / "Проекты" / "вкс"),
              "summary": "summary/вкс.md"})
    noted.put(external.tasks(MD), key_of("Согласовать"), True, "ivanov", "подписано")
    service = types.SimpleNamespace(cfg=cfg, ledger=book, queue=[], pending={}, running=[])
    return cfg, service, file


def marked(cfg, service, file):
    """Отметки записи так, как их видит сервис: по её нынешнему пути в журнале."""
    protocol = pipeline.protocol_path(cfg, file, service.ledger.get(file))
    return [(t["task"], t["done_by"], t["comment"])
            for t in marks.merged(external.tasks(protocol.read_text(encoding="utf-8")),
                                  marks.Store(service.ledger.db).load(file))
            if t["done"]]


def in_trash(cfg, done):
    """Что записано о удалённом в корзине — файл «удалено.json»."""
    return json.loads((cfg.outbox / done["trash"] / "удалено.json").read_text(encoding="utf-8"))


def test_marks_follow_the_record_when_it_is_renamed_moved_and_deleted(tmp_path):
    cfg, service, file = record_on_disk(tmp_path)
    kept = [("Согласовать смету по складу", "ivanov", "подписано")]
    assert marked(cfg, service, file) == kept

    noted = marks.Store(service.ledger.db)
    renamed = records.rename(cfg, service, file, "Итоги")
    assert not (cfg.outbox / "Проекты/вкс").exists() and noted.load(file) == {}
    assert marked(cfg, service, renamed) == kept

    moved = records.move(cfg, service, renamed, "Архив")
    assert marked(cfg, service, moved) == kept and list(noted.everything()) == [moved]

    held = noted.load(moved)
    done = records.remove(cfg, service, moved, "ivanov")
    # из базы отметки уходят вместе с записью, а в корзине остаются — на случай возврата руками
    assert noted.everything() == {} and in_trash(cfg, done)["marks"] == {moved: held}


def test_marks_follow_the_folder_when_it_is_renamed_and_deleted(tmp_path):
    cfg, service, file = record_on_disk(tmp_path)
    people = users.Users(service.ledger.db, "секрет")
    noted = marks.Store(service.ledger.db)
    assert folders.rename(cfg, service, people, "Проекты", "Планёрки") == "Планёрки"
    assert marked(cfg, service, "Планёрки/вкс.mp4") == [("Согласовать смету по складу", "ivanov", "подписано")]
    held = noted.load("Планёрки/вкс.mp4")
    done = folders.remove(cfg, service, people, "Планёрки", "admin")
    assert noted.everything() == {} and in_trash(cfg, done)["marks"] == {"Планёрки/вкс.mp4": held}
    assert not (cfg.outbox / "Планёрки").exists()


def test_two_marks_set_at_once_do_not_wipe_each_other(tmp_path):
    """Прочитать, поправить и записать отметку — одна транзакция: шестнадцать потоков отмечают
    три поручения одной записи, и ни одна отметка не теряется."""
    protocol, file = saved(tmp_path)
    tasks = external.tasks(MD)
    failures = []

    def work(n):
        try:
            for part in ("Согласовать", "Закрыть", "Проверить"):
                file.put(tasks, key_of(part), True, f"user{n}")
                file.put_due(tasks, key_of(part), f"2026-10-{10 + n:02d}", f"user{n}")
        except Exception as e:      # любая ошибка потока — провал теста
            failures.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert failures == []
    found = Noted(db.Database(tmp_path / "state"), file.file).load()
    assert len(found) == 3 and all(m["status"] == "done" and m["due"] for m in found.values())


# ---------- срок датой ----------

def test_due_date_is_kept_in_the_same_record_and_the_protocol_words_are_not_lost(tmp_path):
    protocol, file = saved(tmp_path)
    tasks = external.tasks(MD)
    key = key_of("Согласовать")
    task, changed, was = file.put_due(tasks, key, "2026-10-20", "ivanov")
    assert changed and was is None
    assert (task["due"], task["due_text"], task["due_by"], task["done"]) == ("2026-10-20", "до пятницы", "ivanov", False)
    assert task["due_at"].startswith("20")
    assert file.load() == {key: {
        "due": "2026-10-20", "due_by": "ivanov", "due_at": task["due_at"],
        "who": "Иванов Иван", "task": "Согласовать смету по складу"}}
    assert protocol.read_text(encoding="utf-8") == MD                       # протокол не тронут
    assert file.put_due(tasks, key, "2026-10-20", "petrov")[1:] == (False, "2026-10-20")     # та же дата — не правка

    # отметка «выполнено» ложится в ту же запись и срок не трогает; снятие отметки срок оставляет
    done, _ = file.put(tasks, key, True, "petrov", "сдано")
    assert (done["done"], done["done_by"], done["due"], done["due_by"]) == (True, "petrov", "2026-10-20", "ivanov")
    on_disk = file.load()
    assert list(on_disk) == [key] and on_disk[key]["status"] == "done" and on_disk[key]["due"] == "2026-10-20"
    back, changed = file.put(tasks, key, False, "petrov")
    assert changed and not back["done"] and back["due"] == "2026-10-20"
    assert "status" not in file.load()[key]

    # перенос срока называет прежний; снятие возвращает срок из протокола, и запись уходит из файла
    moved, changed, was = file.put_due(tasks, key, "2026-11-01", "petrov")
    assert changed and was == "2026-10-20" and (moved["due"], moved["due_by"]) == ("2026-11-01", "petrov")
    cleared, changed, was = file.put_due(tasks, key, None, "petrov")
    assert changed and was == "2026-11-01"
    assert (cleared["due"], cleared["due_text"], cleared["due_by"], cleared["due_at"]) == (None, "до пятницы", None, None)
    assert file.load() == {}
    assert file.put_due(tasks, key, None, "petrov")[1] is False
    # срок, снятый у выполненного поручения, отметку оставляет
    file.put(tasks, key, True, "ivanov")
    file.put_due(tasks, key, "2026-10-20", "ivanov")
    assert file.put_due(tasks, key, None, "ivanov")[0]["done"] is True
    with pytest.raises(KeyError):
        file.put_due(tasks, "0" * 16, "2026-10-20", "ivanov")


def test_due_date_survives_a_rebuilt_protocol_like_the_mark_does(tmp_path):
    protocol, file = saved(tmp_path)
    file.put_due(external.tasks(MD), key_of("Согласовать"), "2026-10-20", "ivanov")
    # пересборка переставила строки и немного переписала формулировку — срок нашёл своё поручение
    again = MD.replace("Согласовать смету по складу", "Согласовать смету по складу окончательно")
    found = marks.merged(external.tasks(again), file.load())
    assert [(t["task"], t["due"]) for t in found if t["due"]] == [("Согласовать смету по складу окончательно", "2026-10-20")]
    # правка после пересборки переносит запись под новый ключ — старого ключа в файле не остаётся
    fresh = marks.key("Иванов Иван", "Согласовать смету по складу окончательно")
    file.put(external.tasks(again), fresh, True, "petrov")
    on_disk = file.load()
    assert list(on_disk) == [fresh] and on_disk[fresh]["due"] == "2026-10-20" and on_disk[fresh]["status"] == "done"
    # переписано сильно или исполнитель другой — срок остаётся в файле без пары и нигде не показывается
    other = MD.replace("Согласовать смету по складу", "Купить кабель")
    assert not any(t["due"] for t in marks.merged(external.tasks(other), file.load()))
    renamed = MD.replace("**Иванов Иван**", "**Петров Пётр**")
    assert not any(t["due"] for t in marks.merged(external.tasks(renamed), file.load()))


def test_exported_files_show_the_date_instead_of_the_words(tmp_path):
    protocol, file = saved(tmp_path)
    tasks = external.tasks(MD)
    file.put_due(tasks, key_of("Согласовать"), "2026-10-20", "ivanov")
    file.put_due(tasks, key_of("Проверить"), "2026-11-05", "ivanov")
    file.put(tasks, key_of("Проверить"), True, "ivanov")
    day = marks.day(file.load()[key_of("Проверить")]["at"])
    text = marks.stamp(MD, file.load())
    assert "| Согласовать смету по складу | 20.10.2026 | [00:10:05] | в работе |" in text
    assert f"| Проверить остатки | 05.11.2026 | — | выполнено, {day} |" in text
    assert "| Закрыть акт | 15.10 | [00:12:00] | выполнено |" in text and text.count("\n") == MD.count("\n")
    assert protocol.read_text(encoding="utf-8") == MD
    table = next(b for b in export.parse(text) if isinstance(b, export.TableBlock))
    assert export.text_of(table.rows[1].cells[1]) == "20.10.2026"
    assert "20.10.2026" in mindmap.ensure(protocol, file.load(), file.changed()).read_text(encoding="utf-8")
    # старый протокол без колонки срока — дата в самом поручении
    old_key = marks.key("Петров", "Прислать договор")
    stamped = marks.stamp(OLD, {old_key: {"due": "2026-10-20", "due_by": "a", "due_at": "2026-10-10T12:00:00+03:00",
                                          "who": "Петров", "task": "Прислать договор"}})
    assert "| Петров | Прислать договор (срок: 20.10.2026) |" in stamped


def test_registry_sorts_by_set_dates_first_and_finds_the_overdue(tmp_path):
    protocol, file = saved(tmp_path)
    tasks = external.tasks(MD)
    record = {"file": "Планёрки/вкс.mp4", "stem": "вкс", "status": "ok", "finished_at": "2026-10-01T10:00:00+03:00"}
    rows = lambda now: registry.rows([record], lambda rec: protocol, registry.Cache(), now=now, marked=file.store)[0]
    file.put_due(tasks, key_of("Проверить"), "2026-10-08", "ivanov")
    file.put_due(tasks, key_of("Закрыть"), "2026-10-05", "ivanov")       # закрыто словом протокола
    found = rows("2026-10-10")
    assert [(r["task"], r["due"], r["due_text"], r["due_date"], r["overdue"]) for r in found] == [
        ("Согласовать смету по складу", None, "до пятницы", "2026-10-02", False),    # дата из слов — не просрочка
        ("Закрыть акт", "2026-10-05", "15.10", "2026-10-05", False),                 # закрытое не просрочено
        ("Проверить остатки", "2026-10-08", None, "2026-10-08", True)]
    assert [r["task"] for r in registry.order(found, "due")] == [
        "Закрыть акт", "Проверить остатки", "Согласовать смету по складу"]       # сначала заданные даты, потом слова
    assert [r["task"] for r in registry.pick(found, overdue=True)] == ["Проверить остатки"]
    assert [r["task"] for r in registry.pick(found, due=True)] == [r["task"] for r in found]      # дата — тоже срок
    assert [r["task"] for r in registry.pick(found, q="08.10.2026")] == ["Проверить остатки"]
    assert not any(r["overdue"] for r in rows("2026-10-08"))                     # в день срока ещё не просрочено
    file.put(tasks, key_of("Проверить"), True, "ivanov")
    assert not any(r["overdue"] for r in rows("2026-10-10"))                     # выполнили — просрочки нет

    # «Поручения прошлого совещания»: срок, заданный человеком, вместо слов протокола
    file.put_due(tasks, key_of("Согласовать"), "2026-10-20", "ivanov")
    block = registry.block(record, marks.merged(tasks, file.load()))
    assert "Согласовать смету по складу; срок: 20.10.2026; статус: в работе" in block and "до пятницы" not in block



# ---------- календарь ICS ----------

def _row(**over):
    return {"key": "677770baf98235ca", "file": "Проекты/вкс.mp4", "who": "Иванов Иван", "task": "Согласовать смету",
            "title": "Планёрка", "date": "2026-10-09", "due": "2026-10-16", "due_date": "2026-10-16", "due_text": None,
            "status": None, "done": False, "done_by": None, "done_at": None, "link": "http://podyachiy.test/?open=x", **over}


def _unfold(text):
    return text.replace("\r\n ", "")


def test_ics_has_an_all_day_event_with_a_stable_uid_per_task_with_a_due_date():
    now = __import__("datetime").datetime(2026, 10, 28, 9, 0, tzinfo=__import__("datetime").timezone.utc)
    text = marks.ics([_row(), _row(), _row(key="другой", due=None, due_date=None),
                      _row(key="словами", due=None, due_date="2026-10-17", due_text="до пятницы")], now)
    assert text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "\n" not in text.replace("\r\n", "")                  # строки — только через CRLF
    events = _unfold(text).split("BEGIN:VEVENT")[1:]
    assert len(events) == 2                                          # повтор — одно событие, без срока — ни одного
    assert "DTSTART;VALUE=DATE:20261016\r\nDTEND;VALUE=DATE:20261017" in events[0]
    assert "DTSTAMP:20261028T090000Z" in events[0] and "URL:http://podyachiy.test/?open=x" in events[0]
    uid = next(line for line in events[0].split("\r\n") if line.startswith("UID:"))
    assert uid in _unfold(marks.ics([_row(task="Согласовать смету")]))      # тот же файл заново — тот же UID
    assert "Срок по словам протокола: до пятницы — дата вычислена сервисом" in events[1]


def test_ics_escapes_text_and_folds_long_lines_at_75_octets():
    assert marks.ics_text("а,б;в\\г\nд\r\nе\x07") == "а\\,б\\;в\\\\г\\nд\\nе"
    text = marks.ics([_row(task="Смета, акт; " + "длинное поручение " * 20)])
    lines = text.split("\r\n")
    assert all(len(line.encode("utf-8")) <= 75 for line in lines)
    assert any(line.startswith(" ") for line in lines)
    for line in lines:
        line.encode("utf-8").decode("utf-8")                         # знак UTF-8 не разрезан
    assert "SUMMARY:Смета\\, акт\\; длинное" in _unfold(text)
