"""Запуск и фоновая работа: шаблоны по умолчанию, проверки при запуске, чистка корзины, отметка
«сервис жив», резервная копия по расписанию, напоминания о сроках, проверка ключей, lifespan."""
from __future__ import annotations

import asyncio
import datetime
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException

from .. import about, backup, db, folders, mail, migrate, notify, providers, templates, upload, webhook
from ..ledger import Ledger
from ..watcher import Service
from . import core
from .access import domain_off
from .api_v1 import _task_rows
from .common import _parent_of
from .core import (CLEAN_EVERY_SEC, base, cfg, domain, hooks, log, noted, options, people, serial, speech_model, trail,
                   webhook_off, webhook_warnings, words)
from .letters import _hook, _tell


DEFAULT_PROMPTS = Path(__file__).resolve().parent.parent.parent / "prompts_default"


def _seed_prompts() -> None:
    """Кладёт в рабочую папку шаблонов файлы, которых там нет.

    Свежая установка монтирует пустую папку, и без этого шаблоны пришлось бы
    доставать из образа руками. Существующие файлы не трогаются: их правят
    на хосте, и обновление образа не должно затирать правки.

    Одно исключение — стандартный шаблон, который теперь подключает общие правила
    (templates.COMMON_FILE), а в папке лежит целиком, как до общего файла, и не правлен:
    его текст для модели тот же, что у нового с подставленными правилами. Такой файл
    заменяется новым — модель получает ровно то же, а правка общего файла дальше
    действует и в нём; без этого нетронутый шаблон числился бы изменённым в обход
    страницы. Правленный руками остаётся как есть и собирается как раньше.
    """
    if not DEFAULT_PROMPTS.is_dir():
        return
    try:
        cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
        added = []
        for src in sorted(DEFAULT_PROMPTS.iterdir()):
            target = cfg.prompts_dir / src.name
            if src.is_file() and not target.exists():
                shutil.copyfile(src, target)
                added.append(src.name)
        moved = []
        for src in sorted(DEFAULT_PROMPTS.iterdir()):
            fresh = templates.read(src) if src.is_file() else None
            if fresh is None or not templates.includes(fresh) or not (cfg.prompts_dir / src.name).is_file():
                continue
            mine = templates.read(cfg.prompts_dir / src.name)
            if mine == fresh:
                continue
            try:
                same = templates.expand(cfg.prompts_dir, fresh).strip() == mine.strip()
            except templates.TemplateError:
                continue                    # общего файла нет или он другой — сравнивать не с чем
            if same:
                templates._write(cfg.prompts_dir / src.name, fresh)
                moved.append(src.name)
    except OSError as e:
        log(f"ВНИМАНИЕ: не удалось разложить шаблоны в {cfg.prompts_dir}: {e}")
        return
    if added:
        log(f"шаблоны по умолчанию разложены в {cfg.prompts_dir}: {', '.join(added)}")
    if moved:
        log(f"шаблоны {', '.join(moved)} переведены на общие правила из {templates.COMMON_FILE}: "
            "текст для модели тот же")


