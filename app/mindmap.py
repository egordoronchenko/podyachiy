"""Карта протокола: самостоятельный HTML-файл с деревом разделов, протоколом и расшифровкой.

Карта строится из готового .md разбором разметки — модель в этом не участвует,
поэтому карта ничего не стоит, появляется мгновенно и не может разойтись с
протоколом по смыслу. В файле нет внешних ссылок: его можно переслать, и он
откроется в любом браузере без сервиса и без интернета.

Пересобрать карты у всех уже обработанных записей (в контейнере сервиса; папки и база берутся
из его настроек, отметки и сроки поручений — из базы):

    python -m app.mindmap
"""
from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path

from . import marks, names

FILE_SUFFIX = ".map.html"

TS = re.compile(r"\[(\d{1,2}:\d\d:\d\d)\]")
LIST_ITEM = re.compile(r"\s*(-|\d+[.)])\s+")
TABLE_RULE = re.compile(r":?-+:?")
SHORT_LIMIT = 78


# [текст](адрес) — ссылка на другую запись сервиса: такие ставит сам сервис в разделе
# «Поручения прошлого совещания». Адрес — только сайта (с / или http), остальное — не ссылка.
LINK = re.compile(r"\[([^\]\[]+)\]\(((?:https?://|/)[^)\s]*)\)")


def plain(s: str) -> str:
    s = re.sub(r"<!--.*?-->", "", s)
    s = LINK.sub(r"\1", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"`(.+?)`", r"\1", s)
    return s.strip()


