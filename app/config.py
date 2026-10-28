"""Настройки сервиса из окружения (.env). Со страницы меняется только модель
распознавания для новых записей — она хранится отдельно, в app/settings.py.

Один объект Settings передаётся во все модули обработки под именем cfg —
модели, ключ и язык берутся из него так же, как в эталонном проекте.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from . import providers, speech


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, minimum)


def _price_env(name: str, default: float) -> float:
    """Потолок цены. Пусто — значение по умолчанию, 0 — потолка нет."""
    raw = (os.environ.get(name) or "").strip().replace(",", ".")
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return default


def _limit_env(name: str) -> int:
    """Лимит, который можно снять совсем. Пусто, 0 или «нет» — не ограничивать."""
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("", "0", "нет", "no", "none", "auto", "unlimited"):
        return 0
    try:
        return max(int(raw), 1000)
    except ValueError:
        return 0


def _flag_env(name: str, default: bool = False) -> bool:
    """Выключатель: 1, да, yes, on, true — включено; 0, нет, no, off, false — выключено."""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "да", "yes", "on", "true")


def _currency_env(name: str) -> str:
    """Код валюты. Раньше здесь писали знак для показа на странице — он читается как код."""
    raw = (os.environ.get(name) or "").strip().upper()
    return {"": "USD", "$": "USD", "₽": "RUB", "РУБ": "RUB", "РУБ.": "RUB"}.get(raw, raw)


@dataclass(frozen=True)
class Settings:
    # основной поставщик и модели
    api_key: str
    base_url: str
    # Модель распознавания из .env. Для новых записей администратор может выбрать другую
    # на странице (app/settings.py) — обработчик подставляет её в свою копию настроек.
    model: str
    # Текстовые этапы (разбор, поручения, протокол) ходят по своему адресу со своим
    # ключом: речь остаётся в облаке, а текст можно отдать модели на своей машине.
    # Не заданы в .env — равны адресу и ключу речи.
    summary_base_url: str
    summary_api_key: str
    # Сколько секунд ждать текстовый ответ. Пятнадцать минут выбраны по замерам:
    # обычно разбор укладывается в полторы минуты, а протокол в две с половиной,
    # но в плохой день у провайдера те же запросы честно отвечали пять с половиной
    # минут. Обрыв на таком ответе — оплаченная работа впустую и повтор с начала,
    # поэтому запас тройной. Бесконечным он быть не должен: зависший запрос держит
    # очередь, пока не оборвётся. Локальной модели пятнадцати минут может не хватить.
    summary_timeout_sec: int
    summary_model: str   # текстовая модель: разбор записи и протокол по шаблону
    summary_max_tokens: int
    actions_models: tuple[str, ...]  # кто независимо выписывает поручения перед протоколом
    summary_reasoning: str   # сколько модели рассуждать: low / medium / high, пусто — решает сама
    # Запасной поставщик: ему уходит вызов, на который основной не ответил (providers.py).
    # Не задан в .env — запасного нет.
    backup_base_url: str
    backup_api_key: str
    # Кому из поставщиков текстовой модели OpenRouter отдаёт запрос. Без этих правил
    # он выбирает по цене, а у одной модели поставщики различаются по скорости
    # в 14 раз (от 21 до 290 токенов в секунду) и по степени сжатия весов.
    provider_min_throughput: int             # токенов в секунду; медленнее — только если быстрых нет
    provider_max_price: float                # $ за миллион токенов ответа; 0 — без потолка
    provider_quantizations: tuple[str, ...]  # допустимые степени сжатия; пусто — любые
    language: str

    # папки
    inbox: Path
    outbox: Path
    prompts_dir: Path
    state_dir: Path          # база сервиса и ключ подписи входов — не рядом с результатами
    backup_dir: Path         # куда команда резервной копии кладёт архивы (app/backup.py)

    # corporate — вход обязателен, у каждого свои папки; personal — сервис на своём
    # компьютере для одного человека: входа нет, он и есть администратор
    personal: bool

    # обход папки
    scan_interval_sec: int
    stable_sec: int

    # сколько записей обрабатывается одновременно
    workers: int

    # загрузка записей через страницу
    upload_max_bytes: int

    # администрирование
    admin_password: str
    # В чём считает стоимость поставщик, чей адрес сервису незнаком (код валюты: USD, RUB).
    # У RouterAI и OpenRouter валюта известна по адресу, и эта настройка на них не действует.
    currency: str

    # дописывать в новый протокол незакрытые поручения предыдущей записи той же папки
    prev_tasks: bool = True

    # Почта: заявки на доступ, ссылки «задать пароль», письма о готовности записи, протокол
    # на почту. Без SMTP_HOST или PUBLIC_URL она выключена, и страницы всего этого не показывают.
    smtp_host: str = ""
    smtp_port: int = 0               # 0 — обычный порт для выбранной защиты
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_security: str = "starttls"  # none | starttls | ssl
    smtp_ca_file: str = ""           # корневой сертификат организации для почтового сервера; пусто — системные
    public_url: str = ""             # адрес сервиса, как его открывают люди: для ссылок в письмах
    admin_emails: tuple[str, ...] = ()
    # Почтовые домены, на адреса которых можно отправить протокол со страницы записи.
    # Пусто — на любые: протокол совещания уйдёт туда, куда его отправит хозяин записи.
    mail_domains: tuple[str, ...] = ()

    # Webhook «запись готова» (app/webhook.py): куда сервис сам шлёт событие, когда запись дошла до исхода,
    # и чем подписывает тело. Задаёт администратор сервиса здесь, в .env; без адреса или секрета webhook выключен.
    webhook_urls: tuple[str, ...] = ()
    webhook_secret: str = ""

    # Вход под учётной записью домена (Active Directory по LDAP) — app/directory.py. Только
    # корпоративный режим. Пароль служебной учётной записи живёт только здесь, из .env:
    # ни в settings.json, ни на странице, ни в логе его нет.
    ldap_urls: tuple[str, ...] = ()      # контроллеры по порядку: ldaps://dc1.corp.local:636, …
    ldap_base_dn: str = ""               # где искать людей и группы: DC=corp,DC=local
    ldap_bind_dn: str = ""               # служебная учётная запись только на чтение
    ldap_bind_password: str = ""
    ldap_user_filter: str = ""           # пусто — фильтр по умолчанию для Active Directory
    ldap_access_group: str = ""          # DN или имя группы; пусто — любой действующий пользователь из базы поиска
    ldap_admin_group: str = ""           # её члены — администраторы сервиса
    ldap_ca_file: str = ""               # корневой сертификат организации; пусто — state/ldap-ca.pem, а нет его — системные
    ldap_tls_verify: bool = True         # проверять сертификат контроллера
    ldap_starttls: bool = False          # ldap:// + StartTLS вместо ldaps://
    ldap_allow_http: bool = False        # исключение для стенда: без HTTPS и без шифрования до контроллера
    ldap_domain: str = ""                # короткое имя домена — подсказка на странице входа
    ldap_recheck_min: int = 15           # как часто перепроверять в домене вошедшего человека

    @property
    def mail_on(self) -> bool:
        return bool(self.smtp_host and self.smtp_from and self.public_url
                    and self.smtp_security in ("none", "starttls", "ssl")
                    and (not self.smtp_ca_file or Path(self.smtp_ca_file).is_file())) and not self.personal

    @property
    def speech_provider(self) -> providers.Provider:
        """Основной поставщик: распознавание и, если у текста нет своего адреса, текст."""
        return providers.make(self.base_url, self.api_key, "OPENAI_API_KEY", self.currency)

    @property
    def text_provider(self) -> providers.Provider:
        own_key = self.summary_api_key != self.api_key
        return providers.make(self.summary_base_url, self.summary_api_key,
                              "SUMMARY_API_KEY" if own_key else "OPENAI_API_KEY", self.currency)

    @property
    def backup_provider(self) -> providers.Provider | None:
        if not self.backup_base_url:
            return None
        return providers.make(self.backup_base_url, self.backup_api_key, "BACKUP_API_KEY", self.currency)

    @property
    def text_backup(self) -> providers.Provider | None:
        """Запасной для текстовых этапов. Он подменяет основного поставщика, а у текста
        со своим адресом (SUMMARY_BASE_URL — модель на своей машине) модели называются
        по-своему, и запасному их отдать нельзя."""
        return self.backup_provider if self.summary_base_url == self.base_url else None

    def provider_prefs(self) -> dict:
        """Поле provider запроса к текстовой модели (маршрутизация OpenRouter).

        Порог скорости — предпочтение: кто его не проходит, уходит в конец очереди,
        а среди прошедших OpenRouter выбирает сам, по цене. Потолок цены и степени
        сжатия — жёсткий отбор.

        Уходит только поставщику, который эти правила понимает (Provider.routes):
        RouterAI из поля provider знает лишь only и order, а чужой сервер (локальная
        модель в LM Studio) вправе отказать на незнакомом поле.
        """
        prefs: dict = {}
        if self.provider_min_throughput > 0:
            prefs["preferred_min_throughput"] = self.provider_min_throughput
        if self.provider_max_price > 0:
            prefs["max_price"] = {"completion": self.provider_max_price}
        if self.provider_quantizations:
            prefs["quantizations"] = list(self.provider_quantizations)
        return prefs

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = (os.environ.get("OPENAI_API_KEY")
                   or os.environ.get("OPENROUTER_API_KEY") or "").strip()
        base_url = os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1").strip()
        model = (os.environ.get("SPEECH_MODEL") or "").strip() or speech.DEFAULT_MODEL
        # опечатка в модели должна остановить запуск, а не первую же запись через час
        speech.engine(model)
        return cls(
            api_key=api_key,
            base_url=base_url,
            summary_base_url=(os.environ.get("SUMMARY_BASE_URL") or "").strip() or base_url,
            summary_api_key=(os.environ.get("SUMMARY_API_KEY") or "").strip() or api_key,
            summary_timeout_sec=_int_env("SUMMARY_TIMEOUT_SEC", 900, minimum=10),
            model=model,
            backup_base_url=(os.environ.get("BACKUP_BASE_URL") or "").strip(),
            backup_api_key=(os.environ.get("BACKUP_API_KEY") or "").strip(),
            summary_model=os.environ.get("SUMMARY_MODEL", "deepseek/deepseek-v4.1-flash").strip(),
            # 0 — не ограничивать вовсе: max_tokens в запрос не уходит, предел
            # определяет сам провайдер по модели. У рассуждающих моделей этот
            # лимит покрывает рассуждения и ответ вместе, и любое своё число
            # здесь — способ однажды упереться в него и получить пустой ответ
            summary_max_tokens=_limit_env("SUMMARY_MAX_TOKENS"),
            actions_models=tuple(m.strip() for m in os.environ.get(
                "ACTIONS_MODELS", "deepseek/deepseek-v4.1-flash,openai/gpt-6-luna").split(",") if m.strip()),
            summary_reasoning=(os.environ.get("SUMMARY_REASONING") or "").strip().lower(),
            provider_min_throughput=_int_env("PROVIDER_MIN_THROUGHPUT", 150, minimum=0),
            provider_max_price=_price_env("PROVIDER_MAX_PRICE", 1.5),
            provider_quantizations=tuple(q.strip().lower() for q in os.environ.get(
                "PROVIDER_QUANTIZATIONS", "fp8,fp16,bf16,fp32,unknown").split(",") if q.strip()),
            language=os.environ.get("LANGUAGE", "ru").strip(),
            inbox=Path(os.environ.get("INBOX_DIR", "/data/inbox")),
            outbox=Path(os.environ.get("OUTBOX_DIR", "/data/outbox")),
            prompts_dir=Path(os.environ.get("PROMPTS_DIR", "/app/prompts")),
            state_dir=Path(os.environ.get("STATE_DIR", "/data/state")),
            backup_dir=Path(os.environ.get("BACKUP_DIR", "/data/backup")),
            personal=os.environ.get("MODE", "corporate").strip().lower() == "personal",
            scan_interval_sec=_int_env("SCAN_INTERVAL_SEC", 10),
            stable_sec=_int_env("STABLE_SEC", 30),
            workers=_int_env("WORKERS", 3),
            upload_max_bytes=_int_env("UPLOAD_MAX_GB", 4, minimum=1) * 2**30,
            admin_password=os.environ.get("ADMIN_PASSWORD", ""),
            currency=_currency_env("COST_CURRENCY"),
            prev_tasks=(os.environ.get("PREV_TASKS") or "1").strip().lower() not in ("0", "нет", "no", "off", "false"),
            smtp_host=(os.environ.get("SMTP_HOST") or "").strip(),
            smtp_port=_int_env("SMTP_PORT", 0, minimum=0),
            smtp_user=(os.environ.get("SMTP_USER") or "").strip(),
            smtp_password=os.environ.get("SMTP_PASSWORD") or "",
            smtp_from=(os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "").strip(),
            smtp_security=(os.environ.get("SMTP_SECURITY") or "starttls").strip().lower(),
            smtp_ca_file=(os.environ.get("SMTP_CA_FILE") or "").strip(),
            public_url=(os.environ.get("PUBLIC_URL") or "").strip().rstrip("/"),
            admin_emails=tuple(a.strip().lower() for a in (os.environ.get("ADMIN_EMAIL") or "").split(",")
                               if a.strip()),
            mail_domains=tuple(d.strip().lower().lstrip("@") for d in (os.environ.get("MAIL_DOMAINS") or "").split(",")
                               if d.strip().lstrip("@")),
            webhook_urls=tuple(u.strip() for u in (os.environ.get("WEBHOOK_URL") or "").split(",") if u.strip()),
            webhook_secret=(os.environ.get("WEBHOOK_SECRET") or "").strip(),
            ldap_urls=tuple(u.strip() for u in (os.environ.get("LDAP_URL") or "").split(",") if u.strip()),
            ldap_base_dn=(os.environ.get("LDAP_BASE_DN") or "").strip(),
            ldap_bind_dn=(os.environ.get("LDAP_BIND_DN") or "").strip(),
            ldap_bind_password=os.environ.get("LDAP_BIND_PASSWORD") or "",
            ldap_user_filter=(os.environ.get("LDAP_USER_FILTER") or "").strip(),
            ldap_access_group=(os.environ.get("LDAP_ACCESS_GROUP") or "").strip(),
            ldap_admin_group=(os.environ.get("LDAP_ADMIN_GROUP") or "").strip(),
            ldap_ca_file=(os.environ.get("LDAP_CA_FILE") or "").strip(),
            ldap_tls_verify=_flag_env("LDAP_TLS_VERIFY", True),
            ldap_starttls=_flag_env("LDAP_STARTTLS"),
            ldap_allow_http=_flag_env("LDAP_ALLOW_HTTP"),
            ldap_domain=(os.environ.get("LDAP_DOMAIN") or "").strip(),
            ldap_recheck_min=_int_env("LDAP_RECHECK_MIN", 15),
        )

    @property
    def stable_samples(self) -> int:
        """Сколько одинаковых замеров подряд нужно, чтобы наблюдение покрыло
        STABLE_SEC. Первый замер — только точка отсчёта, поэтому при интервале
        10 с и STABLE_SEC=30 это четыре замера: между первым и четвёртым
        проходит ровно 30 секунд без изменений. Величина справочная, решение
        принимается по фактически прошедшему времени."""
        return math.floor(self.stable_sec / self.scan_interval_sec) + 1

    @property
    def outbox_inside_inbox(self) -> Path | None:
        """Путь OUTBOX относительно INBOX, если выходная папка лежит внутри входной.

        В этом случае сканер обязан пропускать собственные результаты,
        иначе расшифровки и извлечённый звук пойдут на обработку по кругу.
        """
        try:
            return self.outbox.resolve().relative_to(self.inbox.resolve())
        except (ValueError, OSError):
            return None
