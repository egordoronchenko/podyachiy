"""Обработка одного файла: звук → расшифровка с говорящими → разбор → протокол.

Разбор — дешёвый вызов текстовой модели по готовой расшифровке: он выбирает
шаблон протокола (совещание, лекция, созвон, постановка задачи) и сводит метки говорящих с
именами, которые звучат в разговоре. Раньше автоматика брала шаблон совещания
для любой записи, а выбрать другой можно было только руками из админки.

Ничего из этого не настраивается из интерфейса: автоматика работает одинаково
всегда. Ручной запуск отличается только тем, что администратор может задать
шаблон сам — тогда выбор разбора игнорируется, а имена говорящих берутся всё
равно.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import tempfile
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Callable

from . import actions, dictionary, external, ledger, marks, media, names, registry, speech, summarize, templates, triage, usage
from .config import Settings

MODE = "transcript"


class NoSpeechError(media.NoAudioError):
    """Звук в записи есть, но распознавание не нашло в нём ни слова. Исход тот же, что у записи без
    звука (no_audio), — окончательный; только деньги за распознавание уже потрачены."""


# Отмена прогона администратором (watcher.Service.cancel). Прогон останавливается не посреди дела, а
# между платными запросами: перед каждым шагом и перед каждой следующей частью распознавания. Идущий
# запрос поставщику не обрывается — поставщик мог уже выполнить работу и списать за неё деньги, а
# оборванный ответ сервис бы не получил: ни суммы в расходах, ни результата на диске. Дожидается и
# ffmpeg — он бесплатный. Всё, что успело сделаться, остаётся на диске, и повтор берёт его готовым.
# Спросить «не отменили ли» — halt: переменная контекста задания, как накопитель расходов в usage.py;
# обработчик кладёт в неё вопрос перед прогоном. Ответ — {"by", "at"}, кто и когда отменил, или None.
halt: ContextVar[Callable[[], dict | None]] = ContextVar("halt", default=lambda: None)
CANCELLED_TEXT = "обработку отменил администратор {by} — автоматически не повторяется: нужна — запустите вручную"


class Cancelled(Exception):
    """Прогон отменён администратором; mark — кто и когда."""

    def __init__(self, mark: dict):
        super().__init__(CANCELLED_TEXT.format(by=mark.get("by") or "сервиса"))
        self.mark = mark


def checkpoint() -> None:
    """Перед платным шагом: отменили — дальше не идти."""
    mark = halt.get()()
    if mark:
        raise Cancelled(mark)


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_stage(out_dir: Path, stem: str, stage: str) -> dict | None:
    """Готовый результат шага, если он уже лежит на диске.

    Нужно после обрыва: контейнер могли перезапустить между расшифровкой
    и протоколом, и платить за уже сделанное второй раз незачем.
    Недописанный при аварии файл не разберётся как JSON — шаг просто
    выполнится заново. Удачное распознавание без единой реплики (в записи нет речи) — тоже
    готовый результат: повтор без force его не оплачивает.

    Какой моделью расшифровка сделана, здесь не сверяется: готовая расшифровка
    годится при любой выбранной сейчас модели. Иначе смена модели распознавания
    превращала бы каждую пересборку протокола старой записи в платное
    распознавание заново, со сбросом подписей говорящих. Другая модель для уже
    распознанной записи — только явный полный прогон (force). Чем расшифровка
    сделана, записано в её meta.json и в строке журнала обработки (speech_model).
    """
    data = _load_json(out_dir / stage / f"{stem}.json")
    meta = _load_json(out_dir / stage / f"{stem}.meta.json")
    if not isinstance(data.get("segments"), list) or not meta.get("success"):
        return None
    return {"meta": meta, "segments": data["segments"],
            "speakers": data.get("speakers") or [],
            "crowded_meeting": bool(data.get("crowded_meeting"))}


def _size_of(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _mtime_of(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def source_note(src: Path) -> str:
    """Имя и дата файла записи — по ним протокол ставит дату встречи."""
    saved = datetime.datetime.fromtimestamp(src.stat().st_mtime).strftime("%d.%m.%Y")
    return f"«{src.name}», сохранён {saved}"


def _by_neighbours(inbox: Path, rel: str) -> str:
    """Имя папки результата по соседям-файлам: при совпадении имён без расширения
    в одной папке (запись.mp4 и запись.mp3) — полное имя файла."""
    p = Path(rel)
    stem = p.stem
    try:
        same = [c for c in (inbox / p.parent).iterdir()
                if c.is_file() and c.suffix.lower() in media.MEDIA_EXTS
                and c.stem.lower() == stem.lower()]
    except OSError:
        return stem
    return p.name if len(same) > 1 else stem


def stem_for(inbox: Path, rel: str, known: dict[str, dict]) -> str:
    """Имя папки результата для новой записи. При совпадении имён без расширения
    в одной папке (запись.mp4 и запись.mp3) берётся полное имя файла, чтобы
    результаты двух разных записей не легли друг на друга.

    Полное имя берётся и тогда, когда имя без расширения уже занято (known — журнал
    обработки): подпапкой рядом — её результаты лежат в папке результатов с тем же
    именем — или записью из журнала, чей исходник убрали с диска, а результаты остались.
    Иначе запись «Сметы.mp4» делила бы папку результатов с подпапкой «Сметы», и удаление
    одной уносило бы в корзину результаты другой, а новая «вкс.mp3» взяла бы готовую
    расшифровку и протокол прежней «вкс.wav».

    Ответ зависит от соседей по папке, поэтому годится только для записи, которой
    ещё нет в журнале обработки. Для остальных — stem_of."""
    stem = _by_neighbours(inbox, rel)
    folder = rel.rpartition("/")[0]
    prefix = folder + "/" if folder else ""
    taken = set()
    try:
        taken.update(c.name.lower() for c in (inbox / folder).iterdir() if c.is_dir())
    except OSError:
        pass
    for file, record in known.items():
        if file == rel or not file.startswith(prefix):
            continue
        inner = file[len(prefix):]
        taken.add(inner.split("/", 1)[0].lower() if "/" in inner else stem_of(inbox, file, record).lower())
    return Path(rel).name if stem.lower() in taken else stem


def protocol_path(cfg: Settings, rel: str, record: dict | None) -> Path:
    """Файл протокола записи."""
    stem = stem_of(cfg.inbox, rel, record)
    return cfg.outbox / Path(rel).parent / stem / "summary" / f"{stem}.md"


def previous_tasks(cfg: Settings, rel: str, neighbours: list[dict], day: str, started: str, log,
                   marked: marks.Store | None = None, *, series: bool) -> str:
    """Раздел «Поручения прошлого совещания» для протокола записи rel, либо пусто.

    Раздел собирается, только если папка записи — серия совещаний (series — её признак,
    series.py) и человек не убрал раздел у этой записи (ledger.REMOVED). Запись
    в корне — без раздела всегда: в корень кладут разрозненные записи, и две из них серией
    не становятся оттого, что лежат рядом.

    neighbours — строки журнала обработки. «Предыдущая» — запись той же папки, которая в ряду
    совещаний стоит прямо перед этой (registry.order_key): по дате совещания, а когда разбор
    дату не назвал — по моменту первой обработки. У записи, которую пересобирают, место в ряду
    прежнее: её первая обработка уже записана в журнале.
    """
    own = next((r for r in neighbours if r["file"] == rel), {})
    if "/" not in rel:
        log("запись в корне — поручения прошлого совещания не ищу")
        return ""
    if not series:
        log("папка не отмечена как серия совещаний — поручения прошлого совещания не ищу")
        return ""
    if own.get(ledger.REMOVED):
        log("раздел «Поручения прошлого совещания» у этой записи убран вручную — не добавляю")
        return ""
    key = registry.order_key({"record_date": day, "finished_at": started,
                              "first_done_at": own.get("first_done_at") or own.get("finished_at")})
    prev = registry.previous(neighbours, rel, key)
    if prev is None:
        return ""
    path = protocol_path(cfg, prev["file"], prev)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    noted = marked.load(prev["file"]) if marked else {}
    tail = registry.block(prev, marks.merged(external.tasks(text), noted), cfg.public_url)
    log(f"поручения прошлого совещания: предыдущая запись папки — {prev['file']}, "
        + ("незакрытые поручения дописываю в конец протокола" if tail else "незакрытых поручений в ней нет"))
    return tail


def stem_of(inbox: Path, rel: str, record: dict | None) -> str:
    """Имя папки результата записи: у уже обработанной — то, под которым её результаты
    лежат (оно сохранено в журнале обработки), и только у новой вычисляется.

    Вычислять каждый раз нельзя: «запись.mp3» рядом с «запись.mp4» получает имя
    «запись.mp3», а стоит соседку удалить или перенести — имя стало бы «запись»,
    прошлая расшифровка не нашлась бы, и пересборка распознала бы запись заново за деньги.

    Строка без имени (из файлов состояния до базы) — по соседям-файлам, как тогда
    и вычислялось."""
    return (record or {}).get("stem") or _by_neighbours(inbox, rel)


async def process(
    cfg: Settings,
    rel: str,
    stem: str,
    template_key: str | None,
    force: bool,
    manual: bool,
    log,
    on_progress=None,
    neighbours: list[dict] | None = None,
    words: dict | None = None,
    marked: marks.Store | None = None,
    series: bool = False,
) -> dict:
    """Прогоняет один файл целиком и возвращает запись для журнала.

    stem — имя папки результата (stem_of): OUTBOX повторяет структуру подпапок INBOX,
    результаты одного файла лежат в собственной папке.

    neighbours — строки журнала обработки: по ним находится предыдущая запись папки, чьи
    незакрытые поручения дописываются в конец протокола (PREV_TASKS) — если папка записи
    отмечена как серия совещаний (series, признак папки — series.py).

    words — действующий словарь записи (dictionary.Store.effective): участники и сокращения
    её самой, её папки и папок выше. Термины и имена из него уходят подсказками распознаванию,
    весь список — текстовым шагам, следом за общим словарём сервиса.

    marked — отметки и сроки поручений (marks.Store): по ним решается, какие поручения
    прошлого совещания ещё не закрыты, и с ними собирается карта протокола.

    Исключения не выбрасываются: любой исход — это запись со статусом,
    потому что ошибка на одном файле не должна останавливать обход папки.
    """
    src = cfg.inbox / rel
    out_dir = cfg.outbox / Path(rel).parent / stem
    started = now_iso()
    t0 = time.perf_counter()
    record = {
        "file": rel,
        "stem": stem,
        "output": str(out_dir),
        "mode": MODE,
        "template": template_key,
        "template_auto": template_key is None,
        "manual": manual,
        "force": force,
        # каким был исходник, когда его взяли в работу: по размеру, времени изменения и отпечатку
        # содержимого сканер узнаёт его под другим именем и замечает, что он изменился (watcher)
        "source_size": await asyncio.to_thread(_size_of, src),
        "source_mtime": await asyncio.to_thread(_mtime_of, src),
        "source_print": await asyncio.to_thread(media.fingerprint, src),
        "started_at": started,
        "finished_at": None,
        "duration_sec": None,
        "audio_duration_sec": None,
        "cost": {},
        "status": "error",
        "error": None,
    }

    usage.reset()
    try:
        if not await asyncio.to_thread(src.is_file):
            raise RuntimeError("файл не найден во входной папке")

        # шаблоны и словарь читаются заново на каждое задание: правка
        # prompts/* вступает в силу без перезапуска контейнера
        words = words or dictionary.merge([])
        common = await asyncio.to_thread(templates.load_glossary, cfg.prompts_dir)
        glossary = common + dictionary.block(words, after_glossary=bool(common))
        # типы — стандартные и свои, добавленные на странице: из них выбирает разбор
        kinds = await asyncio.to_thread(templates.types, cfg.prompts_dir)
        if template_key is None:
            choice = "шаблон выберет разбор записи"
        else:
            record["template_title"] = (await asyncio.to_thread(
                templates.load_template, cfg.prompts_dir, template_key))[0]
            choice = f"шаблон «{record['template_title']}» задан вручную"
        log(f"начинаю: {rel} — {choice}"
            + (", со словарём терминов" if common else ", без словаря терминов")
            + (f", словарь папки и записи: участников {len(words['people'])}, "
               f"сокращений {len(words['terms'])}" if words["people"] or words["terms"] else "")
            + (", перезапись включена (force)" if force else "")
            + (", ручной запуск" if manual else ""))

        # Расшифровка переиспользуется, какой бы моделью она ни была сделана: часовая
        # запись стоит десятки рублей, и повторно оплачивать уже полученное незачем.
        # Полный прогон заново — это force, осознанное действие администратора;
        # только он распознаёт запись моделью, выбранной сейчас (cfg.model).
        t = None if force else await asyncio.to_thread(_load_stage, out_dir, stem, MODE)
        record["reused_transcript"] = bool(t)

        if t is None:
            checkpoint()
            audio_path = await asyncio.to_thread(
                media.ensure_audio, src, out_dir, stem, force, log)

            async def progress(done: int, total: int) -> None:
                if on_progress:
                    await on_progress(done, total)
                if done < total:
                    checkpoint()            # следующая часть — новый платный запрос

            with tempfile.TemporaryDirectory(prefix="chunks_") as tmp:
                log(f"шаг 1: расшифровка с разделением говорящих — {cfg.model}")
                t = await speech.transcribe_one(
                    audio_path, out_dir, stem, cfg, Path(tmp), on_progress=progress, log=log,
                    terms=dictionary.hints(words) + speech.glossary_terms(cfg.prompts_dir))
            if await asyncio.to_thread(names.forget, out_dir, stem):
                log("подписи говорящих сброшены: запись распознана заново, "
                    "у новой расшифровки другие метки и реплики")
        else:
            log("шаг 1: расшифровка — беру результат прошлого запуска"
                + (f" ({t['meta']['model']})" if t["meta"].get("model") else "") + ", в API не отправляю")
        meta = t["meta"]
        if not t["segments"]:
            # Распознавание прошло и оплачено, но не услышало ни слова. Исход окончательный, как
            # у записи без звука: с ошибкой повтор по расписанию распознавал бы тишину снова, за деньги.
            record.update({"speech_model": meta.get("model"), "audio_duration_sec": meta.get("audio_duration_sec")})
            raise NoSpeechError("распознавание не нашло в записи речи")

        record.update({
            "speech_model": meta.get("model"),
            "audio_duration_sec": meta.get("audio_duration_sec"),
            "speakers": meta.get("speakers"),
            "segments": meta.get("segments"),
            "unlabeled": meta.get("unlabeled"),
            "speakers_unreliable": meta.get("speakers_unreliable"),
            "crowded_meeting": meta.get("crowded_meeting"),
            "transcript": f"{MODE}/{stem}.txt",
            "summary": f"summary/{stem}.md",
        })

        # Разбор и протокол — отдельный исход: расшифровка уже на диске и
        # оплачена, терять её из-за осечки на последних шагах нельзя.
        try:
            # разбор зависит от расшифровки и от списка участников: если расшифровка взята
            # с прошлого запуска, а словарь с тех пор не меняли, разбор рядом с ней ещё верен
            # и оплачен
            listed = dictionary.fingerprint(words)
            tri = (await asyncio.to_thread(triage.load_saved, out_dir, stem, log, kinds, listed)
                   if record["reused_transcript"] else None)
            record["reused_triage"] = bool(tri)
            if tri is None:
                checkpoint()
                log("шаг 2: разбор записи — тип, название, кто есть кто")
                tri = await triage.triage_file(
                    out_dir, stem, templates.load_triage(cfg.prompts_dir, kinds), cfg, log, glossary, kinds,
                    listed)
            else:
                log("шаг 2: разбор — беру результат прошлого запуска, в API не отправляю")
            who, signed, unsigned = await asyncio.to_thread(names.for_protocol, out_dir, stem)
            if signed:
                log(f"подписи вручную: {len(signed)} ФИО, реплик с ними "
                    f"{sum(1 for w in who if w)} из {len(who)} — идут в протокол как есть")

            if template_key is None:
                template_key = tri["type"]
                record["template"] = template_key
            elif template_key != tri["type"]:
                log(f"разбор считает запись типом «{tri['type']}», но шаблон задан "
                    f"вручную — оставляю выбор администратора")
            title, template_text = await asyncio.to_thread(templates.load_template, cfg.prompts_dir, template_key)
            record.update({
                "template_title": title,
                "title": tri["title"],
                "gist": tri["gist"],
                "record_date": tri["date"],
                "topics": tri["topics"],
                "named_speakers": len(signed) + sum(
                    1 for sp in tri["speakers"] if sp["name"] and sp["label"] in unsigned),
                "type_confidence": tri["confidence"],
                "type_reason": tri["type_reason"],
                "type_unsure": triage.unsure(tri),
            })

            notes = triage.notes(tri, signed, unsigned, await asyncio.to_thread(source_note, src))
            if kinds[template_key].get("actions"):
                checkpoint()
                log(f"шаг 3: поручения — независимо выписывают {', '.join(cfg.actions_models)}")
                notes += await actions.extract_file(
                    out_dir, stem, await asyncio.to_thread(templates.load_actions, cfg.prompts_dir), cfg, log,
                    notes=notes, glossary=glossary, who=who)
                checkpoint()
                log(f"шаг 4: протокол по шаблону «{title}»")
            else:
                checkpoint()
                log(f"шаг 3: протокол по шаблону «{title}»")
            tail = (await asyncio.to_thread(previous_tasks, cfg, rel, neighbours or [], tri["date"], started, log,
                                            marked, series=series)
                    if cfg.prev_tasks else "")
            smeta = await summarize.summarize_file(
                out_dir, stem, template_key, template_text, cfg, log,
                notes=notes, who=who, glossary=glossary, tail=tail,
                noted=await asyncio.to_thread(marked.load, rel) if marked else None)
            record["status"] = "ok"
            record["summary_chars"] = smeta["summary_chars"]
            record["timestamps"] = smeta["timestamps"]
            record["timestamps_unknown"] = smeta["timestamps_unknown"]
        except Cancelled:
            raise                   # отмена — не сбой протокола
        except Exception as e:
            record.update({"status": "no_summary", "error": str(e)[:500],
                           "summary_chars": 0})
            log(f"протокол не сделан: {rel} — {e}. Расшифровка на месте и при "
                f"повторе без force заново оплачена не будет")
    except Cancelled as e:
        record.update({"status": "error", "error": str(e), ledger.CANCELLED: e.mark})
        log(f"отменено: {rel} — прогон отменил {e.mark.get('by') or 'администратор'}. Сделанное до отмены "
            f"оплачено и лежит на диске: при запуске заново готовое возьмётся без новой оплаты")
    except NoSpeechError as e:
        record.update({"status": "no_audio", "error": str(e)})
        log(f"нет речи: {rel} — {e}. Автоматически не повторяю: распознавание той же тишины снова стоило бы денег")
    except media.NoAudioError as e:
        record.update({"status": "no_audio", "error": str(e)})
        log(f"нет звука: {rel} — {e}. В API не отправляю, деньги не тратятся")
    except Exception as e:
        record.update({"status": "error", "error": str(e)[:500]})
        log(f"ошибка: {rel} — {e}")

    spent = usage.snapshot()
    record["finished_at"] = now_iso()
    record["duration_sec"] = round(time.perf_counter() - t0, 2)
    record["cost"] = spent["cost"]                      # по валютам: {"RUB": 12.1, "USD": 0.03}
    record["cost_details"] = spent["by_stage"]
    record["cost_models"] = spent["by_model"]
    record["cost_providers"] = spent["by_provider"]
    record["api_calls"] = spent["calls"]

    if record["status"] == "ok":
        cost = usage.text(spent["cost"]) or "стоимость поставщик не вернул"
        log(f"готово: {rel} за {record['duration_sec']:.0f} с, "
            f"говорящих {record.get('speakers') or 0}, "
            f"слов без метки {(record.get('unlabeled') or 0) * 100:.0f}%, {cost}")

    if record.get(ledger.CANCELLED):
        return record               # отмена — не исход записи: на диске что было и что прогон успел сделать
    await asyncio.to_thread(_write_outcome, out_dir, stem, record, log)
    return record


def _write_outcome(out_dir: Path, stem: str, record: dict, log) -> None:
    """Исход прогона на диск: маркер готовности — у удачного, error.json — у неудачного."""
    if record["status"] in ("ok", "no_audio"):
        ledger.write_marker(out_dir, stem, record, log)
        # прошлый неудачный прогон больше не описывает состояние записи
        (out_dir / f"{stem}.error.json").unlink(missing_ok=True)
    else:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{stem}.error.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
