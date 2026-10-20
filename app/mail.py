"""Почта сервиса: заявка на доступ, ссылка «задать пароль», пробное письмо, «запись готова»
и протокол на почту.

Письма уходят через почтовый сервер организации (SMTP), настройки — в .env. Отправка
блокирующая: зовут её через asyncio.to_thread. Ошибку сервера отдаём как есть — её читает
администратор, которому и чинить настройки; для обычного человека у ошибки есть второй текст,
человеческий (MailError.plain).

Письмо — простой текст и тот же текст в HTML, без картинок и внешних ссылок: почтовые
программы режут чужие картинки, а сервис живёт во внутренней сети. Всё, что пришло от
человека (ФИО, комментарий заявки, приписка к протоколу, название записи), в HTML
экранируется; в заголовки письма попадают только проверенные адреса, логин и название записи.

Вложение — протокол файлом. Имя файла кириллицей кодируется по RFC 2231 (filename*=utf-8''…),
это делает email.message.EmailMessage. Длинное значение она режет на продолжения
(filename*0*=, filename*1*=), а их понимают не все почтовые программы, поэтому два заголовка
вложения — Content-Disposition и Content-Type — идут одной строкой, до предела RFC 5322 в
998 знаков: имя уходит одним куском. Старые Outlook RFC 2231 не знают вовсе и берут имя из
name= в Content-Type, записанное словами RFC 2047 (=?utf-8?b?…?=), — оно там так и стоит.
Всё остальное — обычной длины: строки base64 по 76 знаков (RFC 2045), тема — короткими словами
RFC 2047, по одному на строку; за строки длиннее фильтры спама снижают письму оценку.
"""
from __future__ import annotations

import contextlib
import email.policy
import html
import smtplib
import ssl
from email.header import Header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path

SERVICE = "Подьячий"
# Название организации из раздела «Оформление» (корпоративный режим): стоит рядом с названием
# сервиса в теме и в шапке письма. Логотипа в письмах нет. Сервис подставляет сюда свою функцию
# при запуске (web.py); название читается на каждое письмо — смена на странице действует сразу.
organization = lambda: ""       # noqa: E731


def service_label() -> str:
    """Как письмо подписано: «Подьячий» или «Подьячий · ООО Ромашка»."""
    org = organization()
    return f"{SERVICE} · {org}" if org else SERVICE


PORTS = {"none": 25, "starttls": 587, "ssl": 465}       # защита соединения → обычный порт
TIMEOUT_SEC = 20
LINK_HOURS = 48
# Вложение больше этого сервис не шлёт: почтовые серверы организаций режут письма от 10–25 МБ,
# а вложение в письме ещё и на треть тяжелее файла. Протокол весит десятки и сотни килобайт.
ATTACH_MAX_BYTES = 10 * 2**20
_WIDE = email.policy.SMTP.clone(max_line_length=998)


class _Letter(email.policy.EmailPolicy):
    """Как письмо складывается в строки: всё по обычным правилам, два заголовка с именем
    вложения — одной строкой (почему — в описании модуля)."""

    def _one_line(self, name: str, value) -> str | None:
        """Заголовок вложения одной строкой; у прочих заголовков — None."""
        params = getattr(value, "params", {})
        if name.lower() == "content-disposition" and "filename" in params:
            return _WIDE.fold(name, value)
        if name.lower() != "content-type" or "name" not in params:
            return None
        file = params["name"]
        if file.isascii():
            return _WIDE.fold(name, value)
        # имя в Content-Type — словами RFC 2047 в кавычках: так его читают старые Outlook
        words = " ".join(Header(file, "utf-8", maxlinelen=75).encode().split())
        rest = "".join(f'; {k}="{v}"' for k, v in params.items() if k != "name")
        return f'{name}: {value.content_type}{rest}; name="{words}"{self.linesep}'

    def fold(self, name, value):
        return self._one_line(name, value) or super().fold(name, value)

    def fold_binary(self, name, value):
        line = self._one_line(name, value)
        return line.encode("ascii") if line else super().fold_binary(name, value)


# cte_type="7bit": тело письма всегда закодировано (base64 или quoted-printable) и проходит
# через почтовый сервер без расширения 8BITMIME.
POLICY = _Letter(linesep="\r\n", cte_type="7bit")

# Что видит обычный человек, когда письмо не ушло: ответ сервера ему ничего не скажет.
DOWN = "почтовый сервер не отвечает — сообщите администратору сервиса"
UNSENT = "почтовый сервер не принял письмо — сообщите администратору сервиса"
NO_ANSWER = "почтовый сервер не ответил"


