"""Документация для людей одним файлом: инструкция по установке и руководство пользователя.

Источники — два текста в Markdown, они же читаются прямо в репозитории:

    install/README.md        → site/podyachiy-install.html   (к визитке, открывается с диска)
                             → app/static/install-guide.html (та же инструкция в сервисе: /install-guide)
    install/personal.md      → site/podyachiy-personal.html  (установить для себя, личный режим)
    docs/guide/README.md     → site/podyachiy-guide.html     (руководство пользователя со снимками экрана)

Собранные файлы самодостаточны: стили, шрифт надписи, логотип и снимки лежат внутри, сети не нужно.
В них оглавление с якорями, кнопка «Скопировать» у блоков кода, прокрутка широких таблиц внутри
таблицы, стили для печати, светлая и тёмная тема (по настройке системы).

Пересобрать после правки любого из трёх текстов, снимков или номера версии:

    python tools/docs_build.py            # записать все четыре файла
    python tools/docs_build.py --check    # ничего не писать; расхождение — код возврата 1

Собранные файлы лежат в репозитории; tests/test_docs.py падает, если они не совпадают с тем, что
даёт этот сценарий из текущих источников. Сборка воспроизводима: дат и случайных значений в файлах нет.
Внешних библиотек не нужно — только стандартная библиотека Python.

Разметка. Свой небольшой разбор Markdown — ровно под то, что есть в двух текстах: заголовки # … ####,
абзацы, списки «-» и «1.» с вложенностью отступом (внутри пункта — абзацы, код, таблицы), таблицы
с «|», блоки кода ``` с языком, `код`, **жирный**, *курсив*, ссылки [текст](адрес) и <http://…>,
разделитель ---, цитата «>» (выноска), картинка на отдельной строке ![подпись](shots/имя.webp).
Чего нет в этом списке — в текстах не использовать: сценарий остановится на неразобранной ссылке,
а лишняя разметка выйдет буквально. Слова «раздел 8», «раздела 10а», «раздел 5.1» в тексте становятся
ссылками на раздел с таким номером. Якоря заголовков — как на GitHub, поэтому ссылки вида
(#3-настройки-env) работают и в Markdown, и в собранном файле.

Снимки экрана руководства лежат в docs/guide/shots/*.webp и вшиваются в файл как data: URI.
Пересъёмка — на одноразовом стенде с выдуманными данными, шаги записаны в tools/guide_shots.mjs
(стенд из tools/demo_seed.py, корпоративный режим, светлая тема, окно 1280 точек, WebP).
После пересъёмки просмотрите каждый снимок глазами — на нём не должно быть настоящих людей, адресов,
паролей и значений ключей — и пересоберите документы этим сценарием.
"""
from __future__ import annotations

import base64
import html
import posixpath
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
SITE = ROOT / "site"
INSTALL_SRC = ROOT / "install" / "README.md"
GUIDE_SRC = ROOT / "docs" / "guide" / "README.md"
INSTALL_SITE = SITE / "podyachiy-install.html"
INSTALL_APP = STATIC / "install-guide.html"
GUIDE_SITE = SITE / "podyachiy-guide.html"
PERSONAL_SRC = ROOT / "install" / "personal.md"
PERSONAL_SITE = SITE / "podyachiy-personal.html"
# Ссылки на соседние документы написаны так, чтобы работать в репозитории (путь от файла-источника);
# в собранном файле документы лежат рядом. Ключ — путь от корня репозитория.
SIBLINGS = {"install/README.md": "podyachiy-install.html",
            "install/personal.md": "podyachiy-personal.html",
            "docs/guide/README.md": "podyachiy-guide.html",
            "site/podyachiy-install.html": "podyachiy-install.html",
            "site/podyachiy-personal.html": "podyachiy-personal.html",
            "site/podyachiy-guide.html": "podyachiy-guide.html",
            "site/podyachiy.html": "podyachiy.html",
            "site/podyachiy-demo.html": "podyachiy-demo.html"}
# Верхняя строка страниц сайта; у экземпляра в сервисе (/install-guide) она своя.
SITE_NAV = (("О сервисе", "podyachiy.html"), ("Руководство пользователя", "podyachiy-guide.html"),
            ("Установить для себя", "podyachiy-personal.html"),
            ("Установка в организации", "podyachiy-install.html"))

esc = html.escape


def data_uri(path: Path, kind: str) -> str:
    return f"data:{kind};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def webp_size(data: bytes) -> tuple[int, int]:
    """Ширина и высота снимка из заголовка WebP — чтобы место под картинку было занято до её отрисовки."""
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("снимок — не WebP")
    kind = data[12:16]
    if kind == b"VP8 ":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if kind == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X":
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    raise ValueError("неизвестный вид WebP")


