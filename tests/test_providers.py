"""Два поставщика: основной и запасной.

Основной — RouterAI (рубли), запасной — OpenRouter (доллары). Вызов уходит запасному, когда
основной не ответил как поставщик: сеть, таймаут, 5xx и 429 после всех повторов, а 401, 402,
403 — сразу. Плохой запрос и негодный ответ запасному не передаются. Сеть подменена: ответы
заготовлены по хостам, наружу ничего не уходит.
"""
import asyncio
import json

import httpx
import pytest

from app import llm, providers, speech, usage
from app.config import Settings

MAIN, SPARE = "routerai.ru", "openrouter.ai"
ENV = {"OPENAI_BASE_URL": f"https://{MAIN}/api/v1", "OPENAI_API_KEY": "main-key",
       "BACKUP_BASE_URL": f"https://{SPARE}/api/v1", "BACKUP_API_KEY": "spare-key",
       "SPEECH_MODEL": "elevenlabs/scribe-v2"}
CLEAN = ("OPENROUTER_API_KEY", "SUMMARY_BASE_URL", "SUMMARY_API_KEY", "SUMMARY_REASONING", "SUMMARY_MAX_TOKENS",
         "COST_CURRENCY", "PROVIDER_MIN_THROUGHPUT", "PROVIDER_MAX_PRICE", "PROVIDER_QUANTIZATIONS",
         "BACKUP_BASE_URL", "BACKUP_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY")

WORDS = {"words": [{"word": "да", "start": 0.1, "end": 0.3, "speaker": 0}], "usage": {"seconds": 5, "cost": 1.0}}
TEXT = {"choices": [{"message": {"content": "готово"}, "finish_reason": "stop"}], "usage": {"cost": 0.5}}


def settings(monkeypatch, **env) -> Settings:
    for key in CLEAN:
        monkeypatch.delenv(key, raising=False)
    for key, value in {**ENV, **env}.items():
        monkeypatch.setenv(key, value)
    return Settings.from_env()


class Reply:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self.data, self.headers = status, data, {}
        self.text = text or (json.dumps(data) if data is not None else "")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=None, response=self)

    def json(self):
        if self.data is None:
            raise json.JSONDecodeError("не JSON", self.text, 0)
        return self.data


@pytest.fixture
def net(monkeypatch):
    """Сеть: что отвечает каждый хост и что ему прислали. Ответ — Reply, исключение или их
    список по очереди; одиночный ответ повторяется сколько угодно раз."""
    answers: dict[str, object] = {}
    calls: list[dict] = []
    pauses: list[float] = []
    clock = [1000.0]

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None, data=None, files=None, timeout=None):
            host = httpx.URL(url).host
            calls.append({"host": host, "url": url, "key": headers["Authorization"], "body": json, "form": data})
            answer = answers[host]
            if isinstance(answer, list):
                answer = answer.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    async def no_sleep(sec):
        pauses.append(sec)

    monkeypatch.setattr(llm.httpx, "AsyncClient", Client)
    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    # состояние «основной отдыхает» живёт в модуле — у каждого теста своё, и свои часы
    monkeypatch.setattr(providers, "_resting", {})
    monkeypatch.setattr(providers, "_last", None)
    monkeypatch.setattr(providers.time, "monotonic", lambda: clock[0])
    usage.reset()

    class Net:
        pass

    net = Net()
    net.answers, net.calls, net.pauses, net.clock, net.Client = answers, calls, pauses, clock, Client
    net.hosts = lambda: [c["host"] for c in calls]
    return net


def recognize(cfg, net, tmp_path, lines=None):
    chunk = tmp_path / "часть.mp3"
    chunk.write_bytes(b"mp3")
    return asyncio.run(speech.call_stt(net.Client(), cfg, chunk, log=(lines if lines is not None else []).append))


def ask(cfg, lines=None, **kw):
    return asyncio.run(llm.chat(cfg, "summary", "система", "вопрос", log=(lines if lines is not None else []).append, **kw))


# ---------- поставщик — одно понятие ----------

