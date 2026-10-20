"""Словарь папки и записи: разбор вставленного списка, хранение, наследование по папкам,
переезд вместе с папкой и записью, подсказки распознаванию и блок для текстовой модели.

Права, адреса и журнал действий проверяет tests/test_web.py, пределы модели распознавания —
tests/test_speech_engines.py, путь словаря через обработку — tests/test_templates.py.
"""

import pytest

from app import db, dictionary
from app.dictionary import FOLDERS, RECORDS

ERSHOVA = {"name": "Ершова Лидия Марковна", "role": "главный бухгалтер", "aka": []}


# ---------- разбор списка ----------

def test_people_are_read_from_lines_tables_and_a_meeting_invitation():
    pasted = ("1. Ершова Лидия Марковна — главный бухгалтер\n"
              "- Громов Пётр Андреевич (Петя, Громов П. А.) - заместитель директора — начальник склада\n"
              "\n"
              "Юдина Анна, отдел кадров\n"
              "Лаптев Олег Игоревич\tинженер\tОлежка; Лаптев\n"
              "\"Зуев Марк\" <zuev@example.ru>; Родина Вера Павловна <rodina@example.ru>;\n"
              "Салтыков-Щедрин Михаил\n")
    assert dictionary.parse_people(pasted) == [
        ERSHOVA,
        # тире внутри должности остаётся должностью, а «как ещё называют» — в скобках после ФИО
        {"name": "Громов Пётр Андреевич", "role": "заместитель директора — начальник склада",
         "aka": ["Петя", "Громов П. А."]},
        {"name": "Юдина Анна", "role": "отдел кадров", "aka": []},
        {"name": "Лаптев Олег Игоревич", "role": "инженер", "aka": ["Олежка", "Лаптев"]},
        {"name": "Зуев Марк", "role": "", "aka": []},
        {"name": "Родина Вера Павловна", "role": "", "aka": []},
        {"name": "Салтыков-Щедрин Михаил", "role": "", "aka": []},           # дефис без пробелов — часть фамилии
    ]


def test_terms_are_read_with_and_without_a_meaning():
    assert dictionary.parse_terms("КС-2 — акт о приёмке выполненных работ\nСКУД, ТМЦ, план-факт\n"
                                  "ГИС = геоинформационная система\nБСП\tбиблиотека стандартных подсистем\n") == [
        {"term": "КС-2", "meaning": "акт о приёмке выполненных работ"},
        {"term": "СКУД", "meaning": ""}, {"term": "ТМЦ", "meaning": ""}, {"term": "план-факт", "meaning": ""},
        {"term": "ГИС", "meaning": "геоинформационная система"},
        {"term": "БСП", "meaning": "библиотека стандартных подсистем"},
    ]


def test_lists_and_objects_are_accepted_and_repeats_dropped():
    people = dictionary.parse_people(["Ершова Лидия Марковна — главный бухгалтер",
                                      {"name": "  ершова  лидия марковна ", "role": "повтор"},
                                      {"name": "Громов Пётр", "aka": ["Петя", "петя", "Громов Пётр"]},
                                      {"role": "без имени"}, ""])
    assert people == [ERSHOVA, {"name": "Громов Пётр", "role": "", "aka": ["Петя"]}]
    assert dictionary.parse_terms([{"term": "КС-2", "meaning": "акт"}, "кс-2 — другое", {"meaning": "без слова"}]) == [
        {"term": "КС-2", "meaning": "акт"}]
    assert dictionary.parse_people("") == [] and dictionary.parse_terms([]) == []


def test_too_long_and_wrong_input_is_refused_in_plain_words():
    with pytest.raises(dictionary.DictionaryError, match="не больше 200 строк"):
        dictionary.parse_people("\n".join(f"Человек Номер{n}" for n in range(201)))
    with pytest.raises(dictionary.DictionaryError, match="не больше 300 строк"):
        dictionary.parse_terms([f"Т-{n}" for n in range(301)])
    for bad in (5, {"name": "Ершова"}, [5]):
        with pytest.raises(dictionary.DictionaryError):
            dictionary.parse_people(bad)
    # длинные поля обрезаются, а не роняют сохранение
    long = dictionary.parse_people([{"name": "Я" * 500, "role": "р" * 500, "aka": ["а" * 500] * 9}])[0]
    assert (len(long["name"]), len(long["role"]), len(long["aka"]), len(long["aka"][0])) == (120, 120, 1, 80)


