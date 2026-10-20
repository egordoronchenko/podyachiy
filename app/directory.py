"""Вход под учётной записью домена: Active Directory (и Samba) по LDAP.

Человек вводит доменный логин и пароль на странице входа сервиса; сервис проверяет их
у контроллера домена. Единого входа Windows (Kerberos, Negotiate) здесь нет.

Как идёт проверка. Служебная учётная запись (только чтение) находит человека в базе поиска
по sAMAccountName; затем отдельным соединением делается привязка от его имени с введённым
паролем — так пароль проверяет сам контроллер, вместе с блокировками и сроками. После этого
служебной записью читаются группы: группа доступа (кого пускать) и группа администраторов
сервиса. Вложенные группы раскрывает контроллер — правило LDAP_MATCHING_RULE_IN_CHAIN,
его понимают и Active Directory, и Samba (проверено на Samba 4).

Что здесь обязательно:
- пустой пароль отклоняется до обращения к каталогу: привязка с пустым паролем по
  протоколу — анонимная, и контроллер ответил бы на неё «успех»;
- всё, что пришло от человека, попадает в фильтр поиска только через esc() (RFC 4515).
  DN из введённого не собирается вовсе: DN человека берётся из ответа каталога, DN групп —
  из настроек или из ответа каталога;
- до контроллера — только LDAPS или StartTLS, сертификат проверяется (см. problem());
- пароли — человека и служебный — не попадают ни в текст ошибок, ни в отчёт проверки.

Сетевые вызовы здесь блокирующие: вызывающий уводит их в поток (asyncio.to_thread).
Соединение с каталогом спрятано за connect(): тесты подменяют его поддельным каталогом.
"""
from __future__ import annotations

import re
import secrets
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import ldap3
from cryptography import x509
from ldap3.core.exceptions import LDAPException

IN_CHAIN = "1.2.840.113556.1.4.1941"      # LDAP_MATCHING_RULE_IN_CHAIN: членство с учётом вложенных групп
BIT_AND = "1.2.840.113556.1.4.803"        # LDAP_MATCHING_RULE_BIT_AND
UAC_DISABLED = 2                          # бит userAccountControl: учётная запись отключена
CONNECT_SEC = 5                           # сколько ждать соединения с контроллером, прежде чем идти к следующему
REPLY_SEC = 10                            # сколько ждать ответа на запрос
PAGE = 500                                # записей на страницу при подсчёте членов группы
COUNT_MAX = 5000                          # дальше не считаем: «больше 5000»
LOGIN_MAX = 256                           # длиннее логин не бывает; остальное — мусор, в каталог не идёт
CA_FILE = "ldap-ca.pem"                   # корневой сертификат организации в папке состояния
DEFAULT_USER_FILTER = "(&(objectCategory=person)(objectClass=user))"
USER_ATTRS = ("sAMAccountName", "displayName", "mail", "userAccountControl", "accountExpires")
NEVER = 2 ** 63 - 1                       # accountExpires: срока нет
FILETIME_EPOCH_SEC = 11_644_473_600       # секунд от 1601 года (отсчёт времени Windows) до 1970-го

INVALID = "неверный логин или пароль"
# Причины отказа и что сказать человеку. Подробности об учётной записи контроллер отдаёт
# только тому, кто назвал верный пароль (кроме блокировки) — так же поступает сам домен.
REASONS = {
    "invalid": INVALID,
    "disabled": "учётная запись отключена в домене — обратитесь к администратору домена",
    "locked": "учётная запись заблокирована в домене после неудачных попыток входа — подождите "
              "или обратитесь к администратору домена",
    "password_expired": "срок действия пароля в домене истёк — смените пароль на рабочем компьютере "
                        "(Ctrl+Alt+Del → «Изменить пароль») и войдите снова",
    "must_change": "домен требует сменить пароль при следующем входе — смените его на рабочем компьютере "
                   "(Ctrl+Alt+Del → «Изменить пароль») и войдите снова",
    "account_expired": "срок действия учётной записи в домене истёк — обратитесь к администратору домена",
    "hours": "вход в это время запрещён настройками учётной записи в домене",
    "workstation": "вход запрещён настройками учётной записи в домене (список разрешённых компьютеров) — "
                   "обратитесь к администратору домена",
    "no_access": "доступа к сервису нет: учётная запись домена не входит в группу доступа — "
                 "обратитесь к администратору сервиса",
    "gone": "такой учётной записи в домене нет",
}
# Подкоды Active Directory в сообщении об отказе привязки: «… AcceptSecurityContext error, data 52e, v2580».
SUBCODES = {"525": "invalid", "52e": "invalid", "530": "hours", "531": "workstation",
            "532": "password_expired", "533": "disabled", "701": "account_expired",
            "773": "must_change", "775": "locked"}