def test_provider_is_known_by_the_host_of_its_address():
    make = lambda url, currency="USD": providers.make(url, "k", "OPENAI_API_KEY", currency)
    router = make("https://routerai.ru/api/v1")
    assert (router.title, router.currency, router.routes) == ("RouterAI", "RUB", False)
    assert make("http://routerai.ru/api/v1") == providers.make("http://routerai.ru/api/v1", "k", "OPENAI_API_KEY", "USD")
    assert make("http://ROUTERAI.ru:8080/api/v1").title == "RouterAI"       # схема и порт ничего не меняют
    open_router = make("https://openrouter.ai/api/v1", "RUB")               # валюта известна по адресу
    assert (open_router.title, open_router.currency, open_router.routes) == ("OpenRouter", "USD", True)
    # слово в пути или в чужом имени — не тот хост
    for url in ("https://example.org/routerai.ru/v1", "https://notrouterai.ru/v1", "https://openrouter.ai.evil.org/v1"):
        assert make(url).title not in ("RouterAI", "OpenRouter"), url
    local = make("http://host.docker.internal:1234/v1", "RUB")
    assert (local.title, local.currency, local.routes) == ("host.docker.internal", "RUB", False)
    assert router.public() == {"title": "RouterAI", "currency": "RUB", "host": "routerai.ru", "key_present": True}


def test_backup_is_optional_and_comes_from_env(monkeypatch):
    cfg = settings(monkeypatch)
    assert (cfg.speech_provider.title, cfg.backup_provider.title) == ("RouterAI", "OpenRouter")
    assert cfg.backup_provider.api_key == "spare-key" and cfg.text_backup == cfg.backup_provider
    assert cfg.text_provider.title == "RouterAI"
    alone = settings(monkeypatch, BACKUP_BASE_URL="", BACKUP_API_KEY="")
    assert alone.backup_provider is None and alone.text_backup is None
    # у текста свой адрес и свои имена моделей — запасной его не подхватывает
    local = settings(monkeypatch, SUMMARY_BASE_URL="http://host.docker.internal:1234/v1")
    assert local.backup_provider is not None and local.text_backup is None


def test_default_address_is_still_openrouter_and_currency_setting_covers_unknown_hosts(monkeypatch):
    for key in CLEAN:
        monkeypatch.delenv(key, raising=False)
    cfg = Settings.from_env()
    assert cfg.base_url == "https://openrouter.ai/api/v1" and cfg.speech_provider.currency == "USD"
    assert cfg.backup_provider is None
    for raw, code in (("$", "USD"), ("₽", "RUB"), ("rub", "RUB"), ("", "USD"), ("EUR", "EUR")):
        other = settings(monkeypatch, OPENAI_BASE_URL="http://gateway.local/v1", COST_CURRENCY=raw)
        assert other.speech_provider.currency == code, raw


# ---------- распознавание ----------

FAULTS = {
    "503": Reply(503, text="upstream down"),
    "500": Reply(500),
    "429": Reply(429, text="rate limited"),
    "таймаут": httpx.ReadTimeout("время вышло"),
    "нет связи": httpx.ConnectError("соединение не установлено"),
}


@pytest.mark.parametrize("reason", FAULTS)
def test_speech_goes_to_the_backup_when_the_main_ran_out_of_retries(monkeypatch, net, tmp_path, reason):
    cfg = settings(monkeypatch)
    net.answers.update({MAIN: FAULTS[reason], SPARE: Reply(200, WORDS)})
    lines = []
    assert recognize(cfg, net, tmp_path, lines) == WORDS
    assert net.hosts() == [MAIN] * 6 + [SPARE] and len(net.pauses) == 5       # основной — со всеми своими повторами
    assert net.calls[0]["key"] == "Bearer main-key" and net.calls[-1]["key"] == "Bearer spare-key"
    # параметры распознавания у обоих одни и те же
    assert net.calls[0]["form"] == net.calls[-1]["form"]
    assert json.loads(net.calls[-1]["form"]["provider"]) == {"options": {"elevenlabs": {"diarize": True}}}
    switch = [ln for ln in lines if "запасному" in ln]
    assert len(switch) == 1 and switch[0].startswith("основной поставщик RouterAI не ответил (")
    assert "запрос ушёл запасному OpenRouter" in switch[0] and "ВНИМАНИЕ" not in switch[0]
    spent = usage.snapshot()
    assert spent["cost"] == {"USD": 1.0}
    assert spent["by_provider"] == [{"provider": "OpenRouter", "currency": "USD", "cost": 1.0}]


