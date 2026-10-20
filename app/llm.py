"""Один вызов текстовой модели: запрос, повторы, проверка что ответ не пустой.

Общий для разбора, поручений и протокола: все шаги ходят к поставщику текстовой
модели (SUMMARY_BASE_URL, по умолчанию тот же, что у речи; не ответил он — к
запасному, см. providers.py) через OpenAI-совместимый эндпоинт и одинаково страдают от пустого ответа
рассуждающей модели, поэтому логика повторов живёт здесь, а не в каждом шаге.
"""
from __future__ import annotations

import asyncio
import re
from typing import Callable

import httpx

from . import providers, usage
from .speech import RateLimited, describe_error, rate_limit_pause, retry_after

# Штатно лимит ответа не задаётся вовсе (SUMMARY_MAX_TOKENS пуст) — предел
# определяет провайдер. Числа ниже нужны, только если ответ всё равно обрезался
# по длине: тогда вызов ставит явный бюджет и повышает его.
FALLBACK_MAX_TOKENS = 150000
MAX_TOKENS_CEILING = 300000
ATTEMPTS = 4

# Блок рассуждений в начале ответа. Локальные рассуждающие модели (Qwen3) пишут
# его прямо в content: <think>…</think>, а при шаблоне, где открывающий тег стоит
# в самом запросе, — только хвост с закрывающим. Незакрытый блок значит, что
# ответ оборвался ещё на рассуждениях и самого ответа нет.
_THINK_CLOSED = re.compile(r"\A\s*(?:<think>)?(?:(?!<think>).)*?</think>", re.S | re.I)
_THINK_OPEN = re.compile(r"\A\s*<think>", re.I)


def strip_reasoning(content: str) -> str:
    """Ответ модели без блока рассуждений в начале."""
    content = content or ""
    while True:
        m = _THINK_CLOSED.match(content)
        if not m:
            break
        content = content[m.end():]
    if _THINK_OPEN.match(content):
        return ""
    return content.strip()


class EmptyAnswerError(RuntimeError):
    """Модель ответила, деньги списаны, а годного текста нет."""


