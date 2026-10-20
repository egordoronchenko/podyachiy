"""Подписанное вручную ФИО — окончательное: его не перебивает догадка разбора,
и в протокол оно уходит прямо в тексте расшифровки.

Подписи ставятся по частям записи: после стыка модель раздаёт метки заново,
и S3 до стыка и S3 после — не обязательно один человек."""
from app import names, speech, triage

SEGMENTS = [{"speaker": "S1", "start": 5.0, "end": 8.0, "text": "Начнём."},
            {"speaker": "S3", "start": 9.0, "end": 12.0, "text": "Да."},
            {"speaker": "S3", "start": 3300.0, "end": 3305.0, "text": "Это уже другой."},
            {"speaker": "S1", "start": 3310.0, "end": 3312.0, "text": "Хорошо."}]
CUTS = [3240.0]


def test_part_names_do_not_cross_the_seam():
    signed = {"parts": {"1": {"S3": "Петров В. С."}}, "lines": {}}
    assert names.signed_names(SEGMENTS, CUTS, signed) == ["", "Петров В. С.", "", ""]


def test_line_name_beats_part_name():
    signed = {"parts": {"1": {"S1": "Иванов И. И."}, "2": {"S1": "Иванов И. И."}},
              "lines": {names.line_key(3310.0): "Сидорова А. Л."}}
    assert names.signed_names(SEGMENTS, CUTS, signed) == [
        "Иванов И. И.", "", "", "Сидорова А. Л."]


def test_protocol_source_carries_names():
    who = ["Петров В. С.", "", "", ""]
    text, marks = speech.to_text_timed(SEGMENTS, who)
    assert "[00:00:05] [Петров В. С.] Начнём." in text
    assert "[00:00:09] [S3] Да." in text
    assert "00:55:00" in marks


def test_notes_keep_guesses_only_for_unsigned_labels():
    tri = {"speakers": [{"label": "S1", "name": "Пётр", "role": "ведёт"},
                        {"label": "S3", "name": "Мария", "role": ""}]}
    text = triage.notes(tri, ["Петров Пётр Иванович"], {"S3"})
    assert "Петров Пётр Иванович" in text
    assert "S3 — Мария" in text
    assert "S1 — Пётр" not in text


def test_subtitles_follow_line_names():
    srt = "1\n00:54:59,900 --> 00:55:03,000\n[S3] Это уже\n\n2\n00:55:10,000 --> 00:55:11,000\n[S1] Хорошо.\n"
    lines = [{"start": 3300.0, "who": "Петров В. С."}, {"start": 3310.0, "who": "S1"}]
    out = names.name_srt(srt, lines)
    assert "[Петров В. С.] Это уже" in out and "[S1] Хорошо." in out