def test_adding_keeps_the_old_lines_and_replaces_the_same_person():
    old = dictionary.parse_people("Ершова Лидия Марковна — бухгалтер\nГромов Пётр")
    new = dictionary.parse_people("ершова лидия марковна — главный бухгалтер\nЮдина Анна")
    assert [(p["name"], p["role"]) for p in dictionary.joined(old, new, "name", 200)] == [
        ("ершова лидия марковна", "главный бухгалтер"), ("Громов Пётр", ""), ("Юдина Анна", "")]
    with pytest.raises(dictionary.DictionaryError, match="не больше 2 строк"):
        dictionary.joined(old, new, "name", 2)


# ---------- хранение и наследование ----------

@pytest.fixture
def store(tmp_path):
    return dictionary.Store(db.Database(tmp_path / "state"))


def fill(store, scope, path, people="", terms="", by="orlova"):
    return store.put(scope, path, dictionary.parse_people(people), dictionary.parse_terms(terms), by)


def test_dictionary_is_kept_in_the_base_and_read_back(store, tmp_path):
    assert store.get(FOLDERS, "Проекты") == {"people": [], "terms": [], "by": "", "at": "", "changed": 0}
    saved = fill(store, FOLDERS, "Проекты", "Ершова Лидия Марковна — главный бухгалтер", "КС-2 — акт")
    assert saved["people"] == [ERSHOVA] and saved["by"] == "orlova" and saved["changed"] > 0 and saved["at"]
    rows = lambda: [(r["scope"], r["path"]) for r in store.db.rows("SELECT scope, path FROM dictionaries")]
    assert rows() == [("folders", "Проекты")]
    assert dictionary.Store(db.Database(tmp_path / "state")).get(FOLDERS, "Проекты") == saved          # перезапуск сервиса
    # пустой словарь в базе не остаётся
    fill(store, FOLDERS, "Проекты")
    assert rows() == [] and store.effective("Проекты/вкс.mp3") == {"people": [], "terms": []}


def test_nearest_dictionary_adds_to_the_farther_ones_and_wins_on_a_match(store):
    fill(store, FOLDERS, "Проекты", "Ершова Лидия Марковна — бухгалтер\nГромов Пётр — склад", "КС-2 — акт\nСКУД")
    fill(store, FOLDERS, "Проекты/Сметы", "Юдина Анна — сметчик", "кс-2 — акт о приёмке работ")
    fill(store, RECORDS, "Проекты/Сметы/вкс.mp3", "ершова лидия марковна — главный бухгалтер, ведёт встречу")
    fill(store, FOLDERS, "Снабжение", "Чужой Человек", "ЧУЖОЕ")

    words = store.effective("Проекты/Сметы/вкс.mp3")
    assert [(p["name"], p["role"], p["scope"], p["path"]) for p in words["people"]] == [
        ("ершова лидия марковна", "главный бухгалтер, ведёт встречу", "record", "Проекты/Сметы/вкс.mp3"),
        ("Юдина Анна", "сметчик", "folder", "Проекты/Сметы"),
        ("Громов Пётр", "склад", "folder", "Проекты")]
    assert [(t["term"], t["meaning"], t["path"]) for t in words["terms"]] == [
        ("кс-2", "акт о приёмке работ", "Проекты/Сметы"), ("СКУД", "", "Проекты")]
    # запись соседней подпапки берёт только словарь общей папки выше; запись в корне — ничего
    assert [p["name"] for p in store.effective("Проекты/Склад/вкс.mp3")["people"]] == ["Ершова Лидия Марковна", "Громов Пётр"]
    assert store.effective("вкс.mp3") == {"people": [], "terms": []}
    # похожее имя папки — не подпапка
    assert store.effective("Проекты отдела/вкс.mp3") == {"people": [], "terms": []}
    # слои для окна словаря: что приходит папке с папок выше
    assert [(x["scope"], x["path"]) for x in store.layers(folder="Проекты/Сметы")] == [
        ("folder", "Проекты/Сметы"), ("folder", "Проекты")]


