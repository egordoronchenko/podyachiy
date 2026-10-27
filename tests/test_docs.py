"""Документация одним файлом: инструкция по установке и руководство пользователя.

Источник — Markdown (install/README.md, docs/guide/README.md), собранные файлы лежат в репозитории
и в образе. Проверяется, что они не отстали от источника, что каждая внутренняя ссылка куда-то ведёт
и что файл ничего не просит из сети: его открывают с диска и из вложения письма. Экземпляр в сервисе
(/install-guide) — страница сервиса: сценарий у него отдельным файлом, шрифт и картинки — из /static/.
"""
import importlib.util
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("docs_build", ROOT / "tools" / "docs_build.py")
docs_build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docs_build)

BUILT = docs_build.build()
FILES = sorted((path for path in BUILT if path.suffix == ".html"), key=str)
SIBLINGS = {"podyachiy.html", "podyachiy-demo.html", "podyachiy-guide.html", "podyachiy-install.html",
            "podyachiy-personal.html"}
SERVICE_PAGES = {"/", "/help", "/install-guide"}


class Page(HTMLParser):
    """Что в странице может обратиться наружу и куда ведут её ссылки."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.links, self.loads, self.styles, self.in_style = set(), [], [], [], False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            assert attrs["id"] not in self.ids, f"якорь повторяется: {attrs['id']}"
            self.ids.add(attrs["id"])
        if tag == "a":
            self.links.append(attrs.get("href") or "")
        elif tag == "style":
            self.in_style = True
        else:
            # всё, что браузер загружает сам: картинки, стили, сценарии, шрифты, вложенные страницы
            self.loads += [attrs[name] for name in ("src", "href", "srcset", "poster", "data", "action") if attrs.get(name)]

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style:
            self.styles.append(data)


def parsed(path: Path) -> Page:
    page = Page()
    page.feed(path.read_text(encoding="utf-8"))
    return page


def test_all_four_documents_are_built():
    assert {path.relative_to(ROOT).as_posix() for path in BUILT} == {
        "site/podyachiy-install.html", "app/static/install-guide.html", "app/static/install-guide.js",
        "site/podyachiy-guide.html", "site/podyachiy-personal.html"}


@pytest.mark.parametrize("path", sorted(BUILT, key=str), ids=lambda p: p.name)
def test_built_file_matches_its_source(path):
    """Правили install/README.md, docs/guide/README.md, снимки или VERSION — пересоберите документы."""
    assert path.is_file(), f"нет файла {path.relative_to(ROOT)} — пересоберите: python tools/docs_build.py"
    assert path.read_bytes() == BUILT[path].encode("utf-8"), (
        f"{path.relative_to(ROOT).as_posix()} отстал от источника — пересоберите: python tools/docs_build.py")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_internal_link_resolves(path):
    page = parsed(path)
    in_service = path.parent.name == "static"
    assert len(page.ids) > (10 if path == docs_build.PERSONAL_SITE else 20)     # личная инструкция короткая
    for href in page.links:
        if href.startswith("#"):
            assert href[1:] in page.ids, f"ссылка на якорь, которого нет: {href}"
        elif re.match(r"https?://|mailto:", href):
            continue                                    # внешняя ссылка: по ней идёт человек, а не страница
        elif in_service:
            assert href in SERVICE_PAGES, f"в сервисе такой страницы нет: {href}"
        else:
            name, _, anchor = href.partition("#")
            assert name in SIBLINGS and (ROOT / "site" / name).is_file(), f"рядом такого файла нет: {href}"
            # якорь в соседнем собранном документе: личная инструкция ведёт на разделы инструкции администратора
            built = next((p for p in FILES if p.parent.name == "site" and p.name == name), None)
            if anchor and built is not None:
                assert anchor in parsed(built).ids, f"в {name} нет раздела #{anchor}"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_file_asks_nothing_from_the_network(path):
    """Картинки, шрифт и значок вшиты; сценарий и стили — внутри; адресов в стилях и сценарии нет.
    У экземпляра в сервисе всё это — файлы самого сервиса (/static/): встроенный сценарий его
    страница не исполнит (Content-Security-Policy)."""
    page = parsed(path)
    own = "/static/" if path == docs_build.INSTALL_APP else "data:"
    assert page.loads and all(value.startswith(own) for value in page.loads), [v[:60] for v in page.loads if not v.startswith(own)]
    for css in page.styles:
        assert all(url.strip("\"'").startswith(own) for url in re.findall(r"url\(([^)]+)\)", css))
        assert "@import" not in css
    text = path.read_text(encoding="utf-8")
    if path == docs_build.INSTALL_APP:
        assert '<script src="/static/install-guide.js"></script>' in text and "<script>" not in text
        script = docs_build.INSTALL_APP_JS.read_text(encoding="utf-8")
        assert all((docs_build.STATIC / value.removeprefix("/static/")).is_file() for value in page.loads)
    else:
        script = re.search(r"<script>(.*?)</script>", text, flags=re.S).group(1)
    assert not re.search(r"https?://|fetch\(|XMLHttpRequest|import\(|new Image|sendBeacon", script)
    assert text.count("<script") == 1 and "<link rel=\"stylesheet\"" not in text and "<iframe" not in text


def test_install_guide_keeps_every_section_of_the_readme():
    source = docs_build.INSTALL_SRC.read_text(encoding="utf-8")
    heads = [line for line in source.split("\n") if line.startswith("## ")]
    page = parsed(docs_build.INSTALL_SITE)
    assert len(heads) >= 18
    for head in heads:
        assert docs_build.slug(head[3:]) in page.ids, head
    text = docs_build.INSTALL_SITE.read_text(encoding="utf-8")
    body = re.sub(r"<pre.*?</pre>|<style>.*?</style>|<script>.*?</script>", "", text, flags=re.S)
    plain = re.sub(r"<[^>]+>", "", body)
    # разметка разобрана вся: в тексте не осталось ни звёздочек, ни обратных кавычек, ни скобок ссылок
    assert not re.search(r"\*\*|`|\]\(|^#{1,4} ", plain, flags=re.M)


def test_guide_screenshots_are_embedded_and_sized():
    text = docs_build.GUIDE_SITE.read_text(encoding="utf-8")
    shots = sorted((docs_build.GUIDE_SRC.parent / "shots").glob("*.webp"))
    figures = re.findall(r'<img src="data:image/webp;base64,[^"]+" alt="([^"]+)" width="(\d+)" height="(\d+)"', text)
    assert len(figures) >= 10 and len(figures) == len(shots), "снимок лежит в папке, но в руководстве не показан (или наоборот)"
    assert len(text.encode("utf-8")) < 4 * 2**20, "руководство разрослось: сожмите снимки"


def test_service_serves_the_install_guide_from_the_image(tmp_path):
    """Образ не возит папку site/: сервис отдаёт свой экземпляр из app/static."""
    from tests.test_web import running
    with running(tmp_path) as (_, guest):
        page = guest.get("/install-guide")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        assert page.content == docs_build.INSTALL_APP.read_bytes()
        assert 'href="/install-guide"' in guest.get("/help").text
