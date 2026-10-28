"""Протокол на диске: новый встаёт на место целиком, а обрыв посреди записи не оставляет полупустой файл."""
import asyncio
import json
import os
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


def test_rebuild_keeps_the_hand_edited_text_in_versions_with_its_author(tmp_path, monkeypatch):
    """Протокол поправил человек, потом его пересобрала модель: правленый текст не пропал — он в версиях,
    с тем, кто правил; признак «правили вручную» с пересборкой снят."""
    base = record_dir(tmp_path)
    build(base, monkeypatch, "# Первый\n\nВсё решили [00:00:05].")
    summary = base / "summary"
    target = summary / f"{STEM}.md"
    text = summarize.with_header(target.read_text(encoding="utf-8"), "# Первый\n\nВсё решили, смету — Иванову.")
    made = {"by": "ivanov", "at": "2026-10-28T12:00:00+03:00", "how": "edit"}
    assert summarize.rewrite(summary, STEM, text, made, summarize.digest(target.read_bytes()))
    assert summarize.edited(summary, STEM) == made
    with pytest.raises(summarize.Changed):          # отпечаток прежнего текста больше не годится
        summarize.rewrite(summary, STEM, text + "ещё", made, "0" * 16)

    build(base, monkeypatch, "# Второй\n\nВсё решили [00:00:05].")
    assert "# Второй" in target.read_text(encoding="utf-8") and summarize.edited(summary, STEM) is None
    hand, model = summarize.versions(summary, STEM)
    assert (hand["by"], hand["how"], hand["at"]) == ("ivanov", "edit", made["at"])
    assert summarize.version_text(summary, STEM, hand["stamp"]) == text
    assert (model["by"], model["how"]) == (None, "model")
    assert "# Первый\n\nВсё решили [00:00:05]." in summarize.version_text(summary, STEM, model["stamp"])
    assert not list(summary.rglob("*.tmp"))


def test_versions_made_in_one_second_do_not_overwrite_each_other(tmp_path, monkeypatch):
    base = record_dir(tmp_path)
    build(base, monkeypatch, "# Первый\n\nВсё решили [00:00:05].")
    summary = base / "summary"
    target = summary / f"{STEM}.md"
    for n in range(3):
        before = target.stat().st_mtime
        made = {"by": "ivanov", "at": f"2026-10-28T12:00:0{n}+03:00", "how": "edit"}
        summarize.rewrite(summary, STEM, f"# Правка {n}\n", made, summarize.digest(target.read_bytes()))
        os.utime(target, (before, before))          # все правки — в одну и ту же секунду
    found = summarize.versions(summary, STEM)
    assert len(found) == 3 and len({v["stamp"] for v in found}) == 3
    assert [summarize.version_text(summary, STEM, v["stamp"]).split("\n")[0] for v in found] == [
        "# Правка 1", "# Правка 0", "<!-- протокол: модель тест, шаблон meeting, источник transcript/вкс.json -->"]
    # сломанная запись описи не трогает протокол: прежний текст возвращается на место
    real = summarize._save_index
    monkeypatch.setattr(summarize, "_save_index", lambda *a: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    with pytest.raises(OSError):
        summarize.rewrite(summary, STEM, "# Не встанет\n", made, summarize.digest(target.read_bytes()))
    monkeypatch.setattr(summarize, "_save_index", real)
    assert target.read_text(encoding="utf-8") == "# Правка 2\n" and len(summarize.versions(summary, STEM)) == 3
