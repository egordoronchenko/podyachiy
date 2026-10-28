"""Запись без речи: распознавание оплачено один раз, исход окончательный, повторов по расписанию нет.

Распознавание подменено на уровне запроса к поставщику (speech.call_stt) — всё остальное в
шаге 1 настоящее: части, сохранённые ответы, файлы расшифровки."""
import asyncio
import json
import types
from pathlib import Path

import pytest

from app import actions, ledger, pipeline, speech, summarize, triage
from tests.test_reuse import Cfg

REL, STEM = "Планёрки/тишина.mp3", "тишина"


@pytest.fixture
def silent(tmp_path, monkeypatch):
    cfg = Cfg(inbox=tmp_path / "in", outbox=tmp_path / "out", model="deepgram/nova-3",
              prompts_dir=Path(__file__).resolve().parent.parent / "prompts")
    (cfg.inbox / "Планёрки").mkdir(parents=True)
    (cfg.inbox / REL).write_bytes(b"x")
    seen = types.SimpleNamespace(paid=0, text=0, lines=[])

    def audio(src, out_dir, stem, force, log):
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{stem}.audio.mp3"
        if force or not path.exists():
            path.write_bytes(b"sound")
        return path

    async def stt(client, cfg, chunk, log=None, terms=()):
        seen.paid += 1
        return {"text": "", "words": []}                    # поставщик ничего не услышал

    async def text_step(*a, **kw):
        seen.text += 1
        raise AssertionError("текстовые шаги по пустой расшифровке не идут")

    monkeypatch.setattr(pipeline.media, "ensure_audio", audio)
    monkeypatch.setattr(speech, "audio_duration", lambda path: 300.0)
    monkeypatch.setattr(speech, "make_chunk", lambda src, dst, start, length: dst.write_bytes(b"part"))
    monkeypatch.setattr(speech, "call_stt", stt)
    monkeypatch.setattr(triage, "triage_file", text_step)
    monkeypatch.setattr(summarize, "summarize_file", text_step)
    monkeypatch.setattr(actions, "extract_file", text_step)
    return cfg, seen


def run(cfg, seen, force=False):
    return asyncio.run(pipeline.process(cfg, REL, STEM, None, force, False, seen.lines.append))


def test_recording_without_speech_is_final_and_paid_once(silent):
    cfg, seen = silent
    record = run(cfg, seen)
    assert record["status"] == "no_audio" and "речи" in record["error"]
    assert record["status"] not in ledger.RETRY_STATUSES            # по расписанию повторять нечего
    assert seen.paid == 1 and seen.text == 0
    out = cfg.outbox / "Планёрки" / STEM
    assert json.loads((out / f"{STEM}{ledger.MARKER_SUFFIX}").read_text(encoding="utf-8"))["status"] == "no_audio"
    assert not (out / f"{STEM}.error.json").exists()
    assert any(line.startswith("нет речи: ") and "не повторяю" in line for line in seen.lines)


def test_rerun_without_force_does_not_pay_again(silent):
    """Человек запустил запись вручную ещё раз: пустая расшифровка уже есть — тот же исход бесплатно."""
    cfg, seen = silent
    run(cfg, seen)
    again = run(cfg, seen)
    assert again["status"] == "no_audio" and again["reused_transcript"]
    assert seen.paid == 1 and seen.text == 0


def test_forced_run_recognizes_again(silent):
    cfg, seen = silent
    run(cfg, seen)
    assert run(cfg, seen, force=True)["status"] == "no_audio" and seen.paid == 2


def test_transcript_with_speech_still_goes_on_to_the_text_steps(silent, monkeypatch):
    cfg, seen = silent

    async def stt(client, cfg, chunk, log=None, terms=()):
        seen.paid += 1
        return {"text": "Добрый день", "words": [{"word": "Добрый", "start": 0.1, "end": 0.4, "speaker": 0},
                                                 {"word": "день", "start": 0.5, "end": 0.8, "speaker": 0}]}

    monkeypatch.setattr(speech, "call_stt", stt)
    record = run(cfg, seen)
    assert record["status"] == "no_summary" and seen.text == 1     # дошло до разбора — пустой расшифровки нет


def test_record_keeps_what_the_source_was_when_it_was_taken(silent):
    """Каким был исходник, когда его взяли в работу: размер, время изменения и отпечаток содержимого. По ним
    сканер узнаёт запись под другим именем и замечает, что исходник изменился (test_watcher)."""
    from app import media
    cfg, seen = silent
    src = cfg.inbox / REL
    record = run(cfg, seen)
    assert record["source_size"] == 1 and record["source_mtime"] == src.stat().st_mtime
    assert record["source_print"] == media.fingerprint(src) and len(record["source_print"]) == 64


def test_fingerprint_reads_the_edges_and_tells_files_apart(tmp_path, monkeypatch):
    from app import media
    monkeypatch.setattr(media, "PRINT_EDGE", 4)
    a, b, c, d = (tmp_path / n for n in "abcd")
    a.write_bytes(b"head-middle-tail")
    b.write_bytes(b"head-MIDDLE-tail")                  # середину отпечаток не читает
    c.write_bytes(b"head-middle-tall")
    d.write_bytes(b"head-middle-tail!")
    assert media.fingerprint(a) == media.fingerprint(b)
    assert len({media.fingerprint(p) for p in (a, c, d)}) == 3
    assert media.fingerprint(tmp_path / "нет") is None
