"""Поставщики моделей: основной и запасной.

Сервис ходит к моделям через шлюз с OpenAI-совместимым API. Шлюзов два, и у заказчика
может быть любой один из них или оба: RouterAI (российский, счёт в рублях) и OpenRouter
(зарубежный, счёт в долларах). Имена моделей и вид запросов у них совпадают, отличается
только то, что собрано здесь в одном описании поставщика: адрес, ключ, название для
человека, валюта стоимости в ответе и понимает ли он правила выбора поставщика модели.

Запасной подхватывает вызов, на который основной не ответил как поставщик: сеть, таймаут,
5xx и 429 после всех повторов либо отказ по ключу или деньгам (401, 402, 403) — сразу.
Плохой запрос (400) и негодный по содержанию ответ запасному не передаются: это не сбой
поставщика, и второй ответил бы так же.

Проверка ключа (check) — два бесплатных запроса: сведения о ключе и список моделей. Она
отвечает до первой записи на вопрос, который иначе выясняется только на ней: принят ли ключ.
"""
from __future__ import annotations

import datetime
import threading
import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

# Сколько после перехода на запасного к основному не ходим вовсе. Без этой паузы запись
# из десяти частей при лежащем основном десять раз прошла бы все его повторы (у
# распознавания это до восьми минут на часть). Пять минут — дольше обычного короткого
# сбоя шлюза и достаточно мало, чтобы после его возвращения расходы недолго шли в другой
# валюте. Состояние живёт в памяти процесса: после перезапуска основной пробуется сразу.
REST_SEC = 300

REFUSED = (401, 402, 403)       # ключ не тот, деньги кончились, доступ закрыт: повтор не поможет

# Какой шлюз стоит за адресом: по хосту. Незнакомый хост (своя модель в LM Studio, другой
# шлюз) называется своим хостом, а валюту берёт из COST_CURRENCY.
_KNOWN = (
    # хост, название, валюта usage.cost, понимает ли правила выбора поставщика модели
    ("routerai.ru", "RouterAI", "RUB", False),      # из поля provider понимает только only/order
    ("openrouter.ai", "OpenRouter", "USD", True),
)


@dataclass(frozen=True)
class Provider:
    base_url: str
    api_key: str
    title: str          # название для человека
    currency: str       # в чём приходит стоимость вызова: USD, RUB
    routes: bool        # понимает правила выбора поставщика модели (Settings.provider_prefs)
    key_env: str        # как ключ называется в .env — для сообщения об отказе

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    def public(self) -> dict:
        """Для страницы администратора: без ключа."""
        return {"title": self.title, "currency": self.currency,
                "host": urlsplit(self.base_url).netloc, "key_present": bool(self.api_key)}


def make(base_url: str, api_key: str, key_env: str, currency: str) -> Provider:
    """Поставщик по адресу. currency — валюта для адреса, который сервис не знает."""
    host = (urlsplit(base_url).hostname or "").lower()
    for known, title, money, routes in _KNOWN:
        if host == known or host.endswith("." + known):
            return Provider(base_url, api_key, title, money, routes, key_env)
    return Provider(base_url, api_key, host or base_url, currency, False, key_env)


class Fault(RuntimeError):
    """Сбой поставщика, а не запроса: 5xx, 429. Повторяется, а после повторов — повод
    идти к запасному. Сетевые ошибки и таймауты httpx считаются тем же (is_fault)."""


class Down(RuntimeError):
    """Поставщик на вызов не ответил, и повторять у него больше нечего.
    refused — отказал по ключу или деньгам: это надо чинить человеку."""

    def __init__(self, text: str, refused: bool = False):
        super().__init__(text)
        self.refused = refused


def is_fault(e: Exception) -> bool:
    return isinstance(e, (Fault, httpx.TransportError))


_lock = threading.Lock()            # обработчиков несколько, страница читает из своего потока
_resting: dict[str, float] = {}     # адрес основного → до какого момента (monotonic) к нему не ходим
_last: dict | None = None           # последний переход на запасного — для страницы администратора


def resting(primary: Provider) -> bool:
    with _lock:
        return time.monotonic() < _resting.get(primary.base_url, 0.0)


def _switched(primary: Provider, backup: Provider, why: Down) -> str:
    global _last
    with _lock:
        _resting[primary.base_url] = time.monotonic() + REST_SEC
        _last = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "primary": primary.title, "backup": backup.title,
                 "reason": str(why)[:300], "refused": why.refused}
    line = (f"основной поставщик {primary.title} не ответил ({why}) — запрос ушёл запасному "
            f"{backup.title}; ближайшие {REST_SEC // 60} мин запросы идут сразу ему, "
            f"стоимость считается в {backup.currency}")
    if why.refused:
        # отказ по ключу или деньгам сам не пройдёт: без пометки запасной тихо стал бы основным
        line = (f"ВНИМАНИЕ: {line}. Это отказ по ключу или балансу — проверьте {primary.key_env} "
                f"в .env и счёт у {primary.title}")
    return line


def state() -> dict:
    """Когда запрос последний раз уходил запасному и отдыхает ли основной сейчас."""
    with _lock:
        now = time.monotonic()
        return {"last_switch": dict(_last) if _last else None,
                "resting_sec": max((round(until - now) for until in _resting.values() if until > now),
                                   default=0)}


