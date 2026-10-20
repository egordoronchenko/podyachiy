import json

import pytest

from app import triage


def test_schema_instead_of_answer_is_rejected():
    # так однажды ответила модель: схема JSON вместо разбора
    raw = json.dumps({"type": "object", "properties": {"type": {"type": "string"}}})
    with pytest.raises(ValueError):
        triage.parse(raw)


def test_valid_answer_is_parsed():
    raw = json.dumps({"type": "spec", "confidence": "высокая", "title": "План закупок",
                      "speakers": [{"label": "S1", "name": "Иван", "role": ""}]})
    data = triage.parse(raw)
    assert data["type"] == "spec"
    assert data["speakers"] == [{"label": "S1", "name": "Иван", "heard": [], "role": ""}]


def test_other_spellings_of_a_name_reach_the_protocol_notes():
    """Распознавание пишет одно имя по-разному. Разбор выбирает один вариант, остальные отдаёт
    в heard — и справка для протокола называет их, чтобы выбор не был сделан молча."""
    raw = json.dumps({"type": "meeting", "speakers": [
        {"label": "S1", "name": "Нина Аркадьевна Зорина", "role": "ведёт встречу",
         "heard": ["Инна Аркадьевна", "Нина Аркадьевна Зорина", "", "Инна Аркадьевна", 7, {"x": 1}]},
        {"label": "S2", "name": "", "role": "докладчик", "heard": ["Павел"]},
        {"label": "S3", "name": "Павел Тихонов", "role": "", "heard": "Павел Тихомиров"}]})
    data = triage.parse(raw)
    assert [s["heard"] for s in data["speakers"]] == [["Инна Аркадьевна", "7"], ["Павел"], []]
    notes = triage.notes(data)
    assert ("- S1 — Нина Аркадьевна Зорина, ведёт встречу (имя на слух: в записи это же имя звучит ещё "
            "как Инна Аркадьевна, 7 — человек один; в протоколе перечисли варианты в «Комментариях»)") in notes
    lines = notes.splitlines()
    assert "- S2 — докладчик" in lines              # имени нет — и разночтениям не к чему относиться
    assert "- S3 — Павел Тихонов" in lines


def test_triage_saved_before_the_field_appeared_still_gives_notes():
    saved = {"type": "meeting", "speakers": [{"label": "S1", "name": "Иван", "role": "ведёт"}]}
    assert "- S1 — Иван, ведёт" in triage.notes(saved)


def test_standard_task_asks_for_the_field_the_parser_reads():
    from pathlib import Path
    task = (Path(__file__).resolve().parent.parent / "prompts" / "triage.md").read_text(encoding="utf-8")
    assert "`heard`" in task and '"heard": [' in task
