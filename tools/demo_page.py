"""Демонстрация сервиса одним файлом: site/podyachiy-demo.html.

Файл открывается двойным щелчком с диска или из вложения письма — без сервера, без сети и без
соседних файлов. Внутри настоящие страницы сервиса (app/static: записи и поручения, common.js,
app.css, шрифт надписи, логотип), а вместо сервера — вшитые ответы на выдуманных записях
(tools/demo_data). Только чтение: любая запись получает отказ «в демонстрации изменения
не сохраняются», и страница показывает его там же, где показала бы отказ сервера.

Ответы считает тот же код, что и в сервисе: реплики и говорящие — app/names.py, поручения —
app/external.py, app/marks.py и app/registry.py, карта — app/mindmap.py. Поэтому демонстрация
не расходится с сервисом по виду данных. Пересобрать после правки страниц или записей:

    python tools/demo_page.py
"""
from __future__ import annotations

import base64
import json
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import (dictionary, external, marks, media, mindmap, names, notify, registry, search, templates,  # noqa: E402
                 triage, users)

STATIC = ROOT / "app" / "static"
DATA = ROOT / "tools" / "demo_data"
TARGET = ROOT / "site" / "podyachiy-demo.html"
PAGES = {"/": "index.html", "/tasks": "tasks.html"}        # адрес страницы в сервисе → её файл
MIME = {".png": "image/png", ".woff2": "font/woff2"}
LINE = re.compile(r"\[(\d\d):(\d\d):(\d\d)\] (S\d+): (.+)")
BAR = "Демонстрация: данные выдуманы, изменения не сохраняются"
INFO = ('<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/>'
        '<path d="M12 11v5"/><path d="M12 8h.01"/></svg>')


def parent(file: str) -> str:
    folder = PurePosixPath(file).parent.as_posix()
    return "" if folder == "." else folder


def inline_assets(text: str) -> str:
    """Адреса /static/… — содержимым файла: шрифт надписи, логотип, значок вкладки."""
    def embed(m: re.Match) -> str:
        path = STATIC / m.group(1)
        return f"data:{MIME[path.suffix]};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    out = re.sub(r"/static/([\w./-]+\.(?:png|woff2))", embed, text)
    assert "/static/" not in out, "остался адрес /static/, который в файл не вшит"
    return out


def in_script(text: str) -> str:
    """Текст внутри <script>: закрывающий тег и начало комментария не должны оборвать сценарий."""
    return text.replace("</", "<\\/").replace("<!--", "<\\!--")


def segments(rec: dict) -> list[dict]:
    rows = [LINE.fullmatch(ln) for ln in (DATA / f"{rec['id']}.txt").read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert all(rows), f"{rec['id']}.txt: строка не вида «[чч:мм:сс] S1: текст»"
    starts = [int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) for m in rows]
    return [{"start": float(s), "end": float(starts[i + 1] if i + 1 < len(starts) else s + 5),
             "speaker": m[4], "text": m[5]} for i, (s, m) in enumerate(zip(starts, rows))]


def clock_srt(sec: float) -> str:
    return f"{int(sec) // 3600:02d}:{int(sec) % 3600 // 60:02d}:{int(sec) % 60:02d},000"


def words_of(meta: dict, scope: str, path: str) -> dict:
    """Словарь папки или записи из данных демонстрации — в том виде, в каком его хранит сервис."""
    saved = meta["dictionaries"][scope].get(path) or {"people": [], "terms": [], "by": "", "at": ""}
    return {"people": dictionary.parse_people(saved["people"]), "terms": dictionary.parse_terms(saved["terms"]),
            "by": saved["by"], "at": saved["at"]}


def layers(meta: dict, folder: str, file: str = "") -> list[dict]:
    """Слои словаря от ближнего к дальнему — как dictionary.Store.layers, только из данных демонстрации."""
    found = [(dictionary.RECORD, file, words_of(meta, "records", file))] if file else []
    found += [(dictionary.FOLDER, f, words_of(meta, "folders", f)) for f in dictionary.parents(folder)]
    return [{"scope": scope, "path": path, **own} for scope, path, own in found if own["people"] or own["terms"]]


