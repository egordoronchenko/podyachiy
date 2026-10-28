"""Webhook «запись готова»: когда событие положено, что в теле, подпись, повторы, очередь в базе.

Наружу ничего не уходит: запросы принимает httpx.MockTransport, время — подменённые часы.
"""
import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest

from app import apidoc, db, external, watcher, webhook
from app.ledger import MAX_FAILURES
from tests.test_web import ADMIN_PASSWORD, enter, running

SECRET = "секрет-получателя-длинный-и-случайный"
URL = "https://erp.example.ru/podyachiy/hook?token=тайный"
RECORD = {"file": "Проекты/планёрка.mp4", "status": "ok", "stem": "планёрка", "title": "Планёрка",
          "record_date": "2026-03-12", "finished_at": "2026-03-12T12:00:00+03:00", "audio_duration_sec": 600,
          "cost": {"RUB": 12.5}, "error": None}
BASE = "https://podyachiy.example.ru"


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Receiver:
    """Получатель: запоминает запросы и отвечает тем, что ему велели (код или исключение httpx)."""

    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer)


def hooks(tmp_path, receiver, clock, lines, urls=(URL,)):
    return webhook.Hooks(db.Database(tmp_path / "state"), urls, SECRET, lines.append, "1.2.0", clock,
                         httpx.MockTransport(receiver))


def deliver(h):
    return asyncio.run(h.deliver_due())


def rows(h):
    return [dict(r) for r in h.db.rows("SELECT * FROM webhook_queue")]


# ---------- когда событие положено ----------

def test_event_follows_the_ready_letter_rule():
    ok, prev_ok = {**RECORD}, {**RECORD}
    failed = {**RECORD, "status": "error", "error": "нет связи", "failures": 1}
    last = {**failed, "failures": MAX_FAILURES}
    assert webhook.due(ok, None, "new") == webhook.READY
    assert webhook.due(ok, {**failed, "announced": None}, "retry") == webhook.READY
    assert webhook.due(ok, prev_ok, "new") is None                          # о готовности уже сообщено
    assert webhook.due(ok, prev_ok, "rebuild") is None and webhook.due(ok, None, "rebuild") is None
    assert webhook.due({**RECORD, "status": "no_audio"}, None, "new") == webhook.FAILED
    assert webhook.due(failed, None, "new") is None                         # автоматика ещё повторит
    assert webhook.due(last, failed, "retry") == webhook.FAILED
    assert webhook.due(last, {**failed, "announced": "failed"}, "retry") is None
    # ручной запуск: готовность, о которой ещё не сообщалось, — да; сбой — знает запустивший
    assert webhook.due(ok, last, "manual") == webhook.READY
    assert webhook.due(ok, prev_ok, "manual") is None
    assert webhook.due(last, None, "manual") is None


# ---------- тело и подпись ----------