# ---------- разбор Markdown ----------

def slug(title: str) -> str:
    """Якорь заголовка, как его делает GitHub: строчные, без знаков, пробелы — дефисами."""
    plain = re.sub(r"[`*]", "", title).strip().lower()
    return re.sub(r"[^\w\- ]", "", plain).replace(" ", "-")


INLINE = re.compile(r"(`[^`]+`)"                                   # 1 код
                    r"|<(https?://[^>\s]+)>"                        # 2 адрес в угловых скобках
                    r"|\[([^\]]+)\]\(([^)\s]+)\)"                   # 3, 4 ссылка
                    r"|\*\*(.+?)\*\*"                               # 5 жирный
                    r"|(?<![\w*])\*([^*\s][^*]*?)\*(?![\w*])")      # 6 курсив
SECTION_REF = re.compile(r"(раздел(?:а|е|у|ом|ы|ах|ов)?)\s(\d+а?(?:\.\d+)?)(?![\d.]*\d)")


class Doc:
    """Один документ: разбор текста, заголовки для оглавления, проверка ссылок."""

    def __init__(self, source: Path, shots: Path | None = None):
        self.source, self.shots = source, shots
        self.heads: list[tuple[int, str, str]] = []      # уровень, якорь, заголовок (HTML)
        self.taken: dict[str, int] = {}
        self.links: list[str] = []                       # внутренние ссылки — на проверку
        self.numbered: dict[str, str] = {}               # «10а» → якорь раздела с этим номером
        self.title = ""
        text = source.read_text(encoding="utf-8")
        if "\r" in text:
            raise SystemExit(f"{source}: окончания строк должны быть LF")
        lines = text.split("\n")
        self._scan_heads(lines)
        self.body = self.blocks(lines)
        missing = sorted({link for link in self.links if link[1:] not in {anchor for _, anchor, _ in self.heads}})
        if missing:
            raise SystemExit(f"{source}: ссылки на якоря, которых нет: {', '.join(missing)}")

    def _scan_heads(self, lines: list[str]) -> None:
        """Номера разделов известны до разбора текста: ссылка «раздел 14» может стоять раньше раздела."""
        seen: dict[str, int] = {}
        fenced = False
        for line in lines:
            if line.lstrip().startswith("```"):
                fenced = not fenced
            m = None if fenced else re.match(r"(#{2,4}) (.+)", line)
            if not m:
                continue
            base = slug(m.group(2))
            count = seen.get(base, 0)
            seen[base] = count + 1
            number = re.match(r"(\d+а?(?:\.\d+)?)\. ", m.group(2))
            if number:
                self.numbered.setdefault(number.group(1), base if not count else f"{base}-{count}")

    def anchor(self, title: str) -> str:
        base = slug(title)
        count = self.taken.get(base, 0)
        self.taken[base] = count + 1
        return base if not count else f"{base}-{count}"

    # --- строчная разметка

    def plain(self, text: str) -> str:
        """Обычный текст: «раздел 8» — ссылкой на раздел с этим номером, если такой есть."""
        out, pos = [], 0
        for m in SECTION_REF.finditer(text):
            target = self.numbered.get(m.group(2))
            if not target:
                continue
            out.append(esc(text[pos:m.start()], quote=False))
            out.append(f'<a href="#{esc(target)}">{esc(m.group(1))}&nbsp;{esc(m.group(2))}</a>')
            pos = m.end()
        out.append(esc(text[pos:], quote=False))
        return "".join(out)

    def inline(self, text: str, linked: bool = False) -> str:
        out, pos = [], 0
        for m in INLINE.finditer(text):
            before = text[pos:m.start()]
            out.append(esc(before, quote=False) if linked else self.plain(before))
            code, auto, label, href, bold, italic = m.groups()
            if code:
                out.append(f"<code>{esc(code[1:-1], quote=False)}</code>")
            elif auto:
                out.append(f'<a href="{esc(auto)}" rel="noopener">{esc(auto, quote=False)}</a>')
            elif label:
                out.append(self.link(label, href))
            elif bold:
                out.append(f"<b>{self.inline(bold, linked)}</b>")
            else:
                out.append(f"<em>{self.inline(italic, linked)}</em>")
            pos = m.end()
        rest = text[pos:]
        out.append(esc(rest, quote=False) if linked else self.plain(rest))
        return "".join(out)

    def link(self, label: str, href: str) -> str:
        target, mark, anchor = href.partition("#")
        if target and not re.match(r"[a-z]+:", target):
            place = posixpath.normpath(posixpath.join(self.source.parent.relative_to(ROOT).as_posix(), target))
            href = SIBLINGS[place] + mark + anchor if place in SIBLINGS else href
        if href.startswith("#"):
            self.links.append(href)
            return f'<a href="{esc(href)}">{self.inline(label, True)}</a>'
        if re.match(r"https?://|mailto:", href):
            return f'<a href="{esc(href)}" rel="noopener">{self.inline(label, True)}</a>'
        if re.fullmatch(r"podyachiy(-\w+)?\.html(#[\w-]+)?", href):
            return f'<a href="{esc(href)}">{self.inline(label, True)}</a>'
        raise SystemExit(f"{self.source}: ссылка, которую собранный файл не откроет: ({href})")

    # --- блоки

    def blocks(self, lines: list[str]) -> str:
        out: list[str] = []
        para: list[str] = []
        i = 0

        def flush() -> None:
            if para:
                out.append(f"<p>{self.inline(' '.join(part.strip() for part in para))}</p>")
                para.clear()

        while i < len(lines):
            line = lines[i]
            bare = line.strip()
            if not bare:
                flush()
                i += 1
            elif bare.startswith("```"):
                flush()
                indent = len(line) - len(line.lstrip())
                end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == "```"), None)
                if end is None:
                    raise SystemExit(f"{self.source}: блок кода со строки «{bare}» не закрыт")
                out.append(self.code(bare[3:].strip(), [row[indent:] if row[:indent].isspace() else row.lstrip()
                                                        for row in lines[i + 1:end]]))
                i = end + 1
            elif m := re.match(r"(#{1,4}) (.+)", line):
                flush()
                out.append(self.heading(len(m.group(1)), m.group(2).strip()))
                i += 1
            elif bare == "---":
                flush()
                i += 1
            elif bare.startswith("|"):
                flush()
                end = i
                while end < len(lines) and lines[end].strip().startswith("|"):
                    end += 1
                out.append(self.table([row.strip() for row in lines[i:end]]))
                i = end
            elif bare.startswith(">"):
                flush()
                end = i
                while end < len(lines) and lines[end].strip().startswith(">"):
                    end += 1
                inner = [re.sub(r"^\s*> ?", "", row) for row in lines[i:end]]
                out.append(f'<div class="note">{self.blocks(inner)}</div>')
                i = end
            elif m := re.fullmatch(r"!\[([^\]]*)\]\(([^)\s]+)\)", bare):
                flush()
                out.append(self.figure(m.group(1), m.group(2)))
                i += 1
            elif re.match(r"\s*(?:[-*]|\d+\.) ", line):
                flush()
                block, i = self.list(lines, i)
                out.append(block)
            else:
                para.append(line)
                i += 1
        flush()
        return "\n".join(out)

    def heading(self, level: int, title: str) -> str:
        if level == 1:
            self.title = re.sub(r"[`*]", "", title)
            return ""
        anchor = self.anchor(title)
        shown = self.inline(title, True)
        self.heads.append((level, anchor, shown))
        return f'<h{level} id="{esc(anchor)}">{shown}<a class="hash" href="#{esc(anchor)}" aria-label="Ссылка на этот раздел">#</a></h{level}>'

    def code(self, lang: str, rows: list[str]) -> str:
        body = esc("\n".join(rows), quote=False)
        label = {"bash": "bash", "ini": ".env", "powershell": "PowerShell", "toml": "toml", "json": "json"}.get(lang, "")
        return (f'<div class="code"><div class="code-head"><span>{label}</span>'
                f'<button type="button" class="copy" hidden>Скопировать</button></div>'
                f'<pre tabindex="0"><code>{body}</code></pre></div>')

    def table(self, rows: list[str]) -> str:
        def cells(row: str) -> list[str]:
            parts, cur, in_code = [], "", False
            for ch in row.strip()[1:]:
                if ch == "`":
                    in_code = not in_code
                if ch == "|" and not in_code:
                    parts.append(cur.strip())
                    cur = ""
                else:
                    cur += ch
            if cur.strip():
                parts.append(cur.strip())
            return parts

        if len(rows) < 2 or not re.fullmatch(r"\|[\s:|-]+\|?", rows[1]):
            raise SystemExit(f"{self.source}: таблица без строки-разделителя: {rows[0][:60]}")
        head = cells(rows[0])
        out = [f'<div class="tbl" tabindex="0" role="region" aria-label="Таблица: {esc(re.sub(r"[`*]", "", head[0]))}">'
               f'<table class="{"wide" if len(head) > 2 else "pair"}"><thead><tr>',
               "".join(f'<th scope="col">{self.inline(cell)}</th>' for cell in head), "</tr></thead><tbody>"]
        for row in rows[2:]:
            got = cells(row)
            if len(got) != len(head):
                raise SystemExit(f"{self.source}: в строке таблицы {len(got)} ячеек вместо {len(head)}: {row[:70]}")
            out.append("<tr>" + "".join(f"<td>{self.inline(cell)}</td>" for cell in got) + "</tr>")
        out.append("</tbody></table></div>")
        return "".join(out)

    def figure(self, caption: str, src: str) -> str:
        if self.shots is None or not re.fullmatch(r"shots/[\w-]+\.webp", src):
            raise SystemExit(f"{self.source}: картинка {src} — ждал shots/имя.webp")
        data = (self.shots / src).read_bytes()
        width, height = webp_size(data)
        uri = "data:image/webp;base64," + base64.b64encode(data).decode("ascii")
        return (f'<figure><img src="{uri}" alt="{esc(caption)}" width="{width}" height="{height}" loading="lazy" '
                f'decoding="async"><figcaption>{self.inline(caption, True)}</figcaption></figure>')

    def list(self, lines: list[str], i: int) -> tuple[str, int]:
        first = re.match(r"(\s*)([-*]|\d+\.) ", lines[i])
        indent, ordered = len(first.group(1)), first.group(2)[0].isdigit()
        items: list[str] = []
        while i < len(lines):
            m = re.match(r"(\s*)([-*]|\d+\.) (.*)", lines[i])
            if not m or len(m.group(1)) != indent or m.group(2)[0].isdigit() != ordered:
                break
            inner_indent = indent + len(m.group(2)) + 1
            body = [m.group(3)]
            i += 1
            while i < len(lines):
                row = lines[i]
                if not row.strip():
                    # пустая строка внутри пункта — только если дальше идёт его же продолжение
                    after = next((r for r in lines[i + 1:] if r.strip()), "")
                    if len(after) - len(after.lstrip()) >= inner_indent:
                        body.append("")
                        i += 1
                        continue
                    break
                if len(row) - len(row.lstrip()) < min(inner_indent, indent + 2):
                    break
                body.append(row[inner_indent:] if row[:inner_indent].isspace() else row.lstrip())
                i += 1
            inner = self.blocks(body)
            if inner.count("<p>") == 1 and inner.startswith("<p>") and inner.endswith("</p>"):
                inner = inner[3:-4]         # пункт из одного абзаца — без отбивки
            items.append(f"<li>{inner}</li>")
            while i < len(lines) and not lines[i].strip():      # пустые строки между пунктами
                after = next((r for r in lines[i:] if r.strip()), "")
                again = re.match(r"(\s*)([-*]|\d+\.) ", after)
                if again and len(again.group(1)) == indent and again.group(2)[0].isdigit() == ordered:
                    i += 1
                else:
                    break
        tag = "ol" if ordered else "ul"
        return f"<{tag}>" + "".join(items) + f"</{tag}>", i


