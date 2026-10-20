"""Модели распознавания: чем отличается запрос и ответ у каждой.

Ответы ниже — выдуманные, но той же структуры, что настоящие ответы поставщиков
через OpenRouter: у Scribe в словах есть confidence, speaker_label и события
type=audio_event, сегмент один на всю запись и без говорящего; у nova-3 слово —
только word, start, end, speaker; у MAI — то же, а говорящий стоит ещё и на сегменте-фразе.
"""
import asyncio
import json
import types

import pytest

from app import providers, speech, usage
from app.config import Settings

NOVA = "deepgram/nova-3"
MAI = "microsoft/mai-transcribe-2"
SCRIBE = "elevenlabs/scribe-v2"


def cfg(model: str, language: str = "ru", prompts_dir=None):
    return types.SimpleNamespace(model=model, language=language, prompts_dir=prompts_dir, backup_provider=None,
                                 speech_provider=providers.make("http://x/v1", "k", "OPENAI_API_KEY", "USD"))


def scribe_word(text: str, start: float, end: float, who: int, kind: str | None = None) -> dict:
    w = {"word": text, "start": start, "end": end, "speaker": who,
         "confidence": 0.97, "speaker_label": f"speaker_{who}"}
    if kind:
        w["type"] = kind
    return w


def scribe_reply(words: list[dict], seconds: float, cost: float) -> dict:
    return {"text": " ".join(w["word"] for w in words), "task": "transcribe",
            "language": "rus", "language_confidence": 1, "duration": seconds,
            "usage": {"seconds": seconds, "cost": cost},
            "segments": [{"id": 0, "start": 0, "end": seconds, "text": "…"}],
            "words": words}


# 14 элементов: 12 слов двух голосов, одно событие и одно тире — отдельным элементом, как у Scribe
SCRIBE_WORDS = [
    scribe_word("Коллеги,", 0, 0.5, 0),
    scribe_word("начинаем", 0.6, 1.1, 0),
    scribe_word("планёрку.", 1.2, 1.8, 0),
    scribe_word("[смеётся]", 2.0, 3.5, 0, "audio_event"),
    scribe_word("Выгрузку", 4.0, 4.5, 1),
    scribe_word("в", 4.6, 4.7, 1),
    scribe_word("Excel", 4.8, 5.2, 1),
    scribe_word("доделаю", 5.3, 5.8, 1),
    scribe_word("—", 5.8, 5.8, 1),
    scribe_word("к", 5.9, 6.0, 1),
    scribe_word("пятнице.", 6.1, 6.6, 1),
    scribe_word("Хорошо,", 7.0, 7.4, 0),
    scribe_word("записали.", 7.5, 8.0, 0),
    scribe_word("[кашель]", 8.2, 8.4, 1, "audio_event"),
]

NOVA_REPLY = {
    "text": "коллеги начинаем выгрузку доделаю", "language": "ru", "duration": 6.0,
    "usage": {"seconds": 6.0, "cost": 0.00043},
    "segments": [{"id": 0, "start": 0, "end": 1.1, "text": "коллеги начинаем", "speaker": 0}],
    "words": [{"word": "коллеги", "start": 0, "end": 0.5, "speaker": 0},
              {"word": "начинаем", "start": 0.5, "end": 1.1, "speaker": 0},
              {"word": "выгрузку", "start": 4.0, "end": 4.5, "speaker": 1},
              {"word": "доделаю", "start": 5.3, "end": 5.8, "speaker": 1}],
}


def test_scribe_reply_becomes_words_with_speakers_and_time():
    reply = scribe_reply(SCRIBE_WORDS, 9.0, 0.000275)
    words, unlabeled = speech.normalize_words(speech.spoken_words(reply, SCRIBE), offset=100.0)
    assert unlabeled == 0 and len(words) == 12
    assert words[0] == {"speaker": "S1", "start": 100.0, "end": 100.5, "text": "Коллеги,"}
    assert words[5] == {"speaker": "S2", "start": 104.8, "end": 105.2, "text": "Excel"}
    segments = speech.segments_from_words(words)
    assert [(s["speaker"], s["text"]) for s in segments] == [
        ("S1", "Коллеги, начинаем планёрку."),
        ("S2", "Выгрузку в Excel доделаю — к пятнице."),
        ("S1", "Хорошо, записали."),
    ]