class MailError(Exception):
    """Письмо не ушло. Текст — ответ сервера как есть: для администратора и журналов.
    plain — то же человеческим языком, для того, кто настройки не чинит. refused — адреса,
    которые сервер не принял, с его ответом: когда дело в адресах, а не в сервере."""

    def __init__(self, text: str, plain: str = UNSENT, refused: dict[str, str] | None = None):
        super().__init__(text)
        self.plain, self.refused = plain, refused or {}


def why_off(cfg) -> str | None:
    """Почему почта выключена, хотя сервер задан, — для лога при запуске."""
    if not cfg.smtp_host or cfg.personal:
        return None
    if cfg.smtp_security not in PORTS:
        return f"SMTP_SECURITY={cfg.smtp_security!r}: нужно none, starttls или ssl"
    if not cfg.smtp_from:
        return "не задан SMTP_FROM — адрес, от которого уходят письма"
    if not cfg.public_url:
        return "не задан PUBLIC_URL — адрес сервиса для ссылок в письмах"
    if cfg.smtp_ca_file and not Path(cfg.smtp_ca_file).is_file():
        return (f"SMTP_CA_FILE={cfg.smtp_ca_file}: такого файла в контейнере нет — положите сертификат центра "
                "сертификации организации (PEM) в папку состояния и укажите путь вида /data/state/smtp-ca.pem")
    return None


def warnings(cfg) -> list[str]:
    """Что в настройках почты небезопасно — для лога при запуске и блока «Почта»."""
    if cfg.smtp_security == "none" and cfg.smtp_user:
        return ["SMTP_SECURITY=none при заданном SMTP_USER: логин и пароль почты идут до почтового сервера "
                "открытым текстом. Задайте SMTP_SECURITY=starttls или ssl"]
    return []


def tls_context(cfg) -> ssl.SSLContext:
    """Чем проверяется сертификат почтового сервера: центры, которым доверяет система, и корневой
    сертификат организации из SMTP_CA_FILE, если он задан."""
    context = ssl.create_default_context()
    if cfg.smtp_ca_file:
        try:
            context.load_verify_locations(cafile=cfg.smtp_ca_file)
        except OSError as e:
            raise MailError(f"SMTP_CA_FILE={cfg.smtp_ca_file}: файл не читается как сертификат PEM ({e})") from e
    return context


def message(cfg, to: list[str], subject: str, text: str, body_html: str,
            attachments: list[tuple[bytes, str, str]] = (), reply_to: str = "") -> EmailMessage:
    """attachments — (содержимое, тип вида application/pdf, имя файла)."""
    msg = EmailMessage(policy=POLICY)
    msg["From"] = cfg.smtp_from
    msg["To"] = ", ".join(to)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = f"{service_label()}: {subject}"
    msg["Date"] = formatdate(localtime=True)
    # домен — из адреса отправителя: по умолчанию там имя контейнера, а оно ни о чём не говорит
    msg["Message-ID"] = make_msgid(domain=parseaddr(cfg.smtp_from)[1].rpartition("@")[2] or None)
    msg.set_content(text)
    msg.add_alternative(body_html, subtype="html")
    for data, kind, name in attachments:
        maintype, _, subtype = kind.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name, params={"name": name})
    return msg


@contextlib.contextmanager
def _server(cfg):
    """Соединение с почтовым сервером: защита и вход — по настройкам. Не получилось — MailError
    с ответом сервера."""
    port = cfg.smtp_port or PORTS[cfg.smtp_security]
    context = tls_context(cfg) if cfg.smtp_security != "none" else None
    try:
        if cfg.smtp_security == "ssl":
            client = smtplib.SMTP_SSL(cfg.smtp_host, port, timeout=TIMEOUT_SEC, context=context)
        else:
            client = smtplib.SMTP(cfg.smtp_host, port, timeout=TIMEOUT_SEC)
        with client:
            if cfg.smtp_security == "starttls":
                client.starttls(context=context)
            if cfg.smtp_user:
                client.login(cfg.smtp_user, cfg.smtp_password)
            yield client
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"{type(e).__name__}: {e}", DOWN if _lost(e) else UNSENT) from e