@pytest.mark.parametrize("status", [401, 402, 403])
def test_refused_key_or_balance_goes_to_the_backup_at_once_and_loudly(monkeypatch, net, tmp_path, status):
    cfg = settings(monkeypatch)
    net.answers.update({MAIN: Reply(status, text="Insufficient credits"), SPARE: Reply(200, WORDS)})
    lines = []
    assert recognize(cfg, net, tmp_path, lines) == WORDS
    assert net.hosts() == [MAIN, SPARE] and net.pauses == []                  # повторять отказ незачем
    assert len(lines) == 1 and lines[0].startswith("ВНИМАНИЕ: основной поставщик RouterAI не ответил")
    assert f"HTTP {status}" in lines[0] and "OPENAI_API_KEY" in lines[0] and "Insufficient credits" in lines[0]
    assert providers.state()["last_switch"]["refused"] is True


@pytest.mark.parametrize("answer", [Reply(400, text="bad request"), Reply(404), Reply(200, text="<html>"),
                                    Reply(200, {"error": {"message": "модель не поняла запрос"}})],
                         ids=["400", "404", "не JSON", "ошибка в теле"])
def test_bad_request_and_bad_answer_are_not_the_providers_failure(monkeypatch, net, tmp_path, answer):
    cfg = settings(monkeypatch)
    net.answers.update({MAIN: answer, SPARE: Reply(200, WORDS)})
    lines = []
    with pytest.raises(RuntimeError) as failed:
        recognize(cfg, net, tmp_path, lines)
    assert not isinstance(failed.value, providers.Down)
    assert SPARE not in net.hosts() and not any("запасному" in ln for ln in lines)
    assert providers.state() == {"last_switch": None, "resting_sec": 0}


def test_without_a_backup_the_failure_is_just_the_failure(monkeypatch, net, tmp_path):
    cfg = settings(monkeypatch, BACKUP_BASE_URL="", BACKUP_API_KEY="")
    net.answers[MAIN] = Reply(503)
    with pytest.raises(RuntimeError, match="распознавание: .*HTTP 503"):
        recognize(cfg, net, tmp_path)
    assert net.hosts() == [MAIN] * 6
    net.calls.clear()
    net.answers[MAIN] = Reply(402, text="нет средств")
    with pytest.raises(RuntimeError, match="HTTP 402.*OPENAI_API_KEY"):
        recognize(cfg, net, tmp_path)
    assert net.hosts() == [MAIN]


def test_backup_failure_fails_the_call(monkeypatch, net, tmp_path):
    cfg = settings(monkeypatch)
    net.answers.update({MAIN: Reply(503), SPARE: Reply(401, text="no key")})
    with pytest.raises(RuntimeError, match="HTTP 401.*OpenRouter.*BACKUP_API_KEY"):
        recognize(cfg, net, tmp_path)
    assert net.hosts() == [MAIN] * 6 + [SPARE]


def test_main_rests_for_a_while_after_the_switch_and_is_tried_again_later(monkeypatch, net, tmp_path):
    cfg = settings(monkeypatch)
    net.answers.update({MAIN: Reply(503), SPARE: Reply(200, WORDS)})
    recognize(cfg, net, tmp_path)
    assert providers.state()["resting_sec"] == providers.REST_SEC
    assert providers.state()["last_switch"]["primary"] == "RouterAI"

    # следующие части и текстовые этапы идут сразу запасному: повторы основного не ждём
    net.calls.clear(), net.pauses.clear()
    lines = []
    recognize(cfg, net, tmp_path, lines)
    net.answers[SPARE] = Reply(200, TEXT)
    assert ask(cfg, lines)[0] == "готово"
    assert net.hosts() == [SPARE, SPARE] and net.pauses == [] and lines == []

    # срок вышел — основной пробуется снова, и он уже жив
    net.clock[0] += providers.REST_SEC + 1
    net.calls.clear()
    net.answers.update({MAIN: Reply(200, WORDS), SPARE: Reply(500)})
    recognize(cfg, net, tmp_path)
    assert net.hosts() == [MAIN] and providers.state()["resting_sec"] == 0
    assert usage.snapshot()["cost"] == {"RUB": 1.0, "USD": 2.5}
    assert usage.snapshot()["by_provider"] == [{"provider": "OpenRouter", "currency": "USD", "cost": 2.5},
                                               {"provider": "RouterAI", "currency": "RUB", "cost": 1.0}]


