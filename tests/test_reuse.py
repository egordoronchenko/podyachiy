"""Готовая расшифровка переиспользуется, какая бы модель распознавания ни была выбрана сейчас.

Распознавание — самая дорогая часть обработки, а у новой расшифровки другие метки говорящих,
и ручные подписи к ней не подходят. Поэтому смена модели не должна превращать пересборку
протокола старой записи в платное распознавание заново: заново распознаёт только явный
полный прогон (force) — и только он берёт модель, выбранную сейчас.
"""
import asyncio
import dataclasses
import json
import types
from pathlib import Path

import pytest

from app import actions, db, dictionary, names, pipeline, speech, summarize, triage, usage, watcher
from app.ledger import Ledger

NOVA, SCRIBE, MAI = "deepgram/nova-3", "elevenlabs/scribe-v2", "microsoft/mai-transcribe-2"
REL, STEM = "Планёрки/вкс.mp3", "вкс"
TRI = {"type": "meeting", "title": "Планёрка", "gist": "О складе", "date": "", "topics": [],
       "speakers": [{"label": "S1", "name": "", "role": ""}], "confidence": "высокая", "type_reason": ""}
SIGNED = {"parts": {"1": {"S1": "Иванов Иван Иванович"}}, "lines": {}}


@dataclasses.dataclass(frozen=True)
class Cfg:
    inbox: Path
    outbox: Path
    prompts_dir: Path
    model: str
    summary_model: str = "тест"
    actions_models: tuple = ("тест",)
    summary_max_tokens: int = 0
    prev_tasks: bool = False
    public_url: str = ""
    workers: int = 1
    language: str = "ru"