# ---------- страница ----------

CSS = r"""
:root {
  --bg: #eef2f7; --surface: #ffffff; --surface-2: #f8fafc;
  --ink: #0f172a; --ink-2: #4b5a6b; --ink-3: #5b6b7b;
  --line: #dde5ee; --line-2: #c8d4e0;
  --primary: #1d4ed8; --primary-ink: #ffffff; --info-bg: #dbeafe; --ok: #15803d;
  --shadow: 0 1px 2px rgba(15, 23, 42, .06), 0 1px 3px rgba(15, 23, 42, .04);
  --radius: .625rem; --gutter: 1.25rem;
  --ui: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, "Cascadia Mono", Consolas, "DejaVu Sans Mono", Menlo, monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0a0f15; --surface: #131b24; --surface-2: #0f1720;
    --ink: #e7eef6; --ink-2: #a3b3c4; --ink-3: #8fa2b5;
    --line: #22303d; --line-2: #2e3f4f;
    --primary: #6ba5fb; --primary-ink: #08111d; --info-bg: #10233d; --ok: #58d68a;
    --shadow: 0 1px 2px rgba(0, 0, 0, .4);
    color-scheme: dark;
  }
  .brand img { filter: drop-shadow(0 0 1px rgba(231, 238, 246, .55)); }
  figure img { filter: brightness(.92); }
}
@font-face {
  font-family: "Wordmark"; font-weight: 400; font-style: normal; font-display: block;
  src: url("%FONT%") format("woff2");
}
*, *::before, *::after { box-sizing: border-box; }
[hidden] { display: none !important; }
html { font-size: 100%; scroll-behavior: smooth; -webkit-text-size-adjust: 100%; scroll-padding-top: 1rem; }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
body { margin: 0; background: var(--bg); color: var(--ink); font: 1rem/1.65 var(--ui); -webkit-font-smoothing: antialiased; overflow-wrap: break-word; }
a { color: var(--primary); text-underline-offset: .19em; }
:focus-visible { outline: 3px solid var(--primary); outline-offset: 2px; border-radius: .375rem; }
.skip { position: absolute; left: .75rem; top: -5rem; z-index: 20; padding: .625rem .875rem; background: var(--primary); color: var(--primary-ink); border-radius: .5rem; text-decoration: none; }
.skip:focus { top: .75rem; }
.wrap { width: min(100% - 2 * var(--gutter), 84rem); margin-inline: auto; }

.top { border-bottom: 1px solid var(--line); background: var(--surface); }
.top-in { display: flex; align-items: center; justify-content: space-between; gap: .5rem 1.25rem; flex-wrap: wrap; min-height: 4rem; padding-block: .5rem; }
.brand { display: inline-flex; align-items: center; gap: .625rem; color: var(--ink); font: 400 2rem/1 "Wordmark", "Segoe UI", serif; white-space: nowrap; text-decoration: none; -webkit-text-stroke: .012em currentColor; }
.brand img { height: 2.25rem; width: auto; display: block; }
.top nav { display: flex; flex-wrap: wrap; gap: .25rem 1.125rem; font-size: .95rem; }
.top nav a { display: inline-flex; align-items: center; min-height: 2.75rem; }
.top nav [aria-current] { color: var(--ink); font-weight: 650; text-decoration: none; }

.layout { display: grid; grid-template-columns: 17.5rem minmax(0, 1fr); gap: 2rem; align-items: start; padding-block: 1.75rem 3rem; }
.toc { position: sticky; top: 1rem; max-height: calc(100vh - 2rem); overflow-y: auto; background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); font-size: .92rem; line-height: 1.35; }
.toc summary { padding: .75rem 1rem; font-weight: 650; cursor: pointer; min-height: 2.75rem; }
.toc ol { list-style: none; margin: 0; padding: 0 .5rem .625rem; }
.toc ol ol { padding: 0 0 .25rem .75rem; font-size: .875rem; }
.toc a { display: block; padding: .375rem .5rem; border-radius: .375rem; color: var(--ink-2); text-decoration: none; }
.toc a:hover { background: var(--surface-2); color: var(--ink); }
.toc a.on { background: var(--info-bg); color: var(--ink); font-weight: 600; }

main { min-width: 0; background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); padding: 2rem clamp(1rem, 4vw, 2.75rem) 2.5rem; }
h1, h2, h3, h4 { line-height: 1.25; text-wrap: balance; scroll-margin-top: 1rem; }
h1 { font-size: clamp(1.6rem, 1.2rem + 1.6vw, 2.2rem); letter-spacing: -.015em; margin: 0 0 .75rem; }
h2 { font-size: 1.5rem; letter-spacing: -.01em; margin: 0 0 1rem; }
h3 { font-size: 1.15rem; margin: 1.75rem 0 .625rem; }
h4 { font-size: 1rem; margin: 1.25rem 0 .5rem; }
.sec { padding-top: 2rem; margin-top: 2rem; border-top: 1px solid var(--line); }
.hash { margin-left: .4em; color: var(--ink-3); text-decoration: none; opacity: 0; font-weight: 400; }
h2:hover .hash, h3:hover .hash, h4:hover .hash, .hash:focus-visible { opacity: 1; }
p, li { max-width: 46rem; }
p { margin: 0 0 .875rem; }
ul, ol { margin: 0 0 .875rem; padding-left: 1.5rem; }
li { margin: .3rem 0; }
li > p { margin-bottom: .5rem; }
li > ul, li > ol { margin: .3rem 0; }
.intro p { font-size: 1.08rem; color: var(--ink-2); }
.ver { color: var(--ink-3); font-size: .9rem; }
code { font-family: var(--mono); font-size: .875em; background: var(--surface-2); border: 1px solid var(--line); border-radius: .3rem; padding: .05em .3em; overflow-wrap: anywhere; }
.note { border-left: 3px solid var(--primary); background: var(--info-bg); border-radius: 0 var(--radius) var(--radius) 0; padding: .75rem 1rem; margin: 0 0 1rem; max-width: 46rem; }
.note p:last-child { margin-bottom: 0; }

.code { margin: 0 0 1rem; border: 1px solid var(--line-2); border-radius: var(--radius); background: var(--surface-2); overflow: hidden; }
.code-head { display: flex; align-items: center; justify-content: space-between; gap: .5rem; min-height: 2.25rem; padding: .125rem .25rem .125rem .875rem; border-bottom: 1px solid var(--line); color: var(--ink-3); font-size: .8rem; }
.copy { font: 600 .8rem/1 var(--ui); color: var(--primary); background: none; border: 1px solid transparent; border-radius: .375rem; padding: 0 .625rem; min-height: 2rem; cursor: pointer; }
.copy:hover { border-color: var(--line-2); background: var(--surface); }
.copy.done { color: var(--ok); }
pre { margin: 0; padding: .75rem .875rem; overflow-x: auto; font: .85rem/1.6 var(--mono); tab-size: 4; }
pre code { background: none; border: 0; padding: 0; font-size: inherit; overflow-wrap: normal; white-space: pre; }

.tbl { overflow-x: auto; margin: 0 0 1rem; border: 1px solid var(--line-2); border-radius: var(--radius); -webkit-overflow-scrolling: touch; }
table { border-collapse: collapse; width: 100%; font-size: .92rem; line-height: 1.5; }
table.wide { min-width: 44rem; }
table.pair { min-width: 26rem; }
th, td { text-align: left; vertical-align: top; padding: .5rem .75rem; border-bottom: 1px solid var(--line); }
th { background: var(--surface-2); font-weight: 650; position: sticky; top: 0; }
tbody tr:last-child td { border-bottom: 0; }
td:first-child code { white-space: nowrap; overflow-wrap: normal; }

figure { margin: 1.25rem 0 1.5rem; }
figure img { display: block; max-width: 100%; height: auto; border: 1px solid var(--line-2); border-radius: var(--radius); box-shadow: var(--shadow); background: #fff; }
.js figure img { cursor: zoom-in; }
.zoom { border: 0; padding: 0; margin: 0; background: transparent; width: 100vw; height: 100vh; height: 100dvh; max-width: none; max-height: none; overflow: auto; cursor: zoom-out; }
.zoom[open] { display: grid; place-items: center; }
.zoom::backdrop { background: rgba(5, 9, 14, .86); }
.zoom img { display: block; max-width: none; border-radius: .5rem; background: #fff; }
.zoom button { position: fixed; top: .75rem; right: .75rem; width: 2.75rem; height: 2.75rem; border: 1px solid var(--line-2); border-radius: .5rem; background: var(--surface); color: var(--ink); font: 400 1.5rem/1 var(--ui); cursor: pointer; }
figcaption { margin-top: .5rem; font-size: .9rem; color: var(--ink-3); max-width: 46rem; }
.foot { margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid var(--line); color: var(--ink-3); font-size: .9rem; }

@media (max-width: 62rem) {
  .layout { grid-template-columns: minmax(0, 1fr); gap: 1rem; padding-top: 1rem; }
  .toc { position: static; max-height: none; }
  .toc[open] { max-height: 70vh; }
  .toc a { padding-block: .625rem; }
  .copy { min-height: 2.75rem; }
  .code-head { min-height: 2.75rem; }
}
@media (max-width: 30rem) {
  :root { --gutter: .75rem; }
  .brand { font-size: 1.6rem; }
  .brand img { height: 1.9rem; }
  h2 { font-size: 1.3rem; }
}
@media print {
  :root { --bg: #fff; --surface: #fff; --surface-2: #f6f8fa; --ink: #000; --ink-2: #222; --ink-3: #444; --line: #ccc; --line-2: #bbb; --primary: #000; --info-bg: #f2f4f7; color-scheme: light; }
  @page { margin: 16mm 14mm; }
  html { font-size: 10.5pt; }
  body { background: #fff; }
  .toc, .top nav, .copy, .skip, .hash, .zoom { display: none !important; }
  .top { border: 0; }
  .wrap { width: 100%; }
  .layout { display: block; padding: 0; }
  main { border: 0; box-shadow: none; padding: 0; }
  p, li, .note, figcaption { max-width: none; }
  a { color: inherit; text-decoration: none; }
  .sec { border-top: 0; margin-top: 1.5rem; padding-top: 0; }
  .sec ~ .sec { margin-top: 0; break-before: page; }
  h2, h3, h4 { break-after: avoid; }
  .code, figure, tr, .note { break-inside: avoid; }
  .code-head { min-height: 0; padding: .2rem .6rem; }
  pre code { white-space: pre-wrap; overflow-wrap: anywhere; }
  pre { overflow: visible; }
  .tbl { overflow: visible; }
  table.wide, table.pair { min-width: 0; font-size: 8.5pt; }
  th { position: static; }
  td:first-child code { white-space: normal; overflow-wrap: anywhere; }
  figure img { box-shadow: none; filter: none; max-height: 150mm; width: auto; }
}
"""