async def through(primary: Provider, backup: Provider | None,
                  call: Callable[[Provider], Awaitable],
                  log: Callable[[str], None] = lambda s: None):
    """Один вызов модели: у основного, а не ответил он — у запасного.

    call(поставщик) делает вызов со своими повторами и бросает Down, когда поставщик не
    ответил. Любая другая ошибка — не сбой поставщика и уходит наверх как есть.
    Повторы у основного обрываются раньше срока, если за это время он отправлен отдыхать
    другим вызовом (resting): записей идёт несколько, и ждать всем по очереди незачем.
    """
    if backup is None:
        return await call(primary)
    if resting(primary):
        return await call(backup)
    try:
        return await call(primary)
    except Down as why:
        log(_switched(primary, backup, why))
        return await call(backup)


# ---------- проверка ключа ----------
# Неверный ключ иначе виден только на первой настоящей записи. Оба запроса проверки бесплатные:
# GET /key — сведения о ключе (есть у OpenRouter и RouterAI; чужой ключ — 401), GET /models —
# список моделей (у обоих открыт и без ключа, поэтому сам по себе ключ не проверяет). Распознавание
# и текстовые модели проверка не вызывает. Баланс счёта она видит, только если поставщик сам
# отказывает из-за него (402) или сообщает, что предел расходов ключа исчерпан.
CHECK_TIMEOUT_SEC = 15

CHECK_OK, CHECK_WARN, CHECK_REFUSED, CHECK_DOWN = "ok", "warn", "refused", "down"


def _verdict(state: str, text: str, hint: str = "") -> dict:
    return {"state": state, "text": text, "hint": hint}


def _json(reply) -> dict:
    try:
        data = reply.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def check(provider: Provider, models: tuple[str, ...] = ()) -> dict:
    """Принят ли ключ поставщиком и есть ли у него модели models (текстовые: модели распознавания
    в общий список входят не у всех). Отдаёт state — ok, warn, refused (отказ: чинить человеку)
    или down (поставщик не ответил) — и по-русски text и hint: что вышло и что делать."""
    redo = "в .env и пересоздайте контейнер: docker compose up -d"
    if not provider.api_key:
        return _verdict(CHECK_REFUSED, "ключ не задан", f"впишите {provider.key_env} {redo}")
    headers = {"Authorization": f"Bearer {provider.api_key}"}
    try:
        async with httpx.AsyncClient() as client:
            key = await client.get(provider.url("/key"), headers=headers, timeout=CHECK_TIMEOUT_SEC)
            listed = await client.get(provider.url("/models"), headers=headers, timeout=CHECK_TIMEOUT_SEC)
    except httpx.HTTPError as e:
        return _verdict(CHECK_DOWN, f"поставщик недоступен: до адреса {provider.base_url} сервис не достучался "
                              f"({type(e).__name__})",
                        "проверьте интернет с сервера, адрес поставщика в .env и не закрыт ли он сетевым экраном")
    for reply in (key, listed):
        code = reply.status_code
        if code in (401, 403):
            return _verdict(CHECK_REFUSED, f"ключ не принят (HTTP {code})",
                            f"ключ неверный, отозван или от другого поставщика — проверьте {provider.key_env} {redo}")
        if code == 402:
            return _verdict(CHECK_REFUSED, "поставщик отказал из-за баланса (HTTP 402)",
                            f"пополните счёт у {provider.title}")
        if code == 429 or code >= 500:
            return _verdict(CHECK_DOWN, f"поставщик не ответил на проверку (HTTP {code})",
                            "обычно это проходит само — повторите проверку через несколько минут")
    known = {m.get("id") for m in _json(listed).get("data") or [] if isinstance(m, dict)}
    if key.status_code != 200:
        # сведений о ключе у этого адреса нет: так отвечает своя модель (LM Studio) и чужой шлюз
        if listed.status_code != 200:
            return _verdict(CHECK_DOWN, f"по адресу {provider.base_url} нет API моделей (HTTP {listed.status_code})",
                            "проверьте адрес поставщика в .env: он должен оканчиваться на /v1 или /api/v1")
        return _verdict(CHECK_WARN, "адрес отвечает, но принят ли ключ, этот поставщик бесплатным запросом не сообщает",
                        "ключ проверит первая запись: загрузите короткую")
    about = _json(key).get("data") or {}
    limit, left = about.get("limit"), about.get("limit_remaining")
    # предела нет: у OpenRouter limit — null, у RouterAI — 0 (а limit_remaining уходит в минус на расход)
    if isinstance(limit, (int, float)) and limit > 0 and isinstance(left, (int, float)) and left <= 0:
        return _verdict(CHECK_REFUSED, "ключ принят, но предел расходов этого ключа исчерпан",
                        f"поднимите предел ключа в кабинете {provider.title} или выпустите новый ключ")
    missing = [m for m in models if known and m not in known]
    if missing:
        return _verdict(CHECK_WARN, "ключ принят, но в списке моделей поставщика нет: " + ", ".join(missing),
                        "проверьте названия моделей в .env (SUMMARY_MODEL, ACTIONS_MODELS) по списку моделей поставщика")
    return _verdict(CHECK_OK, "ключ принят" + (f"; текстовые модели у поставщика есть: {', '.join(models)}"
                                         if models and known else ""))