def words_view(meta: dict, folder: str, file: str = "") -> dict:
    """Ответ /api/dictionary — как его собирает app/web.py (_dict_view)."""
    home = parent(file) if file else folder
    own = words_of(meta, "records" if file else "folders", file or folder)
    owner = meta["folders"].get(home, {"owner": False})["owner"]
    view = {"scope": dictionary.RECORD if file else dictionary.FOLDER, "path": file or folder,
            "people": own["people"], "terms": own["terms"], "changed_at": own["at"], "changed_by": own["by"],
            "inherited": dictionary.merge(layers(meta, home if file else parent(folder))), "common": True,
            "can_edit": owner, "limits": {"people": dictionary.PEOPLE_MAX, "terms": dictionary.TERMS_MAX}}
    if file:
        view.update({"processed": True, "can_rebuild": owner, "names_newer": False, "dict_newer": False,
                     "rebuilding": False})
    return view


def build_record(rec: dict, work: Path, meta: dict) -> dict:
    """Всё, что сервис отдал бы по одной записи. Файлы раскладываются во временную папку так же,
    как их кладёт обработка, — и читаются теми же функциями, что на сервере."""
    file, stem = rec["file"], rec["id"]
    folder = parent(file)
    rights = meta["folders"][folder]
    md = (DATA / f"{stem}.md").read_text(encoding="utf-8")
    segs = segments(rec)
    base = work / stem
    (base / "transcript").mkdir(parents=True)
    (base / "summary").mkdir()
    (base / "transcript" / f"{stem}.json").write_text(json.dumps({"segments": segs, "cuts": []}, ensure_ascii=False), encoding="utf-8")
    (base / "transcript" / f"{stem}{triage.FILE_SUFFIX}").write_text(json.dumps({
        "type": rec["template"], "templates": sorted(templates.TEMPLATES), "title": rec["title"], "gist": rec["gist"],
        "topics": rec["topics"], "speakers": [{"label": k, **v} for k, v in rec["auto"].items()]}, ensure_ascii=False), encoding="utf-8")
    if rec["signed"]:
        names.save(base, stem, {"parts": {"1": rec["signed"]}, "lines": {}})
    (base / "summary" / f"{stem}.md").write_text(md, encoding="utf-8")
    view = names.view(base, stem)

    # отметки «выполнено» и сроки датой: в записи названо начало поручения, ключ считает сервис
    found = external.tasks_at(md)
    saved = {}
    for mark in rec["marks"]:
        task = next(t for _, _, t in found if t["task"].startswith(mark["task"]))
        saved[marks.key(task["who"], task["task"])] = {
            "who": task["who"], "task": task["task"],
            **({"status": "done", "by": mark["by"], "at": mark["at"], "comment": mark.get("comment", "")} if mark.get("done") else {}),
            **({k: mark[k] for k in marks.DUE_FIELDS} if mark.get("due") else {})}
    day = external.day_of(rec)
    noted = [{**t, **registry.state(t, day, meta["today"])} for t in marks.merged([t for _, _, t in found], saved)]

    common = {"file": file, "audio": True, "has_map": True, "can_edit": rights["owner"], "can_rebuild": True,
              "can_act": rights["owner"], "can_send": rights["owner"], "names_newer": False, "dict_newer": False,
              "rebuilding": False,
              "roster": [{"name": p["name"], "role": p["role"]}
                         for p in dictionary.merge(layers(meta, folder, file))["people"]]}
    voices = {"parts": view["parts"], "known": view["known"]}
    srt = "\n".join(f"{i}\n{clock_srt(ln['start'])} --> {clock_srt(ln['end'])}\n[{ln['who']}] {ln['text']}\n"
                    for i, ln in enumerate(view["lines"], 1))
    last = segs[-1]["end"]
    return {
        "browse": {
            "file": file, "dir": folder, "name": PurePosixPath(file).name, "status": "ok",
            "kinds": ["summary", "transcript", "srt"], "finished_at": rec["finished_at"],
            "duration_sec": rec["duration_sec"], "audio_duration_sec": last,
            "speakers": len({s["speaker"] for s in segs}), "speech_model": None, "unlabeled": 0.0,
            "speakers_unreliable": False, "crowded_meeting": False, "template": rec["template"], "template_auto": True,
            "title": rec["title"], "gist": rec["gist"], "record_date": rec["record_date"] or None,
            "named_speakers": len(view["known"]), "type_unsure": False, "type_confidence": None, "type_reason": None,
            "timestamps": len(mindmap.TS.findall(md)), "timestamps_unknown": 0, "runs": 1, "error": None},
        "results": {
            "summary": {**common, "kind": "summary", "name": f"{stem}.md", "text": md, **voices, "can_mark": rights["owner"],
                        "tasks": [{"line": n, "due_col": col["срок"], **task} for (n, col, _), task in zip(found, noted)]},
            "transcript": {**common, "kind": "transcript", "name": f"{stem}.txt", "text": names.display_text(view["lines"]), **view},
            "srt": {**common, "kind": "srt", "name": f"{stem}.srt", "text": srt, **voices}},
        "map": mindmap.page(marks.stamp(md, saved), view["lines"]),
        "tasks": [{**task, "unsigned": bool(task["who"] and registry.UNSIGNED.match(task["who"])),
                   "file": file, "dir": folder, "title": rec["title"], "date": day,
                   "link": external.page_url(file, "", task["at_sec"]), "can_mark": rights["owner"]} for task in noted],
        "search": {"file": file, "dir": folder, "finished_at": rec["finished_at"], "texts": {
            "summary": search.plain(md),        # как в сервисе: поиск идёт по протоколу без разметки
            "triage": "\n".join([rec["title"], rec["gist"], " ".join(rec["topics"]),
                                 *(" ".join(x for x in (v["name"], v["role"]) if x) for v in rec["auto"].values()),
                                 *rec["signed"].values()]),
            "transcript": "\n\n".join(f"[{s['speaker']}] {s['text']}" for s in segs) + "\n"}},
        "dated": {"dir": folder, "date": day},
    }


