"""Почта: письма (текст и HTML), выбор защиты соединения, ошибка сервера как есть.

smtplib подменяется; несколько тестов шлют письмо по-настоящему — в маленький SMTP-сервер,
поднятый в этом же процессе (модуля smtpd в Python 3.12 нет, а новые зависимости ни к чему).
Он умеет и STARTTLS — с сертификатом своего центра сертификации, выпущенным тут же.
"""
import datetime
import email
import email.generator
import email.header
import email.policy
import io
import ipaddress
import re
import smtplib
import socketserver
import ssl
import threading
import types

import pytest

from app import mail


def cfg(**over):
    base = dict(smtp_host="smtp.test", smtp_port=0, smtp_user="", smtp_password="", personal=False, smtp_ca_file="",
                smtp_from="podyachiy@example.test", smtp_security="none", public_url="http://192.0.2.10:8082")
    return types.SimpleNamespace(**{**base, **over})


def parts(msg):
    return (msg.get_body(preferencelist=("plain",)).get_content(),
            msg.get_body(preferencelist=("html",)).get_content())


CLAIM = {"name": "Сидоров <b>Пётр</b>", "email": "sidorov@example.test", "login": "sidorov",
         "comment": "Отдел & <script>alert(1)</script>"}


def test_notice_to_admins_has_text_and_escaped_html():
    msg = mail.message(cfg(), ["boss@example.test", "it@example.test"], *mail.notice(cfg(), CLAIM))
    assert msg["To"] == "boss@example.test, it@example.test" and msg["From"] == "podyachiy@example.test"
    assert msg["Subject"] == "Подьячий: заявка на доступ — sidorov" and msg["Message-ID"] and msg["Date"]
    text, html = parts(msg)
    assert "Сидоров <b>Пётр</b>" in text and "sidorov@example.test" in text and "Отдел & <script>" in text
    assert "http://192.0.2.10:8082/admin#users" in text
    # «Ответить» ведёт к заявителю — об этом сказано; без почты в заявке отвечать некому
    assert "Ответ на него уйдёт заявителю" in text and "Ответ на него уйдёт заявителю" in html
    bare = mail.notice(cfg(), {**CLAIM, "email": ""})
    assert "отвечать на него не нужно" in bare[1] and "отвечать на него не нужно" in bare[2]
    # в HTML чужой текст не становится разметкой
    assert "Сидоров &lt;b&gt;Пётр&lt;/b&gt;" in html and "<script>" not in html and "Отдел &amp; &lt;script&gt;" in html
    assert 'href="http://192.0.2.10:8082/admin#users"' in html
    assert "<img" not in html and "Подьячий" in html           # без картинок, с названием сервиса


@pytest.mark.parametrize("letter, word", [(mail.invite, "открыт доступ"), (mail.reset, "смена пароля")])
def test_link_letters_carry_the_login_and_the_link_but_no_password(letter, word):
    subject, text, html = letter(cfg(), "Пётр <Сидоров>", "sidorov", "tok-123_ABC")
    url = "http://192.0.2.10:8082/set-password#tok-123_ABC"
    assert url in text and f'href="{url}"' in html and "sidorov" in text and "<b>sidorov</b>" in html
    assert word in text and "48 часов" in text and "48 часов" in html
    assert "Пётр &lt;Сидоров&gt;" in html and "<Сидоров>" not in html
    # строка ссылки стоит после решётки: серверу в адресе запроса она не уходит
    assert "?token" not in text and "?" not in url


def test_probe_letter_names_the_public_address():
    subject, text, html = mail.probe(cfg())
    assert subject == "пробное письмо" and "http://192.0.2.10:8082" in text and "http://192.0.2.10:8082" in html


class FakeSMTP:
    made = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls, self.sent = [], []
        FakeSMTP.made.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append("send")
        self.sent.append(msg)


class FakeSSL(FakeSMTP):
    pass


@pytest.fixture
def fake(monkeypatch):
    FakeSMTP.made = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
    return FakeSMTP.made


