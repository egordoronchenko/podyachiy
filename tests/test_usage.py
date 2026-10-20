"""Учёт расходов: у каждого задания свой накопитель, сколько бы их ни шло одновременно."""
import asyncio
import contextvars

import pytest

from app import providers, usage

RUB = providers.make("https://routerai.ru/api/v1", "k", "OPENAI_API_KEY", "USD")
USD = providers.make("https://openrouter.ai/api/v1", "k", "BACKUP_API_KEY", "USD")


async def job(name: str, price: float, calls: int) -> dict:
    """Задание, как его ведёт обработчик: вызовы из самой задачи, из потока (asyncio.to_thread)
    и из вложенных задач (gather — так идут поручения двумя моделями)."""
    usage.reset()

    async def nested(model: str) -> None:
        await asyncio.sleep(0.01)
        usage.add("actions", model, {"cost": price}, USD)

    for _ in range(calls):
        await asyncio.sleep(0.01)                      # уступаем соседним заданиям посреди своего
        usage.add("speech", name, {"cost": price}, USD)
        await asyncio.to_thread(usage.add, "triage", name, {"cost": price}, USD)
        await asyncio.gather(nested(f"{name}-a"), nested(f"{name}-b"))
    return usage.snapshot()


def test_parallel_jobs_count_their_costs_apart():
    async def go():
        # три обработчика — три задачи, как в сервисе
        return await asyncio.gather(*(asyncio.create_task(job(name, price, calls))
                                      for name, price, calls in (("а", 1.0, 3), ("б", 0.1, 5), ("в", 0.01, 1))))
    a, b, c = asyncio.run(go())
    assert (a["calls"], a["cost"]) == (12, {"USD": 12.0})
    assert (b["calls"], b["cost"]) == (20, {"USD": 2.0})
    assert (c["calls"], c["cost"]) == (4, {"USD": 0.04})
    assert {(m["stage"], m["model"]) for m in a["by_model"]} == {
        ("speech", "а"), ("triage", "а"), ("actions", "а-a"), ("actions", "а-b")}
    assert b["by_stage"]["actions"] == {"calls": 10, "cost": {"USD": 1.0}, "model": "б-a"}


def test_next_job_of_the_same_worker_starts_from_zero():
    async def worker():
        first = await job("а", 1.0, 1)
        second = await job("б", 0.5, 1)
        return first, second
    first, second = asyncio.run(worker())
    assert (first["cost"], second["cost"]) == ({"USD": 4.0}, {"USD": 2.0})


def test_spending_outside_a_job_is_an_error_not_a_lost_cost():
    async def stray():
        usage.add("summary", "модель", {"cost": 1.0}, USD)
    # чистый контекст: в нём задание ещё никто не начинал
    with pytest.raises(LookupError):
        contextvars.Context().run(asyncio.run, stray())


def test_each_call_keeps_its_provider_and_currency():
    """Запись началась у основного (рубли), закончилась у запасного (доллары): суммы не складываются."""
    usage.reset()
    usage.add("speech", "scribe", {"cost": 12.1}, RUB)
    usage.add("triage", "deepseek", {"cost": 0.9}, RUB)
    usage.add("actions", "deepseek", {"cost": 0.02}, USD)
    usage.add("actions", "luna", {"cost": 0.03}, USD)
    usage.add("summary", "deepseek", {}, USD)                  # стоимость не вернули — в итог не идёт
    spent = usage.snapshot()
    assert spent["cost"] == {"RUB": 13.0, "USD": 0.05} and spent["calls"] == 5
    assert spent["by_provider"] == [{"provider": "RouterAI", "currency": "RUB", "cost": 13.0},
                                    {"provider": "OpenRouter", "currency": "USD", "cost": 0.05}]
    assert {"stage": "actions", "model": "luna", "provider": "OpenRouter", "currency": "USD", "cost": 0.03} \
        in spent["by_model"]
    assert spent["by_stage"]["actions"]["cost"] == {"USD": 0.05}
    assert usage.text(spent["cost"]) == "13.00 ₽ + 0.05 $" and usage.text({}) == ""


def test_old_single_number_is_dollars():
    assert usage.amounts(0.25) == {"USD": 0.25} and usage.amounts(None) == {} and usage.amounts(0) == {}
    assert usage.amounts({"RUB": 3, "USD": 0}) == {"RUB": 3.0}
    assert usage.plus({"USD": 0.1}, {"RUB": 5.0, "USD": 0.2}) == {"RUB": 5.0, "USD": 0.3}