JS = r"""
(function () {
  var toc = document.getElementById("toc"), wide = window.matchMedia("(min-width: 62.01rem)");
  function fit() { toc.open = wide.matches; }
  fit();
  if (wide.addEventListener) wide.addEventListener("change", fit);
  toc.addEventListener("click", function (e) { if (e.target.closest("a") && !wide.matches) toc.open = false; });

  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (done, fail) {
      var box = document.createElement("textarea");
      box.value = text; box.setAttribute("readonly", ""); box.style.position = "fixed"; box.style.opacity = "0";
      document.body.appendChild(box); box.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(box);
      ok ? done() : fail();
    });
  }
  document.querySelectorAll(".copy").forEach(function (btn) {
    btn.hidden = false;
    btn.addEventListener("click", function () {
      var code = btn.closest(".code").querySelector("code").textContent;
      copy(code).then(function () { say("Скопировано", true); }, function () { say("Не вышло — выделите текст", false); });
      function say(text, ok) {
        btn.textContent = text; btn.classList.toggle("done", ok);
        setTimeout(function () { btn.textContent = "Скопировать"; btn.classList.remove("done"); }, 2000);
      }
    });
  });

  // снимок экрана по щелчку открывается в полный размер: на телефоне его можно двигать пальцем
  var shots = document.querySelectorAll("figure img");
  if (shots.length && window.HTMLDialogElement) {
    document.documentElement.classList.add("js");
    var box = document.createElement("dialog"), big = document.createElement("img"), shut = document.createElement("button");
    box.className = "zoom"; shut.type = "button"; shut.setAttribute("aria-label", "Закрыть снимок"); shut.textContent = "×";
    box.appendChild(shut); box.appendChild(big); document.body.appendChild(box);
    box.addEventListener("click", function () { box.close(); });
    shots.forEach(function (img) {
      img.tabIndex = 0; img.setAttribute("role", "button"); img.setAttribute("aria-label", "Увеличить снимок: " + img.alt);
      function show() { big.src = img.src; big.alt = img.alt; big.width = img.naturalWidth; big.height = img.naturalHeight; box.showModal(); }
      img.addEventListener("click", show);
      img.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); show(); } });
    });
  }

  // в оглавлении подсвечен раздел, который сейчас на экране
  if (!("IntersectionObserver" in window)) return;
  var links = {}, current = null;
  toc.querySelectorAll("a").forEach(function (a) { links[decodeURIComponent(a.getAttribute("href").slice(1))] = a; });
  var seen = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      if (current) { current.classList.remove("on"); current.removeAttribute("aria-current"); }
      current = links[entry.target.id];
      if (!current) return;
      current.classList.add("on"); current.setAttribute("aria-current", "location");
      // на широком экране оглавление стоит на месте и прокручивается само: держим текущий пункт на виду
      if (wide.matches) {
        var top = current.offsetTop - toc.offsetTop;
        if (top < toc.scrollTop + 48 || top > toc.scrollTop + toc.clientHeight - 48) toc.scrollTop = top - toc.clientHeight / 2;
      }
    });
  }, { rootMargin: "0px 0px -75% 0px" });
  document.querySelectorAll("main h2[id], main h3[id]").forEach(function (h) { seen.observe(h); });
})();
"""


