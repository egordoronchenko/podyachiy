"""Заявки на доступ: в базе сервиса (db.py: таблица requests) — и ограничение частоты для открытых форм.

Заявку оставляет человек без учётной записи, прямо со страницы входа. Форма открыта
без входа, поэтому всё в ней проверяется здесь: длины полей, вид логина (тем же
правилом, что у администратора) и почты, занятый логин, повтор уже поданной заявки
и предел числа ожидающих — чтобы таблица и почта администратора не росли без конца.

Решённые заявки остаются в базе с пометкой — одобрена или отклонена, кем и когда.
"""
from __future__ import annotations

import secrets
import threading
import time

from . import db, users
from .journal import now_iso

PENDING_MAX = 200         # столько заявок может ждать решения одновременно
DECIDED_KEPT = 500        # столько решённых хранится для истории, старые вытесняются
COMMENT_MAX = 500
PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"


class RequestError(Exception):
    def __init__(self, status: int, detail: str, field: str = ""):
        super().__init__(detail)
        # field — к какому полю формы относится отказ: страница покажет его под этим полем
        self.status, self.detail, self.field = status, detail, field


class Requests:
    def __init__(self, base: db.Database):
        self.db = base

    @staticmethod
    def _claim(row) -> dict:
        claim = {k: row[k] for k in ("id", "at", "name", "email", "login", "comment", "status")}
        if row["status"] != PENDING:
            claim.update(decided_at=row["decided_at"], decided_by=row["decided_by"])
        return claim

    def all(self) -> list[dict]:
        """Все заявки по порядку подачи: ожидающие и решённые."""
        return [self._claim(r) for r in self.db.rows("SELECT * FROM requests ORDER BY n")]

    def pending(self) -> list[dict]:
        return [self._claim(r) for r in self.db.rows("SELECT * FROM requests WHERE status = ? ORDER BY n", PENDING)]

    def add(self, name: str, email: str, login: str, comment: str, people: users.Users,
            email_needed: bool = True) -> dict:
        """Новая заявка. email_needed — обязательна ли почта: да, когда сервис шлёт письма
        (на неё придёт ссылка для входа); без почты в сервисе человек может её не указывать."""
        name = " ".join(name.split())
        comment = comment.strip()
        if not name:
            raise RequestError(400, "укажите фамилию, имя и отчество", "name")
        if len(name) > users.NAME_MAX:
            raise RequestError(400, f"ФИО длиннее {users.NAME_MAX} знаков", "name")
        if len(comment) > COMMENT_MAX:
            raise RequestError(400, f"комментарий длиннее {COMMENT_MAX} знаков", "comment")
        try:
            email = users.norm_email(email)
        except users.UserError as e:
            raise RequestError(400, str(e), "email")
        if not email and email_needed:
            raise RequestError(400, "укажите почту: на неё придёт ссылка для входа", "email")
        try:
            login = users.norm_login(login)
        except users.UserError as e:
            raise RequestError(400, str(e), "login")
        # проверки и запись — одной транзакцией: две одинаковые заявки разом не пройдут обе
        with self.db.write() as con:
            waiting = self.pending()
            if len(waiting) >= PENDING_MAX:
                raise RequestError(503, "сейчас ждёт решения слишком много заявок — попробуйте позже "
                                        "или обратитесь к администратору")
            if people.source(login) is not None:
                raise RequestError(409, "такой логин уже занят — выберите другой", "login")
            if email and people.by_email(email):
                raise RequestError(409, "учётная запись с такой почтой уже есть — "
                                        + ("воспользуйтесь ссылкой «Забыл пароль»" if email_needed
                                           else "обратитесь к администратору"), "email")
            if any(r["login"] == login or (email and r["email"] == email) for r in waiting):
                raise RequestError(409, "заявка с таким логином или почтой уже подана и ждёт решения администратора")
            claim = {"id": secrets.token_hex(8), "at": now_iso(), "name": name, "email": email,
                     "login": login, "comment": comment, "status": PENDING}
            con.execute("INSERT INTO requests (id, at, name, email, login, comment, status) "
                        "VALUES (:id, :at, :name, :email, :login, :comment, :status)", claim)
            return claim

    def get(self, claim_id: str) -> dict:
        row = self.db.row("SELECT * FROM requests WHERE id = ? AND status = ?", claim_id, PENDING)
        if row is None:
            raise RequestError(404, "такой заявки нет среди ожидающих: возможно, её уже рассмотрели")
        return self._claim(row)

    def decide(self, claim_id: str, status: str, by: str, login: str | None = None) -> dict:
        """Помечает заявку одобренной или отклонённой. login — под каким логином открыт доступ,
        если администратор его поправил."""
        with self.db.write() as con:
            self.get(claim_id)
            con.execute("UPDATE requests SET status = ?, decided_at = ?, decided_by = ?, login = coalesce(?, login) "
                        "WHERE id = ?", (status, now_iso(), by, login or None, claim_id))
            # решённые хранятся для истории, старые вытесняются
            con.execute("DELETE FROM requests WHERE status <> ?1 AND n NOT IN "
                        "(SELECT n FROM requests WHERE status <> ?1 ORDER BY n DESC LIMIT ?2)", (PENDING, DECIDED_KEPT))
            return self._claim(con.execute("SELECT * FROM requests WHERE id = ?", (claim_id,)).fetchone())


class Throttle:
    """Не больше limit событий за window_sec на один ключ (адрес клиента, логин).

    Счёт ведётся в памяти процесса: перезапуск сервиса его обнуляет, а процесс у сервиса
    один. Этого достаточно, чтобы форму без входа нельзя было завалить заявками и письмами.
    """

    def __init__(self, limit: int, window_sec: int = 3600):
        self.limit, self.window = limit, window_sec
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _fresh(self, key: str, now: float) -> list[float]:
        hits = [t for t in self._hits.get(key, ()) if t > now - self.window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def full(self, key: str, now: float | None = None) -> bool:
        """Исчерпан ли предел для ключа — без учёта новой попытки."""
        now = time.time() if now is None else now
        with self._lock:
            return len(self._fresh(key, now)) >= self.limit

    def allow(self, key: str, now: float | None = None) -> bool:
        """Засчитывает попытку, если предел не исчерпан."""
        now = time.time() if now is None else now
        with self._lock:
            if len(self._hits) > 5000:          # чужие ключи с истёкшим окном не копим
                for other in list(self._hits):
                    self._fresh(other, now)
            hits = self._fresh(key, now)
            if len(hits) >= self.limit:
                return False
            self._hits[key] = hits + [now]
            return True