_SUBCODE = re.compile(r"\bdata\s+([0-9a-f]{3,4})\b", re.IGNORECASE)


class Unreachable(Exception):
    """Соединение с контроллером не состоялось или оборвалось: сеть, TLS, таймаут."""


class Refused(Exception):
    """Контроллер ответил отказом на привязку или поиск."""

    def __init__(self, description: str, message: str = ""):
        super().__init__(f"{description}: {message}" if message else description)
        self.description, self.message = description, message


class Denied(Exception):
    """Этому человеку вход закрыт. reason — ключ из REASONS, text — что ему сказать."""

    def __init__(self, reason: str):
        super().__init__(REASONS[reason])
        self.reason, self.text = reason, REASONS[reason]


class Unavailable(Exception):
    """Проверить человека сейчас нельзя: контроллеры не отвечают или настройка не годится.
    Текст — администратору (лог, отчёт проверки), не человеку на странице входа."""


@dataclass(frozen=True)
class Person:
    login: str               # sAMAccountName в нижнем регистре
    name: str                # displayName
    email: str               # mail
    admin: bool | None       # член группы администраторов сервиса; None — группа не задана
    dn: str


def esc(value: str) -> str:
    """Значение для фильтра поиска (RFC 4515): звёздочка, скобки, обратная черта и нулевой
    байт заменяются кодами — введённое человеком не может изменить сам фильтр."""
    return "".join(f"\\{ord(c):02x}" if c in "\\*()\0" else c for c in value)


def short_login(typed: str) -> str:
    """Логин без домена, в нижнем регистре: user, DOMAIN\\user и user@domain.tld — одно и то же."""
    typed = typed.strip()
    if "\\" in typed:
        typed = typed.split("\\", 1)[1]
    elif "@" in typed:
        typed = typed.rsplit("@", 1)[0]
    return typed.strip().lower()


def subcode(message: str) -> str:
    """Причина отказа привязки по подкоду Active Directory; незнакомый или без подкода — invalid."""
    found = _SUBCODE.search(message or "")
    return SUBCODES.get(found.group(1).lower(), "invalid") if found else "invalid"


def plain(error: BaseException) -> str:
    """Текст ошибки соединения без обёрток ldap3: он складывает причины во вложенные кортежи,
    а в отказ по имени сертификата кладёт весь сертификат."""
    text = str(error) or type(error).__name__
    if "doesn't match any name" in text:
        names = ", ".join(sorted(set(re.findall(r"'(?:DNS|commonName)', '([^']+)'", text))))
        return f"сертификат контроллера выдан на другое имя ({names or 'имён в нём нет'}) — hostname doesn't match"
    if "invalid server address" in text:
        return "имя контроллера не найдено в DNS (invalid server address)"
    text = text.strip(" ()'\",\\")
    return text + ")" * (text.count("(") - text.count(")"))


def ca_path(cfg) -> str:
    """Файл с корневым сертификатом организации: путь из LDAP_CA_FILE, иначе state/ldap-ca.pem,
    если он есть. Пусто — сертификат контроллера сверяется с центрами, которым доверяет система."""
    if cfg.ldap_ca_file:
        return cfg.ldap_ca_file
    own = Path(cfg.state_dir) / CA_FILE
    return str(own) if own.is_file() else ""