@pytest.fixture
def done(tmp_path, monkeypatch):
    """Запись, распознанная nova-3 и подписанная вручную; модели подменены и считают вызовы."""
    cfg = Cfg(inbox=tmp_path / "in", outbox=tmp_path / "out", model=NOVA,
              prompts_dir=Path(__file__).resolve().parent.parent / "prompts")
    (cfg.inbox / "Планёрки").mkdir(parents=True)
    (cfg.inbox / REL).write_bytes(b"x")
    base = cfg.outbox / "Планёрки" / STEM
    (base / "transcript").mkdir(parents=True)
    (base / "transcript" / f"{STEM}.json").write_text(json.dumps(
        {"model": NOVA, "segments": [{"start": 5, "end": 6, "speaker": "S1", "text": "Согласуйте смету."}],
         "speakers": [{"id": "S1", "description": ""}]}), encoding="utf-8")
    (base / "transcript" / f"{STEM}.meta.json").write_text(json.dumps(
        {"success": True, "model": NOVA, "audio_duration_sec": 700.0, "speakers": 1}), encoding="utf-8")
    names.save(base, STEM, SIGNED)

    seen = types.SimpleNamespace(speech=[], protocol=[], lines=[])

    async def transcribe(audio_path, out_dir, stem, cfg, work_dir, on_progress=None, log=None, suffix="transcript",
                         terms=()):
        seen.speech.append(cfg.model)
        segments = [{"start": 5, "end": 6, "speaker": "S2", "text": "Согласуйте смету."}]
        (out_dir / suffix / f"{stem}.json").write_text(json.dumps({"model": cfg.model, "segments": segments}),
                                                       encoding="utf-8")
        meta = {"success": True, "model": cfg.model, "audio_duration_sec": 700.0, "speakers": 1}
        (out_dir / suffix / f"{stem}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return {"meta": meta, "segments": segments, "speakers": [], "crowded_meeting": False}

    async def protocol(file_dir, stem, template_key, template_text, cfg, log, notes="", who=None, **kw):
        seen.protocol.append(list(who or []))
        return {"summary_chars": 10, "timestamps": 0, "timestamps_unknown": 0}

    async def no_actions(*a, **kw):
        return ""

    monkeypatch.setattr(speech, "transcribe_one", transcribe)
    monkeypatch.setattr(pipeline.media, "ensure_audio", lambda src, out_dir, stem, force, log: src)
    monkeypatch.setattr(triage, "load_saved", lambda *a, **kw: dict(TRI))
    monkeypatch.setattr(triage, "triage_file", lambda *a, **kw: asyncio.sleep(0, dict(TRI)))
    monkeypatch.setattr(summarize, "summarize_file", protocol)
    monkeypatch.setattr(actions, "extract_file", no_actions)
    return cfg, base, seen


def run(cfg, seen, force=False):
    record = asyncio.run(pipeline.process(cfg, REL, STEM, None, force, True, seen.lines.append))
    assert record["status"] == "ok", record.get("error")
    return record


def test_rebuild_after_the_model_change_recognizes_nothing_and_keeps_the_names(done):
    """Обработана nova-3, подписаны ФИО, модель по умолчанию сменили на Scribe, нажали «пересобрать»."""
    cfg, base, seen = done
    record = run(dataclasses.replace(cfg, model=SCRIBE), seen)
    assert seen.speech == []                                    # ни одного вызова распознавания
    assert record["reused_transcript"] and record["reused_triage"]
    assert record["speech_model"] == NOVA                       # чем сделана расшифровка — видно у записи
    assert names.load(base, STEM) == SIGNED                     # подписи на месте
    assert seen.protocol == [["Иванов Иван Иванович"]]          # и ушли в протокол
    assert json.loads((base / "transcript" / f"{STEM}.meta.json").read_text(encoding="utf-8"))["model"] == NOVA
    assert any(f"беру результат прошлого запуска ({NOVA})" in line for line in seen.lines)
    assert record["cost"] == {} and record["named_speakers"] == 1


def test_only_a_forced_run_recognizes_again_with_the_current_model(done):
    cfg, base, seen = done
    record = run(dataclasses.replace(cfg, model=SCRIBE), seen, force=True)
    assert seen.speech == [SCRIBE] and not record["reused_transcript"]
    assert record["speech_model"] == SCRIBE
    # новая расшифровка — другие метки: прежние подписи к ней не относятся
    assert names.load(base, STEM) == {"parts": {}, "lines": {}}
    assert any("подписи говорящих сброшены" in line for line in seen.lines)


@pytest.mark.parametrize("broken", ["нет расшифровки", "битый файл", "неудачный прогон"])
def test_missing_or_broken_transcript_is_recognized_again(done, broken):
    cfg, base, seen = done
    data, meta = base / "transcript" / f"{STEM}.json", base / "transcript" / f"{STEM}.meta.json"
    if broken == "нет расшифровки":
        data.unlink()
    elif broken == "битый файл":
        data.write_text('{"segments": [{"start"', encoding="utf-8")
    else:
        meta.write_text(json.dumps({"success": False, "model": NOVA}), encoding="utf-8")
    record = run(dataclasses.replace(cfg, model=SCRIBE), seen)
    assert seen.speech == [SCRIBE] and record["speech_model"] == SCRIBE


def test_worker_takes_the_model_for_new_records_from_the_settings_and_for_a_run_from_the_run(done, monkeypatch):
    """Обработчик: новая запись — моделью из настроек на этот момент; ручной запуск с перезаписью —
    своей; пересборка — ничем."""
    cfg, base, seen = done
    chosen = [SCRIBE]
    events = []
    trail = types.SimpleNamespace(run=lambda record, by, reason: events.append((reason, record["speech_model"])),
                                  uploader=lambda file: "")
    svc = watcher.Service(cfg=cfg, log=seen.lines.append, ledger=Ledger(db.Database(cfg.outbox.parent / "state")), journal=trail,
                          speech_model=lambda: chosen[0], tell=lambda record, kind, login, by_hand: None,
                          words=lambda rel: dictionary.merge([]))
    usage.reset()

    async def drain():
        while svc.queue:
            await svc._process(*svc._take())

    (cfg.inbox / "новая.mp3").write_bytes(b"x")
    (cfg.outbox / "новая" / "transcript").mkdir(parents=True)
    svc.queue.append(watcher.QueueItem(path="новая.mp3"))
    asyncio.run(drain())
    chosen[0] = NOVA                                            # выбрали другую — без перезапуска
    (cfg.inbox / "ещё.mp3").write_bytes(b"x")
    (cfg.outbox / "ещё" / "transcript").mkdir(parents=True)
    svc.queue.append(watcher.QueueItem(path="ещё.mp3"))
    asyncio.run(drain())
    assert seen.speech == [SCRIBE, NOVA]

    svc.ledger.put({"file": REL, "stem": STEM, "status": "ok", "finished_at": pipeline.now_iso()})
    svc.enqueue_manual(REL, None, False, "ivanov", "rebuild")           # «пересобрать протокол»
    asyncio.run(drain())
    assert seen.speech == [SCRIBE, NOVA] and svc.ledger.get(REL)["speech_model"] == NOVA

    svc.enqueue_manual(REL, None, True, "admin", "manual", MAI)         # ручной запуск с перезаписью и моделью
    assert svc.snapshot()["queue"][0]["model"] == MAI
    asyncio.run(drain())
    assert seen.speech == [SCRIBE, NOVA, MAI] and svc.ledger.get(REL)["speech_model"] == MAI
    assert events[-2:] == [("rebuild", NOVA), ("manual", MAI)]
