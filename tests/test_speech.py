"""Сшивка частей длинной записи.

Сломанная сшивка ничего не роняет: расшифровка просто выходит с задвоенным
перехлёстом и лишними голосами. Так и прожила три записи подряд, поэтому
проверяется здесь, а не глазами.
"""
from app import speech

STEP = speech.CHUNK_SEC - speech.OVERLAP_SEC


def talk(start: float, end: float, voices: list[str]) -> list[dict]:
    """Разговор: по слову в полсекунды, голоса сменяются каждые десять слов.

    Слова пронумерованы по времени — одно и то же слово в двух частях
    совпадает текстом и временем, как у настоящего провайдера.
    """
    words = []
    t = start
    n = int(start * 2)
    while t < end:
        who = voices[(n // 10) % len(voices)]
        words.append({"speaker": who, "start": t, "end": t + 0.4, "text": f"слово{n}"})
        t += 0.5
        n += 1
    return words


def relabel(words: list[dict], mapping: dict[str, str]) -> list[dict]:
    return [{**w, "speaker": mapping[w["speaker"]]} for w in words]


def two_parts(new_labels: dict[str, str]):
    first = talk(0, speech.CHUNK_SEC, ["S1", "S2", "S3"])
    second = relabel(talk(STEP, STEP + 600, ["S1", "S2", "S3"]), new_labels)
    return [(0.0, speech.CHUNK_SEC, first), (STEP, 600.0, second)]


def test_overlap_not_duplicated():
    words, cuts = speech.assemble(two_parts({"S1": "S1", "S2": "S2", "S3": "S3"}))
    assert cuts == [STEP + speech.OVERLAP_SEC / 2]
    starts = [w["start"] for w in words]
    assert starts == sorted(starts)
    assert len(starts) == len(set(starts))
    assert starts[-1] >= STEP + 599


def test_labels_follow_people_across_parts():
    # вторая часть пронумеровала тех же троих по-своему
    words, _ = speech.assemble(two_parts({"S1": "S3", "S2": "S1", "S3": "S2"}))
    tail = [w for w in words if w["start"] >= speech.CHUNK_SEC]
    assert {w["speaker"] for w in words} == {"S1", "S2", "S3"}
    expected = talk(speech.CHUNK_SEC, STEP + 600, ["S1", "S2", "S3"])
    assert [w["speaker"] for w in tail] == [w["speaker"] for w in expected]


def test_voice_absent_from_overlap_gets_fresh_label():
    first = talk(0, speech.CHUNK_SEC, ["S1", "S2"])
    second = (talk(STEP, speech.CHUNK_SEC, ["S1", "S2"])
              + [{"speaker": "S3", "start": speech.CHUNK_SEC + 10 + i * 0.5,
                  "end": speech.CHUNK_SEC + 10.4 + i * 0.5, "text": f"новый{i}"}
                 for i in range(20)])
    words, _ = speech.assemble([(0.0, speech.CHUNK_SEC, first), (STEP, 600.0, second)])
    assert {w["speaker"] for w in words} == {"S1", "S2", "S3"}


def test_common_word_elsewhere_does_not_vote():
    # «да» в разных местах перехлёста — не одно и то же слово
    prev = [{"speaker": "S1", "start": 10.0, "end": 10.3, "text": "да"}]
    new = [{"speaker": "S7", "start": 70.0, "end": 70.3, "text": "Да,"}]
    mapping, matched = speech.match_labels(prev, new, 0.0, 120.0)
    assert (mapping, matched) == ({}, 0)


def test_segment_breaks_at_seam():
    words = [{"speaker": "S1", "start": t, "end": t + 0.4, "text": f"w{t}"} for t in (10.0, 20.0, 30.0)]
    assert [s["start"] for s in speech.segments_from_words(words, [25.0])] == [10.0, 30.0]


def test_long_turn_gets_time_marks_at_phrase_starts():
    # один человек говорит две минуты: фраза каждые десять секунд
    words = [{"speaker": "S1", "start": float(t), "end": t + 0.4, "text": f"фраза{t}."}
             for t in range(0, 120, 10)]
    (seg,) = speech.segments_from_words(words)
    assert [at for _, at in seg["points"]] == [50.0, 100.0]
    text, marks = speech.to_text_timed([seg])
    assert marks == {"00:00:00", "00:00:50", "00:01:40"}
    assert "[00:00:50] [S1] фраза50. фраза60." in text
    assert text.count("фраза") == 12