def _lost(e: Exception) -> bool:
    """Сервера нет на связи (не найден, не ответил, оборвал соединение) — в отличие от отказа,
    который он прислал, и от несошедшегося сертификата."""
    if isinstance(e, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return True
    return not isinstance(e, (smtplib.SMTPException, ssl.SSLError))


def _reply(code: int, text: bytes) -> str:
    """Ответ сервера строкой: «550 5.1.1 no such user»."""
    return f"{code} {text.decode('utf-8', 'replace')}".strip()


def send(cfg, to: list[str], letter: tuple[str, str, str],
         attachments: list[tuple[bytes, str, str]] = (), reply_to: str = "") -> dict:
    """Отправляет письмо (тема, текст, HTML). Не получилось — MailError с ответом сервера.
    Отдаёт адреса, которые сервер не принял, с его ответом: остальным письмо ушло."""
    msg = message(cfg, to, *letter, attachments=attachments, reply_to=reply_to)
    with _server(cfg) as client:
        return client.send_message(msg)


def send_each(cfg, to: list[str], letter: tuple[str, str, str],
              attachments: list[tuple[bytes, str, str]] = (), reply_to: str = "") -> dict[str, str]:
    """Отправляет письмо каждому адресу отдельно: в поле «Кому» только он, чужих адресов
    получатель не видит. Соединение с сервером одно на все письма. Отдаёт адреса, которым письмо
    не ушло, с ответом сервера строкой: остальным ушло. Не ушло никому — MailError: сервер ответил
    отказом на каждый адрес — с этими ответами (refused), пропал — с тем, на чём оборвалось."""
    failed: dict[str, str] = {}
    lost = None
    with _server(cfg) as client:
        for email in to:
            try:
                client.send_message(message(cfg, [email], *letter, attachments=attachments, reply_to=reply_to))
            except smtplib.SMTPRecipientsRefused as e:
                failed[email] = _reply(*e.recipients[email])
            except smtplib.SMTPResponseException as e:
                failed[email] = _reply(e.smtp_code, e.smtp_error)
            except (smtplib.SMTPException, OSError) as e:
                lost = e
                failed[email] = NO_ANSWER
    if len(failed) < len(to):
        return failed
    if lost:
        raise MailError(f"{type(lost).__name__}: {lost}", DOWN) from lost
    said = "; ".join(f"{email} ({why})" for email, why in failed.items())
    raise MailError(f"сервер отказал всем адресам: {said}", refused=failed)


# ---------- тексты писем ----------

class _Block(str):
    """Готовый блок HTML (список): в абзац не заворачивается."""


def _bullets(lines: list[str]) -> _Block:
    return _Block('<ul style="margin:0 0 12px;padding-left:20px">'
                  + "".join(f'<li style="margin:0 0 6px">{html.escape(line)}</li>' for line in lines) + "</ul>")


def _page(title: str, paragraphs: list[str], link: tuple[str, str] | None = None, foot: str = "") -> str:
    """HTML письма. paragraphs — уже готовый HTML: всё чужое в них экранирует тот, кто зовёт."""
    button = ""
    if link:
        url, label = html.escape(link[0], quote=True), html.escape(link[1])
        button = (f'<p style="margin:24px 0"><a href="{url}" style="display:inline-block;padding:11px 20px;'
                  f'background:#1d4ed8;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:600">'
                  f'{label}</a></p>'
                  f'<p style="font-size:13px;color:#4b5a6b">Если кнопка не открывается, скопируйте адрес '
                  f'в браузер:<br><span style="word-break:break-all">{url}</span></p>')
    body = "".join(p if isinstance(p, _Block) else f'<p style="margin:0 0 12px">{p}</p>' for p in paragraphs)
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"></head><body style="margin:0;padding:24px;background:#eef2f7;'
            f'font:15px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#0f172a">'
            f'<div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:10px;padding:24px">'
            f'<div style="font-size:13px;color:#4b5a6b;margin-bottom:6px">{html.escape(service_label())}</div>'
            f'<h1 style="font-size:19px;margin:0 0 16px">{html.escape(title)}</h1>{body}{button}'
            f'<p style="font-size:13px;color:#4b5a6b;margin:20px 0 0">{html.escape(foot)}</p>'
            f'</div></body></html>')


def set_password_url(cfg, token: str) -> str:
    """Ссылка «задать пароль». Сама строка стоит после решётки: браузер эту часть адреса
    серверу не отправляет, поэтому она не попадает ни в журнал запросов, ни в заголовок Referer."""
    return f"{cfg.public_url}/set-password#{token}"


