"""Расшифровка с разделением говорящих: один вызов на всю запись.

Движок распознавания (штатно elevenlabs/scribe-v2) возвращает за один запрос сразу
текст, время каждого слова и метку говорящего на нём. Поэтому здесь нет ни
второй модели, ни сведения двух расшифровок: метки уже стоят на своих словах,
а времена точные, а не оценённые моделью на глаз.

Звук уходит файлом (multipart), а не base64 внутри JSON: OpenRouter принимает
большой base64 только у моделей OpenAI и Groq, у остальных отказывает уже около
4 МБ («does not support large audio inputs»), файлом же берёт до 25 МБ. RouterAI,
шлюз поверх OpenRouter, файл такого размера тоже принимает: 21 МБ (87 минут)
прошёл одним запросом 10.10.2026.

Модели разных поставщиков отличаются тремя вещами: какими параметрами включается
разделение говорящих, принимает ли модель подсказку терминов и что в списке слов
ответа словом не является. Всё это описано в одном месте — таблице моделей
(Engine); остальное (нарезка, сшивка, повторы, учёт расхода) у всех общее.

Записи длиннее полутора часов режутся на части: больше в 25 МБ не помещается.
Каждая часть нумерует говорящих заново, поэтому метки сшиваются по перехлёсту,
где одни и те же слова размечены дважды. Сырые ответы провайдера по частям
сохраняются рядом с расшифровкой: сборку из них можно переделать, не оплачивая
распознавание второй раз, а повтор после сбоя на одной из частей берёт готовые
части из них и заказывает только недостающие.
"""
from __future__ import annotations

import asyncio
import datetime
import difflib
import email.utils
import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from . import media, providers, templates, usage

CHUNK_SEC = 5400.0      # 90 минут: при 32 кбит/с это ~21,6 МБ при потолке 25 МБ
OVERLAP_SEC = 120.0     # общий хвост: по нему сшиваются метки соседних частей
BITRATE = "32k"         # mono 32 kbps — на распознавании речи слышно не хуже
CROWDED_MEETING = 5     # больше пяти голосов — разделение держится хуже
LABEL_MATCH_WORDS = 4   # меньше совпавших слов — совпадение меток случайное
SAME_MOMENT_SEC = 1.0   # одно слово в двух частях: расхождение времени не больше
RAW_DIR = "raw"         # сырые ответы провайдера по частям, рядом с расшифровкой
UNRELIABLE = 0.15       # доля слов без метки, после которой меткам верить нельзя
MARK_EVERY_SEC = 45.0   # в длинной реплике метка времени не реже — иначе момент не найти


@dataclass(frozen=True)
class Engine:
    """Модель распознавания: чем она отличается от остальных — и больше ничем."""
    id: str                             # идентификатор модели — один у OpenRouter и RouterAI
    title: str                          # название для человека
    price_hour: float                   # $ за час звука у OpenRouter — для справки, счёт идёт по usage.cost
    note: str                           # особенности одной строкой
    form: Callable[[list[str]], dict[str, str]]     # поля формы: говорящие и, если модель умеет, термины
    # Подсказка терминов: сколько фраз модель принимает одним запросом и какой длины.
    # Ноль — подсказки не действуют, и словари правят только текст протокола.
    terms_max: int = 0
    term_chars: int = 0
    not_words: frozenset[str] = frozenset()         # значения type у элементов words, которые не слова

    @property
    def takes_terms(self) -> bool:
        return self.terms_max > 0


def _provider_options(slug: str, options: dict) -> dict[str, str]:
    """Параметры самого поставщика: поле provider со строкой JSON, параметры —
    внутри options под слагом поставщика. Слаг именно поставщика, а не модели:
    у microsoft/mai-transcribe-2 это «azure». С неверным слагом ответ приходит
    без говорящих, без всякой ошибки."""
    return {"provider": json.dumps({"options": {slug: options}}, ensure_ascii=False)}


def _scribe_form(terms: list[str]) -> dict[str, str]:
    # У OpenRouter говорящих включает и общее поле формы diarize, но RouterAI
    # его молча не выполняет — ответ приходит без говорящих.
    # Параметр поставщика действует у обоих, поэтому вид запроса один.
    # Термины не передаются: elevenlabs.keyterms принимается, но на текст не влияет.
    return _provider_options("elevenlabs", {"diarize": True})