@pytest.mark.parametrize("security, port, kind, calls", [
    ("none", 25, FakeSMTP, ["send", "quit"]),
    ("starttls", 587, FakeSMTP, ["starttls", "send", "quit"]),
    ("ssl", 465, FakeSSL, ["send", "quit"]),
])
def test_security_picks_the_class_the_port_and_starttls(fake, security, port, kind, calls):
    mail.send(cfg(smtp_security=security), ["a@example.test"], mail.probe(cfg()))
    client, = fake
    assert type(client) is kind and (client.host, client.port) == ("smtp.test", port)
    assert client.calls == calls and client.timeout == mail.TIMEOUT_SEC
    assert client.sent[0]["To"] == "a@example.test"


def test_own_port_and_login_are_used_when_given(fake):
    mail.send(cfg(smtp_security="starttls", smtp_port=2525, smtp_user="robot", smtp_password="секрет"),
              ["a@example.test"], mail.probe(cfg()))
    client, = fake
    assert client.port == 2525
    assert client.calls == ["starttls", ("login", "robot", "секрет"), "send", "quit"]


def test_server_refusal_and_dead_host_come_back_as_they_are(monkeypatch):
    class Refusing(FakeSMTP):
        def send_message(self, msg):
            raise smtplib.SMTPRecipientsRefused({"a@example.test": (550, b"5.1.1 mailbox unavailable")})
    monkeypatch.setattr(smtplib, "SMTP", Refusing)
    with pytest.raises(mail.MailError, match="SMTPRecipientsRefused.*550.*mailbox unavailable"):
        mail.send(cfg(), ["a@example.test"], mail.probe(cfg()))

    def dead(*a, **kw):
        raise ConnectionRefusedError(111, "Connection refused")
    monkeypatch.setattr(smtplib, "SMTP", dead)
    with pytest.raises(mail.MailError, match="ConnectionRefusedError.*Connection refused"):
        mail.send(cfg(), ["a@example.test"], mail.probe(cfg()))


@pytest.mark.parametrize("error, plain", [
    (ConnectionRefusedError(111, "Connection refused"), mail.DOWN),
    (TimeoutError("timed out"), mail.DOWN),
    (OSError(-2, "Name or service not known"), mail.DOWN),
    (smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), mail.DOWN),
    (smtplib.SMTPAuthenticationError(535, b"5.7.8 bad password"), mail.UNSENT),
    (ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED]"), mail.UNSENT),
])
def test_every_failure_has_words_for_a_person_who_does_not_fix_mail(monkeypatch, error, plain):
    def dead(*a, **kw):
        raise error
    monkeypatch.setattr(smtplib, "SMTP", dead)
    for send in (mail.send, mail.send_each):
        with pytest.raises(mail.MailError) as failed:
            send(cfg(), ["a@example.test"], mail.probe(cfg()))
        # ответ сервера как есть — в тексте ошибки, человеку — plain: без имён исключений и b'…'
        assert type(error).__name__ in str(failed.value) and failed.value.plain == plain
        assert "Error" not in plain and "сообщите администратору сервиса" in plain and failed.value.refused == {}


def test_why_off_explains_what_is_missing():
    assert mail.why_off(cfg()) is None
    assert mail.why_off(cfg(smtp_host="")) is None                  # почту и не включали
    assert "SMTP_SECURITY" in mail.why_off(cfg(smtp_security="tls"))
    assert "SMTP_FROM" in mail.why_off(cfg(smtp_from=""))
    assert "PUBLIC_URL" in mail.why_off(cfg(public_url=""))


def test_password_in_the_clear_is_warned_about():
    assert mail.warnings(cfg(smtp_security="none")) == []                           # без входа скрывать нечего
    assert mail.warnings(cfg(smtp_security="starttls", smtp_user="robot")) == []
    warning, = mail.warnings(cfg(smtp_security="none", smtp_user="robot"))
    assert "SMTP_SECURITY=none" in warning and "открытым текстом" in warning and "starttls" in warning


# ---------- настоящая отправка в локальный SMTP-сервер ----------

class Mailbox(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, tls=None):
        super().__init__(("127.0.0.1", 0), Talk)
        self.letters = []       # (от кого, кому, письмо)
        self.raw = []           # те же письма как пришли, байтами
        self.tls = tls          # ssl-контекст сервера: с ним сервер предлагает STARTTLS