def notice(cfg, claim: dict) -> tuple[str, str, str]:
    """Администраторам: подана заявка на доступ. Ответ на письмо уходит заявителю, если он
    указал почту: Reply-To ставит тот, кто шлёт."""
    url = f"{cfg.public_url}/admin#users"
    rows = [("ФИО", claim["name"]), ("Почта", claim["email"]), ("Желаемый логин", claim["login"]),
            ("Комментарий", claim["comment"] or "—")]
    foot = ("Письмо отправлено автоматически. Ответ на него уйдёт заявителю." if claim["email"]
            else "Письмо отправлено автоматически, отвечать на него не нужно.")
    text = ("Подана заявка на доступ к сервису.\n\n"
            + "\n".join(f"{k}: {v}" for k, v in rows)
            + f"\n\nОткрыть доступ или отклонить: {url}\n\n{foot}\n")
    table = "".join(f'<b>{html.escape(k)}:</b> {html.escape(v)}<br>' for k, v in rows)
    return (f"заявка на доступ — {claim['login']}", text,
            _page("Заявка на доступ", ["Подана заявка на доступ к сервису.", table],
                  (url, "Открыть заявки"), foot))


def _link_letter(cfg, subject: str, title: str, lead: str, name: str, login: str, token: str,
                 foot: str) -> tuple[str, str, str]:
    url = set_password_url(cfg, token)
    life = (f"Ссылка действует {LINK_HOURS} часов и срабатывает один раз. Пароль вы придумываете сами: "
            f"его не знает никто, включая администратора.")
    text = (f"Здравствуйте, {name}!\n\n{lead}\n\nВаш логин: {login}\n\n"
            f"Задать пароль: {url}\n\n{life}\n\n{foot}\n")
    return (subject, text,
            _page(title, [f"Здравствуйте, {html.escape(name)}!", html.escape(lead),
                          f"Ваш логин: <b>{html.escape(login)}</b>", html.escape(life)],
                  (url, "Задать пароль"), foot))


def invite(cfg, name: str, login: str, token: str) -> tuple[str, str, str]:
    """Человеку: доступ открыт, осталось задать пароль."""
    return _link_letter(cfg, "доступ открыт", "Вам открыт доступ",
                        f"Вам открыт доступ к сервису {SERVICE} — расшифровки и протоколы совещаний.",
                        name, login, token,
                        "Если вы не просили доступ, просто не открывайте ссылку.")


def reset(cfg, name: str, login: str, token: str) -> tuple[str, str, str]:
    """Человеку: ссылка для нового пароля."""
    return _link_letter(cfg, "новый пароль", "Новый пароль",
                        f"Для вашей учётной записи в сервисе {SERVICE} запрошена смена пароля.",
                        name, login, token,
                        "Если вы этого не просили, ничего делать не нужно: прежний пароль продолжает действовать.")


def probe(cfg) -> tuple[str, str, str]:
    """Пробное письмо из раздела «Пользователи»."""
    lead = "Это пробное письмо: почта в сервисе настроена и письма доходят."
    where = f"Адрес сервиса в письмах: {cfg.public_url}"
    return ("пробное письмо", f"{lead}\n\n{where}\n",
            _page("Пробное письмо", [html.escape(lead), html.escape(where)]))


# ---------- письма о записи ----------
# about — сведения о записи, собранные notify.about: название, имя файла, дата, длительность,
# «Кратко» строками, число поручений, ссылка на запись на странице сервиса.

def _facts(about: dict, tasks: bool) -> list[tuple[str, str]]:
    rows = [("Дата", about["day"]), ("Длительность", about["duration"])]
    if tasks and about["tasks"]:            # ноль — у лекций и разборов, где поручений и не ждут: строка лишняя
        rows.append(("Поручений в протоколе", str(about["tasks"])))
    return [(k, v) for k, v in rows if v]


def _brief(about: dict) -> tuple[str, list[str]]:
    """«Кратко» для текста письма и для HTML."""
    if not about["brief"]:
        return "", []
    return ("Кратко:\n" + "\n".join(f"- {line}" for line in about["brief"]) + "\n\n",
            ["<b>Кратко</b>", _bullets(about["brief"])])


NOTIFY_FOOT = ("Письма о готовности записей отключаются в сервисе: меню учётной записи, "
               "«Письма о готовности».")


def ready(cfg, name: str, about: dict) -> tuple[str, str, str]:
    """Человеку: запись обработана, протокол готов."""
    lead = f"Запись «{about['title']}» обработана: протокол и расшифровка готовы."
    facts = _facts(about, tasks=True)
    brief_text, brief_html = _brief(about)
    text = (f"Здравствуйте, {name}!\n\n{lead}\n\n"
            + "".join(f"{k}: {v}\n" for k, v in facts) + ("\n" if facts else "") + brief_text
            + f"Открыть запись: {about['url']}\n\n{NOTIFY_FOOT}\n")
    table = "".join(f"<b>{html.escape(k)}:</b> {html.escape(v)}<br>" for k, v in facts)
    return (f"запись готова — {about['title']}", text,
            _page("Запись готова", [f"Здравствуйте, {html.escape(name)}!", html.escape(lead),
                                    *([table] if table else []), *brief_html],
                  (about["url"], "Открыть запись"), NOTIFY_FOOT))


