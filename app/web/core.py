"""Настройки сервиса и общее состояние: хранилища, пределы частоты, флаги запуска."""
from __future__ import annotations

import asyncio
from pathlib import Path

from .. import (about, brand, db, dictionary, directory, journal, mail, marks, migrate, registry, series, settings,
                signup, users, webhook)
from ..config import Settings
from ..logbook import Logbook
from ..watcher import Service


STATIC = Path(__file__).parent.parent / "static"
SKILL = Path(__file__).parent.parent / "skill"      # навык для ИИ-клиентов и тексты для модели — шаблонами
SKILL_NAME = "podyachiy"
WRONG_PASSWORD_DELAY_SEC = 1.0

# что можно открыть на странице: подпапка результата и расширение
RESULT_KINDS = {
    "summary": ("summary", ".md"),
    "transcript": ("transcript", ".txt"),
    "srt": ("transcript", ".srt"),
}

# Записи, обработанные прежней связкой Whisper с Gemini, лежат в папке merged.
# Читать их страница должна по-прежнему: архив совещаний никуда не делся,
# а переобрабатывать его заново — платить второй раз за уже сделанное.
LEGACY_DIRS = {"transcript": "merged", "srt": "merged"}
RESULT_MAX_BYTES = 4_000_000

cfg = Settings.from_env()
log = Logbook(cfg.outbox)
service: Service | None = None
# База сервиса — в папке состояния, а она должна быть подключена с хоста: иначе база живёт внутри
# контейнера и пропадает при его пересоздании — вместе с пользователями, расходами и журналом
# обработки, после чего все записи пошли бы в обработку заново, за деньги.
state_kept = db.kept(cfg.state_dir)
try:
    # при первом запуске после обновления сюда же входит перенос прежних файлов состояния в базу
    base = migrate.open_base(cfg, log, state_kept, db.kept(cfg.outbox))
except db.Refusal as refusal:
    log(f"ОТКАЗ ЗАПУСКА: {refusal}")
    raise SystemExit(f"сервис не запущен: {refusal}")
people = users.Users(base, cfg.admin_password)
trail = journal.Journal(base, log)
claims = signup.Requests(base)
options = settings.Store(base, log)              # что администратор выбрал на странице
look = brand.Brand(base, log)                    # логотип и название организации (корпоративный режим)
words = dictionary.Store(base)                   # участники и сокращения папок и записей
serial = series.Store(base)                      # признак папки «Серия совещаний»
noted = marks.Store(base)                        # отметки «выполнено» и сроки поручений
task_cache = registry.Cache()        # разобранные поручения протоколов — чтобы не читать сотни файлов на запрос
domain = directory.Directory(cfg, log)           # вход под учётной записью домена (корпоративный режим)
# webhook «запись готова» (webhook.py): адреса и секрет из .env; не годятся — выключен, причина в журнале при запуске
webhook_off, webhook_warnings = webhook.check(cfg.webhook_urls, cfg.webhook_secret, cfg.public_url)
hooks = webhook.Hooks(base, () if webhook_off else cfg.webhook_urls, cfg.webhook_secret, log, about.version() or "")

