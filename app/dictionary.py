"""Словарь папки и записи: кто бывает на этих совещаниях и какие здесь сокращения.

Общий словарь сервиса (prompts/glossary.txt) один на всех и знает только слова. Но имена и
сокращения у каждого отдела свои: отчество председателя распознавание пишет тремя способами,
фамилию докладчика — двумя, название информационной системы — набором букв. Человек, который
на этих совещаниях бывает, знает всё это заранее. Здесь он записывает это один раз — на папку
(«кто обычно бывает», «какие у нас сокращения») и, если нужно, на отдельную запись («кто был
именно на этом совещании»).

Словарь записи дополняет словари её папки и папок выше, а те — общий словарь сервиса; при
совпадении побеждает ближний к записи. Что с ним делает обработка:
  распознавание — термины и имена уходят подсказками модели, которая их принимает
    (speech.fit_terms), в пределах её ограничений;
  разбор, поручения и протокол — список идёт текстовой модели отдельным блоком (block): имена
    из записи она сводит к ФИО из списка, сокращения пишет как в словаре. Список — подсказка,
    а не перечень присутствующих: того, кого в записи нет, в протокол не вписывают.

Всё лежит в базе сервиса (db.py: таблица dictionaries) — рядом с правами на папки и по той же
причине: ключом служит путь папки или записи, и при переименовании и переносе словарь переезжает
следом (move_folder, move_record), как права, — той же транзакцией. Словарь читается из базы на
каждое обращение: правка действует со следующего задания, без перезапуска.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re

from . import db

FOLDERS, RECORDS = "folders", "records"         # чей словарь: папки или отдельной записи
FOLDER, RECORD = "folder", "record"             # откуда строка действующего словаря (scope)
PEOPLE_MAX, TERMS_MAX = 200, 300                # строк в одном словаре: он уходит модели с каждой записью
NAME_MAX, ROLE_MAX = 120, 120
AKA_MAX, AKA_CHARS = 6, 80                      # «как ещё называют»: вариантов у человека и знаков в варианте
TERM_CHARS, MEANING_MAX = 80, 300

# Колонки одной строки: табуляция (строка из таблицы), тире или дефис с пробелами, черта.
# Дефис без пробелов — часть слова: «Салтыков-Щедрин», «план-факт».
_COLUMN_RE = re.compile(r"\t+|\s*\|\s*|(?:(?<=\s)|^)[—–-](?=\s|$)")
_BULLET_RE = re.compile(r"^\s*(?:\d{1,3}[.)]|[-–—•*·])\s+")        # «1. », «- », «• » в начале строки списка
_MAIL_RE = re.compile(r"[<(\[]?\s*[\w.+-]+@[\w-]+(?:\.[\w-]+)+\s*[>)\]]?")
_AKA_RE = re.compile(r"(.*?)\s*\(([^()]*)\)")                   # «ФИО (как ещё называют)»
_PATRONYMIC_RE = re.compile(r"(?:вич|ьич|вна|чна|шна|оглы|кызы)$", re.I)
# слово ФИО: с заглавной буквы, из букв, через дефис — двойная фамилия, с точкой — инициалы («П.», «П.А.»)
_NAME_WORD_RE = re.compile(r"[^\W\d_](?:[^\W\d_]|[-.](?=[^\W\d_]))*\.?")


class DictionaryError(ValueError):
    """Словарь не принят; сообщение — для человека, который его сохраняет."""


def key(text: str) -> str:
    """По чему строки словаря считаются одной и той же: без регистра, «ё» как «е», пробелы — один."""
    return " ".join(text.casefold().replace("ё", "е").split())


def _one_line(value, limit: int) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, (str, int, float)) else ""


def _columns(line: str) -> list[str]:
    return [" ".join(part.split()) for part in _COLUMN_RE.split(_BULLET_RE.sub("", line.strip()))]


def _person(name, role="", aka=()) -> dict | None:
    name = _one_line(name, NAME_MAX).strip(" ,;")
    if not name:
        return None
    seen, variants = {key(name)}, []
    for item in aka if isinstance(aka, (list, tuple)) else ():
        item = _one_line(item, AKA_CHARS).strip(" ,;")
        if item and key(item) not in seen:
            seen.add(key(item))
            variants.append(item)
    return {"name": name, "role": _one_line(role, ROLE_MAX).strip(" ,;"), "aka": variants[:AKA_MAX]}


def _term(term, meaning="") -> dict | None:
    term = _one_line(term, TERM_CHARS).strip(" ,;")
    return {"term": term, "meaning": _one_line(meaning, MEANING_MAX).strip(" ;")} if term else None


def people_from_line(line: str) -> list[dict]:
    """Строка списка участников → люди. Понимает:
    «ФИО — должность» (тире или дефис с пробелами) и «ФИО, должность»;
    «ФИО (как ещё называют, через запятую) — должность»;
    строку таблицы: ФИО, должность и «как ещё называют» через табуляцию;
    строку из приглашения на встречу: «Тихонов Павел <tikhonov@example.ru>; Юдина Анна <…>» —
    адреса отбрасываются, людей в строке столько, сколько адресов."""
    if _MAIL_RE.search(line):
        found = [_person(_MAIL_RE.sub(" ", piece).strip(" \"'«»")) for piece in line.split(";")]
        return [p for p in found if p]
    cols = _columns(line)
    name, aka = cols[0], []
    if "\t" in line.strip():
        role, aka = (cols[1] if len(cols) > 1 else ""), re.split(r"[,;]", " ".join(cols[2:]))
    else:
        # в должности тире своё («заместитель директора — начальник отдела»): всё после ФИО — она
        role = " — ".join(c for c in cols[1:] if c)
    if len(cols) == 1 and "," in name and "(" not in name:
        name, role = (part.strip() for part in name.split(",", 1))
    also = _AKA_RE.fullmatch(name)
    if also:
        name, aka = also.group(1), re.split(r"[,;]", also.group(2)) + aka
    person = _person(name, role, aka)
    return [person] if person else []


def terms_from_line(line: str) -> list[dict]:
    """Строка списка сокращений → термины: «сокращение — расшифровка» (тире, дефис с пробелами,
    знак равенства, табуляция). Строка без расшифровки и с запятыми — несколько терминов подряд,
    как в общем словаре: «СКУД, ТМЦ, план-факт»."""
    cols = _columns(line)
    if len(cols) == 1 and " = " in cols[0]:
        cols = [part.strip() for part in cols[0].split(" = ", 1)]
    if len(cols) == 1:
        return [t for t in (_term(piece) for piece in cols[0].split(",")) if t]
    term = _term(cols[0], " — ".join(c for c in cols[1:] if c))
    return [term] if term else []


def _parse(value, from_line, from_dict, what: str, limit: int, key_of) -> list[dict]:
    """Список словаря из того, что прислали: текст (строка на запись), список строк или
    список объектов. Повтор отбрасывается — остаётся первая из одинаковых строк."""
    if isinstance(value, str):
        value = value.replace("\r", "\n").split("\n")
    if not isinstance(value, list):
        raise DictionaryError(f"{what} — текстом, по строке на запись, или списком")
    out, seen = [], set()
    for item in value:
        if isinstance(item, str):
            found = from_line(item) if item.strip() else []
        elif isinstance(item, dict):
            found = [x for x in [from_dict(item)] if x]
        else:
            raise DictionaryError(f"{what}: каждая запись — строка или объект")
        for entry in found:
            if key_of(entry) not in seen:
                seen.add(key_of(entry))
                out.append(entry)
    if len(out) > limit:
        raise DictionaryError(f"{what}: не больше {limit} строк в одном словаре, прислано {len(out)} — "
                              "словарь уходит модели с каждой записью целиком")
    return out


def parse_people(value) -> list[dict]:
    return _parse(value, people_from_line,
                  lambda d: _person(d.get("name"), d.get("role") or "", d.get("aka") or ()),
                  "участники", PEOPLE_MAX, lambda p: key(p["name"]))


def parse_terms(value) -> list[dict]:
    return _parse(value, terms_from_line, lambda d: _term(d.get("term"), d.get("meaning") or ""),
                  "сокращения", TERMS_MAX, lambda t: key(t["term"]))


def joined(old: list[dict], new: list[dict], field: str, limit: int) -> list[dict]:
    """Дополнение словаря: новые строки добавляются в конец, совпавшие с прежними (по полю
    field — name или term) заменяют их на прежнем месте."""
    fresh = {key(x[field]): x for x in new}
    out = [fresh.pop(key(x[field]), x) for x in old] + list(fresh.values())
    if len(out) > limit:
        raise DictionaryError(f"в одном словаре не больше {limit} строк, с добавленными выходит {len(out)}")
    return out


def full_name(name: str) -> bool:
    """ФИО, а не просто имя или описание: от двух до четырёх слов, каждое с заглавной буквы и из
    одних букв (дефис и инициалы с точкой — можно). «Иван» модели ничего не подсказывает, а
    «представитель заказчика» и «Голос из зала» — не имена: такую подпись в словарь не берут."""
    words = name.split()
    return 2 <= len(words) <= 4 and all(_NAME_WORD_RE.fullmatch(w) and w[0].isupper() for w in words)


def known(people: list[dict], name: str) -> bool:
    """Есть ли этот человек в списке: то же ФИО или то же «как ещё называют» — без учёта регистра,
    «ё» и лишних пробелов; те же слова в другом порядке («Павел Ильич Тихонов»); или все слова
    одного есть среди слов другого («Тихонов Павел» и «Тихонов Павел Ильич»). Последнее — только
    когда слов хотя бы два: «Павел» с «Павлом Ильичом» не сводится."""
    mine = set(key(name).split())
    for person in people:
        for other in [person["name"], *person["aka"]]:
            theirs = set(key(other).split())
            if mine == theirs or (min(len(mine), len(theirs)) >= 2 and (mine <= theirs or theirs <= mine)):
                return True
    return False


def merge(layers: list[dict]) -> dict:
    """Действующий словарь из слоёв, от ближнего к записи до дальнего: ближний побеждает.
    У каждой строки — откуда она: scope (record — словарь записи, folder — папки) и path."""
    people, terms = {}, {}
    for layer in layers:
        origin = {"scope": layer["scope"], "path": layer["path"]}
        for p in layer["people"]:
            people.setdefault(key(p["name"]), {**p, **origin})
        for t in layer["terms"]:
            terms.setdefault(key(t["term"]), {**t, **origin})
    return {"people": list(people.values()), "terms": list(terms.values())}


def fingerprint(words: dict) -> str:
    """Отпечаток действующего словаря: по нему видно, что разбор записи делался с другим
    списком участников (triage.load_saved). Пустой словарь — пустая строка."""
    if not words["people"] and not words["terms"]:
        return ""
    body = [[[p["name"], p["role"], p["aka"]] for p in words["people"]],
            [[t["term"], t["meaning"]] for t in words["terms"]]]
    return hashlib.sha256(json.dumps(body, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def spoken(person: dict) -> list[str]:
    """Как человека называют вслух — для подсказки распознаванию: ФИО целиком, «имя отчество»
    и фамилия отдельно (в записи звучит «слово Тихонову» и «Павел Ильич», а не всё ФИО разом),
    затем «как ещё называют». Фамилия отделяется, только когда отчество узнаётся по окончанию."""
    words = person["name"].split()
    forms = [person["name"]]
    at = next((i for i, w in enumerate(words) if i and _PATRONYMIC_RE.search(w)), 0)
    if len(words) == 3 and at:
        forms += [f"{words[at - 1]} {words[at]}", words[2] if at == 1 else words[0]]
    return forms + person["aka"]


def hints(words: dict) -> list[str]:
    """Подсказки распознаванию из действующего словаря — в порядке важности: сначала строки
    словаря записи, затем её папки, затем папок выше. Внутри одного словаря термины идут раньше
    имён: сокращение распознавание без подсказки не напишет вовсе, а имя напишет хотя бы
    похоже. Пределы модели применяет speech.fit_terms."""
    def distance(x: dict) -> tuple:
        return x["scope"] != RECORD, -len(x["path"].split("/"))       # чем глубже папка, тем она ближе к записи

    out = []
    for near in sorted({distance(x) for x in words["terms"] + words["people"]}):
        out += [t["term"] for t in words["terms"] if distance(t) == near]
        out += [form for p in words["people"] if distance(p) == near for form in spoken(p)]
    return list(dict.fromkeys(out))


def block(words: dict, after_glossary: bool = False) -> str:
    """Блок для текстовой модели — в разбор записи, поручения и протокол; пустой словарь —
    пустая строка. Правила стоят прямо в блоке, а не только в шаблонах: шаблоны администратор
    правит и заменяет своими, а словарь должен работать и с ними.

    after_glossary — перед блоком идёт общий словарь сервиса (templates.load_glossary): при
    расхождении написания побеждает этот список, он ближе к записи."""
    if not words["people"] and not words["terms"]:
        return ""
    parts = ["\n\n---\nСПИСОК УЧАСТНИКОВ И СОКРАЩЕНИЙ (составил человек, который знает эти совещания)."]
    if words["people"]:
        parts.append(
            "Кто бывает на таких совещаниях — ФИО, должность или роль, как ещё называют. Это подсказка, "
            "а не перечень присутствующих. Имя из записи своди к ФИО из списка, только если совпадение "
            "и по звучанию, и по роли уверенное. Человека, которого в списке нет, называй так, как в "
            "записи: к похожему имени из списка не подгоняй. Человека из списка, который в записи не "
            "говорит и не упомянут, в документ не вписывай и о том, что его не было, не пиши.\n"
            + "\n".join("- " + " — ".join(x for x in (p["name"], p["role"]) if x)
                        + (f" (ещё называют: {', '.join(p['aka'])})" if p["aka"] else "")
                        for p in words["people"]))
    if words["terms"]:
        parts.append(
            "Сокращения и термины: пиши их в точности так, как здесь, — и когда распознавание записало "
            "слово по-другому. После тире — расшифровка, для понимания; в текст её без нужды не вставляй. "
            "Слова, которых в записи нет, не добавляй."
            + (" Если написание расходится со словарём выше, верь этому списку." if after_glossary else "")
            + "\n" + "\n".join(f"- {t['term']}" + (f" — {t['meaning']}" if t["meaning"] else "")
                               for t in words["terms"]))
    return "\n\n".join(parts)


def parents(folder: str) -> list[str]:
    """Папка и все папки выше неё — от ближней к дальней; корень словаря не имеет."""
    parts = folder.split("/") if folder else []
    return ["/".join(parts[:n]) for n in range(len(parts), 0, -1)]


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + "/")


def _moved(path: str, old: str, new: str) -> str:
    return new + path[len(old):] if _inside(path, old) else path


class Store:
    def __init__(self, base: db.Database):
        self.db = base

    def _saved(self, scope: str, path: str) -> dict | None:
        row = self.db.row("SELECT * FROM dictionaries WHERE scope = ? AND path = ?", scope, path)
        return row and {"people": json.loads(row["people"]), "terms": json.loads(row["terms"]),
                        "by": row["author"], "at": row["at"], "changed": row["changed"]}

    def get(self, scope: str, path: str) -> dict:
        """Словарь папки или записи: people, terms, а ещё кто и когда его менял (by, at —
        время, changed — оно же числом). Не задан — пустой."""
        return self._saved(scope, path) or {"people": [], "terms": [], "by": "", "at": "", "changed": 0}

    def put(self, scope: str, path: str, people: list[dict], terms: list[dict], by: str) -> dict:
        """Записывает словарь целиком. Пустой словарь из базы убирается."""
        now = datetime.datetime.now().astimezone()
        with self.db.write() as con:
            if people or terms:
                con.execute(
                    "INSERT INTO dictionaries (scope, path, people, terms, author, at, changed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (scope, path) DO UPDATE SET people = excluded.people, "
                    "terms = excluded.terms, author = excluded.author, at = excluded.at, changed = excluded.changed",
                    (scope, path, json.dumps(people, ensure_ascii=False), json.dumps(terms, ensure_ascii=False), by,
                     now.isoformat(timespec="seconds"), now.timestamp()))
            else:
                con.execute("DELETE FROM dictionaries WHERE scope = ? AND path = ?", (scope, path))
        return self.get(scope, path)

    def welcome(self, folder: str, names: list[str], by: str) -> list[str]:
        """Подтверждённые ФИО — в словарь папки: человек подписал говорящего, значит, этот
        человек на совещаниях папки бывает. Берётся ФИО (full_name), и только тех, кого ещё нет
        в словаре папки и папок выше (known): словарь записи — про одну встречу, и его строки
        папке не мешают. Роль не выдумывается — её допишет человек в окне словаря. Полный
        словарь берёт, сколько влезет. У корня словаря нет — там нечего пополнять.

        Пересборку протоколов это не запускает: словарь действует со следующего задания.
        Отдаёт добавленных, в порядке подписи; author словаря становится by."""
        if not folder:
            return []
        with self.db.write() as con:
            def saved(path: str) -> dict | None:
                row = con.execute("SELECT people, terms FROM dictionaries WHERE scope = ? AND path = ?",
                                  (FOLDERS, path)).fetchone()
                return row and {"people": json.loads(row["people"]), "terms": json.loads(row["terms"])}

            own = saved(folder) or {"people": [], "terms": []}
            seen = [p for f in parents(folder) for p in (saved(f) or {"people": []})["people"]]
            added = []
            for name in names:
                name = _one_line(name, NAME_MAX)
                if len(own["people"]) >= PEOPLE_MAX:
                    break
                if full_name(name) and not known(seen, name):
                    person = {"name": name, "role": "", "aka": []}
                    own["people"].append(person)
                    seen.append(person)
                    added.append(name)
            if added:
                now = datetime.datetime.now().astimezone()
                con.execute(
                    "INSERT INTO dictionaries (scope, path, people, terms, author, at, changed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (scope, path) DO UPDATE SET people = excluded.people, "
                    "author = excluded.author, at = excluded.at, changed = excluded.changed",
                    (FOLDERS, folder, json.dumps(own["people"], ensure_ascii=False),
                     json.dumps(own["terms"], ensure_ascii=False), by, now.isoformat(timespec="seconds"), now.timestamp()))
        return added

    def layers(self, file: str = "", folder: str = "") -> list[dict]:
        """Непустые слои словаря записи file (или папки folder) — от ближнего к дальнему:
        сама запись, её папка, папки выше."""
        found = [(RECORD, file, self._saved(RECORDS, file))] if file else []
        found += [(FOLDER, f, self._saved(FOLDERS, f))
                  for f in parents(file.rpartition("/")[0] if file else folder)]
        return [{"scope": scope, "path": path, "people": saved["people"], "terms": saved["terms"]}
                for scope, path, saved in found if saved]

    def effective(self, file: str) -> dict:
        """Действующий словарь записи: её собственный поверх словарей папок."""
        return merge(self.layers(file=file))

    def move_folder(self, old: str, new: str) -> None:
        """Папку переименовали — словари её самой, её подпапок и её записей переезжают следом."""
        with self.db.write() as con:
            con.execute(f"UPDATE OR REPLACE dictionaries SET path = ? || substr(path, ?) WHERE {db.below('path')}",
                        (new, len(old) + 1, *db.span(old)))

    def drop_folder(self, folder: str) -> None:
        """Папку удалили — словари её, её подпапок и её записей больше ни к чему."""
        with self.db.write() as con:
            con.execute(f"DELETE FROM dictionaries WHERE {db.below('path')}", db.span(folder))

    def move_record(self, old: str, new: str) -> None:
        """Запись переименовали или перенесли — её словарь переезжает на новый путь."""
        with self.db.write() as con:
            con.execute("UPDATE OR REPLACE dictionaries SET path = ? WHERE scope = ? AND path = ?", (new, RECORDS, old))

    def drop_record(self, file: str) -> None:
        with self.db.write() as con:
            con.execute("DELETE FROM dictionaries WHERE scope = ? AND path = ?", (RECORDS, file))
