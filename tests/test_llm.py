"""Текстовые этапы: свой адрес и ключ, поле provider только для OpenRouter, рассуждения вон."""
import asyncio

import pytest

from app import llm, usage
from app.config import Settings

ENV = ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_BASE_URL", "SUMMARY_BASE_URL",
       "BACKUP_BASE_URL", "BACKUP_API_KEY", "COST_CURRENCY",
       "SUMMARY_API_KEY", "SUMMARY_TIMEOUT_SEC", "SUMMARY_MODEL", "ACTIONS_MODELS",
       "SUMMARY_REASONING", "SUMMARY_MAX_TOKENS",
       "PROVIDER_MIN_THROUGHPUT", "PROVIDER_MAX_PRICE", "PROVIDER_QUANTIZATIONS")

LOCAL = {"OPENAI_API_KEY": "cloud-key",
         "SUMMARY_BASE_URL": "http://host.docker.internal:1234/v1",
         "SUMMARY_API_KEY": "lm-studio",
         "SUMMARY_MODEL": "qwen3-14b",
         "ACTIONS_MODELS": "qwen3-14b",
         "SUMMARY_TIMEOUT_SEC": "3600"}


def settings(monkeypatch, **env) -> Settings:
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings.from_env()


class Reply:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def answer(content, finish="stop", **message):
    return {"choices": [{"message": {"content": content, **message}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


@pytest.fixture
def wire(monkeypatch):
    """Подменяет сеть: запросы складываются в список, ответы берутся по очереди."""
    sent, replies = [], []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None, timeout=None):
            sent.append({"url": url, "headers": headers, "body": dict(json), "timeout": timeout})
            return Reply(replies.pop(0))

    async def no_sleep(_):
        pass

    monkeypatch.setattr(llm.httpx, "AsyncClient", Client)
    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    usage.reset()
    yield sent, replies
    usage.reset()


def ask(cfg, **kw):
    return asyncio.run(llm.chat(cfg, "summary", "система", "вопрос", **kw))


def test_text_stages_default_to_speech_address_and_key(monkeypatch):
    cfg = settings(monkeypatch, OPENAI_API_KEY="cloud-key")
    assert cfg.summary_base_url == cfg.base_url == "https://openrouter.ai/api/v1"
    assert cfg.summary_api_key == cfg.api_key == "cloud-key"
    assert cfg.summary_timeout_sec == 900
    # пустая строка в .env — то же, что строки нет
    cfg = settings(monkeypatch, OPENAI_API_KEY="cloud-key", SUMMARY_BASE_URL="", SUMMARY_API_KEY=" ")
    assert (cfg.summary_base_url, cfg.summary_api_key) == (cfg.base_url, "cloud-key")


def test_local_block_from_env_example(monkeypatch):
    cfg = settings(monkeypatch, **LOCAL)
    assert cfg.base_url == "https://openrouter.ai/api/v1" and cfg.api_key == "cloud-key"
    assert cfg.summary_base_url == "http://host.docker.internal:1234/v1"
    assert cfg.summary_api_key == "lm-studio"
    assert cfg.actions_models == ("qwen3-14b",)
    assert cfg.summary_timeout_sec == 3600
    assert not cfg.text_provider.routes and cfg.text_backup is None


def test_openrouter_request_carries_provider_rules(monkeypatch, wire):
    sent, replies = wire
    replies.append(answer("готово"))
    text, _ = ask(settings(monkeypatch, OPENAI_API_KEY="cloud-key"))
    assert text == "готово"
    assert sent[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert sent[0]["headers"]["Authorization"] == "Bearer cloud-key"
    assert sent[0]["body"]["provider"]["preferred_min_throughput"] == 150
    assert sent[0]["timeout"] == 900


def test_local_request_has_own_address_key_and_no_openrouter_fields(monkeypatch, wire):
    sent, replies = wire
    replies.append(answer("готово"))
    ask(settings(monkeypatch, **LOCAL))
    assert sent[0]["url"] == "http://host.docker.internal:1234/v1/chat/completions"
    assert sent[0]["headers"]["Authorization"] == "Bearer lm-studio"
    assert sent[0]["timeout"] == 3600
    assert set(sent[0]["body"]) == {"model", "messages"}
    assert sent[0]["body"]["model"] == "qwen3-14b"


def test_answer_without_cost_counts_as_zero(monkeypatch, wire):
    _, replies = wire
    replies.append(answer("готово"))
    cfg = settings(monkeypatch, **LOCAL)
    ask(cfg)
    spent = usage.snapshot()
    assert (spent["calls"], spent["cost"], spent["by_provider"]) == (1, {}, [])
    usage.add("summary", "qwen3-14b", None, cfg.text_provider)   # сервер не прислал usage вовсе
    assert usage.snapshot()["cost"] == {}


@pytest.mark.parametrize("raw, clean", [
    ("<think>\nсначала подумаю\n</think>\n\n# Протокол", "# Протокол"),
    ("  <THINK>a</THINK><think>b</think>ответ", "ответ"),
    ("хвост рассуждений без открывающего тега</think>\n{\"kind\": \"meeting\"}", '{"kind": "meeting"}'),
    ("<think>оборвалось на рассуждениях", ""),
    ("обычный ответ, в середине <think>не трогаем</think> текст",
     "обычный ответ, в середине <think>не трогаем</think> текст"),
    ("", ""),
    (None, ""),
])
def test_strip_reasoning(raw, clean):
    assert llm.strip_reasoning(raw) == clean


def test_reasoning_block_is_dropped_before_check(monkeypatch, wire):
    _, replies = wire
    replies.append(answer("<think>верну {не json}</think>\n{\"ok\": 1}", reasoning_content="думаю"))
    seen = []
    text, _ = ask(settings(monkeypatch, **LOCAL), check=seen.append)
    assert text == '{"ok": 1}' and seen == ['{"ok": 1}']


def test_answer_cut_inside_reasoning_is_empty_and_retried(monkeypatch, wire):
    sent, replies = wire
    replies += [answer("<think>думаю и не успеваю", finish="length"), answer("<think>а</think>итог")]
    text, _ = ask(settings(monkeypatch, **LOCAL))
    assert text == "итог"
    assert "max_tokens" not in sent[0]["body"] and sent[1]["body"]["max_tokens"] == llm.FALLBACK_MAX_TOKENS