def advice(text: str) -> str:
    """Что делать с отказом — по тексту ошибки соединения или ответа контроллера."""
    low = text.lower()
    if "certificate verify failed" in low or "certificate_verify_failed" in low:
        return ("сертификат контроллера не подписан центром, которому сервис доверяет. Положите корневой "
                f"сертификат центра сертификации организации (PEM) в папку состояния под именем {CA_FILE} "
                "или укажите путь к нему в LDAP_CA_FILE")
    if "doesn't match" in low or "hostname" in low:
        return ("имя в LDAP_URL не совпадает с именем в сертификате контроллера: укажите полное имя "
                "контроллера (dc1.corp.local), а не адрес или короткое имя")
    if "timed out" in low or "timeout" in low:
        return (f"контроллер не ответил за {CONNECT_SEC} с: проверьте адрес и порт в LDAP_URL (LDAPS — 636) "
                "и что брандмауэр пускает сервер сервиса к контроллеру")
    if "refused" in low:
        return ("порт закрыт: проверьте порт в LDAP_URL; LDAPS (636) работает, только когда у контроллера "
                "есть сертификат")
    if "name or service not known" in low or "name resolution" in low or "getaddrinfo" in low \
            or "invalid server address" in low:
        return ("имя контроллера не находится: сервис ищет его через DNS контейнера — проверьте имя "
                "или пропишите его в extra_hosts в compose.yml")
    if "strongerauthrequired" in low or "integrity checking" in low or "encryption required" in low:
        return ("контроллер требует шифрования (политика «Требования к подписыванию LDAP-сервера»): "
                "используйте ldaps:// или LDAP_STARTTLS=1")
    if "wrong version number" in low or "unknown protocol" in low or "reset by peer" in low:
        return "на этом порту нет TLS: для ldaps:// нужен порт 636, а для порта 389 — ldap:// и LDAP_STARTTLS=1"
    if "invalidcredentials" in low:
        return "проверьте LDAP_BIND_DN и LDAP_BIND_PASSWORD: служебная учётная запись не принята"
    if "nosuchobject" in low:
        return "такого объекта в каталоге нет: проверьте DN"
    return ""


class Link:
    """Одно соединение с контроллером через ldap3. Тесты подставляют вместо него поддельное
    с теми же четырьмя действиями: bind, search, peer, close."""

    def __init__(self, url: str, cfg):
        secure = url.lower().startswith("ldaps://")
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED if cfg.ldap_tls_verify else ssl.CERT_NONE,
                        ca_certs_file=ca_path(cfg) or None)
        server = ldap3.Server(url, use_ssl=secure, tls=tls, connect_timeout=CONNECT_SEC, get_info=ldap3.NONE)
        # ссылки на другие разделы каталога (DomainDnsZones и подобные) не разыменовываем:
        # за ними сервис пошёл бы к серверам, которых нет в настройках
        self.conn = ldap3.Connection(server, auto_bind=ldap3.AUTO_BIND_NONE, receive_timeout=REPLY_SEC,
                                     auto_referrals=False, raise_exceptions=False)
        try:
            self.conn.open()
            if not secure and cfg.ldap_starttls and not self.conn.start_tls():
                raise Unreachable("контроллер отказал в StartTLS: " + self._said())
        except (LDAPException, OSError) as e:
            self.close()
            raise Unreachable(plain(e))
        except Unreachable:
            self.close()
            raise

    def _said(self) -> str:
        result = self.conn.result or {}
        return f"{result.get('description', '')}: {result.get('message', '')}".strip(": ")

    def bind(self, dn: str, password: str) -> None:
        self.conn.user, self.conn.password, self.conn.authentication = dn, password, ldap3.SIMPLE
        try:
            accepted = self.conn.bind()
        except (LDAPException, OSError) as e:
            raise Unreachable(plain(e))
        if not accepted:
            raise Refused(self.conn.result.get("description", ""), str(self.conn.result.get("message", "")))

    def search(self, base: str, flt: str, attrs: tuple[str, ...] = (), scope: str = "sub",
               limit: int = 0) -> list[tuple[str, dict[str, list[str]]]]:
        """Записи (DN, атрибуты) под base. scope — sub или base; limit — больше не читать."""
        out: list[tuple[str, dict[str, list[str]]]] = []
        try:
            if scope == "base":
                self.conn.search(base, flt, search_scope=ldap3.BASE, attributes=list(attrs) or ldap3.NO_ATTRIBUTES)
                entries = self.conn.response or []
            else:
                entries = self.conn.extend.standard.paged_search(
                    base, flt, search_scope=ldap3.SUBTREE, attributes=list(attrs) or ldap3.NO_ATTRIBUTES,
                    paged_size=PAGE, generator=True)
            for entry in entries:
                if entry.get("type") != "searchResEntry":
                    continue
                values = {name: [str(v) for v in (value if isinstance(value, (list, tuple)) else [value])]
                          for name, value in entry["attributes"].items()}
                out.append((entry["dn"], values))
                if limit and len(out) >= limit:
                    return out
        except (LDAPException, OSError) as e:
            raise Unreachable(plain(e))
        result = self.conn.result or {}
        if result.get("result") not in (0, None):
            raise Refused(result.get("description", ""), str(result.get("message", "")))
        return out

    def peer(self) -> dict:
        """Чем защищено соединение и чей сертификат предъявил контроллер."""
        sock = self.conn.socket
        if not isinstance(sock, ssl.SSLSocket):
            return {"tls": ""}
        cert = x509.load_der_x509_certificate(sock.getpeercert(binary_form=True))
        return {"tls": sock.version() or "TLS", "subject": cert.subject.rfc4514_string(),
                "issuer": cert.issuer.rfc4514_string(), "until": cert.not_valid_after_utc.strftime("%d.%m.%Y")}

    def close(self) -> None:
        try:
            self.conn.unbind()
        except Exception:        # соединение уже оборвано — закрывать нечего
            pass