def _startup_checks() -> None:
    _seed_prompts()
    if cfg.inbox.resolve() == cfg.outbox.resolve():
        raise RuntimeError(
            "INBOX_DIR и OUTBOX_DIR указывают на одну папку — результаты попадут "
            "во вход и пойдут в обработку по кругу. Задайте разные папки.")
    if not cfg.api_key:
        log("ВНИМАНИЕ: ключ API не задан (OPENAI_API_KEY в .env) — обработка будет падать")
    main, backup = cfg.speech_provider, cfg.backup_provider
    log(f"основной поставщик: {main.title}, стоимость в {main.currency}; "
        + (f"запасной: {backup.title}, стоимость в {backup.currency}" if backup
           else "запасного нет (BACKUP_BASE_URL и BACKUP_API_KEY в .env не заданы)"))
    if backup and not backup.api_key:
        log("ВНИМАНИЕ: у запасного поставщика нет ключа (BACKUP_API_KEY в .env) — "
            "при сбое основного он откажет")
    if cfg.backup_api_key and not backup:
        log("ВНИМАНИЕ: BACKUP_API_KEY задан, а BACKUP_BASE_URL нет — запасного поставщика нет")
    if cfg.personal:
        log("режим личный (MODE=personal): входа нет, сервис открыт любому, кто до него достаёт — "
            "не открывайте его в сеть")
    elif not cfg.admin_password:
        log("ВНИМАНИЕ: ADMIN_PASSWORD не задан в .env — войти в сервис некому: "
            "администратор входит под логином admin с этим паролем")
    listed, problem = templates.types_listed(cfg.prompts_dir), templates.read_custom(cfg.prompts_dir)[1]
    if problem:
        log(f"ВНИМАНИЕ: {problem}")
    for key in listed:
        try:
            templates.load_template(cfg.prompts_dir, key)
        except Exception as e:
            log(f"ВНИМАНИЕ: шаблон {key} недоступен: {e}")
    own = [k for k, m in listed.items() if m.get("custom")]
    if own:
        log(f"свои типы шаблонов: {', '.join(own)}")
    try:
        templates.load_triage(cfg.prompts_dir)
    except Exception as e:
        log(f"ВНИМАНИЕ: задание на разбор записи недоступно ({e}) — "
            f"протоколы делаться не будут, пока файл не появится")
    if not cfg.inbox.is_dir():
        log(f"ВНИМАНИЕ: входная папка не найдена: {cfg.inbox}")
    if not cfg.personal:
        log(f"пользователей: {people.count()}")
    if cfg.ldap_urls:
        off = domain_off()
        if off:
            log(f"ВНИМАНИЕ: вход через домен выключен — {off}")
        else:
            log(f"вход через домен включён: {', '.join(cfg.ldap_urls)}, база {cfg.ldap_base_dn}; группа доступа — "
                f"{cfg.ldap_access_group or 'не задана (любой пользователь домена)'}, группа администраторов — "
                f"{cfg.ldap_admin_group or 'не задана'}; перепроверка раз в {cfg.ldap_recheck_min} мин; "
                + ("принимается и по HTTP — исключение LDAP_ALLOW_HTTP" if cfg.ldap_allow_http
                   else "принимается только по HTTPS"))
            for warning in domain.warnings():
                log(f"ВНИМАНИЕ!!! НЕБЕЗОПАСНО: {warning}")
    if cfg.mail_on:
        log(f"почта включена: {cfg.smtp_host}, защита {cfg.smtp_security}; ссылки в письмах ведут на {cfg.public_url}")
        for warning in mail.warnings(cfg):
            log(f"ВНИМАНИЕ!!! НЕБЕЗОПАСНО: {warning}")
        if not cfg.admin_emails:
            log("ВНИМАНИЕ: ADMIN_EMAIL не задан — о новых заявках на доступ письмо никому не уйдёт, "
                "их будет видно только счётчиком в меню")
        if cfg.mail_domains:
            # без них — предупреждение выше (mail.warnings): заявки и протокол — на любые адреса
            log(f"заявки на доступ принимаются и протокол со страницы записи отправляется только на адреса "
                f"доменов: {', '.join(cfg.mail_domains)}")
    elif mail.why_off(cfg):
        log(f"ВНИМАНИЕ: почта выключена — {mail.why_off(cfg)}")
    if webhook_off:
        log(f"ВНИМАНИЕ: webhook выключен — {webhook_off}")
    elif hooks.on:
        log(f"webhook «запись готова» включён: {', '.join(map(webhook.where, hooks.urls))}")
    for warning in webhook_warnings:
        log(f"ВНИМАНИЕ: {warning}")
    for warning in storage_warnings:
        log(f"ВНИМАНИЕ: {warning}")
    _check_volumes()
    core.upload_ready = cfg.inbox.is_dir() and upload.writable(cfg.inbox)
    if not core.upload_ready:
        log("входная папка доступна только для чтения — загрузка записей через страницу "
            "отключена (в compose.yml уберите :ro у входной папки, чтобы включить)")
    _clean()


