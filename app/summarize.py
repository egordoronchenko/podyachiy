"""Протокол по готовой расшифровке.

Перенесено из эталонного проекта transcribe-web. Отличие: текст шаблона
приходит извне — из prompts/*.md, которые читаются заново на каждое задание,
а не зашиты в код.

Протокол — отдельный производный документ: сама расшифровка никогда не
правится и лежит рядом, поэтому вольности модели всегда можно сверить
с первоисточником. Метки времени в протоколе делают эту сверку быстрой:
модель обязана скопировать метку из расшифровки, а сервис потом проверяет,
что каждая метка в протоколе там действительно есть.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import llm, mindmap, speech

SYSTEM = "Ты аккуратный аналитик. Работаешь только с тем, что есть в тексте."

# [чч:мм:сс] в протоколе. Час без ведущего нуля модель тоже пишет, поэтому
# при сверке метка приводится к двузначному часу.
MARK_RE = re.compile(r"\[(\d{1,2}):([0-5]\d):([0-5]\d)\]")

# подпапка summary/ с прошлыми версиями протокола
PREV_DIR = "prev"


def load_source(file_dir: Path, stem: str,
                who: list[str] | None = None) -> tuple[str, set[str]]:
    """Расшифровка для протокола: текст с метками времени и список этих меток.

    Берётся не .txt, а .json рядом с ним: время каждой реплики есть только там.
    """
    path = file_dir / "transcript" / f"{stem}.json"
    if not path.is_file():
        raise RuntimeError("нет расшифровки для протокола")
    segments = (json.loads(path.read_text(encoding="utf-8")) or {}).get("segments") or []
    text, marks = speech.to_text_timed(segments, who)
    if not text.strip():
        raise RuntimeError("расшифровка пустая")
    return text, marks


def check_marks(md: str, marks: set[str]) -> tuple[int, list[str]]:
    """Сколько меток времени в протоколе и какие из них не из расшифровки.

    Выдуманное время выглядит так же правдоподобно, как настоящее, и проверить
    его на глаз нельзя — а сверить со списком реальных меток можно точно.
    """
    used = {f"{int(h):02d}:{m}:{s}" for h, m, s in MARK_RE.findall(md)}
    return len(used), sorted(u for u in used if u not in marks)


async def summarize(text: str, prompt: str, cfg, log=lambda s: None,
                    notes: str = "", glossary: str = "") -> tuple[str, dict]:
    """Просит модель составить протокол по шаблону.

    notes — справка из разбора записи: кто скрывается за метками S1, S2 и о чём
    вообще запись. Идёт отдельным блоком после шаблона и перед расшифровкой,
    чтобы модель видела её как данные, а не как часть инструкции.
    glossary — готовый блок словаря терминов (templates.load_glossary).
    """
    user = prompt + glossary
    if notes:
        user += f"\n\n---\nСПРАВКА ПО ЗАПИСИ (получена отдельным разбором той же расшифровки):\n\n{notes}"
    user += f"\n\n---\nРАСШИФРОВКА:\n\n{text}"
    return await llm.chat(cfg, "summary", SYSTEM, user,
                          budget=cfg.summary_max_tokens,
                          log=lambda s: log(f"протокол: {s}"))


async def summarize_file(
    file_dir: Path,
    stem: str,
    template_key: str,
    prompt: str,
    cfg,
    log=lambda s: None,
    notes: str = "",
    who: list[str] | None = None,
    glossary: str = "",
    tail: str = "",
    noted: dict | None = None,
) -> dict:
    """tail — готовый раздел, который сервис дописывает после текста модели (поручения
    прошлого совещания). В сверку меток времени и в счёт знаков протокола он не входит.
    noted — отметки и сроки поручений записи: при пересборке они идут в карту протокола."""
    text, marks = load_source(file_dir, stem, who)
    t0 = time.perf_counter()
    log(f"протокол по расшифровке: {len(text)} символов, меток времени {len(marks)}, "
        f"шаблон «{template_key}»" + (", со справкой из разбора" if notes else "")
        + (", со словарём терминов" if glossary else ""))
    md, api_usage = await summarize(text, prompt, cfg, log, notes, glossary)
    used, unknown = check_marks(md, marks)

    out_dir = file_dir / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    header = (f"<!-- протокол: модель {cfg.summary_model}, шаблон {template_key}, "
              f"источник transcript/{stem}.json -->\n\n")
    target = out_dir / f"{stem}.md"
    if target.exists():
        # Прошлый протокол не затираем: модель от прогона к прогону пишет
        # по-разному, и без старой версии не понять, что при пересборке потерялось.
        saved = time.strftime("%Y%m%d-%H%M%S", time.localtime(target.stat().st_mtime))
        (out_dir / PREV_DIR).mkdir(exist_ok=True)
        target.replace(out_dir / PREV_DIR / f"{stem}.{saved}.md")
        log(f"прошлый протокол сохранён: summary/{PREV_DIR}/{stem}.{saved}.md")
    target.write_text(header + md.strip() + "\n" + (f"\n{tail}" if tail else ""), encoding="utf-8")
    mindmap.write_for(target, noted)
    meta = {
        "success": True,
        "model": cfg.summary_model,
        "template": template_key,
        "source": f"transcript/{stem}.json",
        "source_chars": len(text),
        "summary_chars": len(md),
        "timestamps": used,
        "timestamps_unknown": len(unknown),
        "wall_clock_sec": round(time.perf_counter() - t0, 2),
        "usage": api_usage,
    }
    (out_dir / f"{stem}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"протокол готов: {len(md)} символов за {meta['wall_clock_sec']:.0f} с, "
        f"меток времени {used}")
    if unknown:
        log(f"внимание: меток времени, которых нет в расшифровке: {len(unknown)} "
            f"({', '.join(unknown[:5])}{' …' if len(unknown) > 5 else ''}) — "
            f"эти моменты модель назвала сама, сверяйте по записи")
    return meta