def browse(rel: str, flat: bool, files: list[dict], meta: dict) -> dict:
    """Ответ /api/browse: «все записи» (flat) либо одна папка — как собирает его app/web.py."""
    prefix = f"{rel}/" if rel else ""
    dirs: dict[str, dict] = {}
    for f in files:
        if f["dir"] == rel or not f["dir"].startswith(prefix):
            continue
        name = f["dir"][len(prefix):].split("/", 1)[0]
        entry = dirs.setdefault(name, {"name": name, "path": prefix + name, "files": 0, "last": None})
        entry["files"] += 1
        entry["last"] = max(entry["last"] or "", f["finished_at"])
    rights = meta["folders"].get(rel, {"owner": False, "manage": False})
    return {
        "path": rel, "parent": None if rel == "" else parent(rel),
        "dirs": sorted(dirs.values(), key=lambda d: d["name"].lower()),
        "files": sorted((f for f in files if flat or f["dir"] == rel), key=lambda f: f["finished_at"], reverse=True),
        "pending": [], "templates": {k: v["title"] for k, v in templates.TEMPLATES.items()},
        "upload": {"enabled": rel in meta["folders"], "max_bytes": 4 * 2 ** 30, "exts": sorted(media.MEDIA_EXTS),
                   "anywhere": True, "denied": "" if rel in meta["folders"] or not rel else
                   "загружать записи в эту папку может тот, кому дано право загрузки"},
        "mkdir": rights["owner"], "owns": rights["owner"], "home_upload": True, "manage": rights["manage"],
        "service": {"running": 0, "queue": 0, "watching": 0}}


def tree(path: str, folders: set[str]) -> dict:
    return {"path": path, "name": PurePosixPath(path).name, "can_upload": False, "records": [],
            "folders": [tree(f, folders) for f in sorted(folders, key=str.lower) if parent(f) == path]}


def page_parts(name: str) -> tuple[str, str, str]:
    """Страница сервиса по частям: заголовок вкладки, разметка тела и её собственный сценарий."""
    html = (STATIC / name).read_text(encoding="utf-8")
    title = re.search(r"<title>(.*?)</title>", html, re.S)[1]
    body = re.search(r"<body[^>]*>(.*)</body>", html, re.S)[1]
    scripts = re.findall(r"<script>(.*?)</script>", body, re.S)
    assert len(scripts) == 1, f"{name}: ждали один встроенный сценарий"
    return title, re.sub(r"<script\b.*?</script>", "", body, flags=re.S).strip(), scripts[0]