async def chat(
    cfg,
    stage: str,
    system: str,
    user: str,
    *,
    budget: int = 0,
    response_format: dict | None = None,
    check: Callable[[str], object] | None = None,
    log=lambda s: None,
) -> tuple[str, dict]:
    """Просит модель ответить и проверяет, что ответ действительно пришёл.

    Пустой ответ здесь не редкость и не ошибка сети: у рассуждающих моделей
    max_tokens ограничивает рассуждения и ответ вместе, и при тесном лимите
    весь бюджет уходит в рассуждения, а content возвращается пустым. Деньги
    при этом списываются.

    Поэтому по умолчанию лимит не передаётся вовсе — предел определяет сам
    провайдер по модели. Если ответ всё равно пришёл пустым: обрезало по длине
    (finish_reason=length) — ставим явный бюджет и повышаем его, обрезало не по
    длине — переспрашиваем, и только потом шаг падает с внятной ошибкой.

    check — проверка ответа по существу: бросает ValueError, если ответ пришёл,
    но не годится (разбору вместо JSON с типом записи однажды вернулась схема
    JSON). Такой ответ переспрашивается так же, как пустой.
    """
    body = {
        "model": cfg.summary_model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    if response_format is not None:
        body["response_format"] = response_format
    if cfg.summary_reasoning:
        # без этого рассуждающая модель тратит на скрытые рассуждения до девяти
        # десятых ответа: и времени, и денег, и лимита выдачи
        body["reasoning"] = {"effort": cfg.summary_reasoning}

    async def ask(provider: providers.Provider) -> tuple[str, dict]:
        return await _ask(provider, cfg, stage, dict(body), budget, check, log)

    return await providers.through(cfg.text_provider, cfg.text_backup, ask, log)


async def _ask(provider: providers.Provider, cfg, stage: str, body: dict, budget: int,
               check: Callable[[str], object] | None, log) -> tuple[str, dict]:
    """Вызов у одного поставщика, со всеми повторами. Поставщик не ответил (сеть, таймаут,
    5xx, 429 после повторов; 401, 402, 403 — сразу) — providers.Down; негодный ответ и
    отказ на сам запрос — обычная ошибка, запасному такой вызов не передаётся."""
    prefs = cfg.provider_prefs() if provider.routes else {}
    if prefs:
        body["provider"] = prefs
    async with httpx.AsyncClient() as client:
        last = ""
        for attempt in range(ATTEMPTS):
            try:
                if budget > 0:
                    body["max_tokens"] = budget
                else:
                    body.pop("max_tokens", None)
                r = await client.post(
                    provider.url("/chat/completions"),
                    headers={"Authorization": f"Bearer {provider.api_key}",
                             "Content-Type": "application/json"},
                    json=body, timeout=cfg.summary_timeout_sec,
                )
                if r.status_code == 429:
                    # провайдер просит подождать — пауза длиннее, чем после обычного сбоя
                    raise RateLimited("HTTP 429", retry_after(r))
                if r.status_code in providers.REFUSED:
                    raise providers.Down(
                        f"HTTP {r.status_code}: поставщик {provider.title} отказал в доступе — "
                        f"проверьте ключ ({provider.key_env} в .env), баланс и открыта ли ключу "
                        f"модель {cfg.summary_model}. Ответ поставщика: {r.text[:200]}", refused=True)
                if r.status_code >= 500:
                    raise providers.Fault(f"HTTP {r.status_code}")
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    raise RuntimeError(str(data["error"])[:300])
                api_usage = data.get("usage") or {}
                usage.add(stage, cfg.summary_model, api_usage, provider)

                choice = (data.get("choices") or [{}])[0]
                # рассуждения в отдельном поле (reasoning_content) в ответ не идут:
                # читается только content, а из него убирается блок <think>
                content = strip_reasoning((choice.get("message") or {}).get("content"))
                finish = choice.get("finish_reason") or "?"
                if content and finish == "length":
                    # Текст есть, но оборван на полуслове: рассуждения съели
                    # лимит выдачи. Выглядит он как готовый ответ, поэтому
                    # без этой проверки протокол без хвоста значился бы готовым.
                    if budget < MAX_TOKENS_CEILING and attempt < ATTEMPTS - 1:
                        budget = (FALLBACK_MAX_TOKENS if budget <= 0
                                  else min(budget * 2, MAX_TOKENS_CEILING))
                        log(f"ответ оборван по длине на {len(content)} символах — "
                            f"ставлю бюджет {budget} токенов и повторяю")
                        continue
                    log("внимание: ответ оборван по длине, а поднять лимит больше "
                        "некуда — текст неполный")
                if content and check is not None:
                    try:
                        check(content)
                    except ValueError as e:
                        last = f"ответ не годится: {str(e)[:200]}"
                        log(last)
                        if attempt < ATTEMPTS - 1:
                            log("переспрашиваю")
                            continue
                        raise EmptyAnswerError(last)
                if content:
                    return content, api_usage

                reasoning = ((api_usage.get("completion_tokens_details") or {})
                             .get("reasoning_tokens") or 0)
                last = (f"модель вернула пустой ответ (finish_reason={finish}, "
                        f"лимит {budget or 'не задавался'}, "
                        f"на рассуждения ушло токенов: {reasoning})")
                log(last)
                if finish == "length" and budget < MAX_TOKENS_CEILING:
                    budget = (FALLBACK_MAX_TOKENS if budget <= 0
                              else min(budget * 2, MAX_TOKENS_CEILING))
                    log(f"ответ обрезан по длине — ставлю явный бюджет "
                        f"{budget} токенов и повторяю")
                    continue
                if attempt < ATTEMPTS - 1:
                    # рассуждающая модель может «додумать» и не написать ничего:
                    # finish_reason=stop при пустом тексте. Это разовый сбой,
                    # и переспросить дешевле, чем оставить задание без ответа
                    log("пустой ответ без обрыва по длине — переспрашиваю")
                    await asyncio.sleep(2 ** attempt * 3)
                    continue
                raise EmptyAnswerError(last)
            except (EmptyAnswerError, providers.Down):
                raise
            except Exception as e:
                last = describe_error(e)
                if attempt == ATTEMPTS - 1:
                    raise (providers.Down if providers.is_fault(e) else RuntimeError)(last)
                if providers.resting(provider):
                    # пока шли повторы, другой вызов уже увёл запросы к запасному — ждать незачем
                    raise providers.Down(last)
                delay = (rate_limit_pause(e, attempt) if isinstance(e, RateLimited)
                         else 2 ** attempt * 3)
                log(f"сбой запроса к {provider.title} ({last}), попытка {attempt + 1} из {ATTEMPTS} — "
                    f"повторяю через {delay:.0f} с")
                await asyncio.sleep(delay)
    raise RuntimeError(last)