def test_scribe_audio_events_are_not_words():
    reply = scribe_reply(SCRIBE_WORDS, 9.0, 0.000275)
    spoken = speech.spoken_words(reply, SCRIBE)
    assert len(spoken) == len(SCRIBE_WORDS) - 2
    assert not any("[" in w["word"] for w in spoken)
    # событие стояло между репликами разных людей — в текст и субтитры оно не попало
    words, _ = speech.normalize_words(spoken)
    assert "смеётся" not in speech.to_text(speech.segments_from_words(words))
    assert "кашель" not in speech.to_srt(words)


def test_nova_reply_is_taken_whole():
    assert speech.spoken_words(NOVA_REPLY, NOVA) == NOVA_REPLY["words"]
    words, unlabeled = speech.normalize_words(speech.spoken_words(NOVA_REPLY, NOVA))
    assert unlabeled == 0 and [w["speaker"] for w in words] == ["S1", "S1", "S2", "S2"]


def test_scribe_speakers_are_matched_across_two_parts():
    """Две части с перехлёстом: вторая нумерует тех же двоих наоборот и приводит третьего."""
    start2, length1 = 60.0, 80.0              # перехлёст — с 60-й по 80-ю секунду

    def talk(begin: float, end: float, number: dict[str, int], shift: float) -> list[dict]:
        out, t = [], begin
        while t < end:
            n = int(t)
            who = "а" if (n // 6) % 2 == 0 else "б"
            out.append(scribe_word(f"слово{n}", t - shift, t - shift + 0.6, number[who]))
            if n % 7 == 0:      # события попадают и в перехлёст: сшивке они мешать не должны
                out.append(scribe_word("[шум]", t - shift + 0.6, t - shift + 0.9,
                                       number[who], "audio_event"))
            t += 1.0
        return out

    first = scribe_reply(talk(0, length1, {"а": 0, "б": 1}, 0.0), length1, 0.0024)
    second_words = talk(start2, 110, {"а": 1, "б": 0}, start2)
    second_words += [scribe_word(f"новый{i}", 50 + i, 50.6 + i, 2) for i in range(5)]
    second = scribe_reply(second_words, 55.0, 0.0017)

    parts = []
    for begin, length, reply in ((0.0, length1, first), (start2, 55.0, second)):
        words, _ = speech.normalize_words(speech.spoken_words(reply, SCRIBE), offset=begin)
        parts.append((begin, length, words))
    lines: list[str] = []
    words, cuts = speech.assemble(parts, lines.append)

    assert cuts == [70.0]
    starts = [w["start"] for w in words]
    assert starts == sorted(starts) and len(starts) == len(set(starts))
    spoken = [w for w in words if w["text"].startswith("слово")]
    assert len(spoken) == 110                  # ни одно слово перехлёста не задвоено и не потеряно
    for w in spoken:
        n = int(w["text"][len("слово"):])
        assert w["speaker"] == ("S1" if (n // 6) % 2 == 0 else "S2"), w
    # кого в перехлёсте не было, тот новый человек со свободным номером
    assert {w["speaker"] for w in words if w["text"].startswith("новый")} == {"S3"}
    assert "S2→S1" in lines[0] and "S1→S2" in lines[0] and "новые голоса: S3" in lines[0]


def test_form_for_nova_turns_speakers_on_by_provider_option():
    form = speech.build_form(cfg(NOVA))
    assert form == {
        "model": NOVA, "response_format": "verbose_json",
        "timestamp_granularities[]": ["word", "segment"], "language": "ru",
        "provider": json.dumps({"options": {"deepgram": {"diarize": True}}}),
    }


def test_form_for_mai_uses_azure_slug_and_carries_terms():
    form = speech.build_form(cfg(MAI), ["СКУД", "регистр сведений"])
    assert json.loads(form["provider"]) == {"options": {"azure": {
        "diarization": {"enabled": True}, "phraseList": {"phrases": ["СКУД", "регистр сведений"]}}}}
    assert "diarize" not in form            # общий diarize у этой модели — отказ 400
    # словаря нет — списка фраз в запросе нет вовсе
    assert json.loads(speech.build_form(cfg(MAI))["provider"]) == {
        "options": {"azure": {"diarization": {"enabled": True}}}}


def test_terms_go_only_to_the_model_that_hears_them():
    terms = ["СКУД"]
    assert "СКУД" not in json.dumps(speech.build_form(cfg(NOVA), terms), ensure_ascii=False)
    assert "СКУД" not in json.dumps(speech.build_form(cfg(SCRIBE), terms), ensure_ascii=False)


def test_glossary_terms_are_split_and_comments_dropped(tmp_path):
    (tmp_path / "glossary.txt").write_text(
        "# пояснение, для людей\n1С:ЗУП, СКУД,  регистр сведений\n\nEDT\nСКУД\n", encoding="utf-8")
    assert speech.glossary_terms(tmp_path) == ["1С:ЗУП", "СКУД", "регистр сведений", "EDT"]
    assert speech.glossary_terms(tmp_path / "нет") == []


def test_mai_reply_keeps_word_times_and_phrase_speakers():
    """Метку MAI ставит на фразу, но в ответе она уже стоит на каждом слове, и время у слова своё."""
    reply = {
        "text": "Коллеги, начинаем. Выгрузку в Excel доделаю. Хорошо.", "language": "ru",
        "duration": 8.0, "usage": {"seconds": 8, "cost": 0.000222},
        "segments": [{"id": 0, "start": 0.1, "end": 1.1, "text": "Коллеги, начинаем.", "speaker": 0},
                     {"id": 1, "start": 4.0, "end": 5.8, "text": "Выгрузку в Excel доделаю.", "speaker": 1},
                     {"id": 2, "start": 7.0, "end": 7.4, "text": "Хорошо.", "speaker": 0}],
        "words": [{"word": "Коллеги,", "start": 0.1, "end": 0.5, "speaker": 0},
                  {"word": "начинаем.", "start": 0.6, "end": 1.1, "speaker": 0},
                  {"word": "Выгрузку", "start": 4.0, "end": 4.5, "speaker": 1},
                  {"word": "в", "start": 4.6, "end": 4.7, "speaker": 1},
                  {"word": "Excel", "start": 4.8, "end": 5.2, "speaker": 1},
                  {"word": "доделаю.", "start": 5.3, "end": 5.8, "speaker": 1},
                  {"word": "Хорошо.", "start": 7.0, "end": 7.4, "speaker": 0}],
    }
    words, unlabeled = speech.normalize_words(speech.spoken_words(reply, MAI))
    assert unlabeled == 0 and len(words) == 7
    segments = speech.segments_from_words(words)
    assert [(s["speaker"], s["start"], s["end"], s["text"]) for s in segments] == [
        ("S1", 0.1, 1.1, "Коллеги, начинаем."),
        ("S2", 4.0, 5.8, "Выгрузку в Excel доделаю."),
        ("S1", 7.0, 7.4, "Хорошо."),
    ]


def test_catalogue_lists_three_models_and_scribe_is_default():
    listed = speech.models()
    assert [m["id"] for m in listed] == [SCRIBE, NOVA, MAI]
    assert speech.DEFAULT_MODEL == SCRIBE and [m["default"] for m in listed] == [True, False, False]
    assert [m["takes_terms"] for m in listed] == [False, False, True]
    for m in listed:
        assert m["title"] and m["note"] and 0 < m["price_hour"] < 1
        speech.engine(m["id"])


def test_form_for_scribe_turns_speakers_on_by_provider_option():
    """Общее поле diarize шлюз routerai.ru молча не выполняет — у Scribe тоже параметр поставщика."""
    form = speech.build_form(cfg(SCRIBE, language="auto"))
    assert form == {
        "model": SCRIBE, "response_format": "verbose_json",
        "timestamp_granularities[]": ["word", "segment"],
        "provider": json.dumps({"options": {"elevenlabs": {"diarize": True}}}),
    }


@pytest.mark.parametrize("model", [SCRIBE, NOVA, MAI])
def test_no_model_uses_the_common_diarize_field(model):
    assert "diarize" not in speech.build_form(cfg(model), ["СКУД"])


def test_unknown_model_is_named_in_the_error():
    with pytest.raises(ValueError) as failed:
        speech.engine("elevenlabs/scribe-v3")
    text = str(failed.value)
    assert "SPEECH_MODEL=elevenlabs/scribe-v3" in text
    assert NOVA in text and SCRIBE in text


def test_unknown_model_stops_the_service_at_start(monkeypatch):
    monkeypatch.setenv("SPEECH_MODEL", "deepgram/nova3")
    with pytest.raises(ValueError, match="SPEECH_MODEL=deepgram/nova3"):
        Settings.from_env()
    monkeypatch.setenv("SPEECH_MODEL", "x-ai/grok-stt-1.0")       # снята с поддержки: нестабильна
    with pytest.raises(ValueError):
        Settings.from_env()
    for known in (NOVA, MAI, SCRIBE):
        monkeypatch.setenv("SPEECH_MODEL", known)
        assert Settings.from_env().model == known


@pytest.mark.parametrize("value", [None, "", "  "])
def test_scribe_is_the_model_when_none_is_set(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SPEECH_MODEL", raising=False)
    else:
        monkeypatch.setenv("SPEECH_MODEL", value)
    assert Settings.from_env().model == SCRIBE


@pytest.fixture
def provider(monkeypatch):
    """Поставщик отвечает заготовленным ответом и запоминает присланную форму."""
    sent = {}

    class Reply:
        status_code, headers, text = 200, {}, ""

        def json(self):
            return sent["reply"]

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kw):
            sent.update(url=url, form=kw["data"])
            return Reply()

    monkeypatch.setattr(speech.httpx, "AsyncClient", Client)
    monkeypatch.setattr(speech, "audio_duration", lambda path: 9.0)
    monkeypatch.setattr(speech, "make_chunk", lambda src, dst, start, length: dst.write_bytes(b"mp3"))
    usage.reset()
    return sent


def test_scribe_goes_through_the_whole_transcription(provider, tmp_path):
    """Запись целиком: запрос собран под Scribe, события отброшены, расход записан на модель."""
    sent = provider
    sent["reply"] = scribe_reply(SCRIBE_WORDS, 9.0, 0.000275)
    work = tmp_path / "work"
    work.mkdir()
    result = asyncio.run(speech.transcribe_one(tmp_path / "запись.mp3", tmp_path, "запись",
                                               cfg(SCRIBE), work))

    assert sent["url"] == "http://x/v1/audio/transcriptions"
    assert json.loads(sent["form"]["provider"]) == {"options": {"elevenlabs": {"diarize": True}}}
    assert result["meta"]["model"] == SCRIBE and result["meta"]["speakers"] == 2
    assert result["meta"]["unlabeled"] == 0.0
    assert "смеётся" not in (tmp_path / "transcript" / "запись.txt").read_text(encoding="utf-8")
    raw = json.loads((tmp_path / "transcript" / "raw" / "запись.part01.json").read_text(encoding="utf-8"))
    assert len(raw["response"]["words"]) == len(SCRIBE_WORDS)      # сырой ответ хранится как пришёл
    spent = usage.snapshot()
    assert spent["by_model"] == [{"stage": "speech", "model": SCRIBE, "provider": "x", "currency": "USD",
                                  "cost": 0.000275}]


def test_mai_gets_the_service_glossary(provider, tmp_path):
    """Словарь сервиса уходит MAI списком фраз — тот же файл, что читает текстовая модель."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "glossary.txt").write_text("# словарь\nСКУД, 1С:ЗУП\n", encoding="utf-8")
    provider["reply"] = {"words": [{"word": "СКУД", "start": 0.1, "end": 0.6, "speaker": 0}],
                         "usage": {"seconds": 9, "cost": 0.00025}}
    work = tmp_path / "work"
    work.mkdir()
    lines: list[str] = []
    asyncio.run(speech.transcribe_one(tmp_path / "запись.mp3", tmp_path, "запись",
                                      cfg(MAI, prompts_dir=prompts), work, log=lines.append,
                                      terms=speech.glossary_terms(prompts)))
    assert json.loads(provider["form"]["provider"])["options"]["azure"]["phraseList"] == {
        "phrases": ["СКУД", "1С:ЗУП"]}
    assert "подсказки из словарей переданы распознаванию: 2" in lines
    assert usage.snapshot()["cost"] == {"USD": 0.00025}


# ---------- подсказки из словарей: пределы модели ----------

def test_hints_fit_the_limits_of_the_engine_and_the_cut_is_logged():
    """Модели уходит не больше её предела; первыми — строки словаря записи, общий словарь страдает первым."""
    limit, chars = speech.ENGINES[MAI].terms_max, speech.ENGINES[MAI].term_chars
    assert (limit, chars) == (500, 60)
    record, folder, common = ["Ершова Лидия Марковна", "КС-2"], [f"папка {n}" for n in range(498)], ["общий 1", "общий 2"]
    lines: list[str] = []
    sent = speech.fit_terms(MAI, record + folder + common, lines.append)
    assert len(sent) == 500 and sent[:2] == record and sent[-1] == "папка 497" and "общий 1" not in sent
    assert lines == ["подсказки распознаванию урезаны по пределам модели: уходит 500 из 502 "
                     "(не больше 500 фраз, каждая до 60 знаков); первыми идут строки словаря записи, "
                     "затем папки, затем общего словаря"]
    # слишком длинная фраза подсказкой не работает — не уходит, и об этом строка в журнале
    lines.clear()
    assert speech.fit_terms(MAI, ["СКУД", "д" * 61, "  скуд ", "", "план-факт"], lines.append) == ["СКУД", "план-факт"]
    assert len(lines) == 1 and "уходит 2 из 3" in lines[0]
    # в пределах — молча
    lines.clear()
    assert speech.fit_terms(MAI, ["СКУД", "КС-2"], lines.append) == ["СКУД", "КС-2"] and lines == []
    # остальным моделям подсказать нечем
    for model in (SCRIBE, NOVA):
        assert speech.fit_terms(model, ["СКУД"], lines.append) == [] and not speech.ENGINES[model].takes_terms
    assert lines == []


def test_dictionary_hints_reach_only_the_engine_that_takes_them(provider, tmp_path):
    """Подсказки из словаря записи и папки уходят MAI списком фраз; Scribe их не получает."""
    work = tmp_path / "work"
    work.mkdir()
    terms = ["КС-2", "Ершова Лидия Марковна", "Лидия Марковна", "Ершова", "СКУД"]
    provider["reply"] = {"words": [{"word": "СКУД", "start": 0.1, "end": 0.6, "speaker": 0}],
                         "usage": {"seconds": 9, "cost": 0.00025}}
    lines: list[str] = []
    asyncio.run(speech.transcribe_one(tmp_path / "запись.mp3", tmp_path, "запись", cfg(MAI), work,
                                      log=lines.append, terms=terms))
    assert json.loads(provider["form"]["provider"])["options"]["azure"]["phraseList"] == {"phrases": terms}
    assert "подсказки из словарей переданы распознаванию: 5" in lines

    provider["reply"] = scribe_reply(SCRIBE_WORDS, 9.0, 0.000275)
    lines.clear()
    asyncio.run(speech.transcribe_one(tmp_path / "запись.mp3", tmp_path, "запись", cfg(SCRIBE), work,
                                      log=lines.append, terms=terms))
    assert json.loads(provider["form"]["provider"]) == {"options": {"elevenlabs": {"diarize": True}}}
    assert not any("подсказки" in ln for ln in lines)