class Talk(socketserver.StreamRequestHandler):
    """SMTP ровно настолько, насколько его использует smtplib: без шифрования и входа."""

    def say(self, line):
        self.wfile.write(line.encode("ascii") + b"\r\n")

    def handle(self):
        sender, rcpt = "", []
        self.say("220 test ESMTP")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            verb = line.decode("utf-8").strip()
            head = verb.upper()
            if head.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250-test\r\n250-SMTPUTF8\r\n" + (b"250-STARTTLS\r\n" if self.server.tls else b"")
                                 + b"250 8BITMIME\r\n")
            elif head == "STARTTLS" and self.server.tls:
                self.say("220 go ahead")
                try:
                    self.connection = self.server.tls.wrap_socket(self.connection, server_side=True)
                except OSError:             # клиент не поверил сертификату и ушёл
                    return
                self.rfile, self.wfile = self.connection.makefile("rb"), self.connection.makefile("wb", 0)
            elif head.startswith("MAIL FROM:"):
                sender = verb[10:].split()[0].strip("<>")
                self.say("250 OK")
            elif head.startswith("RCPT TO:<NOBODY@"):
                self.say("550 5.1.1 no such user")
            elif head.startswith("RCPT TO:"):
                rcpt.append(verb[8:].strip().strip("<>"))
                self.say("250 OK")
            elif head == "DATA":
                self.say("354 go on")
                data = b""
                while not data.endswith(b"\r\n.\r\n"):
                    data += self.rfile.readline()
                body = data[:-5].replace(b"\r\n..", b"\r\n.")       # точка в начале строки удваивается
                self.server.raw.append(body)
                self.server.letters.append((sender, rcpt, email.message_from_bytes(body, policy=email.policy.default)))
                sender, rcpt = "", []
                self.say("250 queued")
            elif head == "RSET":
                sender, rcpt = "", []
                self.say("250 OK")
            elif head == "QUIT":
                self.say("221 bye")
                return
            else:
                self.say("502 not implemented")


def test_letter_really_travels_over_smtp():
    with Mailbox() as box:
        threading.Thread(target=box.serve_forever, daemon=True).start()
        try:
            settings = cfg(smtp_host="127.0.0.1", smtp_port=box.server_address[1])
            mail.send(settings, ["sidorov@example.test"], mail.invite(settings, "Пётр Сидоров", "sidorov", "tok-123"))
            mail.send(settings, ["boss@example.test", "it@example.test"], mail.notice(settings, CLAIM))
        finally:
            box.shutdown()
        (sender, rcpt, letter), (_, admins, notice) = box.letters
    assert sender == "podyachiy@example.test" and rcpt == ["sidorov@example.test"]
    assert letter["Subject"] == "Подьячий: доступ открыт" and letter["To"] == "sidorov@example.test"
    text, html = parts(letter)
    assert "Здравствуйте, Пётр Сидоров!" in text and "http://192.0.2.10:8082/set-password#tok-123" in text
    assert 'href="http://192.0.2.10:8082/set-password#tok-123"' in html
    assert admins == ["boss@example.test", "it@example.test"]
    assert "Сидоров <b>Пётр</b>" in parts(notice)[0] and "&lt;b&gt;" in parts(notice)[1]


# ---------- письма о записи ----------

ABOUT = {"title": "Планёрка <b>по складу</b>", "file_name": "вкс 0610.mp4", "day": "06.10.2026",
         "duration": "1 ч 27 мин", "brief": ["Срок не меняется.", "Стеллажи <заказаны> & оплачены."], "tasks": 4,
         "url": "http://192.0.2.10:8082/?path=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B&open=x.mp4"}