# На чём лежит папка состояния с базой: тип файловой системы, откуда подключена, вид (db.filesystem).
storage = db.filesystem(cfg.state_dir)
# О чём администратору надо знать про хранение — в журнал работы при запуске и на страницу «Обработка».
storage_warnings: list[str] = migrate.storage_warnings(cfg, storage)
clean_skipped: dict[str, str] = {}      # чья корзина → причина, по которой её не чистили в прошлый раз
# Что не так с томами установки (не подключены с хоста, мало места) — по последней проверке: при запуске и
# раз в сутки (_check_volumes). На страницу «Обработка» и в /api/admin/status.
volume_warnings: list[str] = []


def _check_volumes() -> None:
    """Тома установки и место на них (migrate.volume_warnings). Новое предупреждение — строкой в журнал
    работы, а когда всё прошло — строкой, что снова в порядке."""
    found = migrate.volume_warnings(cfg)
    for warning in found:
        if warning not in volume_warnings:
            log(f"ВНИМАНИЕ: {warning}")
    if volume_warnings and not found:
        log("тома установки подключены, места на них достаточно")
    volume_warnings[:] = found


def _clean() -> None:
    """Стирает отжившее: брошенные недогрузки и корзину старше срока хранения. Корзины входной
    папки и папки результатов чистятся каждая сама по себе: входная, подключённая только для
    чтения, не мешает чистить корзину результатов. Причина пропуска пишется в журнал работы
    одной строкой — когда появилась или сменилась, а не при каждой чистке."""
    if core.upload_ready:
        stale = upload.sweep(cfg.inbox)
        if stale:
            log(f"убраны брошенные недогрузки старше недели: {len(stale)}")
    gone, skipped = folders.purge(cfg, log)
    if gone:
        log(f"корзина: стёрто папок старше {folders.TRASH_DAYS} дней — {gone}")
    for whose, why in skipped.items():
        if clean_skipped.get(whose) != why:
            log(f"корзина {whose} не чистится: {why}")
    clean_skipped.clear()
    clean_skipped.update(skipped)


async def _clean_loop() -> None:
    """Сервис работает месяцами без перезапуска — чистки и проверки томов при запуске мало."""
    while True:
        await asyncio.sleep(CLEAN_EVERY_SEC)
        try:
            await asyncio.to_thread(_clean)
        except Exception as e:  # сетевая папка отвалилась — попробуем через сутки
            log(f"сбой чистки корзины и недогрузок (продолжаю): {e!r}")
        try:
            await asyncio.to_thread(_check_volumes)
        except Exception as e:
            log(f"сбой проверки томов (продолжаю): {e!r}")


# Сервис раз в HEARTBEAT_SEC отмечается в базе (таблица meta, ключ ALIVE): когда, какой версии, с какого
# времени работает и сколько обработчиков живо. Отметку видят страница «Обработка», внешний мониторинг (тот же
# /api/admin/status) и тот, кто открыл базу без сервиса: копия или восстановление понимают по ней, когда
# сервис над этой базой работал последний раз.
HEARTBEAT_SEC = 30
ALIVE = "alive"
started_at = ""                     # когда запущен этот процесс сервиса
workers: list[asyncio.Task] = []    # обработчики: живы ли они, видно по задачам


def _beat() -> None:
    assert core.service is not None
    base.note(ALIVE, json.dumps({
        "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "version": about.version() or "", "started": started_at,
        "workers": sum(not task.done() for task in workers), "of": cfg.workers,
        "running": len(core.service.running), "queue": len(core.service.queue)}, ensure_ascii=False))


