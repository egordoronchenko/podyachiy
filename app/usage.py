"""Учёт расхода на поставщиков по текущему заданию.

Поставщик возвращает стоимость вызова в поле usage (usage.cost),
и это единственный честный источник цифры: считать по токенам самостоятельно
означает разойтись с биллингом.

Валюта у стоимости — та, в которой считает поставщик: RouterAI — рубли, OpenRouter —
доллары. Запись может пройти через обоих (основной лёг посреди обработки), поэтому сумма
— не число, а разбивка по валютам: {"RUB": 12.1, "USD": 0.03}. Валюты не пересчитываются
и не складываются между собой.

Заданий идёт несколько одновременно (WORKERS), поэтому накопитель у каждого
свой. Он лежит в переменной контекста: каждый обработчик — отдельная задача
asyncio со своим контекстом, а всё, что задание запускает внутри себя, — потоки
asyncio.to_thread и вложенные задачи (gather по моделям поручений) — получает
копию контекста с тем же самым списком. Вызов модели вне задания (reset не
звали) — ошибка в коде, а не расход «в никуда»: add упадёт с LookupError.
"""
from __future__ import annotations

from contextvars import ContextVar

_records: ContextVar[list[dict]] = ContextVar("usage_records")

# До появления второго поставщика суммы хранились одним числом и валюту не называли:
# сервис работал через OpenRouter, в долларах. Так читаются старые строки журналов.
LEGACY_CURRENCY = "USD"
LEGACY_PROVIDER = "OpenRouter"
SIGNS = {"USD": "$", "RUB": "₽"}


def reset() -> None:
    """Начало задания: с этого места расходы копятся в его собственный список."""
    _records.set([])


def _cost_of(usage: dict) -> float:
    for key in ("cost", "total_cost"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        # некоторые шлюзы кладут разбивку: {"cost": {"total": 0.42}}
        if isinstance(value, dict):
            total = value.get("total")
            if isinstance(total, (int, float)):
                return float(total)
    return 0.0


def amounts(value) -> dict[str, float]:
    """Сумма как разбивка по валютам. Число — сумма из времён одной валюты."""
    if isinstance(value, dict):
        return {cur: float(v) for cur, v in value.items() if v}
    return {LEGACY_CURRENCY: float(value)} if value else {}


def plus(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    total = {cur: round(a.get(cur, 0.0) + b.get(cur, 0.0), 6) for cur in sorted({*a, *b})}
    return {cur: v for cur, v in total.items() if v}


def text(cost: dict[str, float]) -> str:
    """Сумма для лога: «12.10 ₽ + 0.03 $»."""
    return " + ".join(f"{v:.2f} {SIGNS.get(cur, cur)}" for cur, v in cost.items())


def add(stage: str, model: str, usage: dict | None, provider) -> None:
    """stage — speech / triage / actions / summary; usage — поле usage из ответа API;
    provider — кто ответил (providers.Provider): от него название и валюта стоимости."""
    usage = usage if isinstance(usage, dict) else {}
    _records.get().append({
        "stage": stage,
        "model": model,
        "provider": provider.title,
        "currency": provider.currency,
        "cost": _cost_of(usage),
        "usage": {k: v for k, v in usage.items() if isinstance(v, (int, float, str))},
    })


def _split(records: list[dict], *keys: str) -> list[dict]:
    """Расход по сочетаниям полей keys — в порядке первого появления."""
    sums: dict[tuple, float] = {}
    for r in records:
        key = tuple(r[k] for k in keys)
        sums[key] = sums.get(key, 0.0) + r["cost"]
    return [{**dict(zip(keys, key)), "cost": round(cost, 6)} for key, cost in sums.items()]


def snapshot() -> dict:
    records = _records.get()
    paid = [r for r in records if r["cost"]]
    total: dict[str, float] = {}
    for r in paid:
        total = plus(total, {r["currency"]: r["cost"]})
    by_stage: dict[str, dict] = {}
    for r in records:
        s = by_stage.setdefault(r["stage"], {"calls": 0, "cost": {}, "model": r["model"]})
        s["calls"] += 1
        if r["cost"]:
            s["cost"] = plus(s["cost"], {r["currency"]: r["cost"]})
    return {
        # этап поручений идёт двумя моделями, а вызов мог уйти запасному — журналу расходов
        # нужна каждая модель у каждого поставщика отдельно
        "by_model": _split(records, "stage", "model", "provider", "currency"),
        "by_provider": _split(paid, "provider", "currency"),
        "cost": total,
        "calls": len(records),
        "by_stage": by_stage,
    }
