"""Письма о записи: кому и когда уходит «запись готова», что в письме, кому можно слать протокол.

Правило писем о готовности. Обработчик после каждого прогона спрашивает due():

  пересборка протокола (reason=rebuild)      — письма нет никогда;
  ручной запуск администратором (manual)      — письмо тому, кто запустил, об исходе именно
                                                этого прогона: готово либо что не получилось.
                                                Автору загрузки — ничего;
  сервис взял запись сам (new, retry):
    готово                                    — письмо «запись готова» автору загрузки, если
                                                о готовности этой записи ещё не сообщалось;
    нет звука                                 — письмо о сбое автору, если по записи ещё не
                                                было ни одного письма: повторять тут нечего;
    ошибка или нет протокола                  — пока автоматика ещё повторит, письма нет;
                                                на последнем сбое подряд (ledger.MAX_FAILURES),
                                                после которого запись ждёт администратора, —
                                                письмо о сбое автору, если писем ещё не было.

Итого автору по одной записи приходит не больше одного письма «готова» и не больше одного
письма о сбое, причём письмо о сбое — только раньше письма о готовности. Что уже сообщено,
помнит журнал обработки (поле announced): пересборки и повторы его не сбрасывают. У записей,
обработанных до появления писем, готовность считается сообщённой.

Отметка ставится по событию, а не по доставке: у автора может не быть почты, уведомления
могут быть отключены, почтовый сервер может отказать — второй раз событие всё равно не
наступит. Ручной запуск отметку не трогает: его письмо уходит другому человеку.

Автор загрузки известен, если запись загружена через страницу или ключом (journal.uploaded).
У файла, положенного в папку руками, автора нет — и письма нет.

Напоминания о сроках поручений. Раз в день, после REMIND_HOUR по часам сервера, каждому, у кого в
сервисе есть почта и кто сам включил напоминания (users.notify_due), уходит одно письмо-сводка: его
незакрытые поручения с просроченным сроком, со сроком сегодня и завтра — только из папок, которые ему
открыты. Просроченное — как на странице «Поручения»: дата срока, заданная человеком, прошла. Сегодня и
завтра — по дате срока, заданной человеком, а без неё — вычисленной из слов протокола («до пятницы»).
Нечего напомнить — письма нет. Чьё поручение — по ФИО исполнителя в протоколе (assigned): та же
фамилия отдельным словом, а имя, отчество или инициалы, если они там есть, не расходятся с ФИО в учётной
записи. Тот же способ, что «какие на мне поручения» — отбор реестра по фамилии, — только строже: «Иванова»
Иванову не напоминают. Второй раз за день письма нет: день последней рассылки помнит база (app/web/lifecycle.py).
"""
from __future__ import annotations

import datetime
import re
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode

from . import dictionary, external, marks, users
from .ledger import MAX_FAILURES, RETRY_STATUSES, said

READY, FAILED = "ready", "failed"
AUTO = ("new", "retry")             # сервис взял запись сам
BRIEF_MAX = 2000                    # «Кратко» длиннее этого в письмо не идёт: вместо него суть записи одной строкой
RECIPIENTS_MAX = 20                 # получателей протокола за одну отправку
REMIND_HOUR = 8                     # напоминания о сроках уходят после этого часа по часам сервера
REMIND_MAX = 50                     # поручений одного раздела в письме; остальное — на странице «Поручения»
OVERDUE, TODAY, TOMORROW = "overdue", "today", "tomorrow"


def retrying(record: dict) -> bool:
    """Повторит ли автоматика запись после этого прогона."""
    return record.get("status") in RETRY_STATUSES and int(record.get("failures") or 1) < MAX_FAILURES


def due(record: dict, prev: dict | None, reason: str) -> str | None:
    """Какое письмо положено после прогона: READY, FAILED или никакого. record — строка журнала
    обработки после прогона (с числом сбоев подряд), prev — она же до прогона."""
    ok = record.get("status") == "ok"
    if reason == "manual":
        return READY if ok else FAILED
    if reason not in AUTO:
        return None
    before = said(prev)
    if ok:
        return None if before == READY else READY
    return FAILED if before is None and not retrying(record) else None