def test_ready_letter_has_the_link_the_summary_and_the_counts():
    subject, text, html = mail.ready(cfg(), "Пётр <Петров>", ABOUT)
    assert subject == "запись готова — Планёрка <b>по складу</b>"
    assert "Здравствуйте, Пётр <Петров>!" in text and "обработана: протокол и расшифровка готовы" in text
    assert "Дата: 06.10.2026\nДлительность: 1 ч 27 мин\nПоручений в протоколе: 4\n" in text
    assert "Кратко:\n- Срок не меняется.\n- Стеллажи <заказаны> & оплачены.\n" in text
    assert f"Открыть запись: {ABOUT['url']}" in text and "«Письма о готовности»" in text
    # в HTML чужой текст — название записи, имя, строки протокола — не становится разметкой
    assert "Планёрка &lt;b&gt;по складу&lt;/b&gt;" in html and "<b>по складу</b>" not in html
    assert "Пётр &lt;Петров&gt;" in html and "<li" in html and "Стеллажи &lt;заказаны&gt; &amp; оплачены." in html
    assert f'href="{ABOUT["url"].replace("&", "&amp;")}"' in html and "<img" not in html
    # без «Кратко» и без длительности письмо не разваливается
    bare = mail.ready(cfg(), "Пётр", {**ABOUT, "brief": [], "duration": "", "tasks": 0})
    assert "Кратко" not in bare[1] and "Кратко" not in bare[2] and "Длительность" not in bare[1]
    assert "Поручений" not in bare[1] and "Поручений" not in bare[2]       # ноль поручений — строки нет


@pytest.mark.parametrize("status, retrying, subject, said, unsaid", [
    ("no_audio", False, "в записи нет звука — вкс 0610.mp4", ["не нашлось звука", "загрузите файл заново"], ["попробует"]),
    ("error", False, "запись не обработана — вкс 0610.mp4",
     ["не удалось", "Автоматических попыток больше не будет", "администратору", "Загружать файл заново не нужно"], []),
    ("error", True, "запись не обработана — вкс 0610.mp4", ["попробует ещё раз сам"], ["администратору"]),
    ("no_summary", False, "протокол не собран — вкс 0610.mp4",
     ["Речь в записи «вкс 0610.mp4» распознана", "протокол собрать не удалось", "Открыть расшифровку"], []),
])
def test_failure_letter_says_what_happened_and_what_to_do(status, retrying, subject, said, unsaid):
    got, text, html = mail.failed(cfg(), "Пётр", ABOUT, status, retrying)
    assert got == subject and ABOUT["url"] in text
    for words in said:
        assert words in text and words in html, words
    for words in unsaid:
        assert words not in text, words
    # человеческим языком: ни трассировки, ни текста ошибки в письме нет
    assert "Traceback" not in text and "Error" not in text and "<img" not in html


def test_protocol_letter_escapes_the_note_and_says_an_account_is_needed():
    note = "Коллеги, <b>срочно</b> & важно:\nсм. п. 5 <script>alert(1)</script>"
    subject, text, html = mail.protocol(cfg(), "Иванов <Иван>", ABOUT, note, "PDF")
    assert subject == "протокол — Планёрка <b>по складу</b>"
    assert "Отправитель: Иванов <Иван>." in text and "Дата: 06.10.2026" in text and "Кратко:\n- Срок" in text
    assert "Поручений" not in text                                       # счёт — для письма автору, не для рассылки
    assert f"Приписка отправителя:\n{note}\n" in text and "во вложении (PDF)" in text
    assert f"Запись в сервисе: {ABOUT['url']}" in text and "нужна учётная запись в сервисе" in text
    assert "<script>" not in html and "<b>срочно</b>" not in html
    assert "Коллеги, &lt;b&gt;срочно&lt;/b&gt; &amp; важно:<br>см. п. 5 &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Иванов &lt;Иван&gt;" in html and "нужна учётная запись" in html and "<img" not in html
    plain = mail.protocol(cfg(), "Иванов", ABOUT, "", "")
    assert "Приписка" not in plain[1] and "Приписка" not in plain[2] and "по ссылке ниже" in plain[1]
    assert "вложени" not in plain[1]


LONG_NAME = "Планёрка по складу и снабжению, итоги недели и план на следующую — отдел закупок, склад, логистика.pdf"


