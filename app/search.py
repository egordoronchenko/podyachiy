"""Поиск по всем расшифровкам и протоколам.

Протокол, который некому найти, никто и не перечитывает: через месяц человек
помнит, что «где-то обсуждали переезд склада», но не помнит, на каком совещании и
когда. Фильтр по имени записи тут не помогает — имя файла про содержание не
говорит ничего.

Поиск идёт по тем же файлам, что лежат в выходной папке, без отдельного
индекса: расшифровка совещания — это десятки килобайт, сотня записей
читается за доли секунды, а прочитанное держится в памяти до тех пор, пока
файл на диске не изменился. Заводить ради этого базу данных незачем.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Где лежит текст каждого вида. merged — папка прежней связки Whisper с Gemini:
# архив совещаний искать тоже нужно.
KINDS = {"summary": ("summary", ".md", ()),
         "transcript": ("transcript", ".txt", ("merged",)),
         "triage": ("transcript", ".triage.json", ())}

ORDER = ("summary", "triage", "transcript")

MIN_QUERY = 2
MAX_QUERY = 200
CONTEXT = 110          # символов вокруг найденного слова в подсказке
SNIPPETS_PER_FILE = 3
MAX_RESULTS = 60
MAX_SCAN = 2000        # сколько последних записей журнала просматриваем
CACHE_CHARS = 40_000_000   # ~40 МБ текста, дальше кэш сбрасывается целиком

_cache: dict[str, tuple[float, int, str]] = {}
_cached_chars = 0


def plain(md: str) -> str:
    """Протокол без разметки Markdown — таким он идёт в поиск и в выдержки: человек ищет слова,
    а «**», «#» и «|» в куске найденного текста — мусор. Убираются служебные пометки сервиса,
    знаки заголовков, списков и цитат, жирный, курсив, зачёркнутый и код, ссылки (остаётся их
    текст) и рамки таблиц: ячейки строки идут через « · », строка-разделитель пропадает.

    Чистится текст до поиска, а не выдержка после него: смещения найденного слова считаются
    по тому же тексту, из которого вырезается кусок, и подсветка не съезжает."""
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    out = []
    for line in md.splitlines():
        line = line.strip()
        if re.fullmatch(r"\|?[\s:|-]*-[\s:|-]*\|?", line) and "|" in line:
            continue                                            # |---|:---:| под шапкой таблицы
        if line.startswith("|") or line.endswith("|"):
            line = " · ".join(cell.strip() for cell in line.strip("|").split("|") if cell.strip())
        line = re.sub(r"^(?:#{1,6}\s+|>\s*|[-*+]\s+(?:\[[ xXхХ]\]\s+)?)+", "", line)
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"(\*\*|__|~~|`)(.+?)\1", r"\2", line)
        line = re.sub(r"(?<![\w*])([*_])(?=\S)(.+?)(?<=\S)\1(?![\w*])", r"\2", line)
        out.append(line)
    return "\n".join(out)


def _read(path: Path, shape=None) -> str:
    """Текст файла с запоминанием: выходная папка может быть сетевой. shape — во что превратить
    прочитанное перед запоминанием (у протокола — plain): на файл оно делается один раз."""
    global _cached_chars
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        _cache.pop(key, None)
        return ""
    hit = _cache.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    if shape:
        text = shape(text)
    if _cached_chars > CACHE_CHARS:
        _cache.clear()
        _cached_chars = 0
    _cache[key] = (st.st_mtime, st.st_size, text)
    _cached_chars += len(text)
    return text


def fold(text: str) -> str:
    """Регистр и «ё» при поиске значения не имеют."""
    return text.lower().replace("ё", "е")


def matcher(word: str) -> re.Pattern:
    """Слово ищется с начала слова, а не где попало внутри него.

    Иначе «ёлка» находится в «переделке», а «сад» — в «досаде». Конец слова
    свободный нарочно: русское слово склоняется, и «остатки» должны находить
    и «остаткам», и «остатков».
    """
    return re.compile(r"(?<!\w)" + re.escape(word))


def snippets(text: str, folded: str, rx: re.Pattern, kind: str, limit: int) -> list[dict]:
    """Куски текста вокруг найденного слова — по ним видно, то ли это совещание.

    Возвращаются тремя частями, а не одной строкой с разметкой: подсветку
    страница делает сама, и вставлять в текст теги здесь незачем. Текст сюда
    приходит уже без разметки протокола (plain).
    """
    out: list[dict] = []
    for m in rx.finditer(folded):
        if len(out) >= limit:
            break
        i, j = m.start(), m.end()
        # подсвечиваем слово целиком: искали «остатки», нашли «остаткам»
        while j < len(folded) and folded[j].isalnum():
            j += 1
        out.append({
            "kind": kind,
            "before": ("…" if i > CONTEXT else "") + " ".join(text[max(0, i - CONTEXT):i].split()),
            "match": text[i:j],
            "after": " ".join(text[j:j + CONTEXT].split()) + ("…" if j + CONTEXT < len(text) else ""),
        })
    return out


def triage_text(path: Path) -> str:
    """Разбор записи как обычный текст: по нему ищутся имена и темы.

    Искать по самому JSON нельзя: в него попадут имена полей, и запрос «роль»
    находил бы каждую запись подряд. Имена говорящих есть только здесь —
    в расшифровке стоят безымянные метки, — поэтому фамилию иначе не найти.
    """
    try:
        data = json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return ""
    parts = [data.get("title") or "", data.get("gist") or "",
             " ".join(data.get("topics") or [])]
    for who in data.get("speakers") or []:
        if isinstance(who, dict):
            parts.append(" ".join(x for x in (who.get("name"), who.get("role")) if x))
    # подписанные вручную ФИО лежат рядом отдельным файлом (app/names.py)
    signed = path.with_name(path.name.replace(".triage.json", ".names.json"))
    try:
        data = json.loads(_read(signed) or "{}")
    except json.JSONDecodeError:
        data = {}
    for by_label in (data.get("parts") or {}).values():
        parts.extend(v for v in by_label.values() if isinstance(v, str))
    parts.extend(v for v in (data.get("lines") or {}).values() if isinstance(v, str))
    return "\n".join(p for p in parts if p)


def _paths(outbox: Path, rel: str, stem: str) -> dict[str, Path]:
    base = outbox / Path(rel).parent / stem
    found = {}
    for kind, (sub, ext, legacy) in KINDS.items():
        for folder in (sub, *legacy):
            path = base / folder / f"{stem}{ext}"
            if path.is_file():
                found[kind] = path
                break
    return found


def find(records: list[dict], outbox: Path, query: str) -> dict:
    """Записи, где встречаются все слова запроса.

    Все слова, а не точная фраза: человек помнит слова, а не формулировку,
    и «склад остатки» должно находить совещание, где это обсуждали, даже если
    в тексте эти слова стоят через абзац друг от друга.
    """
    words = [w for w in fold(query.strip())[:MAX_QUERY].split() if len(w) >= MIN_QUERY]
    if not words:
        return {"query": query, "words": [], "files": [], "scanned": 0}
    rules = [matcher(w) for w in words]
    longest = matcher(max(words, key=len))

    files = []
    scanned = 0
    for rec in records:
        rel = rec.get("file")
        stem = rec.get("stem")
        if not rel or not stem:
            continue
        found = _paths(outbox, rel, stem)
        if not found:
            continue
        scanned += 1
        hits: dict[str, int] = {}
        pieces: list[dict] = []
        for kind in ORDER:
            path = found.get(kind)
            if path is None:
                continue
            text = triage_text(path) if kind == "triage" else _read(path, plain if kind == "summary" else None)
            folded = fold(text)
            if not all(rx.search(folded) for rx in rules):
                continue
            hits[kind] = len(longest.findall(folded))
            pieces.extend(snippets(text, folded, longest, kind, SNIPPETS_PER_FILE))
        if hits:
            files.append({
                "file": rel,
                "name": Path(rel).name,
                "dir": rel.rsplit("/", 1)[0] if "/" in rel else "",
                "finished_at": rec.get("finished_at"),
                "hits": sum(hits.values()),
                "kinds": hits,
                # протокол человеку нужнее расшифровки, а разбор отдельной
                # вкладкой не показывается — по находке в нём открываем протокол
                "open_kind": "transcript" if set(hits) == {"transcript"} else "summary",
                "snippets": pieces[:SNIPPETS_PER_FILE],
            })

    files.sort(key=lambda f: (f["hits"], f.get("finished_at") or ""), reverse=True)
    return {"query": query, "words": words, "scanned": scanned,
            "files": files[:MAX_RESULTS], "more": max(0, len(files) - MAX_RESULTS)}
