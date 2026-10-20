from app.config import Settings

KEYS = ("PROVIDER_MIN_THROUGHPUT", "PROVIDER_MAX_PRICE", "PROVIDER_QUANTIZATIONS", "OPENAI_BASE_URL",
        "SUMMARY_BASE_URL", "SUMMARY_API_KEY", "BACKUP_BASE_URL", "BACKUP_API_KEY", "COST_CURRENCY")


def settings(monkeypatch, **env):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings.from_env()


def prefs(monkeypatch, **env):
    return settings(monkeypatch, **env).provider_prefs()


def test_defaults_prefer_fast_cap_price_and_skip_heavy_compression(monkeypatch):
    p = prefs(monkeypatch)
    assert p["preferred_min_throughput"] == 150
    assert p["max_price"] == {"completion": 1.5}
    assert "fp4" not in p["quantizations"] and "fp8" in p["quantizations"]


def test_zero_and_empty_switch_rules_off(monkeypatch):
    assert prefs(monkeypatch, PROVIDER_MIN_THROUGHPUT="0", PROVIDER_MAX_PRICE="0",
                 PROVIDER_QUANTIZATIONS="") == {}


def test_price_accepts_comma(monkeypatch):
    assert prefs(monkeypatch, PROVIDER_MAX_PRICE="0,9")["max_price"] == {"completion": 0.9}


def test_rules_go_only_to_openrouter(monkeypatch):
    """Правила выбора поставщика модели понимает только OpenRouter — по хосту адреса, а не по
    вхождению слова в адрес."""
    routes = lambda **env: settings(monkeypatch, **env).text_provider.routes
    assert routes() and routes(OPENAI_BASE_URL="https://eu.openrouter.ai/api/v1")
    assert not routes(SUMMARY_BASE_URL="http://host.docker.internal:1234/v1")
    assert not routes(OPENAI_BASE_URL="https://example.org/openrouter.ai/v1")
    assert not routes(OPENAI_BASE_URL="https://routerai.ru/api/v1")
    assert routes(OPENAI_BASE_URL="http://localhost:1234/v1", SUMMARY_BASE_URL="https://openrouter.ai/api/v1")
