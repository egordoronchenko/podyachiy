from app import mindmap

MD = """<!-- протокол: модель x -->

# Протокол совещания

**Название:** Статус по проектам
**Дата:** 06.10.2026 (из имени файла)

## 1. Кратко

- Каталог: карточки заполняются. [00:01:51]

## 3. Что докладывали

**Каталог — Сидоров** [00:01:51]
- Раздел доставки готов. [00:02:37]

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Иванов** | | | |
| Показать, как оформлять заявки | сегодня | [00:31:23] | — |
| **Ответственный не назван** | | | |
| Разослать ссылку на каталог | — | [00:17:43] | в работе |

Прочерк в колонке «Срок» — срок на совещании не назвали.

## 6. Открытые вопросы

- **График планёрок.** Проводить ли планёрки реже — не решили [00:45:16], [00:46:03].
"""


def branch(tree, name):
    return next(s for s in tree["c"] if s["t"] == name)


def test_title_meta_and_sections():
    tree = mindmap.parse(MD)
    assert tree["t"] == "Статус по проектам"
    assert tree["s"] == "Дата: 06.10.2026 (из имени файла)"
    assert [s["t"] for s in tree["c"]] == ["Кратко", "Что докладывали", "Задачи", "Открытые вопросы"]


def test_tasks_are_grouped_by_person():
    tasks = branch(mindmap.parse(MD), "Задачи")
    assert [g["t"] for g in tasks["c"]] == ["Иванов", "Ответственный не назван"]
    first = tasks["c"][0]["c"][0]
    assert first["t"] == "Показать, как оформлять заявки"
    assert first["s"] == "срок: сегодня"
    assert first["ts"] == ["00:31:23"]
    assert tasks["c"][1]["c"][0]["s"] == "в работе"      # прочерк в сроке не показываем


def test_report_topic_holds_its_bullets():
    topic = branch(mindmap.parse(MD), "Что докладывали")["c"][0]
    assert topic["t"] == "Каталог — Сидоров"
    assert topic["ts"] == ["00:01:51"]
    assert topic["c"][0]["t"] == "Раздел доставки готов"


def test_bold_lead_becomes_title_and_rest_is_detail():
    q = branch(mindmap.parse(MD), "Открытые вопросы")["c"][0]
    assert q["t"] == "График планёрок"
    assert q["d"] == "Проводить ли планёрки реже — не решили."
    assert q["ts"] == ["00:45:16", "00:46:03"]


def test_page_is_self_contained_and_holds_the_protocol():
    page = mindmap.page(MD)
    assert "http://" not in page and "https://" not in page
    assert '<h2 id="s3">5. Задачи</h2>' in page          # якорь, на который ведёт узел карты
    assert '<tr class="who"><td colspan="4"><b>Иванов</b></td></tr>' in page   # ответственный — цветной строкой
    assert branch(mindmap.parse(MD), "Задачи")["a"] == "s3"


def test_nested_list_renders_nested():
    html = mindmap.render("## Раздел\n\n1. Шаг\n   - деталь\n2. Второй\n")
    assert html.count("<ol>") == 1 and html.count("<ul>") == 1
    assert html.index("<ul>") < html.index("Второй")


def test_transcript_goes_into_the_file_as_third_tab():
    lines = [{"start": 3.2, "who": "Иванов Иван", "text": " Добрый день. "},
             {"start": 65.0, "who": "S2", "text": "Начнём </script> с портала."},
             {"start": 70.0, "who": "S2", "text": "   "}]
    page = mindmap.page(MD, lines)
    assert '["00:00:03", "Иванов Иван", "Добрый день."]' in page
    assert r'["00:01:05", "S2", "Начнём <\/script> с портала."]' in page   # тег не рвёт скрипт страницы
    assert page.count('"S2"') == 1                                          # пустая реплика не попала


def test_without_transcript_the_tab_is_empty():
    assert "const LINES = [];" in mindmap.page(MD)


def test_protocol_text_is_embedded_for_copying_without_the_service_header():
    page = mindmap.page(MD)
    assert 'const PROTOCOL = "# Протокол совещания\\n' in page
    assert "протокол: модель x" not in page.split("const PROTOCOL = ")[1].split(";\n")[0]



def test_views_go_protocol_map_transcript_and_the_page_opens_on_the_map():
    page = mindmap.page(MD)
    tabs = page[page.index('<div class="tabs"'):page.index("</div>", page.index('<div class="tabs"'))]
    assert [tabs.index(f">{name}<") for name in ("Протокол", "Карта", "Расшифровка")] == sorted(
        tabs.index(f">{name}<") for name in ("Протокол", "Карта", "Расшифровка"))
    assert '<button id="tmap" role="tab" aria-selected="true">Карта</button>' in tabs and tabs.count('aria-selected="true"') == 1
    assert '<body data-view="map">' in page
    assert 'const TABS = { doc: "tdoc", map: "tmap", tr: "ttr" };' in page


def test_back_button_is_added_by_the_service_and_is_not_in_the_file():
    page = mindmap.page(MD)
    assert "К записи" not in page and "<a " not in page.split("<body")[1].split("<main>")[0]
    served = mindmap.with_back(page, "/?path=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B&open=a%26b.mp4")
    head = served.split("<header>")[1].split("</header>")[0]
    # первой в шапке — раньше названия и переключателя видов: с клавиатуры до неё один Tab
    assert head.lstrip().startswith('<a class="back" href="/?path=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B&amp;open=a%26b.mp4">')
    assert served.count('class="back"') == 1 and served.replace(head, "") == page.replace(page.split("<header>")[1].split("</header>")[0], "")
    # название организации и кнопка возврата дописываются независимо
    both = mindmap.with_back(mindmap.with_org(page, "Ромашка"), "/?open=a.mp4")
    assert '<span class="meta">Ромашка</span>' in both and 'href="/?open=a.mp4"' in both


def test_file_of_the_previous_page_layout_is_rebuilt(tmp_path):
    protocol = tmp_path / "вкс" / "summary" / "вкс.md"
    protocol.parent.mkdir(parents=True)
    protocol.write_text(MD, encoding="utf-8")
    target = mindmap.ensure(protocol)
    assert mindmap.STAMP in target.read_text(encoding="utf-8")[:200]
    # карта, собранная прежней версией сервиса: свежее протокола, но вид страницы старый
    target.write_text('<!doctype html>\n<html lang="ru"><head><meta charset="utf-8">старая', encoding="utf-8")
    assert "старая" not in mindmap.ensure(protocol).read_text(encoding="utf-8")
    # свежий файл не трогается
    stamp = target.stat().st_mtime_ns
    assert mindmap.ensure(protocol).stat().st_mtime_ns == stamp
