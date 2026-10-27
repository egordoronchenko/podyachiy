"""Ответ 429 «слишком много запросов» — пауза и повтор, а не сбой записи.

Записей идёт несколько сразу, и предел провайдера ближе. Пауза растёт от попытки
к попытке; если провайдер назвал срок сам (Retry-After), ждём не меньше него.
"""
import asyncio
import datetime
import email.utils
import types

import pytest

from app import llm, providers, speech, usage


class Reply:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self.data, self.headers = status, data or {}, headers or {}
        self.text = "rate limited" if status == 429 else ""

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


@pytest.fixture
def wire(monkeypatch):
    """Ответы провайдера по очереди; паузы не ждём, а записываем."""
    replies, pauses = [], []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kw):
            return replies.pop(0)

    async def no_sleep(sec):
        pauses.append(sec)

    monkeypatch.setattr(llm.httpx, "AsyncClient", Client)
    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    usage.reset()
    return replies, pauses, Client


ONLY = providers.make("http://x/v1", "k", "OPENAI_API_KEY", "USD")     # поставщик один, запасного нет
CFG = types.SimpleNamespace(
    summary_model="модель", summary_reasoning="", provider_prefs=lambda: {}, summary_timeout_sec=10,
    model="deepgram/nova-3", language="ru",
    speech_provider=ONLY, text_provider=ONLY, backup_provider=None, text_backup=None)
TEXT = {"choices": [{"message": {"content": "готово"}, "finish_reason": "stop"}], "usage": {"cost": 0.5}}


def test_text_model_waits_and_repeats_on_429(wire):
    replies, pauses, _ = wire
    replies += [Reply(429), Reply(429, headers={"retry-after": "70"}), Reply(429, headers={"Retry-After": "1"}),
                Reply(200, TEXT)]
    lines = []
    text, _ = asyncio.run(llm.chat(CFG, "summary", "система", "вопрос", log=lines.append))
    assert text == "готово"
    # 15 с, дальше вдвое; срок провайдера — если он больше нашей паузы
    assert pauses == [15, 70, 60]
    assert usage.snapshot()["cost"] == {"USD": 0.5} and "повторяю через 70 с" in lines[1]


def test_other_failures_of_the_text_model_keep_their_short_pauses(wire):
    replies, pauses, _ = wire
    replies += [Reply(503), Reply(502), Reply(200, TEXT)]
    asyncio.run(llm.chat(CFG, "summary", "система", "вопрос"))
    assert pauses == [3, 6]


def test_speech_waits_and_repeats_on_429(wire, tmp_path):
    replies, pauses, Client = wire
    chunk = tmp_path / "часть.mp3"
    chunk.write_bytes(b"mp3")
    words = {"words": [{"text": "да"}], "usage": {"cost": 0.25}}
    replies += [Reply(429, headers={"retry-after": "40"}), Reply(429), Reply(500), Reply(200, words)]
    data = asyncio.run(speech.call_stt(Client(), CFG, chunk))
    assert data == words and pauses == [40, 30, 60]       # 500 — прежняя пауза: 15 с × 2 в степени попытки
    assert usage.snapshot()["cost"] == {"USD": 0.25}


@pytest.mark.parametrize("status", [401, 402, 403])
def test_speech_does_not_repeat_a_refused_key(wire, tmp_path, status):
    """Неверный ключ от повтора верным не станет: одна попытка, без пауз, и понятная причина."""
    replies, pauses, Client = wire
    chunk = tmp_path / "часть.mp3"
    chunk.write_bytes(b"mp3")
    refused = Reply(status)
    refused.text = '{"error":{"message":"No auth credentials found"}}'
    replies += [refused, Reply(200, {"words": []})]
    lines = []
    with pytest.raises(RuntimeError) as failed:
        asyncio.run(speech.call_stt(Client(), CFG, chunk, log=lines.append))
    text = str(failed.value)
    assert f"HTTP {status}" in text and "OPENAI_API_KEY" in text and "No auth credentials" in text
    assert pauses == [] and lines == [] and len(replies) == 1      # второй ответ не запрошен


def test_speech_does_not_repeat_a_refused_request_but_repeats_a_failure(wire, tmp_path):
    replies, pauses, Client = wire
    chunk = tmp_path / "часть.mp3"
    chunk.write_bytes(b"mp3")
    words = {"words": [], "usage": {"cost": 0.1}}
    # плохой запрос повтор не исправит: сразу ошибка, без пауз, второй ответ не запрошен
    replies += [Reply(400), Reply(200, words)]
    with pytest.raises(providers.Rejected):
        asyncio.run(speech.call_stt(Client(), CFG, chunk))
    assert pauses == [] and len(replies) == 1
    # сбой поставщика повторяется
    replies[:] = [Reply(503), Reply(408), Reply(200, words)]
    assert asyncio.run(speech.call_stt(Client(), CFG, chunk)) == words and pauses == [15, 30]


def test_retry_after_is_read_as_seconds_or_date_and_capped():
    after = lambda value: speech.retry_after(Reply(429, headers={"retry-after": value}))
    assert after("12") == 12.0 and after("") == 0.0 and after("скоро") == 0.0 and after("-5") == 0.0
    soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=90)
    assert 80 < after(email.utils.format_datetime(soon, usegmt=True)) <= 90
    assert speech.retry_after(Reply(429)) == 0.0
    # час ждать не будем: задание держит обработчик
    assert speech.rate_limit_pause(speech.RateLimited("HTTP 429", 3600), 0) == speech.RETRY_AFTER_MAX_SEC
