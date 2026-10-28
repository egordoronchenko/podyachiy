"""Протокол по готовой расшифровке.

Перенесено из эталонного проекта transcribe-web. Отличие: текст шаблона
приходит извне — из prompts/*.md, которые читаются заново на каждое задание,
а не зашиты в код.

Протокол — отдельный производный документ: сама расшифровка никогда не
правится и лежит рядом, поэтому вольности модели всегда можно сверить
с первоисточником. Метки времени в протоколе делают эту сверку быстрой:
модель обязана скопировать метку из расшифровки, а сервис потом проверяет,
что каждая метка в протоколе там действительно есть, и помечает в протоколе
те, которых там нет.
"""
from __future__ import annotations

import datetime
import glob
import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import llm, mindmap, speech, templates

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


def flag_marks(md: str, unknown: list[str]) -> str:
    """Метки не из расшифровки — с вопросительным знаком в самом протоколе: [00:12:34] → [00:12:34?].

    Журнал работы читатель протокола не видит, а выдуманная метка от настоящей на глаз не
    отличается. Помеченная метка не ведёт к моменту записи — ни на странице, ни в карте протокола,
    ни в скачанном файле: все они узнают метку только в виде [чч:мм:сс]."""
    bad = set(unknown)
    return MARK_RE.sub(lambda m: f"[{m.group(1)}:{m.group(2)}:{m.group(3)}?]"
                       if f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3)}" in bad else m.group(0), md)


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
    md = flag_marks(md, unknown)

    out_dir = file_dir / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    header = (f"<!-- протокол: модель {cfg.summary_model}, шаблон {template_key}, "
              f"источник transcript/{stem}.json -->\n\n")
    target = out_dir / f"{stem}.md"
    # Новый протокол сначала пишется целиком рядом и только потом встаёт на место: обрыв посреди
    # записи (диск кончился, контейнер остановили) не оставит на месте протокола его половину.
    fresh = target.with_name(target.name + ".tmp")
    fresh.write_text(header + md.strip() + "\n" + (f"\n{tail}" if tail else ""), encoding="utf-8")
    if target.exists():
        # Прошлый протокол не затираем: модель от прогона к прогону пишет
        # по-разному, и без старой версии не понять, что при пересборке потерялось.
        # Правленный человеком — тоже: он уходит в версии с именем того, кто правил.
        index = _index(out_dir, stem)
        hand = index.get("current")
        saved = keep(out_dir, stem, index)
        if hand:
            _save_index(out_dir, stem, index)
        log(f"прошлый протокол сохранён: summary/{PREV_DIR}/{stem}.{saved}.md"
            + (f" — его правили вручную ({hand.get('by') or 'человек'}, {hand.get('at', '')[:16].replace('T', ' ')}): "
               f"правленый текст остаётся в версиях протокола" if hand else ""))
    os.replace(fresh, target)
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
    templates._write(out_dir / f"{stem}.meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    log(f"протокол готов: {len(md)} символов за {meta['wall_clock_sec']:.0f} с, "
        f"меток времени {used}")
    if unknown:
        log(f"внимание: меток времени, которых нет в расшифровке: {len(unknown)} "
            f"({', '.join(unknown[:5])}{' …' if len(unknown) > 5 else ''}) — "
            f"эти моменты модель назвала сама, в протоколе они помечены знаком «?» — сверяйте по записи")
    return meta


# ---------- правка протокола человеком и версии ----------
# Протокол можно поправить руками на странице или через API. Прежний текст при каждой замене — правке,
# возврате версии, пересборке моделью — уходит в summary/prev/<stem>.<метка>.md; метка — когда тот текст
# появился (время файла, ГГГГММДД-ЧЧММСС; несколько в одну секунду — с «-2», «-3»). Кто и как сделал
# каждую версию, лежит рядом, в описи summary/prev/<stem>.versions.json: {"current": кто сделал текущий
# текст, "versions": {метка: кто сделал тот}}. Версии без строки в описи собрала модель — так лежат и
# версии, сохранённые до появления правки. Текущий текст в описи есть, только если его сделал человек:
# это и есть признак «протокол правили вручную»; пересборка моделью его снимает (summarize_file).
# Опись и версии лежат в папке результатов записи и переезжают с ней при переименовании, переносе и
# удалении в корзину (records.py переименовывает все файлы «<stem>.…» внутри неё).
#
# Чья версия перед глазами у правящего, говорит отпечаток (digest) файла протокола целиком: правка и
# возврат принимаются, только если протокол на диске всё тот же, — иначе два человека затёрли бы правки
# друг друга, а правка поверх свежей пересборки — работу модели.

VERSIONS_SUFFIX = ".versions.json"
EDIT_MAX_CHARS = 200_000            # предел текста правки: протокол совещания — десятки тысяч знаков
STAMP_RE = re.compile(r"(\d{8}-\d{6})(?:-(\d+))?")
HEADER_RE = re.compile(r"\A\s*(<!--.*?-->)\s*", re.S)


