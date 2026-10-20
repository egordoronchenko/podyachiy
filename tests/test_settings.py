"""Настройки, которые администратор меняет на странице: модель распознавания для новых записей."""
import json
import threading

import pytest

from app import db, settings

NOVA, SCRIBE, MAI = "deepgram/nova-3", "elevenlabs/scribe-v2", "microsoft/mai-transcribe-2"


def test_choice_is_kept_with_its_author_and_survives_a_restart(tmp_path):
    store = settings.Store(db.Database(tmp_path / "state"))
    assert store.speech_model() is None                         # ничего не выбрано — действует .env
    store.set_speech_model(NOVA, "admin")
    chosen = settings.Store(db.Database(tmp_path / "state")).speech_model()
    assert chosen["value"] == NOVA and chosen["by"] == "admin" and chosen["at"][:2] == "20"
    assert not (tmp_path / "state" / "settings.tmp").exists()   # запись через временный файл
    store.set_speech_model(None, "admin")                       # вернуть к значению из .env
    assert store.speech_model() is None and settings.Store(db.Database(tmp_path / "state")).speech_model() is None


def test_unknown_model_is_refused_and_a_model_the_service_dropped_falls_back_to_env(tmp_path):
    base = db.Database(tmp_path)
    store = settings.Store(base)
    with pytest.raises(ValueError):
        store.set_speech_model("x-ai/grok-stt-1.0", "admin")
    assert store.speech_model() is None and base.rows("SELECT * FROM settings") == []
    # выбрана моделью прежней версии сервиса, а эта версия её уже не знает
    lines = []
    with base.write() as con:
        con.execute("INSERT INTO settings (key, value, author, at) VALUES ('speech_model', ?, 'a', '')",
                    (json.dumps("x-ai/grok-stt-1.0"),))
    assert settings.Store(db.Database(tmp_path), lines.append).speech_model() is None and "больше не поддерживает" in lines[0]


def test_parallel_writes_leave_one_whole_choice(tmp_path):
    store = settings.Store(db.Database(tmp_path))
    threads = [threading.Thread(target=store.set_speech_model, args=(model, f"u{i}"))
               for i, model in enumerate([NOVA, SCRIBE, MAI] * 10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert settings.Store(db.Database(tmp_path)).speech_model()["value"] == store.speech_model()["value"]