async def _beat_loop() -> None:
    """Отметка «сервис жив» — раз в HEARTBEAT_SEC. Не пишется (база занята, диск полон) — одна строка в журнал
    работы, а когда снова пишется — ещё одна."""
    failing = False
    while True:
        try:
            await asyncio.to_thread(_beat)
            if failing:
                log("отметка «сервис жив» снова пишется в базу")
            failing = False
        except Exception as e:
            if not failing:
                log(f"ВНИМАНИЕ: отметка «сервис жив» не пишется в базу: {e!r}")
            failing = True
        await asyncio.sleep(HEARTBEAT_SEC)


def _heartbeat() -> dict | None:
    """Последняя отметка «сервис жив» из базы — и сколько секунд назад она сделана; None — отметок не было."""
    noted = base.noted(ALIVE)
    if not noted:
        return None
    beat = json.loads(noted)
    now = datetime.datetime.now().astimezone()
    ago = lambda at: round((now - datetime.datetime.fromisoformat(at)).total_seconds()) if at else None
    return {**beat, "ago_sec": ago(beat.get("at")), "uptime_sec": ago(beat.get("started")), "every_sec": HEARTBEAT_SEC}


# Резервную копию делает и сам сервис: по расписанию со страницы «Обработка» и кнопкой «Сделать копию сейчас»
# (backup.create — та же, что у команды). Раз в BACKUP_CHECK_SEC сервис сверяется с расписанием (backup.due:
# пропущенное время не догоняется пачкой). Копия идёт в потоке, обработка при этом работает. Две копии разом
# не идут — ни по расписанию рядом с кнопкой, ни дважды по расписанию: backup_job отмечается в событийном
# цикле без await между проверкой и отметкой. Копия и сбой — строкой в журнал работы, событием в журнал
# действий, отметкой для страницы (backup.last, backup.failure).
BACKUP_CHECK_SEC = 60
BACKUP_BUSY = "резервная копия уже делается — дождитесь её окончания: итог появится на странице «Обработка»"
backup_job: dict | None = None          # идущая копия: {"how", "by", "started"}
backup_task: asyncio.Task | None = None


def _backup_plan() -> dict:
    """Расписание копии со страницы, а не сохраняли — выключенное по умолчанию (by и at пусты)."""
    return options.backup_plan() or {**backup.PLAN, "by": "", "at": ""}


def _backup_due(now: datetime.datetime) -> bool:
    plan, made, failed = _backup_plan(), backup.last(base), backup.failure(base)
    return backup.due(plan, now, [plan["at"], made and made["at"], failed and failed["at"]])


def _backups() -> dict:
    """Копия из сервиса — странице и API: расписание, когда следующая, идущая копия, последняя неудача."""
    plan = _backup_plan()
    now = datetime.datetime.now().astimezone()
    return {"plan": plan, "next": backup.upcoming(plan, now).isoformat(timespec="seconds") if plan["on"] else None,
            "running": backup_job, "failed": backup.failure(base)}


async def _backup_work(how: str, by: str, inbox: bool, keep: int) -> None:
    global backup_job
    try:
        await asyncio.to_thread(backup.create, cfg, None, inbox, keep, lambda line: None, db.kept,
                                by=by, how=how, log=log, base=base)
    except Exception as e:      # нет места, нет тома копий, сбой — отметкой и строкой; следующая — по расписанию
        why = str(e) if isinstance(e, backup.BackupError) else f"сбой: {e!r}"
        await asyncio.to_thread(backup.failed, cfg, why, by=by, how=how, log=log, base=base)
    finally:
        backup_job = None


