"""Разбор записи между расшифровкой и протоколом.

Один дешёвый вызов той же текстовой модели по готовой расшифровке отвечает на
вопросы, на которые до сих пор отвечал администратор или не отвечал никто:
что это за запись (совещание, лекция, созвон, постановка задачи), как её назвать
и кто скрывается
за метками S1, S2, S3.

Ради этого шаг и заведён. Автоматика раньше составляла протокол по шаблону
совещания для любой записи, а метки говорящих оставались безымянными, хотя
имена в разговоре звучат — просто рядом с чужой репликой, и модель протокола,
занятая структурой документа, их не сводила.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import llm, templates

SYSTEM = ("Ты аккуратный аналитик. Работаешь только с тем, что есть в тексте. "
          "Отвечаешь одним объектом JSON без пояснений.")

FILE_SUFFIX = ".triage.json"

# Разбор сам оценивает, насколько уверен в типе записи. Всё, что не «высокая», —
# повод посмотреть на запись глазами: либо разговор смешанный, либо шаблона под
# такие записи у нас просто нет.
SURE = "высокая"

HEARD_MAX = 6      # разночтений одного имени: больше — уже не одно имя, а шум


def _text(value, limit: int) -> str:
    return str(value).strip()[:limit] if isinstance(value, (str, int, float)) else ""


def parse(raw: str, kinds: dict | None = None) -> dict:
    """Разбирает ответ модели и приводит его к известным полям.

    kinds — типы, из которых разбор выбирал (templates.types): стандартные и свои.
    Не переданы — только стандартные.

    Всё, что модель придумала сверх схемы, отбрасывается: дальше эти значения
    уходят в протокол и на страницу, и держать там непроверенный текст модели
    незачем.
    """
    body = raw.strip()
    if body.startswith("```"):
        body = body.split("```")[1]
        if body.lstrip().lower().startswith("json"):
            body = body.lstrip()[4:]
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("ответ разбора — не объект JSON")

    key = _text(data.get("type"), 40)
    if key not in (templates.TEMPLATES if kinds is None else kinds):
        # Не «берём совещание по умолчанию»: так однажды прошёл ответ, где
        # вместо разбора была схема JSON с type «object», — протокол вышел без
        # имён и названия, а в журнале значилось «готово». Такой ответ
        # переспрашивается, а не выдаётся за результат.
        raise ValueError(f"тип записи «{key or 'не назван'}» не из списка")

    speakers = []
    for item in (data.get("speakers") or []):
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"), 12)
        if not label:
            continue
        name = _text(item.get("name"), 80)
        # heard — как ещё распознавание записало это же имя: модель выбрала один вариант,
        # и остальные не должны пропасть молча — протокол перечислит их в «Комментариях»
        heard = []
        for x in item["heard"] if isinstance(item.get("heard"), list) else []:
            x = _text(x, 80)
            if x and x != name and x not in heard:
                heard.append(x)
        speakers.append({"label": label, "name": name, "heard": heard[:HEARD_MAX],
                         "role": _text(item.get("role"), 120)})

    same = []
    for item in (data.get("same_person") or []):
        if not isinstance(item, dict):
            continue
        labels = [_text(x, 12) for x in (item.get("labels") or [])]
        labels = [x for x in labels if x]
        if len(labels) > 1:
            same.append({"labels": labels, "why": _text(item.get("why"), 200)})

    return {
        "type": key,
        "type_reason": _text(data.get("type_reason"), 200),
        "confidence": _text(data.get("confidence"), 20),
        "title": _text(data.get("title"), 120),
        "date": _text(data.get("date"), 10),
        "gist": _text(data.get("gist"), 300),
        "topics": [t for t in (_text(x, 60) for x in (data.get("topics") or [])) if t][:7],
        "speakers": speakers,
        "same_person": same,
    }


def unsure(data: dict) -> bool:
    """Разбор не уверен в типе — значит, подходящего шаблона может и не быть."""
    return (data.get("confidence") or "").strip().lower() != SURE


def notes(data: dict, signed: list[str] = (), unsigned: set[str] | None = None,
          source: str = "") -> str:
    """Справка для шага протокола: кто есть кто и о чём вообще запись.

    signed — ФИО, подписанные вручную (в расшифровке они уже стоят на
    репликах вместо меток); unsigned — метки, у которых остались реплики без
    ручной подписи: только про них и нужна догадка разбора; source — имя и
    дата файла записи: дату встречи в разговоре называют редко, а в имени
    файла она обычно есть.
    """
    lines = []
    if source:
        lines.append(f"Файл записи: {source}")
    if data.get("title"):
        lines.append(f"Название записи: {data['title']}")
    if data.get("date"):
        lines.append(f"Дата встречи: {data['date']}")
    if data.get("gist"):
        lines.append(f"Суть: {data['gist']}")
    for group in data.get("same_person") or []:
        lines.append("Скорее всего один и тот же человек: " + ", ".join(group["labels"])
                     + (f" — {group['why']}" if group.get("why") else "")
                     + ". В протоколе считай их одним участником и укажи это.")
    if signed:
        lines.append("Участники, подписанные вручную участником встречи (это точно; "
                     "в расшифровке их реплики уже стоят под этими ФИО вместо меток): "
                     + "; ".join(signed) + ".")
    known = [s for s in data.get("speakers") or []
             if (unsigned is None or s["label"] in unsigned) and (s.get("name") or s.get("role"))]
    if known:
        lines.append("Говорящие (сопоставлены отдельным разбором той же расшифровки; "
                     "если расшифровка этому противоречит — верь расшифровке):")
        for s in known:
            who = ", ".join(x for x in (s.get("name"), s.get("role")) if x)
            heard = (f" (имя на слух: в записи это же имя звучит ещё как {', '.join(s['heard'])} — "
                     f"человек один; в протоколе перечисли варианты в «Комментариях»)"
                     if s.get("name") and s.get("heard") else "")
            lines.append(f"- {s['label']} — {who}{heard}")
    return "\n".join(lines)


def load_saved(file_dir: Path, stem: str, log=lambda s: None, kinds: dict | None = None,
               words: str | None = None) -> dict | None:
    """Разбор прошлого запуска, если он уже лежит рядом с расшифровкой.

    Разбор зависит только от расшифровки, а она переиспользуется — значит
    и платить за разбор второй раз при повторном запуске без force незачем.

    Кроме одного случая: набор шаблонов с тех пор изменился. Разбор выбирал
    из того списка, который был на тот момент, и после появления нового
    шаблона его ответ описывает уже не все возможные варианты — «совещание»
    в старом файле могло означать «ничего ближе не нашлось». Поэтому вместе
    с разбором пишется список ключей, из которых он выбирал, и при
    несовпадении разбор считается устаревшим. Пересчёт стоит копейки,
    расшифровка при этом не трогается. Так же устаревает разбор, выбравший
    свой тип, который с тех пор удалили.

    И ещё один: словарь папки или записи изменился. Разбор сводил имена с тем списком
    участников, который был тогда; words — отпечаток нынешнего (dictionary.fingerprint),
    и при несовпадении разбор пересчитывается. None — словарь не сверять: так разбор читают
    страницы, им нужен тот, что есть.
    """
    kinds = templates.TEMPLATES if kinds is None else kinds
    path = file_dir / "transcript" / f"{stem}{FILE_SUFFIX}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("type") not in kinds:
        return None
    if data.get("templates") != sorted(kinds):
        log("разбор сделан при другом наборе шаблонов — пересчитываю")
        return None
    if words is not None and data.get("dictionary", "") != words:
        log("разбор сделан с другим списком участников и сокращений — пересчитываю")
        return None
    return data


async def triage_file(file_dir: Path, stem: str, prompt: str, cfg,
                      log=lambda s: None, glossary: str = "", kinds: dict | None = None,
                      words: str = "") -> dict:
    """kinds — типы, перечисленные в задании prompt (templates.types); не переданы — стандартные.
    words — отпечаток словаря папки и записи, с которым делается разбор (сам словарь — в glossary)."""
    kinds = templates.TEMPLATES if kinds is None else kinds
    src = file_dir / "transcript" / f"{stem}.txt"
    if not src.is_file():
        raise RuntimeError("нет расшифровки для разбора")
    text = src.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError("расшифровка пустая")
    # Расшифровка идёт целиком, без сокращения: имена звучат где угодно, а
    # протокол следующим шагом всё равно получает её всю — в контекст модели
    # она влезает.
    t0 = time.perf_counter()
    log(f"разбор записи: {len(text)} символов")

    raw, api_usage = await llm.chat(
        cfg, "triage", SYSTEM, f"{prompt}{glossary}\n\n---\nРАСШИФРОВКА:\n\n{text}",
        response_format={"type": "json_object"}, check=lambda raw: parse(raw, kinds),
        log=lambda s: log(f"разбор: {s}"))
    data = parse(raw, kinds)
    # из чего выбирали: по этому списку потом видно, устарел ли разбор
    data["templates"] = sorted(kinds)
    data["dictionary"] = words
    data["model"] = cfg.summary_model
    data["source_chars"] = len(text)
    data["wall_clock_sec"] = round(time.perf_counter() - t0, 2)
    data["usage"] = api_usage

    (src.parent / f"{stem}{FILE_SUFFIX}").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    named = sum(1 for s in data["speakers"] if s["name"])
    log(f"разбор готов за {data['wall_clock_sec']:.0f} с: "
        f"«{kinds[data['type']]['title']}» "
        f"(уверенность {data['confidence'] or 'не указана'}, {data['type_reason']}), "
        f"название «{data['title'] or 'не предложено'}», "
        f"имён у говорящих: {named} из {len(data['speakers'])}"
        + (f", похоже, одним человеком записаны: "
           + "; ".join("+".join(g["labels"]) for g in data["same_person"])
           if data["same_person"] else ""))
    if unsure(data):
        log(f"внимание: тип записи определён неуверенно "
            f"(уверенность {data['confidence'] or 'не указана'}) — "
            f"возможно, подходящего шаблона под такие записи ещё нет")
    return data
