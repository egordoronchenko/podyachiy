"""Поиск: выдержки — чистым текстом, без разметки протокола, и подсветка стоит на своём месте."""
import json

from app import search

PROTOCOL = """<!-- podyachiy: собрано 2026-10-09 -->
# Протокол совещания

## 1. Кратко

Согласовали **бюджет** склада и *сроки* поставки — см. [смету по складу](https://podyachiy.example/smeta_2026) и `КС-2`.

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|:---:|---|---|
| **Иванов Иван** | | | |
| Согласовать __бюджет__ ремонта | до пятницы | [00:10:05] | — |

- [ ] проверить ~~старый~~ бюджет_план и 2 * 3 * 4 коробки
"""


def record(tmp_path, protocol=PROTOCOL):
    out = tmp_path / "Проекты" / "вкс"
    for sub in ("summary", "transcript"):
        (out / sub).mkdir(parents=True)
    (out / "summary" / "вкс.md").write_text(protocol, encoding="utf-8")
    (out / "transcript" / "вкс.txt").write_text("[00:00:01] S1: Про **бюджет** — звёздочки сказаны вслух.\n",
                                                encoding="utf-8")
    (out / "transcript" / "вкс.triage.json").write_text(json.dumps({"title": "Планёрка"}), encoding="utf-8")
    return [{"file": "Проекты/вкс.mp4", "stem": "вкс", "finished_at": "2026-10-09T12:00:00+03:00"}]


def test_plain_drops_the_markup_and_keeps_the_words():
    assert search.plain(PROTOCOL).split("\n") == [
        "", "Протокол совещания", "", "1. Кратко", "",
        "Согласовали бюджет склада и сроки поставки — см. смету по складу и КС-2.", "",
        "5. Задачи", "",
        "Задача · Срок · В записи · Статус",
        "Иванов Иван",
        "Согласовать бюджет ремонта · до пятницы · [00:10:05] · —", "",
        "проверить старый бюджет_план и 2 * 3 * 4 коробки"]


def test_snippets_of_a_protocol_are_plain_text_with_the_match_in_its_place(tmp_path):
    found = search.find(record(tmp_path), tmp_path, "бюджет")["files"][0]
    assert found["kinds"] == {"summary": 3, "transcript": 1} and found["hits"] == 4
    pieces = found["snippets"]
    assert [p["match"] for p in pieces] == ["бюджет", "бюджет", "бюджет"]
    for piece in pieces:
        whole = piece["before"] + piece["match"] + piece["after"]
        for mark in ("**", "__", "#", "|", "~~", "`", "](", "<!--", "http"):
            assert mark not in whole, (mark, whole)
    # найденное слово стоит между «до» и «после» ровно там, где оно в тексте без разметки
    flat = " ".join(search.plain(PROTOCOL).split())
    first = pieces[0]
    assert first["before"].endswith("Кратко Согласовали") and first["after"].startswith("склада и сроки поставки — см. смету")
    assert f'{first["before"]} {first["match"]} {first["after"]}'.rstrip("…") in flat
    assert pieces[1]["before"].endswith("Иванов Иван Согласовать") and pieces[1]["after"].startswith("ремонта · до пятницы")


def test_transcript_is_searched_as_it_is(tmp_path):
    """Расшифровка — не Markdown: что в ней написано, то и показано."""
    found = search.find(record(tmp_path, "# Протокол\n\nНичего.\n"), tmp_path, "звёздочки")["files"][0]
    assert found["open_kind"] == "transcript"
    assert found["snippets"][0]["before"] == "[00:00:01] S1: Про **бюджет** —"
