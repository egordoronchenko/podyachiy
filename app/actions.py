"""Поручения совещания — отдельным шагом перед протоколом.

Одним запросом модель протокола теряет четверть поручений: занятая структурой
документа, она сама решает, какие мелкие «сделаю» достойны строки, и каждый
раз решает по-разному.

Узкий запрос «выпиши только поручения» находит примерно столько же, но разные
модели пропускают разное. Поэтому расшифровку независимо читают несколько
моделей, а протокол получает все списки сразу и обязан перенести каждую
строку: так теряется заметно меньше. Цена — немного лишних строк
(планы и реплики, записанные задачами): лишнюю строку в протоколе видно и
можно вычеркнуть, потерянную — нет.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

from . import llm, summarize

SYSTEM = "Ты аккуратный аналитик. Работаешь только с тем, что есть в тексте."

FILE_SUFFIX = ".actions.md"

NOTE = """

СПИСКИ ПОРУЧЕНИЙ, РАСПОРЯЖЕНИЙ И РЕШЕНИЙ (получены независимыми проходами по той же расшифровке;
один проход находит то, что пропустил другой, поэтому бери объединение списков, а совпадающие строки считай одной).
Каждая строка списка обязана попасть в протокол: задачи — в «Задачи», решения и распоряжения —
в «Решения». Не выбрасывай строки и не сливай разные поручения в одно ради краткости; объединять
можно только явный повтор одного и того же. Исполнителя, срок и метку времени бери из списка.
Если расшифровка явно противоречит строке списка — верь расшифровке и скажи об этом в
«Комментариях». Если заметишь в расшифровке поручение, которого в списке нет, — добавь его.
Строку, которая по расшифровке оказалась шуткой или уже выполнена на самом совещании, не переноси.
Тип строки проверяй сам: «решение» в списке, у которого есть исполнитель и действие, — это задача.

"""


def rows(table: str) -> int:
    """Сколько поручений в ответе модели: строки таблицы без шапки и черты."""
    lines = [ln for ln in table.splitlines() if ln.startswith("|")]
    return max(len(lines) - 2, 0)


async def extract_file(file_dir: Path, stem: str, prompt: str, cfg,
                       log=lambda s: None, notes: str = "", glossary: str = "",
                       who: list[str] | None = None) -> str:
    """Списки поручений от каждой модели из cfg.actions_models — блок для протокола.

    Модели читают расшифровку одновременно: шаг длится столько, сколько самая
    медленная из них, а не сумму.
    """
    text, _ = summarize.load_source(file_dir, stem, who)
    user = f"{prompt}{glossary}\n\n---\nСПРАВКА ПО ЗАПИСИ:\n\n{notes}\n\n---\nРАСШИФРОВКА:\n\n{text}"
    t0 = time.perf_counter()

    async def one(model: str) -> str:
        answer, _ = await llm.chat(replace(cfg, summary_model=model), "actions", SYSTEM, user,
                                   budget=cfg.summary_max_tokens,
                                   log=lambda s: log(f"поручения ({model}): {s}"))
        return answer

    lists = await asyncio.gather(*(one(m) for m in cfg.actions_models))
    block = "\n\n".join(f"Список {i + 1} (независимый проход):\n\n{lst}"
                        for i, lst in enumerate(lists))
    out_dir = file_dir / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stem}{FILE_SUFFIX}").write_text(block + "\n", encoding="utf-8")
    log("поручения выписаны за {:.0f} с: {}".format(
        time.perf_counter() - t0,
        ", ".join(f"{m} — строк {rows(lst)}" for m, lst in zip(cfg.actions_models, lists))))
    return NOTE + block