def test_attachment_name_in_cyrillic_is_one_rfc2231_value():
    msg = mail.message(cfg(), ["a@example.test"], *mail.probe(cfg()),
                       attachments=[(b"%PDF-1.4 test", "application/pdf", LONG_NAME)], reply_to="ivanov@example.test")
    assert msg["Reply-To"] == "ivanov@example.test"
    raw = msg.as_bytes().decode("ascii")                 # в письме только ASCII: кириллица закодирована
    line = next(ln for ln in raw.splitlines() if ln.lower().startswith("content-disposition: attachment"))
    # имя — одним значением filename*=utf-8''…: без продолжений filename*0*=, которые понимают не все программы
    assert "filename*=utf-8''%D0%9F%D0%BB%D0%B0%D0%BD" in line and line.endswith(".pdf") and "filename*0" not in raw
    assert len(line) < 998
    # старые Outlook берут имя из name= в Content-Type — словами RFC 2047, каждое не длиннее 75 знаков
    kind = next(ln for ln in raw.splitlines() if ln.lower().startswith("content-type: application/pdf"))
    words = re.fullmatch(r'Content-Type: application/pdf; name="((?:=\?utf-8\?b\?[A-Za-z0-9+/=]+\?= ?)+)"', kind).group(1)
    assert all(len(word) <= 75 for word in words.split()) and " name*" not in raw and len(kind) < 998
    assert str(email.header.make_header(email.header.decode_header(words))) == LONG_NAME
    # имя латиницей — как есть, в обоих заголовках
    plain = mail.message(cfg(), ["a@example.test"], *mail.probe(cfg()),
                         attachments=[(b"%PDF", "application/pdf", "weekly report.pdf")]).as_bytes().decode("ascii")
    assert 'Content-Type: application/pdf; name="weekly report.pdf"\r\n' in plain
    assert 'Content-Disposition: attachment; filename="weekly report.pdf"\r\n' in plain
    # и читается обратно как было
    back = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
    part, = back.iter_attachments()
    assert part.get_filename() == LONG_NAME and part.get_content_type() == "application/pdf"
    assert part["Content-Type"].params["name"] == LONG_NAME
    assert part.get_content() == b"%PDF-1.4 test"
    # тело письма осталось на месте: текст и HTML
    assert "пробное письмо" in parts(back)[0] and "<h1" in parts(back)[1]


def test_send_returns_the_addresses_the_server_refused(monkeypatch):
    class Picky(FakeSMTP):
        def send_message(self, msg):
            return {"b@example.test": (550, b"no such user")}
    monkeypatch.setattr(smtplib, "SMTP", Picky)
    assert mail.send(cfg(), ["a@example.test", "b@example.test"], mail.probe(cfg())) == {
        "b@example.test": (550, b"no such user")}


def test_send_each_writes_a_letter_per_address_over_one_connection(fake):
    to = ["a@example.test", "b@example.test", "c@example.test"]
    assert mail.send_each(cfg(), to, mail.probe(cfg()), reply_to="ivanov@example.test") == {}
    client, = fake                                       # соединение одно на все письма
    assert client.calls == ["send", "send", "send", "quit"]
    assert [m["To"] for m in client.sent] == to          # в каждом письме адресат один
    assert len({m["Message-ID"] for m in client.sent}) == 3
    assert {m["Reply-To"] for m in client.sent} == {"ivanov@example.test"}


def test_send_each_goes_on_after_a_failure_and_names_who_got_nothing(monkeypatch):
    made = []

    class Picky(FakeSMTP):
        """Одного адреса на сервере нет, на другом письме сервер ответил ошибкой."""
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

        def send_message(self, msg):
            if msg["To"] == "b@example.test":
                raise smtplib.SMTPRecipientsRefused({"b@example.test": (550, b"5.1.1 no such user")})
            if msg["To"] == "c@example.test":
                raise smtplib.SMTPDataError(451, b"try later")
            super().send_message(msg)
    monkeypatch.setattr(smtplib, "SMTP", Picky)
    to = ["a@example.test", "b@example.test", "c@example.test", "d@example.test"]
    # причина — ответ сервера строкой: его читает обычный человек, имён исключений и b'…' в нём нет
    assert mail.send_each(cfg(), to, mail.probe(cfg())) == {
        "b@example.test": "550 5.1.1 no such user", "c@example.test": "451 try later"}
    assert [m["To"] for m in made[-1].sent] == ["a@example.test", "d@example.test"]
    # письмо не ушло никому — это ошибка отправки: сервер отказал каждому адресу — с его ответами
    with pytest.raises(mail.MailError, match="b@example.test .550 5.1.1 no such user.") as failed:
        mail.send_each(cfg(), ["b@example.test"], mail.probe(cfg()))
    assert failed.value.refused == {"b@example.test": "550 5.1.1 no such user"}
    with pytest.raises(mail.MailError) as failed:
        mail.send_each(cfg(), ["b@example.test", "c@example.test"], mail.probe(cfg()))
    assert failed.value.refused == {"b@example.test": "550 5.1.1 no such user", "c@example.test": "451 try later"}
    assert "b'" not in str(failed.value) and "c@example.test (451 try later)" in str(failed.value)