def toc(heads: list[tuple[int, str, str]]) -> str:
    """Оглавление: разделы и их подразделы. Заголовки четвёртого уровня в него не идут."""
    out, inside = ["<ol>"], False
    for level, anchor, title in heads:
        if level > 3:
            continue
        item = f'<li><a href="#{esc(anchor)}">{title}</a>'
        if level == 2:
            out.append(("</ol></li>" if inside else "</li>" if len(out) > 1 else "") + item)
            inside = False
        else:
            out.append(("</li>" if inside else "<ol>") + item)
            inside = True
    out.append("</li></ol></li></ol>" if inside else "</li></ol>" if len(out) > 1 else "</ol>")
    return "".join(out)


def sections(body: str) -> tuple[str, str]:
    """Текст до первого раздела — вступление; дальше каждый раздел второго уровня — свой блок
    (в печати раздел начинается с новой страницы)."""
    parts = re.split(r"(?=<h2 id=)", body)
    intro = parts[0].strip()
    return intro, "\n".join(f'<section class="sec">\n{part.strip()}\n</section>' for part in parts[1:])


HERE = ' aria-current="page"'


def page(doc: Doc, *, name: str, nav: list[tuple[str, str, bool]], note: str, description: str) -> str:
    logo = data_uri(STATIC / "logo-small.png", "image/png")
    icon = data_uri(STATIC / "favicon.png", "image/png")
    font = data_uri(STATIC / "fonts" / "Vertograd.woff2", "font/woff2")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    intro, body = sections(doc.body)
    links = "".join(f'<a href="{esc(href)}"{HERE if here else ""}>{esc(label)}</a>' for label, href, here in nav)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Подьячий — {esc(name)}</title>