class Changed(Exception):
    """Протокол на диске уже не тот, с которого человек начал правку."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _index_path(out_dir: Path, stem: str) -> Path:
    return out_dir / PREV_DIR / f"{stem}{VERSIONS_SUFFIX}"


def _index(out_dir: Path, stem: str) -> dict:
    path = _index_path(out_dir, stem)
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return {"current": data.get("current"), "versions": data.get("versions") or {}}


def _save_index(out_dir: Path, stem: str, index: dict) -> None:
    templates._write(_index_path(out_dir, stem), json.dumps(index, ensure_ascii=False, indent=2) + "\n")


def _version_path(out_dir: Path, stem: str, stamp: str) -> Path:
    return out_dir / PREV_DIR / f"{stem}.{stamp}.md"


def keep(out_dir: Path, stem: str, index: dict) -> str:
    """Текущий протокол — в версии: файл переезжает в prev/, а кто его сделал (index["current"]) —
    в опись под той же меткой. Опись меняется только в памяти: записывает её тот, кто зовёт.
    Возвращает метку версии."""
    target = out_dir / f"{stem}.md"
    first = time.strftime("%Y%m%d-%H%M%S", time.localtime(target.stat().st_mtime))
    stamp, n = first, 1
    while _version_path(out_dir, stem, stamp).exists():
        n += 1
        stamp = f"{first}-{n}"
    (out_dir / PREV_DIR).mkdir(exist_ok=True)
    target.replace(_version_path(out_dir, stem, stamp))
    made = index.pop("current", None)
    index["current"] = None
    if made:
        index["versions"][stamp] = made
    return stamp


def versions(out_dir: Path, stem: str) -> list[dict]:
    """Прежние версии протокола, свежие первыми: stamp — метка, at — когда текст появился, by — кто его
    сделал (None — модель), how — model, edit или restore (from — метка возвращённой версии), chars."""
    index, found = _index(out_dir, stem), []
    folder = out_dir / PREV_DIR
    for p in (folder.glob(f"{glob.escape(stem)}.*.md") if folder.is_dir() else ()):
        stamp = p.name[len(stem) + 1:-3]
        m = STAMP_RE.fullmatch(stamp)
        if not m:
            continue
        made = index["versions"].get(stamp) or {}
        at = made.get("at") or datetime.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").astimezone().isoformat(
            timespec="seconds")
        found.append(((m.group(1), int(m.group(2) or 1)), {
            "stamp": stamp, "at": at, "by": made.get("by"), "how": made.get("how") or "model",
            **({"from": made["from"]} if made.get("from") else {}),
            "chars": len(p.read_text(encoding="utf-8"))}))
    return [v for _, v in sorted(found, key=lambda pair: pair[0], reverse=True)]


def version_text(out_dir: Path, stem: str, stamp: str) -> str | None:
    """Текст прежней версии целиком, со служебной пометкой; None — такой версии нет."""
    if not STAMP_RE.fullmatch(stamp):
        return None
    path = _version_path(out_dir, stem, stamp)
    return path.read_text(encoding="utf-8") if path.is_file() else None


def edited(out_dir: Path, stem: str) -> dict | None:
    """Кто сделал текущий текст протокола, если это человек: by, at, how (edit или restore), from."""
    return _index(out_dir, stem)["current"]


def body_of(text: str) -> str:
    """Текст протокола без служебной пометки в начале — то, что правит человек."""
    return HEADER_RE.sub("", text, count=1)


def with_header(old: str, body: str) -> str:
    """Правленый текст под служебной пометкой прежнего протокола: пометка — сервиса, правка её не
    меняет (такую же в начале самой правки сервис отбрасывает)."""
    m = HEADER_RE.match(old)
    body = body_of(body.replace("\r\n", "\n").replace("\r", "\n")).strip()
    return (f"{m.group(1)}\n\n" if m else "") + body + "\n"


def rewrite(out_dir: Path, stem: str, text: str, made: dict, base: str) -> bool:
    """Новый текст протокола целиком: прежний — в версии, кто сделал новый (made: by, at, how, from) —
    в опись. base — отпечаток протокола, с которого человек начал; не совпал — Changed. Возвращает,
    изменилось ли что-то: тот же текст новой версии не даёт."""
    target = out_dir / f"{stem}.md"
    if digest(target.read_bytes()) != base:
        raise Changed()
    if target.read_text(encoding="utf-8") == text:
        return False
    # как у пересборки: новый текст сначала целиком рядом, потом встаёт на место
    fresh = target.with_name(target.name + ".tmp")
    fresh.write_text(text, encoding="utf-8", newline="\n")
    index = _index(out_dir, stem)
    stamp = keep(out_dir, stem, index)
    kept = _version_path(out_dir, stem, stamp)
    try:
        os.replace(fresh, target)
        index["current"] = made
        _save_index(out_dir, stem, index)
    except BaseException:
        # не встало целиком — прежний протокол возвращается на место, опись на диске прежняя
        kept.replace(target)
        fresh.unlink(missing_ok=True)
        raise
    return True