def _nova_form(terms: list[str]) -> dict[str, str]:
    # Общее поле diarize — отказ 400. Из параметров Deepgram OpenRouter пропускает
    # только punctuate, diarize, smart_format и detect_language; keyterm
    # отбрасывает молча, поэтому термины не передаются.
    return _provider_options("deepgram", {"diarize": True})


def _mai_form(terms: list[str]) -> dict[str, str]:
    # Общее поле diarize — отказ 400. Список фраз Azure действует: термины из него
    # модель пишет в точности так, как они в нём стоят. Сколько фраз уходит — fit_terms.
    options: dict = {"diarization": {"enabled": True}}
    if terms:
        options["phraseList"] = {"phrases": terms}
    return _provider_options("azure", options)


# Проверенные модели; первая — по умолчанию. Слова у всех трёх
# приходят через OpenRouter в одном виде: word, start, end и speaker — номер
# говорящего числом на каждом слове, цена — в usage.cost. У MAI метку говорящего
# поставщик ставит на фразу, но OpenRouter сам разносит её по словам, и время у
# каждого слова своё. Scribe вставляет в слова события вроде «[смеётся]»
# с type=audio_event: в расшифровке им не место.
_ENGINES = (
    Engine("elevenlabs/scribe-v2", "ElevenLabs Scribe v2", 0.11,
           "говорящих делит надёжно, термины распознаёт лучше других; цена со скидкой 50 %, без неё 0,22 $",
           _scribe_form, not_words=frozenset({"audio_event"})),
    Engine("deepgram/nova-3", "Deepgram Nova-3", 0.26,
           "говорящих делит надёжно, но чаще ошибается в терминах и английских словах",
           _nova_form),
    Engine("microsoft/mai-transcribe-2", "Microsoft MAI-Transcribe 2", 0.10,
           "единственная слышит словарь терминов; редко говорящих сливает с соседями, "
           "метка говорящего меняется только на границе фразы",
           # Пределы подсказок: у Azure список фраз рассчитан на сотни строк из отдельных слов и
           # коротких словосочетаний, длинная фраза подсказкой не работает. 500 и 60 — с запасом
           # внутри этого; на самой модели потолок не искали, общий словарь до сих пор был короче.
           _mai_form, terms_max=500, term_chars=60),
)
ENGINES: dict[str, Engine] = {e.id: e for e in _ENGINES}
DEFAULT_MODEL = _ENGINES[0].id


def models() -> list[dict]:
    """Поддерживаемые модели распознавания — для списка выбора и справки."""
    return [{"id": e.id, "title": e.title, "price_hour": e.price_hour, "note": e.note,
             "takes_terms": e.takes_terms, "default": e.id == DEFAULT_MODEL} for e in _ENGINES]


def engine(model: str) -> Engine:
    """Описание модели. Незнакомая модель — ошибка, а не попытка наугад: неверно
    включённое разделение говорящих поставщик молча не выполняет, и запись
    вышла бы одним голосом."""
    try:
        return ENGINES[model]
    except KeyError:
        raise ValueError(
            f"SPEECH_MODEL={model or '(пусто)'}: такой модели распознавания сервис не знает. "
            f"Поддерживаются: {', '.join(ENGINES)}.") from None


def spoken_words(data: dict, model: str) -> list[dict]:
    """Слова из ответа поставщика, без того, что словами не является."""
    skip = engine(model).not_words
    return [w for w in (data.get("words") or []) if w.get("type") not in skip]


def glossary_terms(prompts_dir: Path) -> list[str]:
    """Термины из словаря сервиса — по одному, для подсказки распознаванию.

    Файл тот же, что уходит текстовой модели (templates.load_glossary): строки
    с # — пояснения для человека, термины разделены запятыми и переводами строк.
    """
    path = prompts_dir / templates.GLOSSARY_FILE
    if not path.is_file():
        return []
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    return list(dict.fromkeys(t.strip() for ln in lines if not ln.startswith("#")
                              for t in ln.split(",") if t.strip()))