def build() -> str:
    meta = json.loads((DATA / "records.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        built = [build_record(rec, Path(tmp), meta) for rec in meta["records"]]
    files = [b["browse"] for b in built]
    folders: set[str] = set()
    for folder in meta["folders"]:
        while folder:
            folders.add(folder)
            folder = parent(folder)
    me, home = meta["me"], f"{users.HOME_ROOT}/{meta['me']['login']}"
    managed = [f for f, r in meta["folders"].items() if r["manage"]]
    data = {
        "me": {"login": me["login"], "name": me["name"], "admin": False, "home": home, "personal": False,
               "home_root": users.HOME_ROOT, "license": None, "url": "", "mail": True, "signup": False,
               "email": me["email"], "notify_ready": True, "requests": 0, "domain_account": False, "notice": "",
               "domain": {"on": False, "blocked": False, "name": "", "https": ""}},
        "browse": {"*": browse("", True, files, meta), **{f: browse(f, False, files, meta) for f in ["", *sorted(folders)]}},
        "results": {b["browse"]["file"]: b["results"] for b in built},
        "maps": {b["browse"]["file"]: b["map"] for b in built},
        "tasks": [t for b in built for t in b["tasks"]],
        "dated": [b["dated"] for b in built],
        "search": [b["search"] for b in built],
        "tree": tree("", folders),
        "own": sorted(f for f, r in meta["folders"].items() if r["owner"]),
        "recipients": {"people": [{"name": u["name"], "email": u["email"]} for u in meta["users"]],
                       "max": notify.RECIPIENTS_MAX, "note_max": 1000, "domains": []},
        # словари: окно открывается на чтение, а у хозяина папки — с полями, но без сохранения
        "dictionaries": {**{f"path:{f}": words_view(meta, f) for f in sorted(folders)},
                         **{f"file:{rec['file']}": words_view(meta, "", rec["file"]) for rec in meta["records"]}},
        "access": {f: {"path": f, "can_manage": False, "users": [
            {"login": u["login"], "name": u["name"], "admin": False, "read": "tree", "read_via": None,
             "upload": "none", "upload_via": None, "manage": "none", "manage_via": None} for u in meta["users"]]} for f in managed},
    }

    common = (STATIC / "common.js").read_text(encoding="utf-8")
    templates_html, scripts = [], []
    for address, name in PAGES.items():
        title, body, script = page_parts(name)
        templates_html.append(f'<template id="demo-page:{address}" data-title="{title} (демонстрация)">\n{body}\n</template>')
        # общий сценарий и сценарий страницы — в одной функции: «location» и «history» в них — адрес
        # внутри файла, а не настоящий; звука в файле нет, поэтому метка времени только объясняет это
        scripts.append(f'<script>\nDEMO.pages["{address}"] = function (location, history) {{\n'
                       f"{in_script(inline_assets(common))}\n{in_script(script)}\nplayFrom = DEMO.noAudio;\n}};\n</script>")
    css = inline_assets((STATIC / "app.css").read_text(encoding="utf-8"))
    icon = inline_assets("/static/favicon.png")
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Подьячий — демонстрация</title>
<link rel="icon" type="image/png" href="{icon}">
<style>
{css}
</style>
<style>
{(DATA / "demo.css").read_text(encoding="utf-8")}</style>
</head>
<body class="three">
<div id="demo-bar" role="note">{INFO}<span>{BAR}</span></div>
<div class="toast" id="demo-toast" role="status" aria-live="polite"></div>
<div id="demo-map" role="dialog" aria-modal="true" aria-labelledby="demo-map-title" hidden>
  <div class="mapbar"><b id="demo-map-title">Карта записи <span class="dim">— в сервисе открывается отдельной страницей и скачивается одним файлом</span></b>
    <button type="button" class="ghost" id="demo-map-close">Закрыть карту</button></div>
  <iframe title="Карта записи"></iframe>
</div>
{chr(10).join(templates_html)}
<script>
window.DEMO = {{ data: {in_script(json.dumps(data, ensure_ascii=False, separators=(",", ":")))} }};
</script>
<script>
{in_script((DATA / "demo.js").read_text(encoding="utf-8"))}</script>
{chr(10).join(scripts)}
<script>DEMO.start();</script>
</body>
</html>
"""


if __name__ == "__main__":
    page = build()
    TARGET.write_text(page, encoding="utf-8", newline="\n")
    print(f"{TARGET}: {len(page.encode('utf-8')) / 1024:.0f} КБ")
