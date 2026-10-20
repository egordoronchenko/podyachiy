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
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode

from . import external, users
from .ledger import MAX_FAILURES, RETRY_STATUSES, said

READY, FAILED = "ready", "failed"
AUTO = ("new", "retry")             # сервис взял запись сам
BRIEF_MAX = 2000                    # «Кратко» длиннее этого в письмо не идёт: вместо него суть записи одной строкой
RECIPIENTS_MAX = 20                 # получателей протокола за одну отправку


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