def test_retries_in_flight_stop_when_another_call_has_already_switched(monkeypatch, net, tmp_path):
    """Часть записи ждёт повтора у основного, а соседний вызов уже ушёл запасному: эта часть
    не досиживает свои повторы (у распознавания это минуты), а идёт следом."""
    cfg = settings(monkeypatch)
    net.answers.update({MAIN: Reply(503), SPARE: Reply(200, WORDS)})

    async def sleep_and_switch(sec):                # во время первой паузы основной отправлен отдыхать
        net.pauses.append(sec)
        providers._switched(cfg.speech_provider, cfg.backup_provider, providers.Down("сосед: HTTP 503"))
    monkeypatch.setattr(llm.asyncio, "sleep", sleep_and_switch)
    assert recognize(cfg, net, tmp_path) == WORDS
    assert net.hosts() == [MAIN, MAIN, SPARE] and len(net.pauses) == 1
    net.calls.clear()
    net.clock[0] += providers.REST_SEC + 1
    net.answers[SPARE] = Reply(200, TEXT)
    assert ask(cfg)[0] == "готово" and net.hosts() == [MAIN, MAIN, SPARE]


def test_rest_state_is_shared_by_parallel_workers(monkeypatch, net, tmp_path):
    """Три записи идут сразу: основной лёг — каждая дошла через запасного."""
    cfg = settings(monkeypatch)
    net.answers.update({MAIN: Reply(503), SPARE: Reply(200, WORDS)})
    chunk = tmp_path / "часть.mp3"
    chunk.write_bytes(b"mp3")

    async def three():
        async def one():
            usage.reset()
            return await speech.call_stt(net.Client(), cfg, chunk)
        return await asyncio.gather(*(asyncio.create_task(one()) for _ in range(3)))

    assert asyncio.run(three()) == [WORDS] * 3
    assert net.hosts().count(SPARE) == 3


# ---------- текстовые этапы ----------

@pytest.mark.parametrize("reason", FAULTS)
def test_text_goes_to_the_backup_and_only_openrouter_gets_the_routing_rules(monkeypatch, net, reason):
    cfg = settings(monkeypatch)
    net.answers.update({MAIN: FAULTS[reason], SPARE: Reply(200, TEXT)})
    lines = []
    assert ask(cfg, lines)[0] == "готово"
    assert net.hosts() == [MAIN] * llm.ATTEMPTS + [SPARE]
    assert net.calls[-1]["url"] == f"https://{SPARE}/api/v1/chat/completions"
    # поле provider — только тому, кто его понимает; модель у обоих называется одинаково
    assert "provider" not in net.calls[0]["body"]
    assert net.calls[-1]["body"]["provider"]["preferred_min_throughput"] == 150
    assert net.calls[0]["body"]["model"] == net.calls[-1]["body"]["model"] == cfg.summary_model
    assert any("запрос ушёл запасному OpenRouter" in ln for ln in lines)
    assert usage.snapshot()["by_model"] == [{"stage": "summary", "model": cfg.summary_model,
                                             "provider": "OpenRouter", "currency": "USD", "cost": 0.5}]


def test_routing_rules_follow_the_provider_when_openrouter_is_the_main_one(monkeypatch, net):
    cfg = settings(monkeypatch, OPENAI_BASE_URL=f"https://{SPARE}/api/v1", BACKUP_BASE_URL=f"https://{MAIN}/api/v1")
    net.answers.update({SPARE: Reply(502), MAIN: Reply(200, TEXT)})
    ask(cfg)
    assert "provider" in net.calls[0]["body"] and "provider" not in net.calls[-1]["body"]
    assert usage.snapshot()["cost"] == {"RUB": 0.5}