def fit_terms(model: str, terms: list[str], log: Callable[[str], None] = lambda s: None) -> list[str]:
    """Подсказки, которые уйдут модели распознавания, — в её пределах по числу и длине.

    terms приходят в порядке важности: словарь записи, словарь её папки и папок выше, общий
    словарь сервиса (pipeline.process). Повторы и слишком длинные фразы отбрасываются, всё,
    что сверх предела, — тоже, с конца: общий словарь страдает первым. Модели, которая
    подсказок не слышит, не уходит ничего.
    """
    e = engine(model)
    if not e.takes_terms:
        return []
    seen, fits, long = set(), [], 0
    for term in (" ".join(t.split()) for t in terms):
        if not term or term.casefold() in seen:
            continue
        seen.add(term.casefold())
        if len(term) > e.term_chars:
            long += 1
        else:
            fits.append(term)
    if long or len(fits) > e.terms_max:
        log(f"подсказки распознаванию урезаны по пределам модели: уходит {min(len(fits), e.terms_max)} "
            f"из {len(fits) + long} (не больше {e.terms_max} фраз, каждая до {e.term_chars} знаков); "
            f"первыми идут строки словаря записи, затем папки, затем общего словаря")
    return fits[:e.terms_max]


def audio_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def make_chunk(src: Path, dst: Path, start: float, length: float) -> None:
    media.run_ffmpeg(
        ["-y", "-v", "error", "-ss", str(start), "-t", str(length),
         "-i", str(src), "-vn", "-ac", "1", "-b:a", BITRATE, str(dst)],
        check=True, timeout=1800,
    )


def plan_chunks(duration: float) -> list[tuple[float, float]]:
    """Возвращает [(start_sec, length_sec)] с перехлёстом между соседями."""
    if duration <= CHUNK_SEC:
        return [(0.0, duration)]
    step = CHUNK_SEC - OVERLAP_SEC
    out = []
    start = 0.0
    while start < duration:
        out.append((start, min(CHUNK_SEC, duration - start)))
        if start + CHUNK_SEC >= duration:
            break
        start += step
    return out


# Ответ 429 «слишком много запросов»: записей идёт несколько сразу, и предел провайдера
# ближе. Это не сбой записи, а просьба подождать: пауза растёт от попытки к попытке,
# а если провайдер сам назвал срок (заголовок Retry-After) — ждём не меньше него.
RATE_LIMIT_PAUSE_SEC = 15       # первая пауза, дальше удваивается
RETRY_AFTER_MAX_SEC = 300       # дольше не ждём, что бы ни просил провайдер: задание держит обработчик


class RateLimited(providers.Fault):
    def __init__(self, text: str, wait: float):
        super().__init__(text)
        self.wait = wait        # сколько просил подождать провайдер; 0 — не сказал


def retry_after(response) -> float:
    """Секунды из заголовка Retry-After: число или дата. Нет заголовка — 0."""
    raw = (response.headers.get("retry-after") or "").strip()
    try:
        return max(float(raw), 0.0)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max((when - datetime.datetime.now(datetime.timezone.utc)).total_seconds(), 0.0)


def rate_limit_pause(e: RateLimited, attempt: int) -> float:
    return min(max(RATE_LIMIT_PAUSE_SEC * 2 ** attempt, e.wait), RETRY_AFTER_MAX_SEC)


STT_TIMEOUT_SEC = 1800          # на одну попытку: соединение, отправка части, ожидание ответа
# Повторы у одного поставщика — не дольше этого срока с первой попытки: паузы между
# быстрыми отказами (15 с, дальше вдвое — меньше восьми минут на все) в него укладываются,
# а шесть долгих попыток держали бы обработчик часами. Срок вышел — к запасному, а нет
# его — сбой записи.
STT_RETRY_WINDOW_SEC = 20 * 60


def build_form(cfg, terms: list[str] = ()) -> dict:
    """Поля запроса на распознавание: общие для всех моделей плюс то, чем
    у этой включается разделение говорящих и передаются термины."""
    form = {
        "model": cfg.model,
        "response_format": "verbose_json",
        "timestamp_granularities[]": ["word", "segment"],
        **engine(cfg.model).form(list(terms)),
    }
    if cfg.language and cfg.language != "auto":
        form["language"] = cfg.language
    return form