<meta name="description" content="{esc(description)}">
<meta name="color-scheme" content="light dark">
<link rel="icon" type="image/png" href="{icon}">
<style>{CSS.replace("%FONT%", font)}</style>
</head>
<body>
<a class="skip" href="#text">К тексту</a>
<header class="top"><div class="wrap top-in">
  <span class="brand"><img src="{logo}" alt="" width="36" height="36">Подьячий</span>
  <nav aria-label="Документы">{links}</nav>
</div></header>
<div class="wrap layout">
<details class="toc" id="toc"><summary>Содержание</summary>
<nav aria-label="Содержание">{toc(doc.heads)}</nav>
</details>
<main id="text">
<h1>{esc(doc.title)}</h1>
<div class="intro">
{intro}
</div>
<p class="ver">{esc(note)} Версия сервиса {esc(version)}.</p>
{body}
<p class="foot">Подьячий {esc(version)} — открытый код под лицензией MIT. Автор — Доронченко Егор.</p>
</main>
</div>
<script>{JS}</script>
</body>
</html>
"""


def drop_contents(doc_body: str) -> str:
    return re.sub(r"<p>Содержание:.*?</p>\n?", "", doc_body, count=1, flags=re.S)


def site_nav(here: str) -> list[tuple[str, str, bool]]:
    return [(label, href, href == here) for label, href in SITE_NAV]


def unlink_siblings(body: str) -> str:
    """Ссылка на соседний файл сайта становится обычным текстом."""
    return re.sub(r'<a href="podyachiy[\w-]*\.html(?:#[^"]*)?">(.*?)</a>', r"\1", body)


def build() -> dict[Path, str]:
    """Все собранные файлы: путь → содержимое."""
    install = Doc(INSTALL_SRC)
    install.body = drop_contents(install.body)      # строка «Содержание: …» нужна в Markdown; здесь оглавление своё
    what = "Инструкция по установке и сопровождению сервиса «Подьячий»: Docker, настройки, сеть, HTTPS, почта, вход через домен, резервная копия."
    out = {INSTALL_SITE: page(install, name="установка и сопровождение", description=what,
                              note="Для администратора сервиса.", nav=site_nav("podyachiy-install.html"))}
    install.body = unlink_siblings(install.body)    # в сервисе файлов сайта рядом нет
    out[INSTALL_APP] = page(install, name="установка и сопровождение", description=what,
                            note="Для администратора сервиса.",
                            nav=[("В сервис", "/", False), ("Справка", "/help", False),
                                 ("Инструкция по установке", "/install-guide", True)])
    out[PERSONAL_SITE] = page(
        Doc(PERSONAL_SRC), name="установить для себя", note="Для одного человека на своём компьютере.",
        description="Как поставить сервис «Подьячий» для себя: Docker Desktop, ключ поставщика, две строки в .env, запуск.",
        nav=site_nav("podyachiy-personal.html"))
    if GUIDE_SRC.is_file():
        guide = Doc(GUIDE_SRC, GUIDE_SRC.parent)
        out[GUIDE_SITE] = page(
            guide, name="руководство пользователя", note="Люди, совещания и суммы на снимках вымышлены.",
            description="Руководство пользователя сервиса «Подьячий»: от загрузки записи до протокола, поручений и доступа коллегам.",
            nav=site_nav("podyachiy-guide.html"))
    return out


def main() -> int:
    check = "--check" in sys.argv[1:]
    stale = []
    for path, text in build().items():
        if check:
            if not path.is_file() or path.read_bytes() != text.encode("utf-8"):
                stale.append(path.relative_to(ROOT).as_posix())
        else:
            path.write_bytes(text.encode("utf-8"))
            print(f"{path.relative_to(ROOT).as_posix()}: {len(text.encode('utf-8')) / 1024:.0f} КБ")
    if stale:
        print("устарели: " + ", ".join(stale) + " — пересоберите: python tools/docs_build.py")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
