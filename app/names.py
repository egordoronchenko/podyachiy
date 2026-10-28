"""Имена говорящих, подписанные человеком.

Разбор записи находит имена сам, но обычно это только имя — «Пётр»,
«Анна Сергеевна», — а бывает, что и двое разных людей у него «Иван».
ФИО знает тот, кто был на совещании, поэтому подписи ставятся руками на
странице записи. Подпись вручную окончательная: её не перебивает ни разбор,
ни его пересчёт.

Подписывают в два слоя:
  по частям — метка → ФИО отдельно для каждой части записи. Длинная запись
    распознаётся кусками по 90 минут, и после стыка модель раздаёт метки
    заново: S3 до стыка и S3 после — не обязательно один человек, поэтому
    общий на всю запись список здесь врал бы;
  по строкам — ФИО одной реплики поверх списка части: распознавание
    иногда отдаёт реплику не тому голосу.

Подписи лежат отдельным файлом рядом с расшифровкой: разбор пересчитывается
и затёр бы ручную работу. Сама расшифровка на диске не правится никогда —
это первоисточник; имена подставляются в показ и в текст для протокола.
"""
from __future__ import annotations

import bisect
import json
from pathlib import Path

from . import speech, triage

FILE_SUFFIX = ".names.json"
MAX_NAME = 120
SAMPLE_WORDS = 8      # реплика, по которой голос проще узнать на слух, — не «да»


def _path(file_dir: Path, stem: str) -> Path:
    return file_dir / "transcript" / f"{stem}{FILE_SUFFIX}"


def line_key(start: float) -> str:
    """Реплика узнаётся по времени начала: номер строки сдвинулся бы, а время
    у расшифровки постоянное, пока её не распознали заново."""
    return f"{float(start):.2f}"