def _backup_start(how: str, by: str) -> dict:
    """Начинает копию в фоне с настройками расписания: с исходными записями или без и сколько копий
    держать (расписание не сохраняли — с записями, старые не стираются). Копия уже идёт — отказ 409."""
    global backup_job, backup_task
    if backup_job is not None:
        raise HTTPException(409, BACKUP_BUSY)
    plan = _backup_plan()
    backup_job = {"how": how, "by": by, "started": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    backup_task = asyncio.create_task(_backup_work(how, by, plan["inbox"], plan["keep"] if plan["at"] else 0))
    log("резервная копия: начинаю " + ("по расписанию" if how == backup.SCHEDULE else f"по кнопке ({by})")
        + ("" if plan["inbox"] else ", без исходных записей"))
    return backup_job


def _backup_tick(now: datetime.datetime) -> bool:
    """Пора ли копию по расписанию — пора, и копия не идёт: начать. Начата ли."""
    if backup_job is not None or not _backup_due(now):
        return False
    _backup_start(backup.SCHEDULE, "")
    return True


async def _backup_loop() -> None:
    while True:
        await asyncio.sleep(BACKUP_CHECK_SEC)
        try:
            _backup_tick(datetime.datetime.now().astimezone())
        except Exception as e:  # база занята — сверимся через минуту
            log(f"сбой расписания резервной копии (продолжаю): {e!r}")


# Напоминания о сроках поручений (кому и что — notify.py): раз в REMIND_CHECK_SEC сервис смотрит, пора ли.
# Пора — после notify.REMIND_HOUR по часам сервера, если сегодня рассылки ещё не было. День рассылки
# отмечается в базе (meta, ключ REMINDED) до отправки: сбой посреди рассылки не пришлёт второе письмо тем,
# кому первое уже ушло, а перезапуск сервиса в тот же день — тем более. Только в корпоративном режиме с почтой.
REMIND_CHECK_SEC = 60
REMINDED = "reminded"


def _remind_due(now: datetime.datetime) -> bool:
    return (cfg.mail_on and now.hour >= notify.REMIND_HOUR
            and base.noted(REMINDED) != now.date().isoformat())


def _remind_all(today: str) -> int:
    """Письмо-сводка каждому, кому есть что напомнить: поручения на него из открытых ему папок.
    Возвращает, скольким ушло. Сбой почты у одного не мешает остальным."""
    sent = 0
    for box in people.reminded():
        who = people.access(box["login"])
        if who is None:
            continue
        found, _ = _task_rows(who, cfg.public_url, "", "", "")
        due = notify.reminders([row for row in found if notify.assigned(row["who"], box["name"])], today)
        count = sum(len(rows) for rows in due.values())
        if not count:
            continue
        url = f"{cfg.public_url}/tasks?" + urlencode({"who": notify.surname(box["name"]), "sort": "due"},
                                                      quote_via=quote)
        try:
            mail.send(cfg, [box["email"]], mail.due_reminder(cfg, box["name"], due, url, notify.REMIND_MAX))
        except Exception as e:      # поток не должен умирать на одном письме
            log(f"ВНИМАНИЕ: напоминание о сроках поручений для {box['login']} не ушло: {e}")
            trail.write("mail_failed", "", what="due", to=box["login"], error=str(e)[:300])
            continue
        trail.write("due_mail", "", to=box["login"], tasks=count)
        sent += 1
    log(f"напоминания о сроках поручений: писем {sent}")
    return sent


async def _remind_tick(now: datetime.datetime) -> bool:
    """Пора — отметить день и разослать (в потоке: протоколы читаются с диска). Разослано ли."""
    if not _remind_due(now):
        return False
    base.note(REMINDED, now.date().isoformat())
    await asyncio.to_thread(_remind_all, now.date().isoformat())
    return True


async def _remind_loop() -> None:
    while True:
        await asyncio.sleep(REMIND_CHECK_SEC)
        try:
            await _remind_tick(datetime.datetime.now().astimezone())
        except Exception as e:  # база занята, диск недоступен — сверимся через минуту
            log(f"сбой напоминаний о сроках поручений (продолжаю): {e!r}")


def _key_targets() -> list[tuple[str, providers.Provider, tuple[str, ...]]]:
    """Чьи ключи проверять: основной поставщик, запасной и свой адрес текстовых этапов, если
    заданы. При каждом — текстовые модели, которые он должен знать: у текста со своим адресом
    основной и запасной их не обслуживают."""
    text_models = tuple(dict.fromkeys((cfg.summary_model, *cfg.actions_models)))
    own_text = cfg.summary_base_url != cfg.base_url
    found = [("основной поставщик", cfg.speech_provider, () if own_text else text_models)]
    if cfg.backup_provider:
        found.append(("запасной поставщик", cfg.backup_provider, () if own_text else text_models))
    if own_text:
        found.append(("текстовые этапы", cfg.text_provider, text_models))
    return found


async def _check_keys() -> list[dict]:
    """Проверка ключей поставщиков бесплатными запросами (providers.check) — кнопке «Проверить
    ключ» и запуску сервиса. Ключей в ответе нет."""
    targets = _key_targets()
    verdicts = await asyncio.gather(*(providers.check(provider, models) for _, provider, models in targets))
    return [{"role": role, "title": provider.title, **verdict}
            for (role, provider, _), verdict in zip(targets, verdicts)]


async def _check_keys_on_start() -> None:
    """Один раз при запуске: неверный ключ иначе виден только на первой записи. Итог — строкой
    в журнал работы; отказ — предупреждение, сервис при этом работает."""
    for found in await _check_keys():
        line = f"проверка ключа: {found['role']} {found['title']} — {found['text']}"
        log(line if found["state"] == providers.CHECK_OK
            else f"ВНИМАНИЕ: {line}" + (f". Что делать: {found['hint']}" if found["hint"] else ""))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _startup_checks()
    log(f"podyachiy {about.version() or 'без номера версии'} запущен: шаблон протокола выбирает разбор записи, "
        f"модели {speech_model()} + {cfg.summary_model}"
        + ("" if speech_model() == cfg.model
           else f" (распознавание выбрано на странице; в .env — {cfg.model})"))
    if cfg.summary_base_url != cfg.base_url:
        log(f"текстовые этапы идут по своему адресу: {cfg.summary_base_url} "
            f"(поручения: {', '.join(cfg.actions_models)}) — запасной поставщик на них не действует")
    global started_at
    core.service = Service(cfg=cfg, log=log, ledger=Ledger(base, log), journal=trail,
                      speech_model=speech_model, tell=_tell, words=words.effective, marked=noted,
                      series=lambda rel: serial.on(_parent_of(rel)), people=people, dictionaries=words,
                      dropped=options.dropped(), keep_dropped=options.set_dropped,
                      hook=_hook if hooks.on else None)
    try:
        core.service.known_to_nfc()          # записи и папки под разложенными именами — разово, до обхода
    except Exception as e:
        log(f"сбой приведения имён к составной форме (продолжаю): {e!r}")
    log(f"обработчиков: {cfg.workers} — столько записей идёт одновременно (WORKERS в .env)")
    started_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    workers[:] = [asyncio.create_task(core.service.worker_loop()) for _ in range(cfg.workers)]
    tasks = [asyncio.create_task(core.service.scan_loop()), *workers,
             asyncio.create_task(_check_keys_on_start())]     # не ждём: запуск не зависит от поставщика
    tasks.append(asyncio.create_task(_clean_loop()))
    tasks.append(asyncio.create_task(_beat_loop()))
    tasks.append(asyncio.create_task(_backup_loop()))
    tasks.append(asyncio.create_task(_remind_loop()))
    if hooks.on:
        tasks.append(asyncio.create_task(hooks.run()))     # доставка webhook — своей задачей, обработку не держит
    try:
        yield
    finally:
        for t in tasks + ([backup_task] if backup_task else []):
            t.cancel()
        log("podyachiy остановлен")
        base.close()            # база дописывает журнал WAL в основной файл: после остановки она — один файл