# Неудачные входы через домен ограничены по логину и по адресу клиента. Без этого страница входа
# сервиса стала бы инструментом подбора доменных паролей и — хуже — блокировки чужих учётных
# записей: контроллер блокирует запись после нескольких неверных паролей подряд (обычный порог —
# 5–10 за 15–30 минут). Сервис отправляет в домен не больше 4 неверных паролей на один логин
# за 15 минут — ниже обычного порога блокировки. Считаются только отказы.
DOMAIN_FAILS_SEC = 15 * 60
domain_fails_by_login = signup.Throttle(4, DOMAIN_FAILS_SEC)
domain_fails_by_address = signup.Throttle(30, DOMAIN_FAILS_SEC)
# Пароли локальных учётных записей и admin — свои пределы, общие для страницы входа и логина с
# паролем в самом запросе (HTTP Basic): иначе пароль подбирали бы без конца, а каждая сверка — это
# scrypt на десятки миллисекунд, и поток неверных паролей занял бы ими весь сервис. Считаются только
# отказы; предел исчерпан — отказ 429 сразу, пароль не сверяется. На логин предел выше доменного:
# блокировки в домене здесь нет, а запереть человека посторонний может лишь на 15 минут. Адрес
# клиента за прокси и в Docker Desktop общий на всех — об этом ниже, у форм без входа. Ключей доступа
# не подобрать (в них 256 случайных бит), но и поток неверных ключей ограничен по адресу.
PASSWORD_FAILS_SEC = 15 * 60
password_fails_by_login = signup.Throttle(10, PASSWORD_FAILS_SEC)
password_fails_by_address = signup.Throttle(30, PASSWORD_FAILS_SEC)
key_fails_by_address = signup.Throttle(60, PASSWORD_FAILS_SEC)
PASSWORD_TOO_OFTEN = (f"слишком много неудачных попыток входа — подождите {PASSWORD_FAILS_SEC // 60} минут "
                      "и проверьте логин и пароль")
KEY_TOO_OFTEN = (f"слишком много запросов с неверным ключом доступа с вашего адреса — подождите "
                 f"{PASSWORD_FAILS_SEC // 60} минут и проверьте ключ в настройках клиента")

# Формы «Запросить доступ» и «Забыл пароль» открыты без входа — частоту ограничиваем по
# адресу клиента. Адрес берётся из соединения (request.client). Заголовкам X-Forwarded-For и
# X-Forwarded-Proto uvicorn верит только от адресов из FORWARDED_ALLOW_IPS; переменная не
# задана (в Dockerfile нет и ключа --forwarded-allow-ips) — только от 127.0.0.1. Поэтому
# заголовок от клиента подделать нельзя, но за обратным прокси на другом адресе все посетители
# приходят с адреса прокси и делят один предел на всех. То же в Docker Desktop (Windows,
# macOS): опубликованный порт отдаёт сервису адрес шлюза, а не клиента. Ставя сервис за
# прокси, задайте в окружении контейнера FORWARDED_ALLOW_IPS=<адрес прокси> (не «*») — uvicorn
# начнёт брать адрес клиента из заголовка этого прокси. От наплыва заявок при общем адресе
# страхует предел числа ожидающих (signup.PENDING_MAX).
claim_tries = signup.Throttle(30)        # любые отправки формы заявки с одного адреса, в час
claim_accepted = signup.Throttle(5)      # принятые заявки с одного адреса, в час
reset_by_address = signup.Throttle(10)   # запросы сброса пароля с одного адреса, в час
reset_by_login = signup.Throttle(3)      # запросы сброса на один логин или почту, в час
# Отправок протокола на почту в час на одного человека. Разослать протокол участникам после
# совещания — одна-две отправки; десяти хватает на рабочий день с несколькими совещаниями,
# а взломанная или забытая открытой учётная запись не превратит сервис в рассылку: за час
# с неё уйдёт не больше 10 отправок по 20 адресов (notify.RECIPIENTS_MAX), каждому адресу — письмо.
SENDS_PER_HOUR = 10
protocol_sends = signup.Throttle(SENDS_PER_HOUR)
NOTE_MAX = 1000                          # знаков в приписке к письму с протоколом
SEND_FORMATS = {"pdf": "PDF", "docx": "DOCX", "none": ""}      # вложение → как оно названо в письме
CLEAN_EVERY_SEC = 86400         # корзина и недогрузки чистятся при запуске и дальше раз в сутки
upload_ready = False     # входная папка доступна на запись — выясняется при запуске
upload_lock = asyncio.Lock()


def speech_model() -> str:
    """Модель распознавания для новых записей: выбранная на странице, а не выбрана — из .env."""
    chosen = options.speech_model()
    return chosen["value"] if chosen else cfg.model


def branded() -> bool:
    """Своё оформление — возможность корпоративного режима."""
    return not cfg.personal


def organization() -> str:
    """Название организации для писем и скачиваемых файлов; пусто — не задано или режим личный."""
    return look.name() if branded() else ""


mail.organization = organization
