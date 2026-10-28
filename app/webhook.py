"""Webhook «запись готова»: сервис сам сообщает внешней системе, что запись дошла до исхода.

Когда. То же правило, что у письма автору загрузки (notify.due): сервис взял запись сам — событие
record.ready, когда она готова и о готовности ещё не сообщалось; record.failed — «нет звука» или
последний сбой подряд, после которого запись ждёт администратора, и только если по записи ещё не
сообщалось ничего. Ручной запуск, доведший запись до готовности, — тоже record.ready, если о
готовности ещё не сообщалось; о сбое ручного прогона знает тот, кто его запустил. Пересборка
протокола и повтор готовой записи событий не дают: новое по готовым записям — /api/v1/records?since.

Куда. Адреса и секрет — WEBHOOK_URL (можно несколько через запятую) и WEBHOOK_SECRET в .env: их задаёт
администратор сервиса при установке, страницы для этого нет. Поэтому адрес не проверяется на частные
сети и localhost: подделка запроса на стороне сервера (SSRF) — это когда адрес подсовывает тот, кому
сеть сервиса не открыта, а здесь адрес пишет тот, кто и так управляет установкой и её сетью. Адрес по
http принимается (получатель бывает во внутренней сети без сертификата), но при запуске об этом
предупреждение: тело идёт открыто, хотя подделать его без секрета нельзя. Без секрета webhook выключен.

Что. POST, тело JSON: event, id (одна доставка — один id во всех попытках: по нему получатель
отбрасывает повтор), created (когда случилось), record — те же поля, что у записи в /api/v1/records,
без сумм и без ответа поставщика в тексте сбоя, url — запись на странице сервиса (от PUBLIC_URL).

Подпись. X-Podyachiy-Timestamp — секунды Unix этой попытки; X-Podyachiy-Signature —
sha256=<hex HMAC-SHA256(WEBHOOK_SECRET, timestamp + "." + тело)>. Подпись считается заново при каждой
попытке: получатель сверяет её и отвергает запрос со слишком старым временем (повтор перехваченного).

Доставка. Ответ 2xx — доставлено. Иначе, и если ответа нет за TIMEOUT_SEC, — повтор через RETRY_SEC:
1 мин, 5 мин, 30 мин, 2 ч, 12 ч; не принял и шестую попытку — доставка снимается строкой в журнале
работы. Очередь — в базе (таблица webhook_queue): перезапуск её не теряет. Доставка идёт своей задачей
(run) и обработку записей не держит. Сервис упал между ответом получателя и отметкой в базе — доставка
уйдёт ещё раз с тем же id: доставка «хотя бы раз», а не «ровно раз». В журнал работы — первый сбой
доставки на адрес, возврат к доставке после сбоев и окончательная неудача, а не каждая попытка.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import secrets
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx

from . import db, external, notify
from .ledger import said

READY, FAILED = "record.ready", "record.failed"
EVENTS = {notify.READY: READY, notify.FAILED: FAILED}
TIMEOUT_SEC = 10
RETRY_SEC = (60, 5 * 60, 30 * 60, 2 * 3600, 12 * 3600)     # паузы перед повторами: 14,6 ч от первой попытки
WAIT_MAX_SEC = 60            # дольше этого очередь не спит, даже если до ближайшей попытки далеко
STALE_SEC = 86400            # доставка старше суток при запуске не уходит: сервис стоял, событие устарело
SECRET_MIN = 16              # короче — подпись подбирается
STATE = "webhook"            # ключ в meta: последняя доставка и последний сбой — для страницы «Обработка»


def due(record: dict, prev: dict | None, reason: str) -> str | None:
    """Какое событие положено после прогона: READY, FAILED или никакого. Правило — в начале файла."""
    if reason == "manual":
        return READY if record.get("status") == "ok" and said(prev) != notify.READY else None
    return EVENTS.get(notify.due(record, prev, reason))


def sign(secret: str, stamp: str, body: bytes) -> str:
    """Значение X-Podyachiy-Signature: HMAC-SHA256 секретом от «время.тело»."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), stamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def where(url: str) -> str:
    """Адрес получателя для журнала и страницы: только схема и узел — в пути и параметрах бывает токен."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}" + (f":{parts.port}" if parts.port else "")


def check(urls: tuple[str, ...], secret: str, public_url: str) -> tuple[str, list[str]]:
    """Почему webhook выключен ("" — включён или не задан вовсе) и о чём предупредить при запуске."""
    if not urls:
        return "", []
    wrong = [u for u in urls if urlsplit(u).scheme not in ("http", "https") or not urlsplit(u).hostname]
    if wrong:
        return f"WEBHOOK_URL «{wrong[0][:80]}» — не адрес вида https://узел/путь", []
    if not secret:
        return ("WEBHOOK_SECRET не задан: без подписи получатель не отличит сервис от любого, кто знает адрес. "
                "Задайте секрет — длинную случайную строку, ту же, что у получателя"), []
    warnings = [f"webhook на {where(u)} идёт по http: тело (названия и пути записей) видно в сети; подделать его "
                "без секрета нельзя. Лучше https" for u in urls if urlsplit(u).scheme == "http"]
    if len(secret) < SECRET_MIN:
        warnings.append(f"WEBHOOK_SECRET короче {SECRET_MIN} знаков — подпись можно подобрать: возьмите длинную "
                        "случайную строку")
    if not public_url:
        warnings.append("PUBLIC_URL не задан — ссылки на запись в теле webhook без адреса сервиса, от корня сайта")
    return "", warnings


def _now_iso(moment: float) -> str:
    return datetime.datetime.fromtimestamp(moment).astimezone().isoformat(timespec="seconds")


def _failure(e: httpx.HTTPError) -> str:
    if isinstance(e, httpx.TimeoutException):
        return f"нет ответа за {TIMEOUT_SEC} с"
    return f"не удалось соединиться ({type(e).__name__})"


class Hooks:
    """Очередь доставки webhook. urls пусты — выключен: события не копятся, run не нужен."""

    def __init__(self, base: db.Database, urls: tuple[str, ...], secret: str, log: Callable[[str], None],
                 version: str = "", clock: Callable[[], float] = time.time,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.db, self.urls, self.secret, self.log, self.version = base, urls, secret, log, version
        self.clock, self.transport = clock, transport
        self.failing: set[str] = set()      # адреса, чей сбой уже в журнале работы: до удачной доставки не повторяем
        self.wake = asyncio.Event()

    @property
    def on(self) -> bool:
        return bool(self.urls)

    def add(self, kind: str, record: dict, base: str) -> str | None:
        """Событие по записи — в очередь каждому адресу. record — строка журнала обработки с текстом сбоя,
        каким его видит не-администратор. Отдаёт id доставки; webhook выключен — None."""
        if not self.on:
            return None
        moment = self.clock()
        item = external.brief(record, base)
        delivery = secrets.token_hex(16)
        body = json.dumps({"event": kind, "id": delivery, "created": _now_iso(moment), "record": item,
                           "url": item["links"]["page"]}, ensure_ascii=False)
        with self.db.write() as con:
            con.executemany("INSERT INTO webhook_queue (id, url, event, body, created, next_at) VALUES (?, ?, ?, ?, ?, ?)",
                            [(delivery, url, kind, body, moment, moment) for url in self.urls])
        self.wake.set()
        return delivery

    def forget(self) -> None:
        """Доставки, которым уже не уйти: на адрес, которого больше нет в WEBHOOK_URL, и старше суток (сервис
        долго стоял) — снимаются при запуске одной строкой в журнале работы."""
        marks = ",".join("?" * len(self.urls))
        with self.db.write() as con:
            gone = con.execute(f"DELETE FROM webhook_queue WHERE url NOT IN ({marks}) OR created < ?",
                               (*self.urls, self.clock() - STALE_SEC)).rowcount
        if gone:
            self.log(f"webhook: снято доставок на прежние адреса или старше суток — {gone}")

    async def run(self) -> None:
        """Доставка по очереди: ближайшие попытки, потом сон до следующей или до нового события."""
        self.forget()
        while True:
            try:
                await self.deliver_due()
                nearest = self.db.value("SELECT min(next_at) FROM webhook_queue")
            except Exception as e:      # база занята, сбой сети вне httpx — следующая попытка позже
                self.log(f"сбой доставки webhook (продолжаю): {e!r}")
                nearest = None
            delay = WAIT_MAX_SEC if nearest is None else min(max(nearest - self.clock(), 0.0), WAIT_MAX_SEC)
            try:
                await asyncio.wait_for(self.wake.wait(), delay)
            except TimeoutError:
                pass
            self.wake.clear()

    async def deliver_due(self) -> int:
        """Попытки, чьё время пришло. Отдаёт, сколько доставлено."""
        rows = self.db.rows("SELECT * FROM webhook_queue WHERE next_at <= ? ORDER BY next_at, created",
                            self.clock())
        if not rows:
            return 0
        delivered = 0
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC, transport=self.transport, follow_redirects=False) as client:
            for row in rows:
                delivered += await self._attempt(client, row)
        return delivered

    async def _attempt(self, client: httpx.AsyncClient, row) -> bool:
        stamp = str(int(self.clock()))
        body = row["body"].encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": f"podyachiy/{self.version}",
                   "X-Podyachiy-Event": row["event"], "X-Podyachiy-Delivery": row["id"],
                   "X-Podyachiy-Timestamp": stamp, "X-Podyachiy-Signature": sign(self.secret, stamp, body)}
        try:
            answer = await client.post(row["url"], content=body, headers=headers)
            why = "" if 200 <= answer.status_code < 300 else f"ответ HTTP {answer.status_code}"
        except httpx.HTTPError as e:
            why = _failure(e)
        now, target = self.clock(), where(row["url"])
        if not why:
            with self.db.write() as con:
                con.execute("DELETE FROM webhook_queue WHERE id = ? AND url = ?", (row["id"], row["url"]))
                self._note(con, delivered=_now_iso(now))
            if row["url"] in self.failing:
                self.failing.discard(row["url"])
                self.log(f"webhook на {target}: доставка снова идёт")
            return True
        tries = row["attempts"] + 1
        with self.db.write() as con:
            if tries > len(RETRY_SEC):
                con.execute("DELETE FROM webhook_queue WHERE id = ? AND url = ?", (row["id"], row["url"]))
            else:
                con.execute("UPDATE webhook_queue SET attempts = ?, next_at = ?, error = ? WHERE id = ? AND url = ?",
                            (tries, now + RETRY_SEC[tries - 1], why, row["id"], row["url"]))
            self._note(con, failed=_now_iso(now), error=f"{target}: {why}")
        record = json.loads(row["body"])["record"]["file"]
        if tries > len(RETRY_SEC):
            self.log(f"ВНИМАНИЕ: webhook {row['event']} по записи {record} на {target} не доставлен: {why}; попыток "
                     f"{tries} за {(now - row['created']) / 3600:.1f} ч — больше не повторяю")
        elif row["url"] not in self.failing:
            self.failing.add(row["url"])
            self.log(f"ВНИМАНИЕ: webhook на {target} не доставлен ({why}) — повторю через "
                     f"{RETRY_SEC[tries - 1] // 60} мин; дальнейшие сбои на этот адрес в журнал не пишутся, "
                     "пока доставка не пойдёт")
        return False

    def _note(self, con, **fields) -> None:
        """Последняя доставка и последний сбой — в meta: странице «Обработка», и после перезапуска тоже."""
        seen = json.loads(self.db.noted(STATE) or "{}")
        seen.update(fields)
        con.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (STATE, json.dumps(seen, ensure_ascii=False)))

    def state(self) -> dict:
        """Состояние для /api/admin/status: куда (без пути и токенов), сколько ждёт, когда последняя доставка."""
        waiting = self.db.row("SELECT count(*) AS n, min(created) AS oldest, min(next_at) AS nearest FROM webhook_queue")
        seen = json.loads(self.db.noted(STATE) or "{}")
        return {"on": self.on, "targets": [where(u) for u in self.urls], "waiting": waiting["n"],
                "oldest": _now_iso(waiting["oldest"]) if waiting["oldest"] is not None else None,
                "next_at": _now_iso(waiting["nearest"]) if waiting["nearest"] is not None else None,
                "delivered_at": seen.get("delivered"), "failed_at": seen.get("failed"), "error": seen.get("error")}