@pytest.mark.parametrize("status", [401, 402, 403])
def test_text_refusal_goes_to_the_backup_at_once(monkeypatch, net, status):
    cfg = settings(monkeypatch)
    net.answers.update({MAIN: Reply(status, text="нет средств"), SPARE: Reply(200, TEXT)})
    lines = []
    assert ask(cfg, lines)[0] == "готово"
    assert net.hosts() == [MAIN, SPARE] and lines[0].startswith("ВНИМАНИЕ:")


def test_text_bad_request_and_empty_answer_stay_with_the_main(monkeypatch, net):
    cfg = settings(monkeypatch)
    net.answers.update({MAIN: Reply(400, text="response_format не поддержан"), SPARE: Reply(200, TEXT)})
    with pytest.raises(RuntimeError) as failed:
        ask(cfg)
    assert not isinstance(failed.value, providers.Down) and net.hosts() == [MAIN] * llm.ATTEMPTS

    net.calls.clear()
    empty = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}], "usage": {"cost": 0.1}}
    net.answers[MAIN] = Reply(200, empty)
    with pytest.raises(llm.EmptyAnswerError):
        ask(cfg)
    assert SPARE not in net.hosts() and providers.state()["last_switch"] is None

    net.calls.clear()
    net.answers[MAIN] = Reply(200, TEXT)

    def never(text):
        raise ValueError("не то")
    with pytest.raises(llm.EmptyAnswerError):
        ask(cfg, check=never)
    assert SPARE not in net.hosts()


def test_text_with_its_own_address_has_no_backup(monkeypatch, net):
    cfg = settings(monkeypatch, SUMMARY_BASE_URL="http://lm.local:1234/v1", SUMMARY_API_KEY="lm")
    net.answers.update({"lm.local": Reply(503), SPARE: Reply(200, TEXT)})
    with pytest.raises(RuntimeError):
        ask(cfg)
    assert net.hosts() == ["lm.local"] * llm.ATTEMPTS


# ---------- проверка ключа: бесплатные запросы, без единого вызова модели ----------

MODELS = {"data": [{"id": "deepseek/deepseek-v4.1-flash"}, {"id": "openai/gpt-6-luna"}]}
KEY_INFO = {"data": {"label": "sk-…", "limit": None, "limit_remaining": None}}


@pytest.fixture
def gate(monkeypatch):
    """Поставщик для проверки ключа: что отвечает адрес (хост, путь) и что у него спросили.
    Отвечает только на GET — любой другой запрос (а значит, и платный вызов модели) уронит тест."""
    answers: dict[tuple[str, str], object] = {}
    asked: list[dict] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, timeout=None):
            host, path = httpx.URL(url).host, httpx.URL(url).path.rsplit("/", 1)[-1]
            asked.append({"host": host, "path": path, "key": headers["Authorization"]})
            answer = answers[(host, path)]
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)

    class Gate:
        pass

    gate = Gate()
    gate.answers, gate.asked = answers, asked
    gate.set = lambda host, key, models=None: answers.update({(host, "key"): key,
                                                              (host, "models"): models or Reply(200, MODELS)})
    return gate


def verdict(provider, models=()):
    return asyncio.run(providers.check(provider, tuple(models)))


def test_key_check_says_the_key_is_accepted_and_asks_nothing_paid(monkeypatch, gate):
    cfg = settings(monkeypatch)
    gate.set(MAIN, Reply(200, KEY_INFO))
    found = verdict(cfg.speech_provider, [cfg.summary_model, *cfg.actions_models])
    assert found["state"] == providers.CHECK_OK and found["text"].startswith("ключ принят") and not found["hint"]
    assert "deepseek/deepseek-v4.1-flash" in found["text"]
    assert gate.asked == [{"host": MAIN, "path": "key", "key": "Bearer main-key"},
                          {"host": MAIN, "path": "models", "key": "Bearer main-key"}]
    assert "main-key" not in json.dumps(found, ensure_ascii=False)