def no_ts(s: str) -> str:
    """Текст без меток времени и без знаков, которые остались висеть после них."""
    s = TS.sub("", s)
    s = re.sub(r"(?:\s*[,/])+\s*([.;:]|$)", r"\1", s)      # «…не решили [..], [..].» → «…не решили.»
    s = re.sub(r"\s*[–—-]\s*$", "", s)
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    s = re.sub(r"[\s,/;]+$", "", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def short(s: str) -> str:
    """Подпись узла: первая мысль пункта, не длиннее SHORT_LIMIT."""
    s = no_ts(plain(s))
    if len(s) <= SHORT_LIMIT:
        return s.rstrip(".")
    for sep in ("; ", ". ", ": ", " — "):
        cut = s.find(sep)
        if 25 <= cut <= SHORT_LIMIT:
            return s[:cut]
    cut = s.rfind(" ", 0, SHORT_LIMIT)
    return s[:cut].rstrip(",;:—-") + "…"


def node(title: str, detail: str = "", sub: str = "", ts: list[str] | None = None,
         kind: str = "", anchor: str = "") -> dict:
    return {"t": title, "d": detail, "s": sub, "ts": ts or [], "k": kind, "a": anchor, "c": []}


def from_text(raw: str, anchor: str) -> dict:
    """Пункт списка или абзац: «**Заголовок.** текст» либо просто текст."""
    ts = TS.findall(raw)
    m = re.match(r"\*\*(.+?)\*\*[.:]?\s*(.*)", raw, re.S)
    if m and m.group(2).strip():
        return node(no_ts(m.group(1)).rstrip(".:"), no_ts(plain(m.group(2))), ts=ts, anchor=anchor)
    full = no_ts(plain(raw))
    title = short(raw)
    return node(title, full if full.rstrip(".") != title else "", ts=ts, anchor=anchor)


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def table_rows(block: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    rows = [r for r in block[1:] if not all(TABLE_RULE.fullmatch(c or "-") for c in r)]
    return block[0], rows


def table_nodes(header: list[str], rows: list[list[str]], anchor: str) -> list[dict]:
    """Строки таблицы — узлы. Жирная строка с пустыми ячейками (имя ответственного
    в таблице задач) открывает группу, и следующие строки ложатся в неё."""
    out: list[dict] = []
    group = None
    low = [h.lower() for h in header]
    for r in rows:
        r = r + [""] * (len(header) - len(r))
        if re.fullmatch(r"\*\*.+\*\*", r[0]) and not any(c.strip() for c in r[1:]):
            group = node(plain(r[0]), kind="group", anchor=anchor)
            out.append(group)
            continue
        # колонку с номером строки в подпись не берём: «1» ни о чём не говорит
        lead = 1 if re.fullmatch(r"\d{1,3}", r[0]) and len(r) > 1 else 0
        first = r[lead]
        pairs = [(header[i], no_ts(plain(r[i]))) for i in range(lead + 1, len(header))]
        pairs = [(h, v) for h, v in pairs if v and v not in ("—", "-")]
        notes = []
        for key in ("срок", "статус"):
            if key in low:
                v = no_ts(plain(r[low.index(key)]))
                if v and v != "—":
                    notes.append(f"срок: {v}" if key == "срок" else v)
        sub = " · ".join(notes)
        if len(header) - lead == 2:               # «Участник | Роль», «Термин | Пояснение»
            sub, pairs = no_ts(plain(r[lead + 1])), []
        full, title = no_ts(plain(first)), short(first)
        detail = "\n".join(([full] if full.rstrip(".") != title else []) +
                           [f"{h}: {v}" for h, v in pairs if h.lower() not in ("срок", "статус")])
        (group["c"] if group else out).append(
            node(title, detail, sub, TS.findall(" ".join(r)), anchor=anchor))
    return out


def parse(md: str) -> dict:
    """Дерево карты: название → разделы → темы и люди → пункты."""
    lines = md.replace("\r\n", "\n").split("\n")
    root = node("Протокол", kind="root")
    meta: list[str] = []
    sec = grp = sub = None
    anchor = ""
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if ln.startswith("# "):
            root["t"] = plain(ln[2:])
        elif ln.startswith("## "):
            anchor = f"s{len(root['c']) + 1}"
            sec = node(re.sub(r"^\d+\.\s*", "", plain(ln[3:])), kind="section", anchor=anchor)
            grp = sub = None
            root["c"].append(sec)
        elif sec is None:
            m = re.match(r"\*\*(.+?):\*\*\s*(.*)", ln)
            if m and m.group(1) == "Название":
                root["t"] = plain(m.group(2))
            elif m:
                meta.append(f"{m.group(1)}: {plain(m.group(2))}")
        elif ln.startswith("### "):
            name = re.sub(r"^[\d.]+\s*", "", plain(ln[4:]))
            grp, sub = node(no_ts(name), ts=TS.findall(ln), kind="group", anchor=anchor), None
            sec["c"].append(grp)
        elif ln.startswith("|"):
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append(cells(lines[i]))
                i += 1
            i -= 1
            (sub or grp or sec)["c"].extend(table_nodes(*table_rows(block), anchor))
        elif LIST_ITEM.match(ln):
            indent = len(ln) - len(ln.lstrip())
            text = LIST_ITEM.sub("", ln, count=1)
            while (i + 1 < len(lines) and lines[i + 1].startswith(" " * (indent + 2))
                   and not LIST_ITEM.match(lines[i + 1])):
                i += 1
                text += " " + lines[i].strip()
            parent = sub or grp or sec
            if indent >= 2 and parent["c"]:
                parent = parent["c"][-1]
            parent["c"].append(from_text(text, anchor))
        elif re.match(r"\*\*.+\*\*", ln):
            # «**Тема — докладчик** [время]» в протоколе совещания
            grp, sub = node(no_ts(plain(ln)), ts=TS.findall(ln), kind="group", anchor=anchor), None
            sec["c"].append(grp)
        elif ln.strip() and not ln.startswith(("Прочерк", "<!--", "---", "```")):
            text = plain(ln)
            if text.endswith(":") and len(text) <= 90:
                # «Что нужно создать (теория):» — подзаголовок для списка под ним
                sub = node(no_ts(text.rstrip(":")), ts=TS.findall(ln), kind="group", anchor=anchor)
                (grp or sec)["c"].append(sub)
            else:
                sub = None
                m = re.match(r"([^:\[]{3,40}):\s+(.+)", text)
                (grp or sec)["c"].append(
                    node(m.group(1), no_ts(m.group(2)), ts=TS.findall(ln), anchor=anchor)
                    if m else from_text(ln, anchor))
        i += 1
    root["s"] = " · ".join(meta)
    root["c"] = [s for s in root["c"] if s["c"]]
    return root


# ---------- протокол в HTML ----------

def inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = LINK.sub(lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return TS.sub(r'<span class="ts">\1</span>', s)


def table_row(r: list[str]) -> str:
    """Жирная строка с пустыми ячейками — имя ответственного над его задачами:
    она идёт во всю ширину таблицы и выделена цветом."""
    if re.fullmatch(r"\*\*.+\*\*", r[0]) and not any(c.strip() for c in r[1:]):
        return f'<tr class="who"><td colspan="{len(r)}">{inline(r[0])}</td></tr>'
    return "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>"


def render(md: str) -> str:
    """Протокол для чтения. Разметка — только та, что встречается в шаблонах
    сервиса: заголовки, списки с вложенностью, таблицы, абзацы, блоки кода."""
    lines = re.sub(r"<!--.*?-->", "", md.replace("\r\n", "\n"), flags=re.S).split("\n")
    out: list[str] = []
    para: list[str] = []
    stack: list[tuple[int, str]] = []          # открытые списки: (отступ, тег)
    sections = 0

    def close_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    def close_lists(to: int = -1) -> None:
        while stack and stack[-1][0] > to:
            out.append(f"</li></{stack.pop()[1]}>")

    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if ln.startswith("```"):
            close_para()
            close_lists()
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre>" + html.escape("\n".join(code), quote=False) + "</pre>")
        elif m := re.match(r"(#{1,4})\s+(.*)", ln):
            close_para()
            close_lists()
            level = len(m.group(1))
            ident = ""
            if level == 2:
                sections += 1
                ident = f' id="s{sections}"'
            out.append(f"<h{level}{ident}>{inline(m.group(2))}</h{level}>")
        elif ln.startswith("|"):
            close_para()
            close_lists()
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append(cells(lines[i]))
                i += 1
            i -= 1
            header, rows = table_rows(block)
            out.append('<div class="tw"><table><thead><tr>'
                       + "".join(f"<th>{inline(c)}</th>" for c in header) + "</tr></thead><tbody>"
                       + "".join(table_row(r) for r in rows) + "</tbody></table></div>")
        elif m := LIST_ITEM.match(ln):
            close_para()
            indent = len(ln) - len(ln.lstrip())
            tag = "ul" if m.group(1) == "-" else "ol"
            close_lists(indent)
            if stack and stack[-1][0] == indent:
                out.append("</li>")
            else:
                out.append(f"<{tag}>")
                stack.append((indent, tag))
            out.append("<li>" + inline(LIST_ITEM.sub("", ln, count=1)))
        elif not ln.strip():
            close_para()
            if stack and not (i + 1 < len(lines) and (LIST_ITEM.match(lines[i + 1])
                                                      or lines[i + 1].startswith(" "))):
                close_lists()
        elif stack and ln.startswith(" "):
            out.append(" " + inline(ln.strip()))       # продолжение пункта списка
        else:
            close_lists()
            para.append(inline(ln.strip()))
        i += 1
    close_para()
    close_lists()
    return "\n".join(out)


# ---------- страница ----------

# Вид страницы меняется вместе с сервисом: номер растёт с каждой правкой PAGE, и файл, собранный
# прежней версией, пересобирается при первом обращении (см. ensure).
STAMP = '<html lang="ru" data-map="2">'

PAGE = r"""<!doctype html>
__STAMP__<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1c2330;--dim:#667085;--line:#c4cad6;--edge:#d9dee7;--hit:#fff3bf;--chip:#eef2f7;
 --c0:#3b6fd4;--c1:#0f8f7e;--c2:#8a56c9;--c3:#3f9142;--c4:#b97a10;--c5:#c8473e;--c6:#2a8fb0;--c7:#a04b8a}
@media (prefers-color-scheme:dark){:root{--bg:#0f141b;--panel:#182029;--ink:#e6eaf0;--dim:#93a0b4;--line:#3a4657;--edge:#2a3442;--hit:#5b4a12;--chip:#222c38;
 --c0:#7aa5f5;--c1:#4fc7b4;--c2:#b993ee;--c3:#7cc47f;--c4:#e2b04f;--c5:#ee8a82;--c6:#6cc3df;--c7:#d48ac0}}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--ink);font:14px/1.45 "Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;display:flex;flex-direction:column}
header{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--edge)}
header h1{font-size:15px;font-weight:600;margin:0 8px 0 0;flex:1 1 260px;min-width:0}
header .meta{display:block;font-size:12px;font-weight:400;color:var(--dim)}
button,input{font:inherit;color:inherit}
button{background:var(--chip);border:1px solid var(--edge);border-radius:7px;padding:5px 10px;cursor:pointer}
button:hover{border-color:var(--line)}
.back{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:7px;padding:5px 12px;color:var(--c0);font-weight:600;text-decoration:none;white-space:nowrap}
.back:hover{border-color:var(--c0);background:var(--chip)}
:focus-visible{outline:2px solid var(--c0);outline-offset:2px}
.tabs{display:flex;gap:4px}
.tabs button[aria-selected=true]{background:var(--c0);border-color:var(--c0);color:var(--panel);font-weight:600}
input[type=search]{background:var(--bg);border:1px solid var(--edge);border-radius:7px;padding:5px 10px;width:200px;max-width:100%}
#hits{font-size:12px;color:var(--dim);min-width:70px}
body:not([data-view=map]) .maponly{display:none}
main{flex:1;display:flex;min-height:0}
body:not([data-view=map]) main{display:none}
#stage{flex:1;overflow:auto;cursor:grab;padding:24px}
#stage.drag{cursor:grabbing;user-select:none}
#map{transform-origin:0 0;display:inline-block;padding-right:80px;padding-bottom:60px}
.node{display:flex;align-items:center;position:relative}
.kids{display:flex;flex-direction:column;margin-left:30px;position:relative}
.kids::before{content:"";position:absolute;left:-30px;top:50%;width:30px;border-top:1.5px solid var(--line)}
.kids>.node{padding:4px 0 4px 20px}
.kids>.node::before{content:"";position:absolute;left:0;top:50%;width:20px;border-top:1.5px solid var(--line)}
.kids>.node::after{content:"";position:absolute;left:0;top:0;bottom:0;border-left:1.5px solid var(--line)}
.kids>.node:first-child::after{top:50%}
.kids>.node:last-child::after{bottom:50%}
.kids>.node:only-child::after{display:none}
.node.shut>.kids{display:none}
.lab{background:var(--panel);border:1px solid var(--edge);border-left:4px solid var(--c);border-radius:8px;padding:6px 10px;max-width:330px;cursor:pointer;flex:none}
.lab:hover{border-color:var(--c)}
.lab.on{outline:2px solid var(--c);outline-offset:1px}
.lab.hit{background:var(--hit)}
.lab .t{display:block}
.lab .s{display:block;font-size:12px;color:var(--dim);margin-top:1px}
.lab .f{display:flex;gap:6px;align-items:center;margin-top:3px;font-size:11.5px;color:var(--dim)}
.ts{background:var(--chip);border-radius:5px;padding:0 5px;font-variant-numeric:tabular-nums;font-size:.92em;white-space:nowrap}
.lab .n{margin-left:auto;font-weight:600;color:var(--c)}
.d0>.lab{font-size:16px;font-weight:600;padding:10px 14px;max-width:300px;border-left-width:1px}
.d1>.lab{font-weight:600;background:color-mix(in srgb,var(--c) 12%,var(--panel))}
.group>.lab{font-weight:600}
aside{width:340px;max-width:42vw;background:var(--panel);border-left:1px solid var(--edge);padding:16px;overflow:auto;flex:none}
aside h2{font-size:15px;margin:0 0 4px}
aside .path{font-size:12px;color:var(--dim);margin-bottom:10px}
aside p{margin:0 0 8px;white-space:pre-wrap}
aside .tss{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
aside .hint{color:var(--dim);font-size:13px}
.sheet{flex:1;overflow:auto;padding:24px 16px 60px}
body:not([data-view=doc]) #doc{display:none}
body:not([data-view=tr]) #tr{display:none}
.sheet article{max-width:920px;margin:0 auto;background:var(--panel);border:1px solid var(--edge);border-radius:12px;padding:22px 28px;font-size:15px;line-height:1.55}
.trbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:14px;color:var(--dim);font-size:13px}
.trbar span{flex:1 1 200px}
.ln{margin:0 0 11px}
.ln b{font-weight:600;margin:0 6px}
#doc h1{font-size:22px;margin:0 0 12px}
#doc h2{font-size:18px;margin:26px 0 10px;padding-top:14px;border-top:1px solid var(--edge);scroll-margin-top:12px}
#doc h3{font-size:15.5px;margin:18px 0 6px}
#doc h2.flash{background:var(--hit)}
#doc ul,#doc ol{padding-left:22px;margin:6px 0}
#doc li{margin:3px 0}
#doc pre{background:var(--chip);border-radius:8px;padding:10px 12px;overflow:auto;font-size:13px}
#doc code{background:var(--chip);border-radius:4px;padding:0 4px;font-size:.93em}
.tw{overflow-x:auto;margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border:1px solid var(--edge);padding:6px 9px;text-align:left;vertical-align:top}
th{background:var(--chip);font-weight:600}
tr.who td{background:color-mix(in srgb,var(--c0) 14%,transparent);color:var(--c0);border-left:3px solid var(--c0)}
@media (max-width:760px),(pointer:coarse){button,.back,input[type=search]{min-height:44px;min-width:44px}}
@media (max-width:760px){main{flex-direction:column}aside{width:auto;max-width:none;border-left:0;border-top:1px solid var(--edge);max-height:38vh}.sheet article{padding:16px}}
@media print{header button,header input,header .back,#hits,.tabs,main,#tr,.trbar{display:none!important}body{height:auto;display:block}#doc{display:block!important;overflow:visible;padding:0}#doc article{border:0}}
</style></head><body data-view="map">
<header>
  <h1>__TITLE__<span class="meta">__META__</span></h1>
  <div class="tabs" role="tablist" aria-label="Вид записи">
    <button id="tdoc" role="tab" aria-selected="false">Протокол</button>
    <button id="tmap" role="tab" aria-selected="true">Карта</button>
    <button id="ttr" role="tab" aria-selected="false">Расшифровка</button>
  </div>
  <input type="search" id="q" class="maponly" placeholder="Найти в карте" aria-label="Найти в карте">
  <span id="hits" class="maponly"></span>
  <button id="open" class="maponly">Развернуть всё</button>
  <button id="shut" class="maponly">Свернуть</button>
  <button id="zo" class="maponly" aria-label="Мельче">−</button>
  <button id="zi" class="maponly" aria-label="Крупнее">+</button>
</header>
<main>
  <div id="stage"><div id="map"></div></div>
  <aside id="side"><p class="hint">Нажмите на узел: ветка развернётся, а здесь появится полный текст пункта и метки времени записи.</p></aside>
</main>
<div id="doc" class="sheet"><article>
  <div class="trbar"><span>Протокол собран моделью по расшифровке — сверяйте по меткам времени.</span><button id="doccopy">Копировать текст</button></div>
  __DOC__
</article></div>
<div id="tr" class="sheet"><article>
  <div class="trbar"><span id="trinfo"></span><button id="trcopy">Копировать текст</button></div>
  <div id="trbody"></div>
</article></div>
<script>
const TREE = __DATA__;
const LINES = __LINES__;   // расшифровка: [время, кто говорит, текст]
const PROTOCOL = __MD__;   // протокол как есть, в Markdown — для копирования
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const count = n => n.c.reduce((a, c) => a + 1 + count(c), 0);
let seq = 0; const byId = {};
function build(n, depth, color, path) {
  const id = seq++; byId[id] = { n, path };
  const here = path.concat(n.t);
  const kids = n.c.map((c, i) => build(c, depth + 1, depth === 0 ? i % 8 : color, depth === 0 ? [] : here)).join("");
  return `<div class="node d${Math.min(depth, 3)} ${n.k || ""} ${n.c.length && depth >= 1 ? "shut" : ""}" data-id="${id}" style="--c:var(--c${color})">
    <div class="lab" tabindex="0"><span class="t">${esc(n.t)}</span>${n.s && depth ? `<span class="s">${esc(n.s)}</span>` : ""}
      ${n.ts.length || n.c.length ? `<span class="f">${n.ts.length ? `<span class="ts">${esc(n.ts[0])}</span>` : ""}${n.c.length ? `<span class="n">${count(n)}</span>` : ""}</span>` : ""}</div>
    ${n.c.length ? `<div class="kids">${kids}</div>` : ""}</div>`;
}
const map = document.getElementById("map"), side = document.getElementById("side");
map.innerHTML = build(TREE, 0, 0, []);

const TABS = { doc: "tdoc", map: "tmap", tr: "ttr" };
function tab(view) {
  document.body.dataset.view = view;
  for (const [v, id] of Object.entries(TABS)) document.getElementById(id).setAttribute("aria-selected", String(v === view));
  if (view === "tr") fillTranscript();
}
for (const [v, id] of Object.entries(TABS)) document.getElementById(id).onclick = () => tab(v);
// стрелки влево и вправо ходят по видам, как по вкладкам
document.querySelector(".tabs").addEventListener("keydown", e => {
  const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
  if (!step) return;
  const tabs = [...e.currentTarget.querySelectorAll("button:not([hidden])")];
  const next = tabs[(tabs.indexOf(e.target) + step + tabs.length) % tabs.length];
  e.preventDefault(); next.focus(); next.click();
});

// Подряд идущие реплики одного человека склеиваются — так же, как в файле расшифровки.
function transcriptText() {
  const blocks = [];
  for (const [, who, text] of LINES) {
    if (blocks.length && blocks[blocks.length - 1][0] === who) blocks[blocks.length - 1][1].push(text);
    else blocks.push([who, [text]]);
  }
  return blocks.map(([who, parts]) => `[${who}] ${parts.join(" ")}`).join("\n\n") + "\n";
}
let transcriptReady = false;
function fillTranscript() {
  if (transcriptReady) return;
  transcriptReady = true;
  document.getElementById("trbody").innerHTML = LINES.map(([t, who, text]) =>
    `<p class="ln"><span class="ts">${esc(t)}</span><b>${esc(who)}</b>${esc(text)}</p>`).join("");
  const voices = new Set(LINES.map(l => l[1])).size;
  document.getElementById("trinfo").textContent = `Реплик: ${LINES.length}, говорящих: ${voices}. Текст не правился: это то, что распознано.`;
}
// Кнопка копирует текст вкладки; shown — что выделить, если буфер обмена недоступен.
function copyButton(id, text, shown) {
  document.getElementById(id).onclick = async e => {
    const b = e.currentTarget;
    try { await navigator.clipboard.writeText(text()); b.textContent = "Скопировано"; }
    catch (err) {
      const r = document.createRange(); r.selectNodeContents(shown());
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); b.textContent = "Выделено — нажмите Ctrl+C";
    }
    setTimeout(() => { b.textContent = "Копировать текст"; }, 2500);
  };
}
copyButton("trcopy", transcriptText, () => document.getElementById("trbody"));
copyButton("doccopy", () => PROTOCOL, () => document.querySelector("#doc article"));
if (!LINES.length) document.getElementById("ttr").hidden = true;

function toDoc(anchor) {
  tab("doc");
  const h = document.getElementById(anchor); if (!h) return;
  h.scrollIntoView({ block: "start" });
  h.classList.add("flash"); setTimeout(() => h.classList.remove("flash"), 1600);
}

function show(el) {
  document.querySelectorAll(".lab.on").forEach(x => x.classList.remove("on"));
  el.querySelector(".lab").classList.add("on");
  const { n, path } = byId[el.dataset.id];
  side.innerHTML = `${path.length ? `<div class="path">${esc(path.join(" → "))}</div>` : ""}<h2>${esc(n.t)}</h2>
    ${n.s ? `<p class="hint">${esc(n.s)}</p>` : ""}${n.d ? `<p>${esc(n.d)}</p>` : ""}
    ${n.c.length ? `<p class="hint">Вложенных пунктов: ${count(n)}</p>` : ""}
    ${n.ts.length ? `<div class="tss">${n.ts.map(t => `<span class="ts">${esc(t)}</span>`).join("")}</div>` : ""}
    ${n.a ? `<button data-anchor="${esc(n.a)}">Открыть в протоколе</button>` : ""}`;
}
side.addEventListener("click", e => { const b = e.target.closest("[data-anchor]"); if (b) toDoc(b.dataset.anchor); });
map.addEventListener("click", e => {
  const lab = e.target.closest(".lab"); if (!lab || moved) return;
  const el = lab.parentElement;
  if (el.querySelector(":scope > .kids")) el.classList.toggle("shut");
  show(el);
});
map.addEventListener("keydown", e => {
  if ((e.key === "Enter" || e.key === " ") && e.target.classList.contains("lab")) { e.preventDefault(); e.target.click(); }
});
const all = () => [...map.querySelectorAll(".node")];
document.getElementById("open").onclick = () => all().forEach(n => n.classList.remove("shut"));
document.getElementById("shut").onclick = () => all().forEach(n => {
  if (n.querySelector(":scope > .kids")) n.classList.toggle("shut", !n.classList.contains("d0")); });

let zoom = 1;
const setZoom = z => { zoom = Math.min(1.6, Math.max(0.4, z)); map.style.transform = `scale(${zoom})`; };
document.getElementById("zi").onclick = () => setZoom(zoom + 0.1);
document.getElementById("zo").onclick = () => setZoom(zoom - 0.1);
const stage = document.getElementById("stage");
stage.addEventListener("wheel", e => { if (e.ctrlKey) { e.preventDefault(); setZoom(zoom - Math.sign(e.deltaY) * 0.1); } }, { passive: false });
let drag = null, moved = false;
stage.addEventListener("mousedown", e => { drag = { x: e.clientX, y: e.clientY, l: stage.scrollLeft, t: stage.scrollTop }; moved = false; });
window.addEventListener("mousemove", e => {
  if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  if (Math.abs(dx) + Math.abs(dy) > 5) { moved = true; stage.classList.add("drag"); }
  if (moved) { stage.scrollLeft = drag.l - dx; stage.scrollTop = drag.t - dy; }
});
window.addEventListener("mouseup", () => { drag = null; stage.classList.remove("drag"); setTimeout(() => { moved = false; }, 0); });

const q = document.getElementById("q"), hits = document.getElementById("hits");
q.addEventListener("input", () => {
  const text = q.value.trim().toLowerCase().replace(/ё/g, "е");
  map.querySelectorAll(".lab.hit").forEach(x => x.classList.remove("hit"));
  if (text.length < 2) { hits.textContent = ""; return; }
  let found = 0, first = null;
  all().forEach(el => {
    const n = byId[el.dataset.id].n;
    if (`${n.t} ${n.d} ${n.s}`.toLowerCase().replace(/ё/g, "е").includes(text)) {
      found++; first = first || el;
      el.querySelector(".lab").classList.add("hit");
      for (let p = el.parentElement.closest(".node"); p; p = p.parentElement.closest(".node")) p.classList.remove("shut");
    }
  });
  hits.textContent = found ? `найдено: ${found}` : "не найдено";
  if (first) first.querySelector(".lab").scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
});
</script></body></html>
"""


def clock(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def page(md: str, lines: list[dict] | None = None) -> str:
    """lines — реплики расшифровки из names.view: с ними в файле появляется третья вкладка."""
    tree = parse(md)
    data = json.dumps(tree, ensure_ascii=False).replace("</", "<\\/")
    rows = [[clock(ln["start"]), ln["who"], ln["text"].strip()]
            for ln in lines or [] if ln["text"].strip()]
    return (PAGE.replace("__STAMP__", STAMP).replace("__DATA__", data)
            .replace("__LINES__", json.dumps(rows, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__MD__", json.dumps(re.sub(r"<!--.*?-->\s*", "", md, flags=re.S).strip() + "\n",
                                          ensure_ascii=False).replace("</", "<\\/"))
            .replace("__DOC__", render(md))
            .replace("__TITLE__", html.escape(tree["t"], quote=False))
            .replace("__META__", html.escape(tree["s"], quote=False)))


ORG_SLOT = '<span class="meta">'


def with_org(page_html: str, org: str) -> str:
    """Готовая страница с названием организации в шапке. Название дописывается при отдаче файла,
    а не при сборке: файл лежит рядом с протоколом месяцами, а название меняют на странице."""
    return page_html.replace(ORG_SLOT, f'<span class="meta">{html.escape(org, quote=False)}</span>{ORG_SLOT}', 1)


BACK_SLOT = "<header>\n"


def with_back(page_html: str, href: str) -> str:
    """Страница с кнопкой возврата к записи слева в шапке. Кнопку дописывает сервис, когда сам
    показывает карту: в файле на диске и в скачанном её нет — без сервиса ей вести некуда."""
    link = f'  <a class="back" href="{html.escape(href)}"><span aria-hidden="true">←</span> К записи</a>\n'
    return page_html.replace(BACK_SLOT, BACK_SLOT + link, 1)


def _sources(protocol: Path) -> list[Path]:
    """Файлы, из которых собрана карта: протокол, расшифровка, ручные подписи говорящих. Ещё в
    неё входят отметки «выполнено» и сроки — они лежат в базе, их время правки передают отдельно."""
    transcript = protocol.parent.parent / "transcript"
    stem = protocol.stem
    return [protocol, transcript / f"{stem}.json", transcript / f"{stem}{names.FILE_SUFFIX}"]


def write_for(protocol: Path, noted: dict | None = None) -> Path:
    """Карта рядом с протоколом: summary/<имя>.md → summary/<имя>.map.html.
    noted — отметки и сроки поручений записи (marks.Store.load)."""
    target = protocol.with_name(protocol.stem + FILE_SUFFIX)
    try:
        lines = names.view(protocol.parent.parent, protocol.stem)["lines"]
    except (OSError, ValueError, KeyError):
        lines = []      # у старых записей расшифровка в другом виде — файл будет без этой вкладки
    # отметки «выполнено» и сроки, заданные человеком, — в таблице задач: файл скачивают и читают без сервиса
    md = marks.stamp(protocol.read_text(encoding="utf-8"), noted or {})
    # через временный файл: оборванная запись не оставит на месте карты её половину
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(page(md, lines), encoding="utf-8")
    os.replace(tmp, target)
    return target


def ensure(protocol: Path, noted: dict | None = None, noted_at: float = 0.0) -> Path:
    """Карта не старше того, из чего собрана: протокол могли поправить руками,
    а ФИО у реплик — подписать уже после сборки протокола; отметку у поручения — поставить
    или снять (noted_at — когда отметки записи меняли последний раз). Файл прежнего вида
    страницы собирается заново."""
    target = protocol.with_name(protocol.stem + FILE_SUFFIX)
    newest = max([noted_at, *(p.stat().st_mtime for p in _sources(protocol) if p.is_file())])
    if not target.is_file() or target.stat().st_mtime < newest or not _current(target):
        write_for(protocol, noted)
    return target


def _current(target: Path) -> bool:
    with target.open(encoding="utf-8") as f:
        return STAMP in f.read(200)


if __name__ == "__main__":
    from . import db, ledger, pipeline
    from .config import Settings

    cfg = Settings.from_env()
    base = db.Database(cfg.state_dir)
    noted = marks.Store(base)
    done = 0
    for file, record in sorted(ledger.Ledger(base).records.items()):
        md_path = pipeline.protocol_path(cfg, file, record)
        if md_path.is_file():
            write_for(md_path, noted.load(file))
            done += 1
    base.close()
    print(f"карт собрано: {done}")