def test_send_each_tells_a_lost_server_from_refused_addresses(monkeypatch):
    class Dying(FakeSMTP):
        """Первое письмо сервер принял, на втором оборвал соединение."""
        def send_message(self, msg):
            if self.sent:
                raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
            super().send_message(msg)
    monkeypatch.setattr(smtplib, "SMTP", Dying)
    assert mail.send_each(cfg(), ["a@example.test", "b@example.test", "c@example.test"], mail.probe(cfg())) == {
        "b@example.test": mail.NO_ANSWER, "c@example.test": mail.NO_ANSWER}

    class Dead(FakeSMTP):
        def send_message(self, msg):
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    monkeypatch.setattr(smtplib, "SMTP", Dead)
    with pytest.raises(mail.MailError, match="SMTPServerDisconnected: Connection unexpectedly closed") as failed:
        mail.send_each(cfg(), ["a@example.test", "b@example.test"], mail.probe(cfg()))
    assert failed.value.plain == mail.DOWN and failed.value.refused == {}


def test_protocol_with_attachments_travels_over_smtp_and_the_files_open():
    """Настоящие PDF и DOCX из выгрузки уходят письмом и открываются после почты."""
    import io

    import docx
    from pypdf import PdfReader

    from app import export
    record = {"file": "Проекты/вкс 0610.mp4", "title": "Планёрка: итоги/план по складу", "record_date": "2026-10-06",
              "audio_duration_sec": 600}
    md = "# Протокол\n\n## 1. Кратко\n\nСогласовали смету по складу.\n"
    files = [(*export.build(md, record, fmt), export.FORMATS[fmt]) for fmt in ("pdf", "docx")]
    with Mailbox() as box:
        threading.Thread(target=box.serve_forever, daemon=True).start()
        try:
            settings = cfg(smtp_host="127.0.0.1", smtp_port=box.server_address[1])
            refused = mail.send_each(settings, ["petrov@example.test", "nobody@example.test", "guest@partner.test"],
                                     mail.protocol(settings, "Иванов", ABOUT, "См. п. 5", "PDF"),
                                     [(data, kind, name) for data, name, kind in files], "ivanov@example.test")
        finally:
            box.shutdown()
        (sender, rcpt, letter), (_, second, copy) = box.letters
    # каждому — своё письмо, в «Кому» только он; адрес, который сервер не принял, остальным не мешает
    assert refused == {"nobody@example.test": "550 5.1.1 no such user"}
    assert (rcpt, second) == (["petrov@example.test"], ["guest@partner.test"])
    assert (letter["To"], copy["To"]) == ("petrov@example.test", "guest@partner.test")
    assert letter["Message-ID"] != copy["Message-ID"] and parts(copy) == parts(letter)
    assert len(list(copy.iter_attachments())) == 2 and letter["Reply-To"] == "ivanov@example.test"
    assert letter["Subject"] == "Подьячий: протокол — Планёрка <b>по складу</b>"
    assert "См. п. 5" in parts(letter)[0] and "См. п. 5" in parts(letter)[1]
    pdf, word = letter.iter_attachments()
    assert pdf.get_filename() == "Планёрка итоги план по складу.pdf" and pdf.get_content_type() == "application/pdf"
    assert word.get_filename() == "Планёрка итоги план по складу.docx"
    assert word.get_content_type() == export.FORMATS["docx"]
    assert "Согласовали смету" in PdfReader(io.BytesIO(pdf.get_content())).pages[0].extract_text()
    assert docx.Document(io.BytesIO(word.get_content())).paragraphs[0].text == "Планёрка: итоги/план по складу"