def load(file_dir: Path, stem: str) -> dict:
    """{"parts": {"1": {"S1": ФИО}}, "lines": {"3291.43": ФИО}}."""
    try:
        data = json.loads(_path(file_dir, stem).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    parts = {str(k): {lab: name for lab, name in v.items() if isinstance(name, str) and name}
             for k, v in (data.get("parts") or {}).items() if isinstance(v, dict)}
    lines = {k: v for k, v in (data.get("lines") or {}).items() if isinstance(v, str) and v}
    return {"parts": {k: v for k, v in parts.items() if v}, "lines": lines}


def save(file_dir: Path, stem: str, signed: dict) -> None:
    path = _path(file_dir, stem)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(signed, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def forget(file_dir: Path, stem: str) -> bool:
    """Подписи к прошлой расшифровке: после нового распознавания метки и
    времена реплик другие, и старые подписи легли бы не на тех людей."""
    path = _path(file_dir, stem)
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return existed


def clean(name: str) -> str:
    return " ".join(name.split())[:MAX_NAME]


def part_of(start: float, cuts: list[float]) -> int:
    """Номер части записи (с единицы), в которую попадает момент."""
    return bisect.bisect_right(cuts, start) + 1


def _transcript(file_dir: Path, stem: str) -> dict:
    try:
        return json.loads((file_dir / "transcript" / f"{stem}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def signed_names(segments: list[dict], cuts: list[float], signed: dict) -> list[str]:
    """Ручное ФИО каждой реплики, пустая строка — не подписана вручную."""
    out = []
    for seg in segments:
        start = float(seg.get("start") or 0)
        by_line = signed["lines"].get(line_key(start))
        by_part = signed["parts"].get(str(part_of(start, cuts)), {}).get(seg.get("speaker"))
        out.append(by_line or by_part or "")
    return out


def for_protocol(file_dir: Path, stem: str) -> tuple[list[str], dict[str, list[str]], set[str]]:
    """Что нужно протоколу: ручное ФИО по каждой реплике, эти ФИО с метками голосов, за которыми
    они стоят, и метки, которые где-то остались без ручной подписи.

    В расшифровке для протокола у подписанной реплики стоит ФИО вместо метки, а таблица
    «Участники» сводит метки с именами: метки подписанных голосов модель узнаёт из справки.
    У записи из нескольких частей метка — с номером части («S1 в части 2»): после стыка метки
    раздаются заново. ФИО, подписанное только отдельным репликам, — без меток."""
    data = _transcript(file_dir, stem)
    segments = data.get("segments") or []
    cuts = data.get("cuts") or []
    signed = load(file_dir, stem)
    who = signed_names(segments, cuts, signed)
    left = {seg.get("speaker") for seg, name in zip(segments, who) if not name}
    voices: dict[str, list[str]] = {}
    for seg, name in zip(segments, who):
        if not name:
            continue
        mine = voices.setdefault(name, [])
        part, label = part_of(float(seg.get("start") or 0), cuts), seg.get("speaker")
        voice = f"{label} в части {part}" if cuts else label
        if signed["parts"].get(str(part), {}).get(label) == name and voice not in mine:
            mine.append(voice)
    return who, dict(sorted(voices.items())), left


def view(file_dir: Path, stem: str) -> dict:
    """Всё для страницы: части с их голосами и реплики с тем, кто их говорит.

    who — что показать у реплики: ручное ФИО как есть; имя от разбора с меткой
    («Иван (S2)» — разбор бывает называет одним именем двух разных людей);
    иначе просто метка.
    """
    data = _transcript(file_dir, stem)
    segments = data.get("segments") or []
    cuts = data.get("cuts") or []
    signed = load(file_dir, stem)
    tri = triage.load_saved(file_dir, stem) or {}
    auto = {s["label"]: s for s in tri.get("speakers") or []}
    manual = signed_names(segments, cuts, signed)

    bounds = [0.0, *cuts]
    parts = [{"index": i + 1, "start": b, "end": (cuts[i] if i < len(cuts) else None),
              "words": {}, "first": {}, "sample": {}} for i, b in enumerate(bounds)]
    lines = []
    for seg, name in zip(segments, manual):
        start = float(seg.get("start") or 0)
        label = seg.get("speaker") or "S?"
        part = parts[part_of(start, cuts) - 1]
        n = len((seg.get("text") or "").split())
        part["words"][label] = part["words"].get(label, 0) + n
        if n:
            part["first"].setdefault(label, start)
        if n >= SAMPLE_WORDS:
            part["sample"].setdefault(label, start)
        guess = (auto.get(label) or {}).get("name")
        key = line_key(start)
        lines.append({
            "key": key, "start": start, "end": float(seg.get("end") or start),
            "part": part["index"], "label": label, "text": seg.get("text") or "",
            "who": name or (f"{guess} ({label})" if guess else label),
            "manual": bool(name), "line": key in signed["lines"],
        })

    out_parts = []
    for part in parts:
        total = sum(part["words"].values()) or 1
        signed_part = signed["parts"].get(str(part["index"]), {})
        out_parts.append({
            "index": part["index"], "start": part["start"], "end": part["end"],
            "speakers": [{
                "label": label,
                "share": round(part["words"][label] / total, 3),
                "sample_sec": part["sample"].get(label, part["first"].get(label)),
                "auto": (auto.get(label) or {}).get("name") or "",
                "role": (auto.get(label) or {}).get("role") or "",
                "manual": signed_part.get(label, ""),
            } for label in sorted(part["words"], key=speech._label_order)],
        })
    return {"parts": out_parts, "lines": lines,
            "known": sorted({n for n in manual if n}
                            | {s["name"] for s in auto.values() if s.get("name")})}


def display_text(lines: list[dict]) -> str:
    """Расшифровка с подписями — для копирования: подряд идущие реплики
    одного человека склеиваются, как в файле расшифровки."""
    blocks: list[list] = []
    for ln in lines:
        text = ln["text"].strip()
        if not text:
            continue
        if blocks and blocks[-1][0] == ln["who"]:
            blocks[-1][1].append(text)
        else:
            blocks.append([ln["who"], [text]])
    return "\n\n".join(f"[{who}] " + " ".join(parts) for who, parts in blocks) + "\n"


def name_srt(srt: str, lines: list[dict]) -> str:
    """Субтитры с подписями: метка каждого субтитра меняется на того, кто
    говорит реплику, в которую этот субтитр попадает по времени."""
    starts = [ln["start"] for ln in lines]
    out = []
    pending = None
    for row in srt.split("\n"):
        if " --> " in row:
            h, m, rest = row.split(" --> ")[0].split(":")
            pending = int(h) * 3600 + int(m) * 60 + float(rest.replace(",", "."))
        elif pending is not None and row.startswith("[") and "] " in row and lines:
            i = max(bisect.bisect_right(starts, pending + 0.01) - 1, 0)
            row = f"[{lines[i]['who']}] " + row.split("] ", 1)[1]
            pending = None
        out.append(row)
    return "\n".join(out)


def changed(file_dir: Path, stem: str) -> float:
    """Когда подписи правили последний раз — секундами эпохи; 0 — их не правили."""
    try:
        return _path(file_dir, stem).stat().st_mtime
    except OSError:
        return 0.0


def names_newer(file_dir: Path, stem: str) -> bool:
    """Подписи правили после того, как собран протокол, — он с ними расходится."""
    try:
        return (_path(file_dir, stem).stat().st_mtime
                > (file_dir / "summary" / f"{stem}.md").stat().st_mtime)
    except OSError:
        return False


SAMPLES = 5           # реплик на говорящего — чтобы по ним понять, кто это
SAMPLE_CHARS = 240


def _clock(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _samples(lines: list[dict]) -> list[dict]:
    """До SAMPLES содержательных реплик человека, равномерно по его речи: первая (в начале
    чаще представляются и здороваются по имени), последняя и между ними. Короткие «да»
    и «угу» берутся, только когда длинных не набралось."""
    full = [ln for ln in lines if len(ln["text"].split()) >= SAMPLE_WORDS]
    pool = full if len(full) >= 3 else sorted(lines, key=lambda ln: -len(ln["text"]))[:SAMPLES]
    pool = sorted(pool, key=lambda ln: ln["start"])
    if len(pool) > SAMPLES:
        step = (len(pool) - 1) / (SAMPLES - 1)
        pool = [pool[round(i * step)] for i in range(SAMPLES)]
    return [{"at": _clock(ln["start"]), "at_sec": int(ln["start"]), "line": ln["key"],
             "text": ln["text"].strip()[:SAMPLE_CHARS]} for ln in pool]


def speakers(file_dir: Path, stem: str) -> dict:
    """Говорящие для ИИ-клиента: то же, что view, но вместо всей расшифровки — по несколько
    реплик на голос. name — ручная подпись (пусто — не подписан), guess и role — догадка
    разбора записи, share — доля слов в этой части."""
    seen = view(file_dir, stem)
    said: dict[tuple[int, str], list[dict]] = {}
    for ln in seen["lines"]:
        said.setdefault((ln["part"], ln["label"]), []).append(ln)
    return {"known": seen["known"], "parts": [{
        "part": part["index"], "start_sec": int(part["start"]),
        "end_sec": None if part["end"] is None else int(part["end"]),
        "speakers": [{"label": s["label"], "name": s["manual"], "guess": s["auto"], "role": s["role"],
                      "share": s["share"], "lines": len(said[part["index"], s["label"]]),
                      "samples": _samples(said[part["index"], s["label"]])}
                     for s in part["speakers"]],
    } for part in seen["parts"]]}
