"""Шаблоны протоколов, задание на разбор и словарь терминов — из файлов,
не из кода.

Читаются заново при запуске каждого задания: администратор правит шаблон на странице
«Управление → Настройки» или prompts/meeting.md в обычном редакторе, и следующее
задание идёт уже по новому тексту, без пересборки образа и без перезапуска контейнера.

Типы шаблонов — данные. Четыре стандартных заданы здесь (TEMPLATES); свои типы
администратор добавляет на странице, они записаны в prompts/types.json рядом с файлами
шаблонов. Всё лежит в одной папке prompts: её правят и файлами, в обход страницы, поэтому
страница показывает то, что действительно лежит в файлах, а испорченный файл не роняет
ни её, ни обработку.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from pathlib import Path

TEMPLATES = {
    # actions — перед протоколом отдельным шагом выписываются поручения (app/actions.py):
    # шаблон совещания рассчитан на этот список, остальные обходятся без него.
    # when — пояснение для страницы; признаки, по которым разбор отличает стандартные
    # типы друг от друга, написаны в задании на разбор (triage.md, правило 4).
    "meeting": {"title": "Совещание", "file": "meeting.md", "actions": True,
                "when": "Несколько человек обсуждают рабочие вопросы и договариваются."},
    "lecture": {"title": "Лекция", "file": "lecture.md",
                "when": "Один говорит долго, остальные только задают вопросы."},
    "call": {"title": "Созвон с заказчиком", "file": "call.md",
             "when": "Разговор двух сторон: одна заказывает, принимает работу или ставит условия."},
    "spec": {"title": "Постановка задачи", "file": "spec.md",
             "when": "Разговор от начала до конца об одной вещи, которую предстоит сделать: участники проектируют."},
}

GLOSSARY_FILE = "glossary.txt"
TRIAGE_FILE = "triage.md"
ACTIONS_FILE = "actions.md"
TYPES_FILE = "types.json"
HISTORY_DIR = "history"          # прошлые версии файлов — рядом, в той же папке prompts
KEEP = 2                         # сколько прошлых версий одного файла хранится
MAX_CHARS = 60_000               # шаблон уходит модели с каждой записью целиком
UPLOAD_MAX_BYTES = 256 * 1024    # файл шаблона при загрузке; 60 тысяч знаков кириллицы — около 120 КБ
TITLE_MAX, WHEN_MAX = 60, 400

# Служебные файлы той же папки: не типы записей, но тоже задания модели и словарь.
SERVICE = {
    "triage": {"title": "Задание на разбор записи", "file": TRIAGE_FILE,
               "when": "Каждая запись: тип, название, темы, кто скрывается за метками S1, S2."},
    "actions": {"title": "Задание на выписку поручений", "file": ACTIONS_FILE,
                "when": "Отдельный шаг перед протоколом — у типов, где он включён (совещание)."},
    "glossary": {"title": "Словарь терминов", "file": GLOSSARY_FILE,
                 "when": "Термины, названия систем, фамилии — как их писать. Уходит в разбор, поручения "
                         "и протокол; распознаванию речи — только у модели, которая принимает подсказки. "
                         "Это общий словарь на все записи; участников и сокращения отдельной папки или "
                         "записи вписывают в её словарь — кнопка «Словарь» в папке и в открытой записи."},
}
KEY_RE = re.compile(r"[a-z][a-z0-9_]{1,29}")

# Разделы, которые читают другие части сервиса, — теми же выражениями, что там
# (external.tasks_at, notify.brief): шаблон без них работает, но часть сервиса остаётся пустой.
TASKS_RE = re.compile(r"^#+\s*(\d+\.\s*)?Задачи\s*$", re.M)
BRIEF_RE = re.compile(r"^#{1,4}\s*(?:\d+[.)]\s*)?Кратко", re.M)
PEOPLE_RE = re.compile(r"^#{1,4}\s*(?:\d+[.)]\s*)?Участники", re.M)
SECTION_RE = re.compile(r"^##\s+\S", re.M)
PLACEHOLDER_RE = re.compile(r"\{\{\s*\w+\s*\}\}|\{(?:transcript|text|расшифровка|текст|glossary|словарь)\}", re.I)
TRIAGE_FIELDS = ("title", "confidence", "date", "gist", "topics", "speakers", "same_person")


class TemplateError(ValueError):
    """Текст не принят; сообщение — для человека, который его сохраняет."""


# ---------- типы ----------

def read_custom(prompts_dir: Path) -> tuple[dict, str]:
    """Свои типы из prompts/types.json и что с файлом не так (пусто — всё в порядке).

    Файл могут править руками: нечитаемый целиком — своих типов нет, неверная запись —
    пропущена; о том и другом страница говорит администратору. Имя файла шаблона всегда
    <ключ>.md: путь из файла не берётся, чтобы запись в нём не увела чтение из папки.
    """
    path = prompts_dir / TYPES_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}, ""
    except (OSError, ValueError) as e:
        return {}, f"файл {TYPES_FILE} не читается ({e}) — свои типы шаблонов не действуют"
    if not isinstance(raw, dict):
        return {}, f"в файле {TYPES_FILE} должен быть объект «ключ: описание» — свои типы шаблонов не действуют"
    out, skipped = {}, []
    for key, meta in raw.items():
        title = meta.get("title") if isinstance(meta, dict) else None
        if (not KEY_RE.fullmatch(str(key)) or key in TEMPLATES or key in SERVICE
                or not isinstance(title, str) or not title.strip()):
            skipped.append(str(key))
            continue
        out[key] = {"title": title.strip()[:TITLE_MAX], "file": f"{key}.md", "custom": True,
                    "when": str(meta.get("when") or "").strip()[:WHEN_MAX],
                    "actions": meta.get("actions") is True}
    return out, (f"в файле {TYPES_FILE} пропущены неверные записи: {', '.join(skipped)}" if skipped else "")


def _filled(path: Path) -> bool:
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False


def types(prompts_dir: Path) -> dict:
    """Все типы записей, из которых выбирает разбор и ручной запуск: стандартные и свои.
    Свой тип без файла шаблона сюда не попадает — выбрать его значило бы оставить запись
    без протокола."""
    custom, _ = read_custom(prompts_dir)
    return {**TEMPLATES, **{k: v for k, v in custom.items() if _filled(prompts_dir / v["file"])}}


def _write_custom(prompts_dir: Path, custom: dict) -> None:
    data = {key: {"title": m["title"], "when": m["when"], "actions": bool(m.get("actions"))}
            for key, m in custom.items()}
    _write(prompts_dir / TYPES_FILE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ("a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                     "t", "u", "f", "h", "c", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya")))


def slug(title: str) -> str:
    """Ключ типа из названия: «Разбор инцидента» → razbor_incidenta."""
    latin = "".join(TRANSLIT.get(ch, ch) for ch in title.lower())
    key = re.sub(r"[^a-z0-9]+", "_", latin).strip("_")
    key = re.sub(r"^[^a-z]+", "", key)[:30].rstrip("_")
    return key


def add_type(prompts_dir: Path, key: str, title: str, when: str, text: str, actions: bool) -> str:
    """Новый свой тип: запись в types.json и файл шаблона. Отдаёт ключ."""
    title, when = " ".join(title.split()), " ".join(when.split())
    key = (key or "").strip().lower() or slug(title)
    if not title:
        raise TemplateError("укажите название типа")
    if len(title) > TITLE_MAX:
        raise TemplateError(f"название типа — до {TITLE_MAX} знаков")
    if not KEY_RE.fullmatch(key):
        raise TemplateError("ключ типа — латинские буквы, цифры и подчёркивание, от 2 до 30 знаков, "
                            "первая — буква; из такого названия сервис ключ составить не смог — задайте его сами")
    if len(when) < 20:
        raise TemplateError("опишите одним-двумя предложениями, когда применять этот тип: по этому описанию "
                            "разбор записи выбирает шаблон")
    if len(when) > WHEN_MAX:
        raise TemplateError(f"описание «когда применять» — до {WHEN_MAX} знаков")
    custom, problem = read_custom(prompts_dir)
    if problem and not custom and (prompts_dir / TYPES_FILE).exists():
        raise TemplateError(problem + f". Исправьте или удалите файл {TYPES_FILE} в папке шаблонов")
    if key in TEMPLATES or key in SERVICE or key in custom:
        raise TemplateError(f"тип с ключом «{key}» уже есть — выберите другой ключ")
    if any(m["title"].lower() == title.lower() for m in types(prompts_dir).values()):
        raise TemplateError(f"тип с названием «{title}» уже есть")
    save(prompts_dir, f"{key}.md", text)
    custom[key] = {"title": title, "when": when, "actions": actions}
    _write_custom(prompts_dir, custom)
    return key


def change_type(prompts_dir: Path, key: str, title: str, when: str, actions: bool) -> None:
    """Название, «когда применять» и шаг поручений своего типа. Стандартные типы заданы кодом."""
    custom, _ = read_custom(prompts_dir)
    if key not in custom:
        raise TemplateError("такого своего типа нет: менять описание можно только у типов, добавленных на странице")
    title, when = " ".join(title.split()), " ".join(when.split())
    if not title or len(title) > TITLE_MAX:
        raise TemplateError(f"название типа — от одного до {TITLE_MAX} знаков")
    if not 20 <= len(when) <= WHEN_MAX:
        raise TemplateError(f"описание «когда применять» — одно-два предложения, от 20 до {WHEN_MAX} знаков")
    if any(m["title"].lower() == title.lower() for k, m in types(prompts_dir).items() if k != key):
        raise TemplateError(f"тип с названием «{title}» уже есть")
    custom[key].update(title=title, when=when, actions=actions)
    _write_custom(prompts_dir, custom)


def remove_type(prompts_dir: Path, key: str) -> None:
    """Убирает свой тип. Текст шаблона уходит в прошлые версии — не пропадает."""
    custom, _ = read_custom(prompts_dir)
    if key not in custom:
        raise TemplateError("удалить можно только свой тип; стандартные шаблоны меняют и возвращают, но не удаляют")
    path = prompts_dir / custom[key]["file"]
    if path.is_file():
        _keep(prompts_dir, path)
        path.unlink()
    del custom[key]
    _write_custom(prompts_dir, custom)


# ---------- файлы: чтение, сохранение, прошлые версии ----------

def file_of(prompts_dir: Path, item: str) -> str:
    """Имя файла по идентификатору: ключ типа либо triage, actions, glossary."""
    meta = SERVICE.get(item) or types_listed(prompts_dir).get(item)
    if meta is None:
        raise TemplateError("такого шаблона нет")
    return meta["file"]


def types_listed(prompts_dir: Path) -> dict:
    """Типы для страницы: как types(), но вместе со своими типами, у которых пропал файл."""
    return {**TEMPLATES, **read_custom(prompts_dir)[0]}


def kind_of(item: str) -> str:
    return item if item in SERVICE else "type"


def digest(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()[:16]


def read(path: Path) -> str | None:
    """Текст файла как он есть; None — файла нет. Не UTF-8 — текст с заменой нечитаемых знаков:
    страница покажет, что лежит в файле, и даст его исправить."""
    try:
        return _text(path.read_bytes())
    except FileNotFoundError:
        return None


def _text(data: bytes) -> str:
    """Байты файла → текст для страницы: без метки порядка байтов, переводы строк — одним знаком
    (файл могли править в Блокноте)."""
    return data.decode("utf-8-sig", "replace").replace("\r\n", "\n")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _versions(prompts_dir: Path, name: str) -> list[Path]:
    """Прошлые версии файла, свежие первыми: history/<имя>.<ГГГГММДД-ЧЧММСС>.bak"""
    found = []
    for p in (prompts_dir / HISTORY_DIR).glob(f"{name}.*.bak"):
        m = re.fullmatch(r"(\d{8}-\d{6})(?:-(\d+))?", p.name[len(name) + 1:-4])
        if m:
            found.append(((m.group(1), int(m.group(2) or 1)), p))       # вторая часть — сохранения в одну секунду
    return [p for _, p in sorted(found, key=lambda pair: pair[0], reverse=True)]


def _keep(prompts_dir: Path, path: Path) -> None:
    """Текущий файл — в прошлые версии; лишние старые версии стираются."""
    name, stamp = path.name, datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # два сохранения в одну секунду: следующая версия получает номер после метки времени
    same = [int(p.name[len(name) + 1:-4][16:] or 1) for p in _versions(prompts_dir, name)
            if p.name[len(name) + 1:-4].startswith(stamp)]
    target = prompts_dir / HISTORY_DIR / (f"{name}.{stamp}-{max(same) + 1}.bak" if same else f"{name}.{stamp}.bak")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(path.read_bytes())
    for old in _versions(prompts_dir, name)[KEEP:]:
        old.unlink(missing_ok=True)


def backups(prompts_dir: Path, name: str) -> list[dict]:
    out = []
    for p in _versions(prompts_dir, name):
        stamp = p.name[len(name) + 1:-4]
        at = datetime.datetime.strptime(stamp[:15], "%Y%m%d-%H%M%S")
        out.append({"id": stamp, "at": at.isoformat(timespec="seconds"), "bytes": p.stat().st_size})
    return out


def backup_text(prompts_dir: Path, name: str, stamp: str) -> str:
    for p in _versions(prompts_dir, name):
        if p.name[len(name) + 1:-4] == stamp:
            return _text(p.read_bytes())
    raise TemplateError("такой прошлой версии нет — она могла быть вытеснена более новой")


def save(prompts_dir: Path, name: str, text: str) -> bool:
    """Пишет файл, прежний текст оставляет в прошлых версиях. Отдаёт False, если текст тот же."""
    path = prompts_dir / name
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n") + "\n"
    old = read(path)
    if old is not None and old == text:
        return False
    if old is not None:
        _keep(prompts_dir, path)
    _write(path, text)
    return True


def decode(data: bytes) -> str:
    """Загруженный файл → текст. Только UTF-8: другую кодировку сервис не угадывает —
    угаданная неверно превратила бы шаблон в кашу, и это увидели бы уже в протоколах."""
    if len(data) > UPLOAD_MAX_BYTES:
        raise TemplateError(f"файл больше {UPLOAD_MAX_BYTES // 1024} КБ — шаблон такого размера сервис не принимает")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise TemplateError("файл не в кодировке UTF-8. Сохраните его как «UTF-8» (в Блокноте: Файл → "
                            "Сохранить как → Кодировка) и загрузите снова")


# ---------- проверка текста ----------

def check(kind: str, text: str) -> list[str]:
    """Проверяет текст перед сохранением. Обязательное — отказ (TemplateError); желательное —
    список предупреждений: текст сохранится, но администратор знает, что перестанет работать.

    В шаблоне нет заполнителей: сервис ничего в него не подставляет, а дописывает после него
    словарь, справку о записи и расшифровку. Поэтому обязательного мало — текст должен быть,
    быть текстом и описывать разделы протокола. Остальное — разделы, которые читают другие
    части сервиса.
    """
    body = text.strip()
    if "\x00" in text or sum(ch < " " and ch not in "\n\r\t" for ch in text) > 0:
        raise TemplateError("это не текстовый файл: в нём есть служебные знаки. Нужен обычный текст "
                            "(.md или .txt) в кодировке UTF-8")
    if len(text) > MAX_CHARS:
        raise TemplateError(f"текст длиннее {MAX_CHARS} знаков ({len(text)}): он уходит модели с каждой записью "
                            "целиком — сократите его")
    if kind == "glossary":
        useful = [ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        return [] if useful else ["В словаре нет ни одного термина (строки с # — пояснения): подсказок "
                                  "модели не будет."]
    if not body:
        raise TemplateError("текст пустой: без задания модели записи останутся без протокола")
    warnings = []
    if PLACEHOLDER_RE.search(text):
        warnings.append("В тексте есть заполнитель в фигурных скобках. Сервис ничего не подставляет: "
                        "расшифровку, словарь и справку о записи он дописывает после шаблона сам, "
                        "а скобки уйдут модели как есть.")
    if kind == "triage":
        if "type" not in text or "json" not in text.lower():
            raise TemplateError("в задании на разбор должно быть сказано вернуть JSON с полем type: по нему "
                                "сервис выбирает шаблон, и без него протоколы не делаются вовсе")
        missing = [f for f in TRIAGE_FIELDS if f not in text]
        if missing:
            warnings.append("В задании не названы поля ответа: " + ", ".join(missing) + ". Без title у записей "
                            "не будет названия, без speakers — имён говорящих, без confidence каждая запись "
                            "будет помечена «тип определён неуверенно».")
        return warnings
    if kind == "actions":
        if "|" not in text:
            warnings.append("В задании нет образца таблицы: список поручений попадает в протокол как есть, "
                            "и шаблон совещания ждёт его таблицей.")
        return warnings

    if not SECTION_RE.search(text):
        raise TemplateError("в шаблоне не описаны разделы протокола: нет ни одной строки, начинающейся с «## ». "
                            "По разделам сервис строит карту протокола, оглавление и документ для скачивания. "
                            "Возьмите за основу стандартный шаблон")
    if not TASKS_RE.search(text):
        warnings.append("Нет раздела «Задачи» (строка вида «## 5. Задачи»): реестр поручений по таким записям "
                        "будет пустым, отметки «выполнено», сроки и блок «Поручения прошлого совещания» "
                        "для них не работают.")
    elif not re.search(r"^\|.*[Зз]адача.*\|", re.split(r"^#", text[TASKS_RE.search(text).end():], 1, flags=re.M)[0], re.M):
        warnings.append("В разделе «Задачи» нет образца таблицы с колонкой «Задача»: поручения читаются "
                        "из таблицы, списком они в реестр не попадут.")
    if not BRIEF_RE.search(text):
        warnings.append("Нет раздела «Кратко»: в письме о готовности и в письме с протоколом вместо него "
                        "будет одна строка — суть записи.")
    if not PEOPLE_RE.search(text):
        warnings.append("Нет раздела «Участники»: в протоколе не будет списка, кто был на встрече.")
    if "чч:мм:сс" not in text:
        warnings.append("Нет требования ставить метки времени [чч:мм:сс]: пункты протокола не будут вести "
                        "к месту в записи.")
    return warnings


# ---------- чтение для обработки ----------

def load_template(prompts_dir: Path, key: str) -> tuple[str, str]:
    """Возвращает (заголовок, текст шаблона). Ключ проверяется по списку типов."""
    meta = types_listed(prompts_dir).get(key)
    if meta is None:
        raise ValueError(f"неизвестный шаблон: {key}")
    path = prompts_dir / meta["file"]
    if not path.is_file():
        raise RuntimeError(f"шаблон не найден: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"шаблон пустой: {path}")
    return meta["title"], text


def load_triage(prompts_dir: Path, kinds: dict | None = None) -> str:
    """Задание на разбор записи: тип, название, кто есть кто.

    Читается так же заново, как и шаблоны протоколов. Перечень допустимых типов дописывается
    кодом — из списка типов, и расходиться с ним разбор не должен. У своих типов рядом
    стоит «когда применять»: признаки стандартных написаны в самом задании, а про свои
    модель знает только то, что написал администратор.
    """
    path = prompts_dir / TRIAGE_FILE
    if not path.is_file():
        raise RuntimeError(f"задание на разбор не найдено: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"задание на разбор пустое: {path}")
    kinds = types(prompts_dir) if kinds is None else kinds
    listing = "\n".join(f"- {key} — {meta['title']}" + (f": {meta['when']}" if meta.get("custom") else "")
                        for key, meta in kinds.items())
    own = ("\nУ типа с пояснением после двоеточия признаки — в этом пояснении: выбирай его, если запись "
           "подходит под пояснение лучше, чем под признаки остальных типов."
           if any(m.get("custom") for m in kinds.values()) else "")
    return f"{text}\n\nДопустимые значения type (другие недопустимы):\n{listing}{own}"


def load_actions(prompts_dir: Path) -> str:
    """Задание на выписку поручений — читается заново, как и шаблоны."""
    path = prompts_dir / ACTIONS_FILE
    if not path.is_file():
        raise RuntimeError(f"задание на поручения не найдено: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"задание на поручения пустое: {path}")
    return text


def load_glossary(prompts_dir: Path) -> str:
    """Словарь: термины, фамилии, названия систем — как их правильно писать.

    Распознавание слышит «scood» вместо «СКУД». Словарь уходит текстовой
    модели — в разбор, поручения и протокол, — и она пишет искажённое слово
    так, как оно стоит в словаре. Готовая расшифровка при этом не правится: она
    остаётся первоисточником. Самому распознаванию словарь подсказывает термины
    только у модели, которая это умеет (speech.glossary_terms, Engine.takes_terms).

    Строки, начинающиеся с #, — пояснения для человека и в модель не идут.
    Возвращает готовый блок для запроса или пустую строку, если словаря нет.
    """
    path = prompts_dir / GLOSSARY_FILE
    if not path.is_file():
        return ""
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    useful = [ln for ln in lines if ln and not ln.startswith("#")]
    if not useful:
        return ""
    return ("\n\n---\nСЛОВАРЬ (термины, названия систем и фамилии, которые звучат на этих "
            "встречах; распознавание речи их коверкает). Если слово в расшифровке по "
            "звучанию и по смыслу фразы — искажённый термин из словаря, пиши его так, как "
            "в словаре. Слова из словаря, которых в записи нет, не добавляй.\n\n"
            + "\n".join(useful))