def test_key_check_names_the_refusal_in_words(monkeypatch, gate):
    cfg = settings(monkeypatch)
    main = cfg.speech_provider
    gate.set(MAIN, Reply(401, {"error": {"message": "No auth credentials found", "code": 401}}))
    found = verdict(main)
    assert found["state"] == providers.CHECK_REFUSED and "ключ не принят (HTTP 401)" in found["text"]
    assert "OPENAI_API_KEY" in found["hint"] and "docker compose up -d" in found["hint"]

    gate.set(MAIN, Reply(402, {"error": "Insufficient credits"}))
    found = verdict(main)
    assert found["state"] == providers.CHECK_REFUSED and "баланса" in found["text"] and "RouterAI" in found["hint"]

    gate.set(MAIN, Reply(200, {"data": {"limit": 5, "limit_remaining": 0}}))
    found = verdict(main)
    assert found["state"] == providers.CHECK_REFUSED and "предел расходов" in found["text"]

    # у RouterAI ключ без предела — limit 0, а limit_remaining уходит в минус на расход
    gate.set(MAIN, Reply(200, {"data": {"limit": 0.0, "limit_remaining": -29.5, "usage": 29.5}}))
    assert verdict(main)["state"] != providers.CHECK_REFUSED

    # ключ запасного называется в .env по-своему
    gate.set(SPARE, Reply(403, {"error": "forbidden"}))
    assert "BACKUP_API_KEY" in verdict(cfg.backup_provider)["hint"]


def test_key_check_tells_a_silent_provider_from_a_refusal(monkeypatch, gate):
    cfg = settings(monkeypatch)
    main = cfg.speech_provider
    gate.set(MAIN, httpx.ConnectError("нет связи"))
    found = verdict(main)
    assert found["state"] == providers.CHECK_DOWN and "поставщик недоступен" in found["text"] and "ConnectError" in found["text"]
    gate.set(MAIN, httpx.ReadTimeout("долго"))
    assert verdict(main)["state"] == providers.CHECK_DOWN
    for code in (429, 500, 503):
        gate.set(MAIN, Reply(code, text="позже"))
        found = verdict(main)
        assert found["state"] == providers.CHECK_DOWN and f"HTTP {code}" in found["text"], code


def test_key_check_notices_a_model_the_provider_does_not_have(monkeypatch, gate):
    cfg = settings(monkeypatch, SUMMARY_MODEL="deepseek/deepseek-v4.1-flsh")
    gate.set(MAIN, Reply(200, KEY_INFO))
    found = verdict(cfg.speech_provider, [cfg.summary_model, *cfg.actions_models])
    assert found["state"] == providers.CHECK_WARN and "deepseek/deepseek-v4.1-flsh" in found["text"]
    assert "openai/gpt-6-luna" not in found["text"] and "SUMMARY_MODEL" in found["hint"]


def test_key_check_of_an_address_without_key_info(monkeypatch, gate):
    """Своя модель (LM Studio) сведений о ключе не даёт: проверка говорит, что адрес отвечает,
    и не выдаёт это за принятый ключ. Адрес, по которому нет и списка моделей, — неверный."""
    cfg = settings(monkeypatch, SUMMARY_BASE_URL="http://lm.local:1234/v1", SUMMARY_API_KEY="lm")
    local = cfg.text_provider
    gate.set("lm.local", Reply(404, text="not found"), Reply(200, {"data": [{"id": "qwen3-14b"}]}))
    found = verdict(local, ["qwen3-14b"])
    assert found["state"] == providers.CHECK_WARN and "адрес отвечает" in found["text"]
    gate.set("lm.local", Reply(404, text="not found"), Reply(404, text="not found"))
    found = verdict(local)
    assert found["state"] == providers.CHECK_DOWN and "HTTP 404" in found["text"] and "/v1" in found["hint"]


def test_key_check_without_a_key_asks_nobody(monkeypatch, gate):
    cfg = settings(monkeypatch, OPENAI_API_KEY="")
    found = verdict(cfg.speech_provider)
    assert found["state"] == providers.CHECK_REFUSED and found["text"] == "ключ не задан" and "OPENAI_API_KEY" in found["hint"]
    assert gate.asked == []