# ---------- длина строк и заголовки ----------

def wire(msg) -> bytes:
    """Письмо байтами — так, как его складывает smtplib.send_message."""
    with io.BytesIO() as buf:
        email.generator.BytesGenerator(buf).flatten(msg, linesep="\r\n")
        return buf.getvalue()


LONG_TITLE = "Планёрка отдела снабжения и логистики, расширенный состав, итоги квартала и план на следующий — 5 октября"


def test_lines_of_a_letter_are_of_usual_length_and_the_attachment_name_stays_whole():
    """Строки base64 длиннее 76 знаков (RFC 2045) и тема одним словом на сотни знаков (RFC 2047) —
    повод фильтру спама снизить письму оценку. Длинными остаются только два заголовка вложения."""
    letter = mail.protocol(cfg(), "Иванов", {**ABOUT, "title": LONG_TITLE, "brief": ["Срок не меняется. " * 20]},
                           "приписка " * 100, "PDF")
    msg = mail.message(cfg(), ["a@example.test"], *letter, reply_to="ivanov@example.test",
                       attachments=[(b"%PDF-1.4 " + bytes(range(256)) * 300, "application/pdf", LONG_NAME)])
    raw = wire(msg)
    lines = raw.decode("ascii").split("\r\n")
    long = [ln for ln in lines if len(ln) > 78]
    assert sorted(ln.split(":")[0] for ln in long) == ["Content-Disposition", "Content-Type"]
    assert all(len(ln) < 998 for ln in long) and "filename*0" not in raw.decode() and " name*" not in raw.decode()
    # тела частей — текст, HTML и вложение — строками base64 не длиннее 76
    packed = [ln for ln in lines if len(ln) > 60 and re.fullmatch(r"[A-Za-z0-9+/=]+", ln)]
    assert len(packed) > 1000 and max(map(len, packed)) == 76
    # тема — короткими словами, по одному на строку (так её складывает сам Python)
    subject = raw.split(b"Subject: ")[1].split(b"\r\nDate:")[0].decode().split()
    assert len(subject) > 2 and all(len(word) <= 76 for word in subject) and subject[0].startswith("=?utf-8?")
    back = email.message_from_bytes(raw, policy=email.policy.default)
    assert back["Subject"] == f"Подьячий: протокол — {LONG_TITLE}"
    part, = back.iter_attachments()
    assert part.get_filename() == LONG_NAME and part.get_content() == b"%PDF-1.4 " + bytes(range(256)) * 300
    assert "приписка приписка" in parts(back)[0] and "Срок не меняется." in parts(back)[1]


def test_message_id_carries_the_domain_of_the_sender_and_the_sender_may_have_a_name():
    msg = mail.message(cfg(), ["a@example.test"], *mail.probe(cfg()))
    assert re.fullmatch(r"<[^@<>\s]+@example\.test>", msg["Message-ID"])
    named = cfg(smtp_from="Подьячий <robot@corp.example>")
    msg = mail.message(named, ["a@example.test"], *mail.probe(named))
    assert msg["Message-ID"].endswith("@corp.example>")
    back = email.message_from_bytes(wire(msg), policy=email.policy.default)
    assert back["From"].addresses[0].display_name == "Подьячий" and back["From"].addresses[0].addr_spec == "robot@corp.example"
    # и письмо с таким отправителем уходит: в конверте — один адрес
    with Mailbox() as box:
        threading.Thread(target=box.serve_forever, daemon=True).start()
        try:
            there = cfg(smtp_host="127.0.0.1", smtp_port=box.server_address[1], smtp_from="Подьячий <robot@corp.example>")
            mail.send(there, ["a@example.test"], mail.probe(there))
        finally:
            box.shutdown()
        (sender, rcpt, letter), = box.letters
    assert sender == "robot@corp.example" and letter["From"].addresses[0].display_name == "Подьячий"


# ---------- сертификат внутреннего центра сертификации ----------