connect: Callable[[str, object], Link] = Link


class Directory:
    def __init__(self, cfg, log: Callable[[str], None] = lambda s: None):
        self.cfg, self.log = cfg, log
        self.user_filter = cfg.ldap_user_filter or DEFAULT_USER_FILTER
        self._first = 0                     # с какого контроллера начинать: последний ответивший
        self._groups: dict[str, str] = {}   # группа из настроек → её DN
        self._lock = threading.Lock()

    # ---------- настройки ----------

    def problem(self) -> str:
        """Почему с этими настройками вход через домен работать не может; пусто — может."""
        cfg = self.cfg
        if not cfg.ldap_urls:
            return "не задан адрес контроллера домена (LDAP_URL в .env)"
        for url in cfg.ldap_urls:
            low = url.lower()
            if not low.startswith(("ldaps://", "ldap://")):
                return f"адрес контроллера «{url}» должен начинаться с ldaps:// (или ldap:// вместе с LDAP_STARTTLS=1)"
            if low.startswith("ldap://") and not cfg.ldap_starttls and not cfg.ldap_allow_http:
                return (f"адрес «{url}» — без шифрования: пароли шли бы до контроллера открыто. "
                        "Используйте ldaps://…:636 или добавьте LDAP_STARTTLS=1")
        if not cfg.ldap_base_dn:
            return "не задана база поиска (LDAP_BASE_DN в .env), например DC=corp,DC=local"
        if not cfg.ldap_bind_dn or not cfg.ldap_bind_password:
            return "не задана служебная учётная запись (LDAP_BIND_DN и LDAP_BIND_PASSWORD в .env)"
        return ""

    def warnings(self) -> list[str]:
        """Послабления, о которых администратор должен знать: пишутся в лог при запуске и
        показываются на странице."""
        cfg, out = self.cfg, []
        if cfg.ldap_allow_http:
            out.append("LDAP_ALLOW_HTTP=1: вход через домен принимается и по HTTP — доменные пароли идут по сети "
                       "открытым текстом. Только для стенда; в работе уберите эту строку и включите HTTPS")
        if any(u.lower().startswith("ldap://") for u in cfg.ldap_urls) and not cfg.ldap_starttls:
            out.append("до контроллера домена — незашифрованное соединение (ldap:// без LDAP_STARTTLS): пароли "
                       "людей и служебной учётной записи идут открыто. Только для стенда")
        if not cfg.ldap_tls_verify:
            out.append("LDAP_TLS_VERIFY=0: сертификат контроллера домена не проверяется — подменённый "
                       "контроллер получит пароли. Положите корневой сертификат организации и уберите эту строку")
        return out

    # ---------- соединение ----------

    def _service(self) -> tuple[Link, str]:
        """Соединение с первым ответившим контроллером, привязанное служебной учётной записью."""
        cfg, faults = self.cfg, []
        urls = list(cfg.ldap_urls)
        order = urls[self._first:] + urls[:self._first]
        for url in order:
            try:
                link = connect(url, cfg)
            except Unreachable as e:
                faults.append(f"{url} — {e}")
                continue
            try:
                link.bind(cfg.ldap_bind_dn, cfg.ldap_bind_password)
            except Unreachable as e:
                link.close()
                faults.append(f"{url} — {e}")
                continue
            except Refused as e:
                link.close()
                raise Unavailable(f"контроллер {url} не принял служебную учётную запись "
                                  f"(LDAP_BIND_DN, LDAP_BIND_PASSWORD): {e}")
            with self._lock:
                moved, self._first = urls.index(url) != self._first, urls.index(url)
            if moved:
                self.log(f"вход через домен: работаю с контроллером {url}; не ответили: {'; '.join(faults)}")
            return link, url
        raise Unavailable("ни один контроллер домена не ответил: " + "; ".join(faults))

    def _try(self, url: str, dn: str, password: str) -> None:
        """Привязка от имени человека отдельным соединением: пароль проверяет контроллер."""
        try:
            link = connect(url, self.cfg)
        except Unreachable as e:
            raise Unavailable(f"контроллер {url} перестал отвечать: {e}")
        try:
            link.bind(dn, password)
        except Unreachable as e:
            raise Unavailable(f"контроллер {url} перестал отвечать: {e}")
        except Refused as e:
            raise Denied(subcode(e.message))
        finally:
            link.close()

    # ---------- поиск ----------

    def _find(self, link: Link, typed: str) -> list[tuple[str, dict[str, list[str]]]]:
        """Человек по введённому логину. user и DOMAIN\\user ищутся по sAMAccountName;
        user@domain.tld — ещё и по userPrincipalName целиком: у человека он может не совпадать
        с коротким логином."""
        typed = typed.strip()
        who = f"(sAMAccountName={esc(short_login(typed))})"
        if "@" in typed and "\\" not in typed:
            who = f"(|{who}(userPrincipalName={esc(typed)}))"
        return link.search(self.cfg.ldap_base_dn, f"(&{self.user_filter}{who})", USER_ATTRS, limit=2)

    def _group(self, link: Link, named: str, what: str) -> str:
        """DN группы из настроек: там может стоять DN целиком или имя группы."""
        if "=" in named:
            return named
        with self._lock:
            if named in self._groups:
                return self._groups[named]
        found = link.search(self.cfg.ldap_base_dn,
                            f"(&(objectClass=group)(|(sAMAccountName={esc(named)})(cn={esc(named)})))", limit=2)
        if len(found) != 1:
            raise Unavailable(f"{what} «{named}» " + ("не найдена в базе поиска" if not found
                              else "найдена не одна — укажите её DN целиком"))
        with self._lock:
            self._groups[named] = found[0][0]
        return found[0][0]

    def _member(self, link: Link, dn: str, group_dn: str) -> bool:
        """Входит ли человек в группу — напрямую или через вложенные группы."""
        return bool(link.search(dn, f"(memberOf:{IN_CHAIN}:={esc(group_dn)})", scope="base"))

    def _admit(self, link: Link, dn: str, attrs: dict[str, list[str]]) -> Person:
        """Действует ли учётная запись и пускают ли её в сервис; заодно — ФИО, почта и роль."""
        cfg = self.cfg

        def one(name: str) -> str:
            return (attrs.get(name) or [""])[0]

        try:
            disabled = int(one("userAccountControl") or 0) & UAC_DISABLED
        except ValueError:
            disabled = 0
        if disabled:
            raise Denied("disabled")
        try:
            expires = int(one("accountExpires") or 0)
        except ValueError:
            expires = 0
        # срок учётной записи — в сотнях наносекунд от 1601 года; 0 и 2^63-1 значат «без срока»
        if 0 < expires < NEVER and expires <= (time.time() + FILETIME_EPOCH_SEC) * 10_000_000:
            raise Denied("account_expired")
        if cfg.ldap_access_group and not self._member(link, dn, self._group(link, cfg.ldap_access_group,
                                                                              "группа доступа")):
            raise Denied("no_access")
        admin = (self._member(link, dn, self._group(link, cfg.ldap_admin_group, "группа администраторов"))
                 if cfg.ldap_admin_group else None)
        login = one("sAMAccountName").lower()
        return Person(login, one("displayName") or login, one("mail"), admin, dn)

    # ---------- вход и перепроверка ----------

    def authenticate(self, typed: str, password: str) -> Person:
        """Проверяет логин и пароль в домене. Denied — человеку отказано; Unavailable — проверить нельзя."""
        if not password:
            # привязка с пустым паролем — анонимная, контроллер её принимает: до каталога не доходит
            raise Denied("invalid")
        if not short_login(typed) or len(typed) > LOGIN_MAX:
            raise Denied("invalid")
        link, url = self._service()
        try:
            found = self._find(link, typed)
            if len(found) != 1:
                # такого человека нет (или логин неоднозначен). Привязка всё равно делается —
                # к несуществующей записи: ответ «нет такого» не должен приходить быстрее ответа
                # «пароль неверный»
                try:
                    self._try(url, f"CN={secrets.token_hex(8)},{self.cfg.ldap_base_dn}", secrets.token_hex(8))
                except Denied:
                    pass
                raise Denied("invalid")
            dn, attrs = found[0]
            self._try(url, dn, password)
            return self._admit(link, dn, attrs)
        except Unreachable as e:
            raise Unavailable(f"контроллер {url} перестал отвечать: {e}")
        except Refused as e:
            raise Unavailable(f"контроллер {url} отказал в поиске: {e}")
        finally:
            link.close()

    def check(self, login: str) -> Person:
        """Перепроверка уже вошедшего, без его пароля: есть ли он в домене, не отключён ли,
        остался ли в группе доступа и в группе администраторов."""
        link, url = self._service()
        try:
            found = link.search(self.cfg.ldap_base_dn, f"(&{self.user_filter}(sAMAccountName={esc(login)}))",
                                USER_ATTRS, limit=2)
            if len(found) != 1:
                raise Denied("gone")
            return self._admit(link, *found[0])
        except Unreachable as e:
            raise Unavailable(f"контроллер {url} перестал отвечать: {e}")
        except Refused as e:
            raise Unavailable(f"контроллер {url} отказал в поиске: {e}")
        finally:
            link.close()

    # ---------- проверка подключения для администратора ----------

    def probe(self, login: str = "", password: str = "") -> list[dict]:
        """Пошаговый отчёт: что получилось, что нет и что с этим делать. Логин и пароль
        человека — необязательная последняя проверка; они нигде не сохраняются, пароля в
        отчёте нет. Шаг: title, ok (True / False / None — предупреждение), text, hint."""
        cfg, steps = self.cfg, []

        def step(title: str, ok: bool | None, text: str, hint: str = "") -> bool:
            steps.append({"title": title, "ok": ok, "text": text, "hint": hint})
            return ok is not False

        wrong = self.problem()
        if not step("Настройки", not wrong, wrong or "адрес, база поиска и служебная учётная запись заданы"):
            return steps
        for text in self.warnings():
            step("Послабление", None, text)

        ready: tuple[Link, str] | None = None
        for url in cfg.ldap_urls:
            try:
                link = connect(url, cfg)
            except Unreachable as e:
                step(f"Соединение с {url}", False, str(e), advice(str(e)))
                continue
            peer = link.peer()
            if peer["tls"]:
                step(f"Соединение с {url}", True if cfg.ldap_tls_verify else None,
                     f"{peer['tls']}; сертификат: {peer['subject']}; выдан: {peer['issuer']}; действует до {peer['until']}"
                     + ("" if cfg.ldap_tls_verify else " — сертификат НЕ проверялся (LDAP_TLS_VERIFY=0)"))
            else:
                step(f"Соединение с {url}", None, "установлено без шифрования")
            try:
                link.bind(cfg.ldap_bind_dn, cfg.ldap_bind_password)
            except (Refused, Unreachable) as e:
                link.close()
                why = subcode(e.message) if isinstance(e, Refused) else ""
                step(f"Служебная учётная запись на {url}", False,
                     f"{cfg.ldap_bind_dn} не принята: {e}",
                     (REASONS[why] + ". " if why and why != "invalid" else "") + (
                         advice(str(e)) or "проверьте LDAP_BIND_DN (DN целиком или логин@домен) и LDAP_BIND_PASSWORD"))
                continue
            step(f"Служебная учётная запись на {url}", True, f"{cfg.ldap_bind_dn} принята")
            if ready is None:
                ready = (link, url)
            else:
                link.close()
        if ready is None:
            return steps
        link, url = ready
        try:
            if self._probe_base(link, step) and self._probe_groups(link, step) and login.strip():
                self._probe_person(link, url, login, password, step)
        except Unreachable as e:
            step("Запрос к каталогу", False, f"контроллер {url} перестал отвечать: {e}", advice(str(e)))
        finally:
            link.close()
        return steps

    def _probe_base(self, link: Link, step) -> bool:
        cfg = self.cfg
        try:
            link.search(cfg.ldap_base_dn, "(objectClass=*)", scope="base")
            people = link.search(cfg.ldap_base_dn, self.user_filter, limit=COUNT_MAX)
        except Refused as e:
            return step("База поиска", False, f"{cfg.ldap_base_dn}: {e}",
                        "проверьте LDAP_BASE_DN (например DC=corp,DC=local) и что служебной учётной записи "
                        "разрешено читать каталог; фильтр — LDAP_USER_FILTER")
        return step("База поиска", bool(people),
                    f"{cfg.ldap_base_dn}: учётных записей людей — {_count(len(people))}",
                    "" if people else "под этой базой фильтр не нашёл ни одного человека: проверьте LDAP_BASE_DN "
                                      "и LDAP_USER_FILTER")

    def _probe_groups(self, link: Link, step) -> bool:
        cfg, fine = self.cfg, True
        for named, title, what in ((cfg.ldap_access_group, "Группа доступа", "группа доступа"),
                                   (cfg.ldap_admin_group, "Группа администраторов сервиса", "группа администраторов")):
            if not named:
                step(title, None, "не задана — " + (
                    "войти может любой действующий пользователь домена из базы поиска"
                    if what == "группа доступа" else
                    "роль администратора сервиса доменным пользователям назначается вручную, в списке пользователей"))
                continue
            try:
                dn = self._group(link, named, what)
                if not link.search(dn, "(objectClass=group)", scope="base"):
                    raise Refused("noSuchObject", f"{dn} — не группа")
                members = link.search(cfg.ldap_base_dn, f"(&{self.user_filter}(memberOf:{IN_CHAIN}:={esc(dn)}))",
                                      limit=COUNT_MAX)
            except (Unavailable, Refused) as e:
                fine = step(title, False, str(e), "проверьте имя или DN группы и что она лежит под базой поиска") and fine
                continue
            step(title, True if members else None,
                 f"{dn}: людей с учётом вложенных групп — {_count(len(members))}",
                 "" if members else "в группе никого нет. Основная группа человека (обычно «Пользователи домена») "
                                    "членством не считается — заведите отдельную группу")
        return fine

    def _probe_person(self, link: Link, url: str, login: str, password: str, step) -> bool:
        title = f"Вход пользователя {login.strip()[:64]}"
        if not password:
            return step(title, False, "пароль не введён", "пустой пароль сервис не отправляет в каталог")
        try:
            found = self._find(link, login)
        except Refused as e:
            return step(title, False, f"поиск не выполнен: {e}")
        if len(found) != 1:
            return step(title, False, "человек не найден в базе поиска" if not found else "под этот логин подходит не одна запись",
                        "проверьте логин, LDAP_BASE_DN и LDAP_USER_FILTER")
        dn, attrs = found[0]
        try:
            self._try(url, dn, password)
            person = self._admit(link, dn, attrs)
        except (Denied, Unavailable) as e:
            return step(title, False, f"{dn}: {e}")
        return step(title, True, f"{dn}: пароль принят; логин в сервисе — {person.login}, ФИО — {person.name}, "
                                 f"почта — {person.email or 'не указана'}; " + (
                                     "администратор сервиса" if person.admin else "обычный пользователь"))


def _count(n: int) -> str:
    return f"больше {COUNT_MAX}" if n >= COUNT_MAX else str(n)