# Что случилось и что делать — по исходу обработки; человеческим языком, без текста ошибки:
# причину сбоя видит администратор в разделе «Обработка».
WHAT_FAILED = {
    "no_audio": ("в записи нет звука",
                 "В файле «{file}» не нашлось звука — расшифровывать нечего.",
                 "Проверьте, что запись велась со звуком, и загрузите файл заново. Если звук в файле есть, "
                 "покажите запись администратору сервиса."),
    "no_summary": ("протокол не собран",
                   "Речь в записи «{file}» распознана, расшифровку уже можно читать, но протокол собрать не удалось.",
                   ""),
    "error": ("запись не обработана", "Обработать запись «{file}» не удалось.", ""),
}
WILL_RETRY = "Сервис попробует ещё раз сам, делать ничего не нужно: состояние записи видно на странице."
WONT_RETRY = ("Автоматических попыток больше не будет. Обратитесь к администратору сервиса: он увидит причину "
              "в разделе «Обработка» и запустит запись вручную. Загружать файл заново не нужно.")
# тому, кто сам запустил запись из раздела «Обработка»: к администратору сервиса его не отправишь
BY_HAND = "Причина написана под записью в разделе «Обработка»."
BY_HAND_RETRY = f"Сервис попробует ещё раз сам. {BY_HAND}"
BY_HAND_LAST = (f"Автоматических попыток больше не будет. {BY_HAND} Устраните её и запустите запись ещё раз — "
                "загружать файл заново не нужно.")


def failed(cfg, name: str, about: dict, status: str, retrying: bool, by_hand: bool = False) -> tuple[str, str, str]:
    """Человеку: запись обработать не удалось — что случилось и что делать. by_hand — письмо идёт
    тому, кто запустил запись вручную, а не автору загрузки."""
    subject, what, advice = WHAT_FAILED[status]
    what = what.format(file=about["file_name"])
    advice = advice or ((BY_HAND_RETRY if retrying else BY_HAND_LAST) if by_hand
                        else WILL_RETRY if retrying else WONT_RETRY)
    label = "Открыть расшифровку" if status == "no_summary" else "Открыть папку записи"
    text = (f"Здравствуйте, {name}!\n\n{what}\n\n{advice}\n\n"
            f"{label}: {about['url']}\n\n{NOTIFY_FOOT}\n")
    return (f"{subject} — {about['file_name']}", text,
            _page(subject[0].upper() + subject[1:], [f"Здравствуйте, {html.escape(name)}!", html.escape(what),
                                                     html.escape(advice)],
                  (about["url"], label), NOTIFY_FOOT))


def protocol(cfg, sender: str, about: dict, note: str, attached: str) -> tuple[str, str, str]:
    """Получателям: протокол совещания. attached — что во вложении («PDF», «DOCX») либо пусто."""
    lead = f"Вам отправлен протокол совещания «{about['title']}». Отправитель: {sender}."
    facts = _facts(about, tasks=False)
    brief_text, brief_html = _brief(about)
    inside = (f"Протокол целиком — во вложении ({attached})." if attached
              else "Протокол целиком — по ссылке ниже.")
    need = "Чтобы открыть ссылку, нужна учётная запись в сервисе и доступ к папке этой записи."
    foot = f"Письмо отправлено из сервиса {SERVICE} по просьбе отправителя ({sender})."
    text = (f"{lead}\n\n" + "".join(f"{k}: {v}\n" for k, v in facts) + ("\n" if facts else "") + brief_text
            + (f"Приписка отправителя:\n{note}\n\n" if note else "")
            + f"{inside}\n\nЗапись в сервисе: {about['url']}\n{need}\n\n{foot}\n")
    table = "".join(f"<b>{html.escape(k)}:</b> {html.escape(v)}<br>" for k, v in facts)
    wrote = [f"<b>Приписка отправителя</b><br>{html.escape(note).replace(chr(10), '<br>')}"] if note else []
    return (f"протокол — {about['title']}", text,
            _page("Протокол совещания", [html.escape(lead), *([table] if table else []), *brief_html, *wrote,
                                         html.escape(inside), html.escape(need)],
                  (about["url"], "Открыть запись в сервисе"), foot))