def issue(tmp_path):
    """Свой центр сертификации и сертификат почтового сервера на 127.0.0.1. Отдаёт путь к
    корневому сертификату (PEM) и ssl-контекст для сервера."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    now = datetime.datetime.now(datetime.timezone.utc)

    def cert(name, key, issuer, issuer_key, *extensions):
        builder = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2)))
        for extension, critical in extensions:
            builder = builder.add_extension(extension, critical)
        return builder.sign(issuer_key, hashes.SHA256())

    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Org Root CA")])
    ca = cert(ca_name, ca_key, ca_name, ca_key, (x509.BasicConstraints(ca=True, path_length=None), True),
              (x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                             key_encipherment=False, data_encipherment=False, key_agreement=False,
                             encipher_only=False, decipher_only=False), True),
              (x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False))
    leaf = cert(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mail")]), key, ca_name, ca_key,
                (x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False),
                (x509.BasicConstraints(ca=False, path_length=None), True),
                (x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False))
    pem = serialization.Encoding.PEM
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(pem))
    (tmp_path / "mail.pem").write_bytes(leaf.public_bytes(pem) + key.private_bytes(
        pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(tmp_path / "mail.pem")
    return str(tmp_path / "ca.pem"), server


def test_certificate_of_the_organisation_is_trusted_only_with_smtp_ca_file(tmp_path):
    ca, server = issue(tmp_path)
    with Mailbox(tls=server) as box:
        threading.Thread(target=box.serve_forever, daemon=True).start()
        try:
            there = cfg(smtp_host="127.0.0.1", smtp_port=box.server_address[1], smtp_security="starttls")
            # без своего корня сертификат почтового сервера сервису незнаком
            with pytest.raises(mail.MailError, match="CERTIFICATE_VERIFY_FAILED") as failed:
                mail.send(there, ["a@example.test"], mail.probe(there))
            assert failed.value.plain == mail.UNSENT and box.letters == []
            trusting = cfg(**{**vars(there), "smtp_ca_file": ca})
            mail.send(trusting, ["a@example.test"], mail.probe(trusting))
            # имя в SMTP_HOST должно совпадать с именем в сертификате: он выдан на 127.0.0.1, не на localhost
            other = cfg(**{**vars(trusting), "smtp_host": "localhost"})
            with pytest.raises(mail.MailError, match="CERTIFICATE_VERIFY_FAILED|Hostname mismatch|not valid for"):
                mail.send(other, ["a@example.test"], mail.probe(other))
        finally:
            box.shutdown()
        (sender, rcpt, letter), = box.letters
    assert rcpt == ["a@example.test"] and letter["Subject"] == "Подьячий: пробное письмо"


def test_missing_or_broken_ca_file_is_named(tmp_path):
    missing = cfg(smtp_security="starttls", smtp_ca_file=str(tmp_path / "нет.pem"))
    why = mail.why_off(missing)
    assert "SMTP_CA_FILE=" in why and "нет.pem" in why and "такого файла" in why
    (tmp_path / "ca.pem").write_text("это не сертификат", encoding="utf-8")
    broken = cfg(smtp_security="starttls", smtp_ca_file=str(tmp_path / "ca.pem"))
    assert mail.why_off(broken) is None
    with pytest.raises(mail.MailError, match="SMTP_CA_FILE=.*ca.pem: файл не читается как сертификат PEM"):
        mail.send(broken, ["a@example.test"], mail.probe(broken))
    # без защиты соединения сертификаты не нужны — и файл не читается
    mail.tls_context(cfg(smtp_ca_file=""))


def test_launcher_is_told_where_the_reason_is_and_not_sent_to_the_administrator():
    for status in ("error", "no_summary"):
        for retrying in (False, True):
            subject, text, html = mail.failed(cfg(), "Пётр", ABOUT, status, retrying, by_hand=True)
            assert "в разделе «Обработка»" in text and "в разделе «Обработка»" in html
            assert "Обратитесь к администратору" not in text and "Обратитесь к администратору" not in html
            assert ("попробует ещё раз сам" in text) is retrying and ("запустите запись ещё раз" in text) is not retrying
    # нет звука — совет тот же, что автору: дело в файле, а не в сбое
    assert mail.failed(cfg(), "Пётр", ABOUT, "no_audio", False, by_hand=True) == mail.failed(cfg(), "Пётр", ABOUT, "no_audio", False)
