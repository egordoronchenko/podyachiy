"""Пользователи и доступ по папкам.

Вход обязателен всегда. У каждого пользователя
есть личная папка «Пользовательские/<логин>»: в ней он хозяин и сам заводит подпапки.
Общих папок сколько угодно: их создаёт администратор. У каждой папки для каждого
человека три права по возрастанию — чтение, загрузка и администрирование, и у каждого
права своя область: «только эта папка» или «папка с подпапками». Читающий видит записи,
протоколы, поиск и поручения, но новых записей не кладёт: обработка каждой записи стоит
денег у поставщика, поэтому загрузка — отдельное право. Оно включает чтение. Администратор
папки ещё и загружает, раздаёт к ней чтение и загрузку, подписывает говорящих и
пересобирает протокол, а с областью «с подпапками» — заводит подпапки.

В личную папку человек загружает, только если ему это разрешено отдельным признаком
учётной записи (home_upload): его ставит администратор сервиса. Без него личная папка
есть, но записей в неё человек не кладёт.

Администраторов сервиса может быть несколько. Первый — admin с паролем из .env:
он есть всегда, и его нельзя ни удалить, ни разжаловать. Остальных он назначает
галочкой у пользователя; они видят всё и управляют пользователями так же, как он.

Учётные записи и права лежат в базе сервиса (db.py: таблицы users и grants), ключ подписи
входов — файлом рядом с ней. И то и другое — в отдельной папке состояния, а не рядом с
результатами: выходную папку раздают по сети, а с ключом подписи можно подделать вход под
кого угодно. Проверка «такой уже есть» и запись идут одной транзакцией базы: двое, сохраняющие
одного человека разом, половину друг у друга не затрут.

Учётная запись может быть и без пароля: так её заводит одобренная заявка на доступ.
Войти с ней нельзя, пока человек сам не задаст пароль по одноразовой ссылке из письма.
Та же ссылка служит для сброса забытого пароля. Ссылка — случайная строка; в базе
лежит только её хеш и срок, у пользователя она одна: новая гасит прежнюю, использованная
стирается.

Ключ доступа — для программ и ИИ-клиентов, которые ходят в сервис от имени человека
(заголовок Authorization: Bearer). Человек выпускает его себе сам, права ключа — его права.
Ключи лежат в своей таблице (см. keys.py) и исчезают вместе с учётной записью.
Выпускает их и admin из .env, хотя учётной записи в базе у него нет. Ключ «с управлением»
выпускает себе только администратор сервиса.

Учётная запись бывает двух источников. Локальная — пароль хранится здесь (хешем). Доменная
(source = domain) — человек входит логином и паролем домена, их проверяет контроллер домена
(directory.py); пароля в сервисе нет вовсе, задать его или сменить здесь нельзя. Доменная
запись заводится сама при первом входе тем же кодом, что и локальная, и помнит, когда домен
последний раз подтвердил человека (checked). Локальная запись в доменную сама не превращается:
это делает администратор сервиса («привязать к домену»).

Пароли хранятся только хешами (scrypt). Сессия — подписанная метка в куке:
серверу нечего хранить, перезапуск никого не разлогинивает, а смена пароля
или удаление пользователя сразу делают его старые метки недействительными.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass

from . import db
from . import keys as vault

ADMIN = "admin"
HOME_ROOT = "Пользовательские"
LOGIN_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,31}")
PASSWORD_MIN = 8
NAME_MAX = 120
EMAIL_MAX = 254
# Проверка формы, а не существования ящика: одна «собака», по обе стороны без пробелов
# и знаков, которыми в заголовок письма дописывают второго адресата; справа есть точка.
EMAIL_RE = re.compile(r"""[^@\s<>"',;:()\\]+@[^@\s<>"',;:()\\]+\.[^@\s<>"',;:()\\.]+""")
LINK_SEC = 48 * 3600          # сколько живёт ссылка «задать пароль»
SESSION_SEC = 30 * 86400
COOKIE = "tw_session"
LOCAL, DOMAIN = "local", "domain"       # источник учётной записи
IN_DOMAIN = "это учётная запись домена: пароль проверяется и меняется в домене, в сервисе его нет"


class UserError(Exception):
    pass


class Conflict(UserError):
    """Логин доменного пользователя занят локальной учётной записью сервиса."""


def norm_login(login: str) -> str:
    """Логин в том виде, в каком он хранится; негодный — отказ. Одно правило и для
    администратора, заводящего пользователя, и для заявки на доступ."""
    login = login.strip().lower()
    if login == ADMIN or not LOGIN_RE.fullmatch(login):
        raise UserError("логин — латинские буквы, цифры, точка, дефис, подчёркивание; от 2 до 32 знаков, первый — буква или цифра; "
                        "admin занят администратором")
    return login


def norm_email(email: str) -> str:
    """Почта в том виде, в каком она хранится. Пустая — «не указана»."""
    email = email.strip().lower()
    if email and (len(email) > EMAIL_MAX or not EMAIL_RE.fullmatch(email)):
        raise UserError("почта — в виде имя@организация.ru")
    return email


def check_new_password(password: str) -> None:
    if len(password) < PASSWORD_MIN:
        raise UserError(f"пароль короче {PASSWORD_MIN} знаков")


def _link_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def norm_folder(folder: str) -> str:
    return "/".join(p for p in folder.replace("\\", "/").split("/") if p.strip())


NONE, ONLY, TREE = "none", "only", "tree"       # области права: нет, только папка, папка с подпапками
SCOPES = (NONE, ONLY, TREE)
KINDS = ("read", "upload", "manage")            # права на папку по возрастанию: каждое включает прежние
Grants = tuple[tuple[str, str], ...]            # (папка, область)


def _covers(grants: Grants, folder: str, tree_only: bool = False) -> bool:
    for f, scope in grants:
        if scope == TREE and (f == "" or folder == f or folder.startswith(f + "/")):
            return True
        if scope == ONLY and not tree_only and folder == f:
            return True
    return False


@dataclass(frozen=True)
class Access:
    """Кто смотрит и что ему можно. У администратора сервиса admin=True, и ему можно всё.
    home — личная папка; read, upload и manage — выданные права с их областями;
    home_upload — можно ли загружать в личную папку."""
    login: str
    name: str
    admin: bool = False
    home: str | None = None
    read: Grants = ()
    manage: Grants = ()
    key: str = ""       # имя ключа доступа, если запрос пришёл с ним, а не от самого человека
    key_kind: str = ""  # и его вид: plain — обычный, manage — с управлением сервисом
    domain: bool = False    # учётная запись домена: её действительность перепроверяется в домене
    upload: Grants = ()     # где человеку дана загрузка (она включает чтение)
    home_upload: bool = False   # загрузка в личную папку

    def _mine(self, folder: str) -> bool:
        return self.home is not None and (folder == self.home or folder.startswith(self.home + "/"))

    def sees(self, folder: str) -> bool:
        """Видны ли записи папки: своя личная, открытая на чтение или на загрузку
        или своя как администратора."""
        return (self.admin or self._mine(folder) or _covers(self.read, folder)
                or _covers(self.upload, folder) or _covers(self.manage, folder))

    def uploads(self, folder: str) -> bool:
        """Можно ли тратить здесь деньги на обработку: загрузить запись, пересобрать протокол.
        В общей папке — по праву загрузки или администрирования, в личной — по признаку
        учётной записи. Одного чтения для этого мало."""
        if self.admin:
            return True
        if self._mine(folder):
            return self.home_upload
        return _covers(self.upload, folder) or _covers(self.manage, folder)

    @property
    def uploader(self) -> bool:
        """Есть ли у человека хоть одно место, куда он может загружать."""
        return bool(self.admin or (self.home and self.home_upload) or self.upload or self.manage)

    def rules(self, folder: str) -> bool:
        """Раздаёт ли человек доступ к этой папке и правит ли в ней подписи."""
        return self.admin or _covers(self.manage, folder)

    def owns(self, folder: str) -> bool:
        """Хозяин записей папки: подписывает говорящих и пересобирает протокол."""
        return self.admin or self._mine(folder) or _covers(self.manage, folder)

    def plants(self, folder: str) -> bool:
        """Можно ли заводить здесь подпапки: у себя и там, где администрирование дано
        вместе с подпапками, — иначе человек создал бы папку, которую сам не видит."""
        return self.admin or self._mine(folder) or _covers(self.manage, folder, tree_only=True)

    @property
    def roots(self) -> tuple[str, ...]:
        """Папки, названные в правах, — чтобы показать их в меню, даже пока они пусты."""
        named = {f for f, _ in self.read + self.upload + self.manage if f}
        return tuple(sorted(named | ({self.home} if self.home else set())))


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, _ = stored.split("$")
        return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)), stored)
    except ValueError:
        return False


class Users:
    def __init__(self, base: db.Database, admin_password: str = ""):
        self.db = base
        self.key_path = base.dir / "session.key"
        self.admin_password = admin_password
        self._key: bytes | None = None
        self.vault = vault.Keys(base)

    # ---------- учётные записи ----------

    @staticmethod
    def _user(row, grants) -> dict:
        """Учётная запись словарём: поля строки таблицы и права на папки — {папка: область}."""
        user = {"name": row["name"], "email": row["email"], "password": row["password"], "home": row["home"],
                "admin": bool(row["admin"]), "read": {}, "upload": {}, "manage": {},
                "home_upload": bool(row["home_upload"]), "notify_ready": bool(row["notify_ready"]),
                "source": row["source"]}
        if row["source"] == DOMAIN:
            user["checked"] = row["checked"] or 0
        if row["link_hash"]:
            user["link"] = {"hash": row["link_hash"], "expires": row["link_expires"]}
        for grant in grants:
            user[grant["kind"]][grant["folder"]] = grant["scope"]
        return user

    def get(self, login: str) -> dict | None:
        """Учётная запись с правами на папки; None — такой нет. Копия: правка её ничего не меняет."""
        row = self.db.row("SELECT * FROM users WHERE login = ?", login)
        if row is None:
            return None
        return self._user(row, self.db.rows("SELECT kind, folder, scope FROM grants WHERE login = ? "
                                            "ORDER BY kind, folder", login))

    @property
    def users(self) -> dict[str, dict]:
        """Все учётные записи: логин → запись, по алфавиту логинов. Копия на этот момент."""
        grants: dict[str, list] = {}
        for grant in self.db.rows("SELECT login, kind, folder, scope FROM grants ORDER BY login, kind, folder"):
            grants.setdefault(grant["login"], []).append(grant)
        return {row["login"]: self._user(row, grants.get(row["login"], ()))
                for row in self.db.rows("SELECT * FROM users ORDER BY login")}

    def count(self) -> int:
        return self.db.value("SELECT count(*) FROM users")

    def owner_of(self, folder: str) -> str | None:
        """Чья это личная папка; None — ничья."""
        return self.db.value("SELECT login FROM users WHERE home = ?", folder)

    def _store(self, con, login: str, user: dict) -> None:
        link = user.get("link") or {}
        con.execute(
            "INSERT INTO users (login, name, email, password, home, admin, home_upload, notify_ready, source, "
            "checked, link_hash, link_expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (login) DO UPDATE SET name = excluded.name, email = excluded.email, "
            "password = excluded.password, home = excluded.home, admin = excluded.admin, "
            "home_upload = excluded.home_upload, notify_ready = excluded.notify_ready, source = excluded.source, "
            "checked = excluded.checked, link_hash = excluded.link_hash, link_expires = excluded.link_expires",
            (login, user["name"], user["email"], user["password"], user["home"], int(user["admin"]),
             int(user["home_upload"]), int(user["notify_ready"]), user["source"],
             user.get("checked") if user["source"] == DOMAIN else None, link.get("hash"), link.get("expires")))

    def put(self, login: str, name: str, password: str = "", admin: bool = False,
            email: str | None = None, invited: bool = False, home_upload: bool | None = None) -> None:
        """Заводит пользователя или меняет ему имя, почту, пароль и роль. Пустой пароль —
        оставить прежний; email=None — почту не трогать. Нового пользователя без пароля
        заводит только одобренная заявка (invited): пароль он задаст сам по ссылке из письма.
        home_upload — загрузка в личную папку; None — не трогать (у нового её нет).
        Права на папки здесь не трогаются: их раздают по папкам (set_access)."""
        login = norm_login(login)
        if len(name.strip()) > NAME_MAX:
            raise UserError(f"имя длиннее {NAME_MAX} знаков")
        if email is not None:
            email = norm_email(email)
        if password:
            check_new_password(password)
        # хеш считается десятки миллисекунд — до транзакции: под ней только сверка и запись
        fresh = hash_password(password) if password else None
        with self.db.write() as con:
            known = self.get(login)
            foreign = bool(known) and known["source"] == DOMAIN
            if password and foreign:
                raise UserError(IN_DOMAIN)
            if not password and not known and not invited:
                raise UserError("новому пользователю нужен пароль")
            if email and self.by_email(email) not in (None, login):
                raise UserError("эта почта уже указана у другого пользователя")
            user = self._filled(login, known, name, email, fresh or (known["password"] if known else ""), admin,
                                DOMAIN if foreign else LOCAL)
            if home_upload is not None:
                user["home_upload"] = bool(home_upload)
            # пароль задал администратор — ссылка, высланная раньше, больше не нужна
            if known and "link" in known and not password:
                user["link"] = known["link"]
            self._store(con, login, user)

    @staticmethod
    def _filled(login: str, known: dict | None, name: str, email: str | None, password: str,
                admin: bool, source: str) -> dict:
        """Учётная запись, как она ляжет в базу: одна форма для заведённого администратором,
        одобренного по заявке и пришедшего из домена. Настройка писем переходит от прежней
        записи; права на папки лежат в своей таблице и здесь не участвуют."""
        user = {
            "name": name.strip() or login,
            "email": email if email is not None else (known["email"] if known else ""),
            "password": password,
            "home": f"{HOME_ROOT}/{login}",
            "admin": admin,
            # загрузка в личную папку: у новой записи её нет, пока администратор не даст
            "home_upload": known["home_upload"] if known else False,
            # письмо, когда его запись готова: включено, пока человек сам не отключит
            "notify_ready": known["notify_ready"] if known else True,
            "source": source,
        }
        if source == DOMAIN:
            user["checked"] = known.get("checked", 0) if known else 0     # когда домен последний раз подтвердил
        return user

    def adopt(self, login: str, name: str, email: str, admin: bool | None, convert: bool = False,
              now: float | None = None) -> dict:
        """Человек из домена: заводит его при первом входе, а при следующих обновляет ФИО,
        почту и роль и отмечает, что домен его подтвердил. admin=None — группа администраторов
        не задана: роль не трогаем (её ставит администратор сервиса вручную).

        Логин, занятый локальной учётной записью, — отказ (Conflict): локальная запись в
        доменную сама не превращается. convert — это делает администратор сервиса: пароль и
        ссылка на пароль стираются, права на папки и ключи доступа остаются.

        Отдаёт, что изменилось: new — запись заведена, role — новая роль, если она сменилась."""
        login = norm_login(login)
        name = " ".join(name.split())[:NAME_MAX]
        try:
            email = norm_email(email)
        except UserError:
            email = ""                      # в домене записано не то, что можно счесть адресом
        with self.db.write() as con:
            known = self.get(login)
            if known and known["source"] != DOMAIN and not convert:
                raise Conflict(f"в сервисе уже есть локальная учётная запись «{login}» — войти под учётной записью "
                               "домена с тем же логином нельзя, пока администратор сервиса не привяжет её к домену")
            if email and self.by_email(email) not in (None, login):
                email = ""                  # почта в сервисе не повторяется, а вход из-за неё не срывается
            role = (known["admin"] if known else False) if admin is None else admin
            user = self._filled(login, known, name, email, "", role, DOMAIN)
            user["checked"] = int(time.time() if now is None else now)
            self._store(con, login, user)
            return {"new": known is None, "role": role if known and known["admin"] != role else None,
                    "admin": role}

    def source(self, login: str) -> str | None:
        """Источник учётной записи: local или domain; None — такой записи нет."""
        return self.db.value("SELECT source FROM users WHERE login = ?", login)

    def checked(self, login: str) -> int:
        """Когда домен последний раз подтвердил человека; 0 — не подтверждал или отказал."""
        return self.db.value("SELECT checked FROM users WHERE login = ?", login) or 0

    def unconfirm(self, login: str) -> None:
        """Домен отказал человеку: прежнее подтверждение больше не в счёт."""
        with self.db.write() as con:
            con.execute("UPDATE users SET checked = 0 WHERE login = ? AND checked <> 0", (login,))

    def by_email(self, email: str) -> str | None:
        """Логин пользователя с такой почтой. Почта у пользователей не повторяется."""
        return self.db.value("SELECT login FROM users WHERE email = ? ORDER BY login", email) if email else None

    def set_notify(self, login: str, ready: bool) -> None:
        """Человек включил или отключил себе письма о готовности записи."""
        with self.db.write() as con:
            if not con.execute("UPDATE users SET notify_ready = ? WHERE login = ?", (int(bool(ready)), login)).rowcount:
                raise UserError("у администратора из .env учётной записи в сервисе нет — и почты тоже")

    def mailbox(self, login: str) -> dict | None:
        """Имя и почта человека и хочет ли он письма о своих записях. Нет такого — None."""
        row = self.db.row("SELECT name, email, notify_ready FROM users WHERE login = ?", login)
        return row and {"name": row["name"], "email": row["email"], "notify_ready": bool(row["notify_ready"])}

    def set_access(self, login: str, folder: str, read: str | None = None, manage: str | None = None,
                   upload: str | None = None) -> None:
        """Права человека на одну папку. read, upload и manage — область: none, only (только
        эта папка) или tree (с подпапками); None — это право не менять."""
        with self.db.write() as con:
            if self.source(login) is None:
                raise UserError("такого пользователя нет")
            folder = norm_folder(folder)
            asked = [(kind, scope) for kind, scope in (("read", read), ("upload", upload), ("manage", manage))
                     if scope is not None]
            # сначала проверка всего запроса, потом правка: отказ не оставляет половину сделанной
            for kind, scope in asked:
                if scope not in SCOPES:
                    raise UserError("область права: none, only или tree")
                if not folder and (kind != "read" or scope == ONLY):
                    # «все папки» — только чтение целиком; загружать и управлять везде может лишь
                    # администратор сервиса
                    raise UserError("на все папки сразу можно дать только чтение с подпапками")
            for kind, scope in asked:
                if scope == NONE:
                    con.execute("DELETE FROM grants WHERE login = ? AND kind = ? AND folder = ?", (login, kind, folder))
                else:
                    con.execute("INSERT INTO grants (login, kind, folder, scope) VALUES (?, ?, ?, ?) "
                                "ON CONFLICT (login, kind, folder) DO UPDATE SET scope = excluded.scope",
                                (login, kind, folder, scope))

    def holders(self, folder: str) -> list[dict]:
        """Все пользователи и их права на папку — для того, кто раздаёт к ней доступ.

        read, upload и manage — что выдано именно на эту папку. read_via, upload_via и
        manage_via — папка выше, откуда право приходит вместе с подпапками: его здесь не
        отнять, только там.
        admin — администратор сервиса: ему и так можно всё.
        """
        folder = norm_folder(folder)

        def above(grants: dict) -> str | None:
            for f, scope in sorted(grants.items()):
                if scope == TREE and f != folder and (f == "" or folder.startswith(f + "/")):
                    return f or "все папки"
            return None

        return [{
            "login": login, "name": u["name"], "admin": u["admin"],
            "read": u["read"].get(folder, NONE), "read_via": above(u["read"]),
            "upload": u["upload"].get(folder, NONE), "upload_via": above(u["upload"]),
            "manage": u["manage"].get(folder, NONE), "manage_via": above(u["manage"]),
        } for login, u in self.users.items()]

    def rename_folder(self, old: str, new: str) -> None:
        """Папку переименовали — выданные на неё и на её подпапки права переезжают следом.
        Право, уже записанное на новое имя, уступает переехавшему."""
        with self.db.write() as con:
            con.execute(f"UPDATE OR REPLACE grants SET folder = ? || substr(folder, ?) WHERE {db.below('folder')}",
                        (new, len(old) + 1, *db.span(old)))

    def drop_folder(self, folder: str) -> None:
        """Папку удалили — права на неё и на её подпапки больше ни к чему."""
        with self.db.write() as con:
            con.execute(f"DELETE FROM grants WHERE {db.below('folder')}", db.span(folder))

    def delete(self, login: str) -> None:
        """Убирает учётную запись; её права и ключи доступа гаснут вместе с ней — одной транзакцией."""
        with self.db.write() as con:
            if con.execute("DELETE FROM users WHERE login = ?", (login,)).rowcount:
                self.vault.drop_owner(login)

    def _stored(self, login: str) -> str | None:
        return self.db.value("SELECT password FROM users WHERE login = ?", login)

    def _replace_password(self, login: str, stored: str, fresh: str) -> None:
        """Пишет новый хеш, если учётную запись за время подсчёта хеша никто не поменял.
        Со сменой пароля гаснет и высланная ссылка."""
        with self.db.write() as con:
            changed = con.execute("UPDATE users SET password = ?, link_hash = NULL, link_expires = NULL "
                                  "WHERE login = ? AND password = ?", (fresh, login, stored)).rowcount
            if not changed:
                raise UserError("учётная запись только что изменилась — повторите")

    def set_password(self, login: str, old: str, new: str) -> None:
        if self.source(login) == DOMAIN:
            raise UserError(IN_DOMAIN)
        stored = self._stored(login)
        if stored is None or not check_password(old, stored):
            raise UserError("текущий пароль неверный")
        check_new_password(new)
        self._replace_password(login, stored, hash_password(new))

    def public(self) -> list[dict]:
        return [{"login": k, "name": v["name"], "email": v["email"], "home": v["home"], "admin": v["admin"],
                 "has_password": bool(v["password"]), "read": dict(v["read"]), "upload": dict(v["upload"]),
                 "manage": dict(v["manage"]), "home_upload": v["home_upload"],
                 "source": v["source"], "checked": v.get("checked", 0)}
                for k, v in self.users.items()]

    # ---------- одноразовая ссылка «задать пароль» ----------

    def issue_link(self, login: str, now: float | None = None) -> str:
        """Новая ссылка для пользователя; прежняя с этого момента не действует.
        Отдаёт саму строку — она уходит в письмо и нигде не сохраняется."""
        token = secrets.token_urlsafe(32)
        with self.db.write() as con:
            source = self.source(login)
            if source is None:
                raise UserError("такого пользователя нет")
            if source == DOMAIN:
                raise UserError(IN_DOMAIN)
            con.execute("UPDATE users SET link_hash = ?, link_expires = ? WHERE login = ?",
                        (_link_hash(token), int((time.time() if now is None else now) + LINK_SEC), login))
        return token

    def link_owner(self, token: str, now: float | None = None) -> str | None:
        """Чья это ссылка, если она ещё действует."""
        row = self.db.row("SELECT login, link_expires FROM users WHERE link_hash = ?", _link_hash(token))
        if row is None:
            return None
        return row["login"] if row["link_expires"] >= (time.time() if now is None else now) else None

    def use_link(self, token: str, password: str, now: float | None = None) -> str:
        """Задаёт пароль по ссылке и гасит её. Отдаёт логин."""
        gone = "ссылка недействительна или её срок вышел — запросите новую"
        login = self.link_owner(token, now)
        if login is None:
            raise UserError(gone)
        check_new_password(password)
        stored = self._stored(login)
        fresh = hash_password(password)
        with self.db.write():
            # пока считался хеш, ссылку могли использовать или выпустить новую
            if self.link_owner(token, now) != login:
                raise UserError(gone)
            self._replace_password(login, stored, fresh)
        return login

    # ---------- ключи доступа ----------

    def keys(self, login: str) -> list[dict]:
        """Ключи человека без отпечатков — для его окна «Ключи доступа»."""
        return self.vault.of(login)

    def all_keys(self, now: float | None = None) -> list[dict]:
        """Все ключи — администратору сервиса: чей, вид, сроки и пометка, если ключ
        истёк (expired) или им давно не пользовались (stale). Значений ключей здесь нет."""
        names = {row["login"]: row["name"] for row in self.db.rows("SELECT login, name FROM users")}
        names[ADMIN] = "Администратор"
        return [{**key, "owner_name": names.get(key["owner"], key["owner"]), "mark": vault.mark(key, now)}
                for key in sorted(self.vault.everyone(), key=lambda k: (k["owner"], k["created"]))]

    def issue_key(self, login: str, name: str, days: int = 365, manage: bool = False,
                  now: float | None = None) -> tuple[str, dict]:
        """Новый ключ человека. Отдаёт саму строку — её показывают один раз и нигде не хранят.
        manage — ключ «с управлением»: только администратору сервиса."""
        who = self.access(login)
        if who is None:
            raise UserError("такого пользователя нет")
        if manage and not who.admin:
            raise UserError("ключ с управлением выпускает себе только администратор сервиса")
        try:
            return self.vault.issue(login, name, days, vault.MANAGE if manage else vault.PLAIN, now)
        except vault.KeysError as e:
            raise UserError(str(e))

    def revoke_key(self, key_id: str, login: str | None = None) -> dict:
        """Отзывает ключ. login — чей он должен быть; None — любой (администратор сервиса)."""
        try:
            return self.vault.revoke(key_id, login)
        except vault.KeysError as e:
            raise UserError(str(e))

    def by_key(self, token: str, now: float | None = None) -> Access | None:
        """Чей это ключ, если он действует, — права человека с пометкой, что пришёл ключ."""
        key = self.vault.find(token, now)
        who = self.access(key["owner"]) if key else None
        return who and dataclasses.replace(who, key=key["name"], key_kind=key["kind"])

    # ---------- вход ----------

    def verify(self, login: str, password: str) -> bool:
        login = login.strip().lower()
        if login == ADMIN:
            return bool(self.admin_password) and hmac.compare_digest(
                password.encode("utf-8"), self.admin_password.encode("utf-8"))
        # у приглашённого пароля ещё нет, у доменного его нет вовсе: пустой хеш не подходит ни к
        # какому паролю — доменный пароль с записью сервиса здесь не сверяется никогда
        stored = self._stored(login)
        return bool(stored) and check_password(password, stored)

    def access(self, login: str) -> Access | None:
        if login == ADMIN:
            return Access(ADMIN, "Администратор", True) if self.admin_password else None
        user = self.get(login)
        if user is None:
            return None
        return Access(login, user["name"], user["admin"], user["home"],
                      tuple(sorted(user["read"].items())), tuple(sorted(user["manage"].items())),
                      domain=user["source"] == DOMAIN, upload=tuple(sorted(user["upload"].items())),
                      home_upload=user["home_upload"])

    # ---------- сессии ----------

    def _secret(self) -> bytes:
        if self._key is None:
            if not self.key_path.is_file():
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                self.key_path.write_bytes(secrets.token_bytes(32))
            self._key = self.key_path.read_bytes()
        return self._key

    def _stamp(self, login: str) -> str:
        """Меняется вместе с паролем: старые метки перестают подходить."""
        source = self.admin_password if login == ADMIN else self._stored(login)
        if source is None:
            raise KeyError(login)
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def token(self, login: str, now: float | None = None) -> str:
        expires = int((time.time() if now is None else now) + SESSION_SEC)
        payload = f"{login}|{expires}|{self._stamp(login)}"
        return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii") + "." + self._sign(payload)

    def by_token(self, token: str, now: float | None = None) -> Access | None:
        try:
            body, sig = token.split(".")
            payload = base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8")
            login, expires, stamp = payload.split("|")
            if not hmac.compare_digest(sig, self._sign(payload)):
                return None
            if int(expires) < (time.time() if now is None else now):
                return None
            who = self.access(login)
            if who is None or not hmac.compare_digest(stamp, self._stamp(login)):
                return None
            return who
        except (ValueError, KeyError, UnicodeError):
            return None
