"""Протокол на диске: новый встаёт на место целиком, а обрыв посреди записи не оставляет полупустой файл."""
import asyncio
import json
import pathlib
import types

import pytest

from app import llm, summarize

STEM = "вкс"


def record_dir(tmp_path):
    base = tmp_path / "вкс"
    (base / "transcript").mkdir(parents=True)
    (base / "transcript" / f"{STEM}.json").write_text(json.dumps(
        {"segments": [{"start": 5, "end": 6, "speaker": "S1", "text": "Согласуйте смету."}]}), encoding="utf-8")
    return base


def build(base, monkeypatch, text="# Протокол\n\nСмету согласовать [00:00:05]."):
    async def chat(cfg, stage, system, user, **kw):
        return text, {"cost": 0}

    monkeypatch.setattr(llm, "chat", chat)
    cfg = types.SimpleNamespace(summary_model="тест", summary_max_tokens=0)
    return asyncio.run(summarize.summarize_file(base, STEM, "meeting", "шаблон", cfg))


def test_break_while_writing_keeps_the_previous_protocol_whole(tmp_path, monkeypatch):
    """Диск кончился посреди записи нового протокола: на месте остаётся прежний, целиком, а не половина нового."""
    base = record_dir(tmp_path)
    build(base, monkeypatch, "# Прежний протокол\n\nВсё решили [00:00:05].")
    target = base / "summary" / f"{STEM}.md"
    before = target.read_text(encoding="utf-8")
    real = pathlib.Path.write_text

    def half(self, data, *args, **kwargs):
        if self.parent == target.parent and self.name.startswith(STEM + ".md"):
            real(self, data[:len(data) // 2], *args, **kwargs)
            raise OSError(28, "No space left on device")
        return real(self, data, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", half)
    with pytest.raises(OSError):
        build(base, monkeypatch, "# Новый протокол\n\n" + "Длинный текст. " * 200)
    assert target.read_text(encoding="utf-8") == before
    assert not (target.parent / summarize.PREV_DIR).exists()           # прежний никуда не убран


def test_new_protocol_replaces_the_old_one_and_the_old_one_is_kept(tmp_path, monkeypatch):
    base = record_dir(tmp_path)
    build(base, monkeypatch, "# Первый\n\nВсё решили [00:00:05].")
    meta = build(base, monkeypatch, "# Второй\n\nВсё решили [00:00:05].")
    summary = base / "summary"
    assert "# Второй" in (summary / f"{STEM}.md").read_text(encoding="utf-8")
    assert ["# Первый" in p.read_text(encoding="utf-8") for p in (summary / summarize.PREV_DIR).iterdir()] == [True]
    assert json.loads((summary / f"{STEM}.meta.json").read_text(encoding="utf-8")) == meta
    assert not list(summary.rglob("*.tmp"))
