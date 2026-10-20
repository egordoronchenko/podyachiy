"""Поручения выписываются отдельным шагом: считаем строки списка, а не шапку."""
from app import actions, templates


def test_rows_skip_header_and_rule():
    table = ("| № | Тип | Кто | Что | Срок | В записи | Цитата |\n|---|---|---|---|---|---|---|\n"
             "| 1 | задача | Иванов | Проверить | сегодня | [00:01:02] | проверю сегодня |\n"
             "| 2 | решение | — | Не уходим | — | [00:02:03] | не уходим |\n")
    assert actions.rows(table) == 2
    assert actions.rows("Поручений нет.") == 0


def test_only_meeting_template_asks_for_actions():
    assert [k for k, m in templates.TEMPLATES.items() if m.get("actions")] == ["meeting"]