def _plain(text: str) -> str:
    """Строка протокола без разметки: в письме она идёт обычным текстом."""
    text = re.sub(r"\s*\[\d{1,2}:\d\d:\d\d\]", "", text)                  # метки времени
    text = re.sub(r"\[([^\]\[]+)\]\([^)\s]*\)", r"\1", text)               # ссылки
    text = re.sub(r"\*\*(.+?)\*\*|`([^`]+)`", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"(?<![\w*])\*([^*\s](?:[^*]*[^*\s])?)\*(?![\w*])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def brief(md: str) -> list[str]:
    """Раздел «Кратко» протокола строками — пункты списка и абзацы. Нет раздела или он длинный — пусто."""
    lines, inside, fresh = [], False, True
    for line in md.splitlines():
        if line.startswith("#"):
            if inside:
                break
            inside = bool(re.match(r"#{1,4}\s*(?:\d+[.)]\s*)?Кратко", line))
            continue
        if not inside:
            continue
        if not line.strip():
            fresh = True                                                    # пустая строка — конец абзаца
            continue
        item = re.match(r"\s*(?:[-*+]|\d+[.)])\s+(.*)", line)
        text = _plain(item.group(1) if item else line)
        if not text:
            continue
        if item or fresh:
            lines.append(text)
        else:
            lines[-1] += " " + text                                         # продолжение пункта или абзаца
        fresh = False
    return lines if sum(len(x) for x in lines) <= BRIEF_MAX else []


def about(record: dict, protocol: str | None, base: str) -> dict:
    """Сведения о записи для писем. protocol — текст протокола, если он есть."""
    file = record["file"]
    path = PurePosixPath(file)
    day = external.day_of(record)
    status = record.get("status")
    short = brief(protocol) if protocol else []
    if not short and protocol and (record.get("gist") or "").strip():
        short = [record["gist"].strip()]
    minutes = max(1, round(float(record.get("audio_duration_sec") or 0) / 60))
    if status == "ok":
        url = external.page_url(file, base)
    elif status == "no_summary":
        url = external.page_url(file, base) + "&kind=transcript"
    else:
        # открыть нечего — ссылка ведёт в папку, где запись стоит в списке с пометкой
        parent = path.parent.as_posix()
        url = base + "/?" + urlencode({"path": "" if parent == "." else parent}, quote_via=quote)
    return {
        "title": (record.get("title") or "").strip() or path.name,
        "file_name": path.name,
        "day": f"{day[8:10]}.{day[5:7]}.{day[:4]}" if day else "",
        "duration": ("" if not record.get("audio_duration_sec")
                     else f"{minutes // 60} ч {minutes % 60:02d} мин" if minutes >= 60 else f"{minutes} мин"),
        "brief": short,
        "tasks": len(external.tasks(protocol)) if protocol else None,
        "url": url,
    }


class AddressError(Exception):
    pass


def allowed(email: str, domains: tuple[str, ...]) -> bool:
    """Можно ли слать протокол на этот адрес: домен из списка MAIL_DOMAINS; список пуст — можно всем."""
    return not domains or email.rpartition("@")[2] in domains


def recipients(typed: str, domains: tuple[str, ...]) -> list[str]:
    """Адреса получателей из того, что вписал человек: через запятую, точку с запятой, пробел
    или с новой строки. Повторы убираются. Негодный адрес, лишние адреса и чужой домен — отказ."""
    found: list[str] = []
    for piece in re.split(r"[\s,;]+", typed):
        if not piece:
            continue
        try:
            email = users.norm_email(piece)
        except users.UserError:
            raise AddressError(f"«{piece[:80]}» не похоже на адрес почты: нужен вид имя@организация.ru")
        if email not in found:
            found.append(email)
    if not found:
        raise AddressError("укажите хотя бы один адрес получателя")
    if len(found) > RECIPIENTS_MAX:
        raise AddressError(f"получателей {len(found)} — за один раз можно не больше {RECIPIENTS_MAX}")
    foreign = [e for e in found if not allowed(e, domains)]
    if foreign:
        raise AddressError("протокол можно отправлять только на адреса почтовых доменов "
                           f"{', '.join(domains)}; не подходят: {', '.join(foreign)}. "
                           "Список доменов задаёт администратор сервиса (MAIL_DOMAINS)")
    return found


# ---------- напоминания о сроках поручений ----------

def _readings(name: str) -> list[tuple[str, list[str]]]:
    """Как прочесть ФИО учётной записи: (фамилия, [имя, отчество]) — словами в виде marks.norm.
    Обычно фамилия первая («Иванов Иван Петрович»); «Иван Петрович Иванов» узнаётся по отчеству,
    а у двух слов («Иван Иванов») годятся оба прочтения."""
    raw, words = name.split(), marks.norm(name).split()
    if not words:
        return []
    if len(raw) == 3 == len(words) and dictionary._PATRONYMIC_RE.search(raw[1]):
        return [(words[2], words[:2])]
    found = [(words[0], words[1:])]
    if len(words) == 2:
        found.append((words[1], words[:1]))
    return found


def assigned(who: str | None, name: str) -> bool:
    """Поручение с исполнителем who (как он записан в протоколе) — этого человека (ФИО name)? Фамилия —
    отдельным словом; остальные слова — его имя или отчество, буквы — их инициалы по порядку. Прочтение
    двух слов «Имя Фамилия» годится, только если кроме фамилии названо и имя: одно слово «Иван» у
    «Иванов Иван» — имя, а не фамилия, и письмо ушло бы не тому."""
    words = marks.norm(who).split()
    for n, (surname, given) in enumerate(_readings(name)):
        if surname not in words:
            continue
        rest = [w for w in words if w != surname]
        if n and not rest:
            continue
        full, initials = [w for w in rest if len(w) > 1], [w for w in rest if len(w) == 1]
        if given and any(w not in given for w in full):
            continue
        if all(i >= len(given) or w == given[i][0] for i, w in enumerate(initials)):
            return True
    return False


def surname(name: str) -> str:
    """Фамилия из ФИО учётной записи, как она написана, — отбору «Поручений» в ссылке письма."""
    raw = name.split()
    if len(raw) == 3 and dictionary._PATRONYMIC_RE.search(raw[1]):
        return raw[2]
    return raw[0] if raw else ""


def reminders(rows: list[dict], today: str) -> dict[str, list[dict]]:
    """Что напомнить из строк реестра поручений (registry.rows): незакрытые просроченные, со сроком
    сегодня и завтра. Одно поручение записи, стоящее в протоколе дважды, — один раз."""
    tomorrow = (datetime.date.fromisoformat(today) + datetime.timedelta(days=1)).isoformat()
    found: dict[str, list[dict]] = {OVERDUE: [], TODAY: [], TOMORROW: []}
    seen = set()
    for row in rows:
        if not row["open"] or (row["file"], row["key"]) in seen:
            continue
        part = (OVERDUE if row["overdue"] else TODAY if row["due_date"] == today
                else TOMORROW if row["due_date"] == tomorrow else None)
        if part:
            seen.add((row["file"], row["key"]))
            found[part].append(row)
    found[OVERDUE].sort(key=lambda r: (r["due"], r["date"]))
    return found