def test_body_is_the_records_row_signed_with_the_secret(tmp_path):
    clock, receiver, lines = Clock(), Receiver(200), []
    h = hooks(tmp_path, receiver, clock, lines)
    delivery = h.add(webhook.READY, RECORD, BASE)
    clock.now += 7
    assert deliver(h) == 1 and rows(h) == []
    request, = receiver.seen
    body = request.content
    stamp = request.headers["X-Podyachiy-Timestamp"]
    assert stamp == str(int(clock.now)) and request.method == "POST" and str(request.url) == httpx.URL(URL)
    expected = "sha256=" + hmac.new(SECRET.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert request.headers["X-Podyachiy-Signature"] == expected == webhook.sign(SECRET, stamp, body)
    assert request.headers["X-Podyachiy-Event"] == "record.ready" and request.headers["X-Podyachiy-Delivery"] == delivery
    sent = json.loads(body)
    assert sent["event"] == "record.ready" and sent["id"] == delivery and sent["created"].startswith("20")
    assert sent["record"] == external.brief(RECORD, BASE) and sent["url"] == external.page_url(RECORD["file"], BASE)
    # сумм в теле нет — ни поля, ни числа
    assert "cost" not in body.decode() and "12.5" not in body.decode()
    assert lines == [] and h.state()["delivered_at"] and h.state()["waiting"] == 0


def test_python_check_from_the_api_description_accepts_only_a_fresh_signed_body(tmp_path, monkeypatch):
    """Пример проверки подписи из раздела «Webhook» описания API работает как написано."""
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    space: dict = {}
    exec(apidoc.WEBHOOK_CHECK, space)
    body = json.dumps(apidoc.WEBHOOK_BODY, ensure_ascii=False).encode()
    stamp = str(int(time.time()))
    headers = {"X-Podyachiy-Timestamp": stamp, "X-Podyachiy-Signature": webhook.sign(SECRET, stamp, body)}
    assert space["accept"](headers, body) == apidoc.WEBHOOK_BODY
    for wrong in ({**headers, "X-Podyachiy-Signature": webhook.sign("другой секрет", stamp, body)},
                  {**headers, "X-Podyachiy-Timestamp": str(int(stamp) - 301)}):
        with pytest.raises(ValueError):
            space["accept"](wrong, body)
    with pytest.raises(ValueError):
        space["accept"](headers, body.replace(b"ready", b"faile"))
    # пример и тело — во вступлении описания, которое читают страница /api и модель
    intro = apidoc._intro("http://podyachiy.example", False, "4")
    assert "## Webhook «запись готова»" in intro and apidoc.WEBHOOK_CHECK in intro and "X-Podyachiy-Signature" in intro


# ---------- повторы ----------

def test_failed_delivery_is_retried_with_growing_pauses_then_dropped(tmp_path):
    clock, receiver, lines = Clock(), Receiver(500), []
    h = hooks(tmp_path, receiver, clock, lines)
    h.add(webhook.FAILED, {**RECORD, "status": "no_audio", "error": "в файле нет звука"}, BASE)
    stamps = []
    for n, pause in enumerate(webhook.RETRY_SEC, 1):
        assert deliver(h) == 0
        row, = rows(h)
        assert row["attempts"] == n and row["next_at"] == clock.now + pause and row["error"] == "ответ HTTP 500"
        stamps.append(receiver.seen[-1].headers["X-Podyachiy-Timestamp"])
        clock.now += pause - 1
        assert deliver(h) == 0 and len(receiver.seen) == n           # раньше срока не повторяет
        clock.now += 1
    assert sum(webhook.RETRY_SEC) < 86400
    assert deliver(h) == 0 and rows(h) == [] and len(receiver.seen) == len(webhook.RETRY_SEC) + 1
    # подпись — заново при каждой попытке; id — один
    assert len(set(stamps)) == len(stamps) and len({r.headers["X-Podyachiy-Delivery"] for r in receiver.seen}) == 1
    # в журнал работы — первый сбой и окончательная неудача, а не каждая попытка
    assert len(lines) == 2 and "повторю через 1 мин" in lines[0] and "token" not in lines[0]
    assert "не доставлен" in lines[1] and "больше не повторяю" in lines[1] and "Проекты/планёрка.mp4" in lines[1]
    assert h.state()["failed_at"] and h.state()["error"] == "https://erp.example.ru: ответ HTTP 500"


def test_timeout_and_refused_connection_are_failures_and_recovery_is_logged_once(tmp_path):
    clock, lines = Clock(), []
    receiver = Receiver(httpx.ReadTimeout("долго"), httpx.ConnectError("закрыто"), 204)
    h = hooks(tmp_path, receiver, clock, lines)
    h.add(webhook.READY, RECORD, BASE)
    assert deliver(h) == 0 and rows(h)[0]["error"] == f"нет ответа за {webhook.TIMEOUT_SEC} с"
    clock.now += webhook.RETRY_SEC[0]
    assert deliver(h) == 0 and rows(h)[0]["error"] == "не удалось соединиться (ConnectError)"
    clock.now += webhook.RETRY_SEC[1]
    assert deliver(h) == 1 and rows(h) == []
    assert len(lines) == 2 and "доставка снова идёт" in lines[1]


def test_queue_survives_a_restart(tmp_path):
    clock, lines = Clock(), []
    first = hooks(tmp_path, Receiver(503), clock, lines)
    delivery = first.add(webhook.READY, RECORD, BASE)
    assert deliver(first) == 0
    first.db.close()
    receiver = Receiver(200)
    again = hooks(tmp_path, receiver, clock, lines)
    clock.now += webhook.RETRY_SEC[0]
    again.forget()                                    # то, с чего начинает run
    assert deliver(again) == 1 and receiver.seen[0].headers["X-Podyachiy-Delivery"] == delivery
    assert rows(again) == []


def test_old_and_foreign_deliveries_are_forgotten_at_start(tmp_path):
    clock, lines = Clock(), []
    old = hooks(tmp_path, Receiver(503), clock, lines, urls=(URL, "https://old.example.ru/hook"))
    old.add(webhook.READY, RECORD, BASE)
    old.db.close()
    fresh = hooks(tmp_path, Receiver(200), clock, lines)
    fresh.forget()
    assert [r["url"] for r in rows(fresh)] == [URL]
    clock.now += webhook.STALE_SEC + 1
    fresh.forget()
    assert rows(fresh) == [] and "снято доставок" in lines[-1]


def test_delivery_loop_runs_on_its_own_and_wakes_on_a_new_event(tmp_path):
    receiver, lines = Receiver(200), []
    h = webhook.Hooks(db.Database(tmp_path / "state"), (URL,), SECRET, lines.append, "1.2.0",
                      transport=httpx.MockTransport(receiver))

    async def go():
        task = asyncio.create_task(h.run())
        await asyncio.sleep(0.05)
        h.add(webhook.READY, RECORD, BASE)
        for _ in range(100):
            if receiver.seen:
                break
            await asyncio.sleep(0.02)
        task.cancel()

    asyncio.run(go())
    assert len(receiver.seen) == 1 and rows(h) == []


# ---------- настройки ----------

def test_settings_are_checked_and_off_without_an_address(tmp_path):
    assert webhook.check((), "", "") == ("", [])
    off, _ = webhook.check((URL,), "", BASE)
    assert "WEBHOOK_SECRET" in off
    off, _ = webhook.check(("erp.example.ru/hook",), SECRET, BASE)
    assert "WEBHOOK_URL" in off
    assert webhook.check((URL,), SECRET, BASE) == ("", [])
    off, warnings = webhook.check(("http://10.0.0.5:8080/hook",), "короткий", "")
    assert off == "" and len(warnings) == 3 and "http://10.0.0.5:8080" in warnings[0] and "/hook" not in warnings[0]
    h = webhook.Hooks(db.Database(tmp_path / "state"), (), SECRET, print)
    assert not h.on and h.add(webhook.READY, RECORD, BASE) is None and rows(h) == []


# ---------- в сервисе ----------

def test_service_queues_the_event_without_sums_and_provider_text(tmp_path):
    env = {"WEBHOOK_URL": URL, "WEBHOOK_SECRET": SECRET, "PUBLIC_URL": BASE}
    with running(tmp_path, extra=env) as (web, _):
        web.core.hooks.transport = httpx.MockTransport(Receiver(503))
        reply = 'распознавание: HTTP 402. Ответ поставщика: {"error":"balance 0.12 USD"}'
        failed = {**RECORD, "status": "error", "error": reply, "failures": MAX_FAILURES}
        web.core.service._hook(watcher.QueueItem(path=RECORD["file"], reason="retry"), failed, None)
        body = json.loads(web.core.base.rows("SELECT body FROM webhook_queue")[0]["body"])
        assert body["event"] == "record.failed" and body["record"]["error"] == web.browse.FAILED_HIDDEN
        assert "0.12" not in json.dumps(body) and "cost" not in json.dumps(body)
        assert body["url"].startswith(BASE + "/?")
        # обработчик после записанного исхода сам ставит событие: «нет звука» у новой записи
        silent = {**RECORD, "file": "Проекты/тишина.mp4", "status": "no_audio", "error": "в файле нет звука"}
        web.core.service.ledger.put(dict(silent))
        web.core.service._after(watcher.QueueItem(path=silent["file"]), web.core.service.ledger.get(silent["file"]), None)
        bodies = [json.loads(r["body"]) for r in web.core.base.rows("SELECT body FROM webhook_queue ORDER BY created")]
        assert [(b["event"], b["record"]["file"], b["record"]["error"]) for b in bodies][1:] == [
            ("record.failed", "Проекты/тишина.mp4", "в файле нет звука")]
        state = enter(web, "admin", ADMIN_PASSWORD).get("/api/admin/status").json()["webhook"]
        assert state["on"] and state["targets"] == ["https://erp.example.ru"] and state["waiting"] == 2
        assert "token" not in json.dumps(state)
        assert any("webhook «запись готова» включён: https://erp.example.ru" in line for line in web.core.log.tail())


def test_service_without_webhook_url_sends_nothing(tmp_path):
    with running(tmp_path) as (web, _):
        assert web.core.service.hook is None and not web.core.hooks.on
        state = enter(web, "admin", ADMIN_PASSWORD).get("/api/admin/status").json()["webhook"]
        assert state["on"] is False and state["targets"] == [] and state["waiting"] == 0
    with running(tmp_path / "без секрета", extra={"WEBHOOK_URL": URL}) as (web, _):
        assert web.core.service.hook is None
        assert any("webhook выключен — WEBHOOK_SECRET не задан" in line for line in web.core.log.tail())