async def call_stt(client: httpx.AsyncClient, cfg, chunk_path: Path,
                   attempts: int = 6,
                   log: Callable[[str], None] = lambda s: None,
                   terms: list[str] = ()) -> dict:
    """Один запрос на распознавание с разделением говорящих.

    Идёт основному поставщику, а не ответил он — запасному (providers.through): вид
    запроса и имя модели у них одинаковые.

    terms — подсказки из словарей, уже в пределах модели (fit_terms); уходят только
    модели, которая их слышит (Engine.takes_terms). Остальным подсказать нечем, и термины
    правит текстовая модель по тем же словарям — в протоколе, а не в расшифровке.
    """
    form = build_form(cfg, terms)
    audio = chunk_path.read_bytes()

    async def ask(provider: providers.Provider) -> dict:
        last = ""
        began = time.monotonic()
        for attempt in range(attempts):
            try:
                r = await client.post(
                    provider.url("/audio/transcriptions"),
                    headers={"Authorization": f"Bearer {provider.api_key}"},
                    data=form, files={"file": (chunk_path.name, audio, "audio/mpeg")},
                    timeout=STT_TIMEOUT_SEC,
                )
                if r.status_code == 429:
                    raise RateLimited(f"HTTP 429: {r.text[:200]}", retry_after(r))
                if r.status_code in providers.REFUSED:
                    # ключ не тот, деньги кончились или доступ закрыт: от повтора ответ не изменится
                    raise providers.Down(
                        f"распознавание: HTTP {r.status_code}: поставщик {provider.title} отказал "
                        f"в доступе — проверьте ключ ({provider.key_env} в .env), баланс и открыта "
                        f"ли ключу модель {cfg.model}. Ответ поставщика: {r.text[:200]}", refused=True)
                if r.status_code >= 500 or r.status_code in providers.RETRIED:
                    raise providers.Fault(f"HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code >= 400:
                    # плохой запрос, нет модели, файл велик: от повтора ответ не изменится, и
                    # запасной его не примет так же. Причина — в теле ответа: по коду её не понять
                    raise providers.Rejected(f"распознавание: HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                if "error" in data:
                    raise RuntimeError(str(data["error"])[:300])
                usage.add("speech", cfg.model, data.get("usage"), provider)
                return data
            except (providers.Down, providers.Rejected):
                raise
            except httpx.ReadTimeout as e:
                # Файл ушёл целиком, а ответа нет: поставщик мог уже распознать запись и
                # списать деньги. Повтор — у него или у запасного — оплатил бы ту же работу
                # второй раз (а при обрыве по простою соединения — и каждый следующий).
                # Повторит вся запись — по расписанию после сбоя (ledger.RETRY_AFTER_SEC) или
                # вручную, и уже распознанные части она возьмёт готовыми (transcribe_one).
                raise RuntimeError(
                    f"распознавание: поставщик {provider.title} получил файл, но не ответил за "
                    f"{STT_TIMEOUT_SEC // 60} мин ({describe_error(e)}). Работу он мог уже выполнить и "
                    "списать за неё деньги, поэтому запрос сразу не повторяю: повтор оплатил бы её "
                    "второй раз") from e
            except Exception as e:
                last = describe_error(e)
                delay = (rate_limit_pause(e, attempt) if isinstance(e, RateLimited)
                         else 2 ** attempt * 15)
                if attempt == attempts - 1 or time.monotonic() - began + delay > STT_RETRY_WINDOW_SEC:
                    # сеть, таймаут, 5xx, 429 — поставщик не ответил; остальное — негодный ответ
                    failed = providers.Down if providers.is_fault(e) else RuntimeError
                    raise failed(f"распознавание: {last}")
                if providers.resting(provider):
                    # пока шли повторы, другой вызов уже увёл запросы к запасному — ждать незачем
                    raise providers.Down(f"распознавание: {last}")
                # связь с провайдером пропадает на минуты: четыре быстрых попытки
                # укладывались в полминуты и сгорали все внутри одного обрыва
                log(f"распознавание: сбой запроса к {provider.title} ({last}), попытка {attempt + 1} "
                    f"из {attempts} — повторяю через {delay:.0f} с")
                await asyncio.sleep(delay)
        raise RuntimeError(f"распознавание: {last}")

    return await providers.through(cfg.speech_provider, cfg.backup_provider, ask, log)


def describe_error(e: Exception) -> str:
    """Причина сбоя для лога. У таймаутов httpx текст бывает пустым, и без
    имени класса в логе осталась бы пустая строка вместо «ReadTimeout»."""
    text = str(e).strip()
    if isinstance(e, providers.Fault):
        return text[:300]           # «HTTP 503: …» понятно и без имени класса
    return (f"{type(e).__name__}: {text}" if text else type(e).__name__)[:300]


def normalize_words(words: list[dict], offset: float = 0.0) -> tuple[list[dict], int]:
    """Слова провайдера в наш вид: метка S1 вместо номера, время от начала записи.

    Слово без метки наследует предыдущего говорящего, но считается отдельно:
    их доля — мера того, насколько разделению можно верить.
    """
    out: list[dict] = []
    unlabeled = 0
    for w in words:
        text = (w.get("word") or "").strip()
        if not text:
            continue
        who = w.get("speaker")
        if who is None:
            unlabeled += 1
            who = out[-1]["speaker"] if out else 0
        out.append({"speaker": f"S{int(who) + 1}" if not isinstance(who, str) else who,
                    "start": float(w.get("start") or 0) + offset,
                    "end": float(w.get("end") or w.get("start") or 0) + offset,
                    "text": text})
    return out, unlabeled


def segments_from_words(words: list[dict], cuts: list[float] = ()) -> list[dict]:
    """Собирает реплики из слов: подряд идущие слова одного голоса — одна реплика.

    Границы реплик берутся по словам, а не по сегментам провайдера: короткий
    перебив внутри чужой фразы иначе достаётся тому, кто говорил вокруг.
    На стыке частей реплика рвётся всегда: по разные стороны стыка метки
    раздавала модель независимо, и подписывать их приходится по отдельности.

    Доклад на семь минут — одна реплика, и одной метки времени на неё мало:
    по ней в записи ничего не найти. Поэтому внутри длинной реплики
    запоминаются точки — начало фразы и его время, не чаще раза в
    MARK_EVERY_SEC. Время берётся у слова, а не высчитывается.
    """
    out: list[dict] = []
    for w in words:
        crossed = out and any(out[-1]["start"] < c <= w["start"] for c in cuts)
        if out and out[-1]["speaker"] == w["speaker"] and not crossed:
            seg = out[-1]
            marked = seg["points"][-1][1] if seg["points"] else seg["start"]
            if seg["text"][-1:] in ".?!" and w["start"] - marked >= MARK_EVERY_SEC:
                seg["points"].append([len(seg["text"]) + 1, w["start"]])
            seg["end"] = w["end"]
            seg["text"] += " " + w["text"]
        else:
            out.append({**w, "points": []})
    return out


def _norm_words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def _token(text: str) -> str:
    """Слово для сравнения: «Да,» и «да» — одно и то же слово."""
    return "".join(_norm_words(text))


def match_labels(prev_words: list[dict], new_words: list[dict],
                 zone_start: float, zone_end: float) -> tuple[dict[str, str], int]:
    """Чем метки новой части были в предыдущей — по словам перехлёста.

    Перехлёст [zone_start, zone_end) размечен дважды: одни и те же слова прошли
    и в конце прошлой части, и в начале новой, каждое под своей меткой.
    Совпавшие слова голосуют за пару меток; пары разбираются от самой уверенной
    к самой слабой, и одна метка достаётся ровно одной — иначе двух человек
    свело бы в один голос.

    Слова сравниваются по порядку, а пара засчитывается, только если оба слова
    прозвучали в одно и то же время: времена от провайдера точные, и частое «да»
    из другого места перехлёста за случайную пару голосовать не должно.
    Возвращает соответствие меток и число совпавших слов.
    """
    p = [w for w in prev_words if w["start"] >= zone_start and _token(w["text"])]
    n = [w for w in new_words if w["start"] < zone_end and _token(w["text"])]
    votes: dict[tuple[str, str], int] = {}
    matched = 0
    matcher = difflib.SequenceMatcher(None, [_token(w["text"]) for w in p],
                                      [_token(w["text"]) for w in n], autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            old, new = p[a + k], n[b + k]
            if abs(old["start"] - new["start"]) > SAME_MOMENT_SEC:
                continue
            matched += 1
            pair = (new["speaker"], old["speaker"])
            votes[pair] = votes.get(pair, 0) + 1
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for (new_label, old_label), count in sorted(votes.items(), key=lambda kv: -kv[1]):
        if count < LABEL_MATCH_WORDS or new_label in mapping or old_label in taken:
            continue
        mapping[new_label] = old_label
        taken.add(old_label)
    return mapping, matched


def _fresh_label(used: set[str]) -> str:
    """Следующий свободный номер: нумерация сквозная по всей записи."""
    numbers = [int(m.group(1)) for m in (re.fullmatch(r"S(\d+)", u) for u in used) if m]
    return f"S{max(numbers, default=0) + 1}"


def assemble(parts: list[tuple[float, float, list[dict]]],
             log: Callable[[str], None] = lambda s: None) -> tuple[list[dict], list[float]]:
    """Слова всей записи из частей: [(начало части, длина, её слова)].

    Возвращает ещё и стыки — время, с которого слова взяты из следующей части.
    По стыкам человек подписывает говорящих отдельно в каждой части: метки
    после стыка раздавала модель заново, и S3 до него и S3 после — не
    обязательно один человек, как бы ни старалась сшивка.

    Соседние части перекрываются. Повтор режется посередине перехлёста — по
    времени слов, которое известно точно: до середины слова берутся из прошлой
    части, после — из новой. Метки новой части сводятся с прежними по словам
    перехлёста (match_labels). Кого в перехлёсте не слышно, узнать не по чему:
    голосовых отпечатков провайдер не отдаёт. Такой голос считается новым
    человеком и получает свободный номер — это честнее, чем угадать и
    приписать чужие слова.
    """
    words: list[dict] = []
    cuts: list[float] = []
    prev_end = 0.0
    for i, (start, length, new_words) in enumerate(parts):
        if i > 0:
            mapping, matched = match_labels(words, new_words, start, prev_end)
            middle = (start + prev_end) / 2
            cuts.append(middle)
            words = [w for w in words if w["start"] < middle]
            kept = [w for w in new_words if w["start"] >= middle]
            used = {w["speaker"] for w in words} | set(mapping.values())
            fresh = []
            for w in kept:
                if w["speaker"] not in mapping:
                    mapping[w["speaker"]] = _fresh_label(used)
                    used.add(mapping[w["speaker"]])
                    fresh.append(mapping[w["speaker"]])
            new_words = [{**w, "speaker": mapping[w["speaker"]]} for w in kept]
            moved = ", ".join(f"{k}→{v}" for k, v in mapping.items()
                              if k != v and v not in fresh)
            log(f"часть {i + 1}: перехлёст обрезан на {hms(middle)}, совпавших в нём "
                f"слов {matched}, метки: {moved or 'совпали'}"
                + (f", новые голоса: {', '.join(fresh)}" if fresh else ""))
        words.extend(new_words)
        prev_end = start + length
    return words, cuts


def _label_order(label: str) -> tuple[int, str]:
    """S2 должна стоять перед S10: сортируем по номеру, а не по алфавиту."""
    m = re.fullmatch(r"S(\d+)", label)
    return (int(m.group(1)), "") if m else (10 ** 6, label)


def to_text(segments: list[dict]) -> str:
    """Склеивает подряд идущие реплики одного говорящего в читаемые блоки."""
    blocks: list[list] = []
    for s in segments:
        t = (s.get("text") or "").strip()
        if not t:
            continue
        if blocks and blocks[-1][0] == s["speaker"]:
            blocks[-1][1].append(t)
        else:
            blocks.append([s["speaker"], [t]])
    return "\n\n".join(f"[{b[0]}] " + " ".join(b[1]) for b in blocks) + "\n"


def hms(seconds: float) -> str:
    """Время от начала записи как чч:мм:сс — метка для протокола."""
    total = max(int(seconds), 0)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def to_text_timed(segments: list[dict],
                  who: list[str] | None = None) -> tuple[str, set[str]]:
    """Расшифровка с меткой времени на каждой реплике — то, что уходит в протокол.

    Протокол без времени нечем сверить: чтобы найти в двухчасовой записи момент,
    где приняли решение, приходится слушать её целиком. Метка ставится на каждую
    реплику, а модель протокола обязана её скопировать, а не сочинить, — поэтому
    вместе с текстом возвращается множество допустимых меток: по нему потом
    проверяется, что все метки в протоколе действительно из расшифровки.

    who — ФИО, подписанные вручную, по одному на реплику (пусто — не
    подписана): ФИО встаёт на место метки прямо в тексте, и модель протокола
    видит у реплики человека, а не номер.

    Длинная реплика идёт несколькими строками — по точкам, расставленным в
    segments_from_words: у каждой строки своя метка времени.
    """
    lines: list[str] = []
    marks: set[str] = set()
    for i, s in enumerate(segments):
        text = s.get("text") or ""
        name = (who[i] if who else "") or s["speaker"]
        starts = [[0, s.get("start") or 0]] + (s.get("points") or [])
        for (offset, at), nxt in zip(starts, starts[1:] + [[len(text), None]]):
            t = text[offset:nxt[0]].strip()
            if not t:
                continue
            mark = hms(at)
            marks.add(mark)
            lines.append(f"[{mark}] [{name}] {t}")
    return "\n\n".join(lines) + "\n", marks


def format_ts(seconds: float, sep: str = ",") -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000.0))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


CUE_SEC = 7.0        # субтитр длиннее семи секунд читать неудобно
CUE_CHARS = 90       # и длиннее двух строк на экране
CUE_GAP_SEC = 1.5    # пауза дольше — уже новая фраза, держать субтитр незачем


def to_cues(words: list[dict]) -> list[dict]:
    """Режет поток слов на субтитры: по смене говорящего, длине и времени.

    Реплика может длиться минуты, а субтитр столько висеть не должен, поэтому
    длинная реплика разбивается по словам. Времена для этого есть на каждом
    слове — раньше их не было, и субтитры собирались из сегментов как есть.
    """
    cues: list[dict] = []
    for w in words:
        cue = cues[-1] if cues else None
        if (cue and cue["speaker"] == w["speaker"]
                and w["end"] - cue["start"] <= CUE_SEC
                and w["start"] - cue["end"] <= CUE_GAP_SEC
                and len(cue["text"]) + len(w["text"]) + 1 <= CUE_CHARS):
            cue["text"] += " " + w["text"]
            cue["end"] = w["end"]
        else:
            cues.append(dict(w))
    return cues


def to_srt(words: list[dict]) -> str:
    lines = []
    for i, cue in enumerate(to_cues(words), 1):
        lines.append(str(i))
        lines.append(f"{format_ts(cue['start'])} --> {format_ts(cue['end'])}")
        lines.append(f"[{cue['speaker']}] {cue['text'].strip()}")
        lines.append("")
    return "\n".join(lines)


def _audio_mark(path: Path) -> dict | None:
    """Звук записи, по которому режутся части, — размером и временем изменения: полный прогон
    (force) извлекает его заново, и ответы по прежнему звуку к новому не подходят. Нет файла — None."""
    try:
        st = path.stat()
    except OSError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _saved_answer(raw: Path, asked: dict) -> dict | None:
    """Сохранённый ответ поставщика на ровно такой же запрос части — или None. Оборванный при
    аварии файл, ответ прежней версии (без примет запроса) и ответ на другой запрос не годятся:
    часть тогда распознаётся заново."""
    if asked["audio"] is None:
        return None
    try:
        saved = json.loads(raw.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict) or not isinstance(saved.get("response"), dict) \
            or any(saved.get(k) != v for k, v in asked.items()):
        return None
    return saved["response"]


async def transcribe_one(
    audio_path: Path,
    out_dir: Path,
    stem: str,
    cfg,
    work_dir: Path,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    log: Callable[[str], None] = lambda s: None,
    suffix: str = "transcript",
    terms: list[str] = (),
) -> dict:
    """terms — подсказки распознаванию в порядке важности: словарь записи, папки, общий
    (dictionary.hints и glossary_terms); в пределы модели их укладывает fit_terms."""
    t0 = time.perf_counter()
    duration = await asyncio.to_thread(audio_duration, audio_path)
    plan = plan_chunks(duration)
    if len(plan) > 1:
        log(f"{len(plan)} частей по {CHUNK_SEC / 60:.0f} мин "
            f"(перехлёст {OVERLAP_SEC:.0f} с) — метки говорящих сшиваются по перехлёсту")
    else:
        log(f"одним запросом, {duration / 60:.1f} мин")

    terms = fit_terms(cfg.model, list(terms), log)
    if terms:
        log(f"подсказки из словарей переданы распознаванию: {len(terms)}")

    parts: list[tuple[float, float, list[dict]]] = []
    words_total = 0
    unlabeled_total = 0
    raw_dir = out_dir / suffix / RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    if on_progress:
        await on_progress(0, len(plan))

    # Чем был запрос каждой части: тот же звук записи, та же модель с теми же полями формы и
    # тот же отрезок. Совпало с сохранённым ответом — часть уже оплачена, второй раз её не
    # заказываем: сбой на части N иначе оплачивал бы заново и части 1…N−1.
    request = {"audio": _audio_mark(audio_path),
               "form": hashlib.sha256(json.dumps(build_form(cfg, terms), sort_keys=True,
                                                 ensure_ascii=False).encode()).hexdigest()}
    async with httpx.AsyncClient() as client:
        for i, (start, length) in enumerate(plan):
            raw = raw_dir / f"{stem}.part{i + 1:02d}.json"
            asked = {"model": cfg.model, "start": start, "length": length, **request}
            data = _saved_answer(raw, asked)
            if data is not None:
                log(f"часть {i + 1}/{len(plan)} — беру ответ прошлого запуска, в API не отправляю")
            else:
                chunk = work_dir / f"stt_{i:03d}.mp3"
                await asyncio.to_thread(make_chunk, audio_path, chunk, start, length)
                data = await call_stt(client, cfg, chunk, log=log, terms=terms)
                chunk.unlink(missing_ok=True)
                raw.write_text(json.dumps({**asked, "response": data}, ensure_ascii=False), encoding="utf-8")
            words = spoken_words(data, cfg.model)
            words_total += len(words)
            new_words, unlabeled = normalize_words(words, offset=start)
            unlabeled_total += unlabeled
            parts.append((start, length, new_words))
            log(f"часть {i + 1}/{len(plan)} распознана, слов: {len(new_words)}")
            if on_progress:
                await on_progress(i + 1, len(plan))

    all_words, cuts = assemble(parts, log)
    segments = segments_from_words(all_words, cuts)
    speakers = sorted({s["speaker"] for s in segments}, key=_label_order)
    unlabeled = round(unlabeled_total / words_total, 3) if words_total else 0.0
    crowded = len(speakers) > CROWDED_MEETING
    if unlabeled >= UNRELIABLE:
        log(f"внимание: без метки говорящего осталось {unlabeled * 100:.0f}% слов — "
            f"разделению голосов верить нельзя")
    if crowded:
        log(f"внимание: голосов {len(speakers)} — на многолюдной записи разделение "
            f"держится хуже, читайте протокол с поправкой")

    changes = sum(1 for a, b in zip(segments, segments[1:]) if a["speaker"] != b["speaker"])
    log(f"расшифровка готова: реплик {len(segments)}, голосов {len(speakers)}, "
        f"смен говорящего {changes}")

    text = to_text(segments)
    out_dir = out_dir / suffix
    (out_dir / f"{stem}.txt").write_text(text, encoding="utf-8")
    (out_dir / f"{stem}.srt").write_text(to_srt(all_words), encoding="utf-8")
    payload = {
        "mode": suffix,
        "model": cfg.model,
        "language": cfg.language,
        "timestamps": "точные: время каждого слова приходит от модели распознавания",
        "speakers": [{"id": s, "description": ""} for s in speakers],
        "unlabeled": unlabeled,
        "speakers_unreliable": unlabeled >= UNRELIABLE,
        "crowded_meeting": crowded,
        "chunks": len(plan),
        "cuts": cuts,
        "segments": segments,
        "text": text,
    }
    (out_dir / f"{stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {
        "success": True,
        "mode": suffix,
        "model": cfg.model,
        "wall_clock_sec": round(time.perf_counter() - t0, 2),
        "audio_duration_sec": round(duration, 2),
        "chunks": len(plan),
        "segments": len(segments),
        "speakers": len(speakers),
        "speaker_changes": changes,
        "unlabeled": unlabeled,
        "speakers_unreliable": unlabeled >= UNRELIABLE,
        "crowded_meeting": crowded,
        "source_audio": str(audio_path),
    }
    (out_dir / f"{stem}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"meta": meta, "segments": segments, "speakers": payload["speakers"],
            "crowded_meeting": crowded}