def test_dictionary_moves_with_the_folder_and_the_record_and_goes_with_them(store):
    fill(store, FOLDERS, "Проекты", "Громов Пётр")
    fill(store, FOLDERS, "Проекты/Сметы", "Юдина Анна")
    fill(store, FOLDERS, "Проекты отдела", "Другой Человек")
    fill(store, RECORDS, "Проекты/Сметы/вкс.mp3", "Ершова Лидия")

    store.move_folder("Проекты", "Стройка")
    assert [p["name"] for p in store.effective("Стройка/Сметы/вкс.mp3")["people"]] == ["Ершова Лидия", "Юдина Анна", "Громов Пётр"]
    assert store.get(FOLDERS, "Проекты")["people"] == [] and store.get(FOLDERS, "Проекты отдела")["people"]

    store.move_record("Стройка/Сметы/вкс.mp3", "Стройка/планёрка.mp3")          # перенос и переименование
    assert [p["name"] for p in store.effective("Стройка/планёрка.mp3")["people"]] == ["Ершова Лидия", "Громов Пётр"]
    assert store.get(RECORDS, "Стройка/Сметы/вкс.mp3")["people"] == []
    store.move_record("нет такой.mp3", "куда-то.mp3")                            # у записи без словаря нечего переносить

    store.drop_record("Стройка/планёрка.mp3")
    assert store.get(RECORDS, "Стройка/планёрка.mp3")["people"] == []
    fill(store, RECORDS, "Стройка/Сметы/смета.mp3", "Ершова Лидия")
    store.drop_folder("Стройка")
    assert store.layers(file="Стройка/Сметы/смета.mp3") == []
    assert store.get(FOLDERS, "Проекты отдела")["people"]                        # соседняя папка не задета


# ---------- что уходит в обработку ----------

def test_hints_go_record_first_then_the_folders_and_terms_before_names(store):
    fill(store, FOLDERS, "Проекты", "Громов Пётр Андреевич (Петя)", "СКУД")
    fill(store, FOLDERS, "Проекты/Сметы", "", "КС-2 — акт")
    fill(store, RECORDS, "Проекты/Сметы/вкс.mp3", "Ершова Лидия Марковна — главный бухгалтер")
    assert dictionary.hints(store.effective("Проекты/Сметы/вкс.mp3")) == [
        "Ершова Лидия Марковна", "Лидия Марковна", "Ершова",        # запись: ФИО, как обращаются, фамилия
        "КС-2",                                                     # ближняя папка
        "СКУД", "Громов Пётр Андреевич", "Пётр Андреевич", "Громов", "Петя"]
    assert dictionary.hints(dictionary.merge([])) == []
    # отчество узнаётся по окончанию; где его нет, ФИО идёт только целиком
    assert dictionary.spoken({"name": "Павел Ильич Тихонов", "aka": []}) == ["Павел Ильич Тихонов", "Павел Ильич", "Тихонов"]
    assert dictionary.spoken({"name": "Юдина Анна", "aka": ["Аня"]}) == ["Юдина Анна", "Аня"]


def test_block_for_the_text_model_says_the_list_is_a_hint(store):
    assert dictionary.block(dictionary.merge([])) == ""
    fill(store, FOLDERS, "Проекты", "Ершова Лидия Марковна (Лида) — главный бухгалтер\nГромов Пётр", "КС-2 — акт\nСКУД")
    block = dictionary.block(store.effective("Проекты/вкс.mp3"), after_glossary=True)
    assert block.startswith("\n\n---\nСПИСОК УЧАСТНИКОВ И СОКРАЩЕНИЙ")
    assert "- Ершова Лидия Марковна — главный бухгалтер (ещё называют: Лида)\n- Громов Пётр" in block
    assert "- КС-2 — акт\n- СКУД" in block
    # правила стоят в самом блоке: шаблоны могут быть своими
    for rule in ("подсказка, а не перечень присутствующих", "по звучанию, и по роли уверенное", "не подгоняй",
                 "в документ не вписывай", "в точности так, как здесь", "верь этому списку"):
        assert rule in block, rule
    assert "верь этому списку" not in dictionary.block(store.effective("Проекты/вкс.mp3"))
    only_terms = dictionary.block({"people": [], "terms": [{"term": "СКУД", "meaning": ""}]})
    assert "Кто бывает" not in only_terms and "- СКУД" in only_terms


def test_fingerprint_changes_with_the_list_and_is_empty_without_it(store):
    assert dictionary.fingerprint(dictionary.merge([])) == ""
    fill(store, FOLDERS, "Проекты", "Ершова Лидия Марковна")
    first = dictionary.fingerprint(store.effective("Проекты/вкс.mp3"))
    assert first and first == dictionary.fingerprint(store.effective("Проекты/другая.mp3"))     # откуда строка — не важно
    fill(store, RECORDS, "Проекты/вкс.mp3", "", "СКУД")
    assert dictionary.fingerprint(store.effective("Проекты/вкс.mp3")) != first
