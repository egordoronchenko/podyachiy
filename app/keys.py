"""Ключи доступа — для программ и ИИ-клиентов, которые ходят в сервис от имени человека
(заголовок Authorization: Bearer).

Лежат в базе сервиса своей таблицей (db.py: keys), а не в учётных записях: ключ есть
и у встроенного администратора, у которого учётной записи в базе нет, а администратор
сервиса видит все ключи разом и может отозвать любой.

Запись на ключ: отпечаток (sha256), первые знаки для опознания, владелец (логин), имя,
вид, когда создан, когда истекает, когда им последний раз пользовались. Ключ — длинная
случайная строка, поэтому хранится её sha256, а не scrypt: подбирать нечего, а
проверяется ключ на каждом запросе. Сама строка показывается человеку один раз.

Вид ключа: обычный — им читают и загружают записи в рамках прав человека; «с управлением» —
им ещё и управляют сервисом по HTTP (пользователи, права, папки). Что именно открыто
какому виду, решает app/web/access.py; здесь вид только хранится. Ключ «с управлением» живёт не
дольше 90 дней: утёкший, он заводит пользователей и раздаёт права.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from . import db

PREFIX = "stg_"             # по началу ключ узнаётся в настройках клиента и в утечках
SHOWN = 12                  # сколько первых знаков ключа хранится для опознания
PLAIN, MANAGE = "plain", "manage"
DAYS = {PLAIN: (0, 90, 365),        # срок в днях; 0 — без срока
        MANAGE: (7, 30, 90)}
NAME_MAX = 60
PER_OWNER = 20
TOUCH_SEC = 300             # «последний раз использован» пишется в базу не чаще
STALE_DAYS = 90             # столько не пользовались — ключ, скорее всего, забыт
PUBLIC = ("id", "owner", "name", "kind", "prefix", "created", "expires", "used")


class KeysError(Exception):
    pass


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mark(key: dict, now: float | None = None) -> str:
    """Чем ключ заслуживает внимания: expired — срок вышел, stale — давно не использовался."""
    now = time.time() if now is None else now
    if key["expires"] is not None and key["expires"] < now:
        return "expired"
    return "stale" if now - (key["used"] or key["created"]) > STALE_DAYS * 86400 else ""


class Keys:
    def __init__(self, base: db.Database):
        self.db = base

    @staticmethod
    def _public(key) -> dict:
        return {k: key[k] for k in PUBLIC}

    def of(self, owner: str) -> list[dict]:
        """Ключи человека без отпечатков — для его окна «Ключи доступа»."""
        return [self._public(k) for k in self.db.rows("SELECT * FROM keys WHERE owner = ? ORDER BY n", owner)]

    def everyone(self) -> list[dict]:
        """Все ключи без отпечатков — администратору сервиса."""
        return [self._public(k) for k in self.db.rows("SELECT * FROM keys ORDER BY n")]

    def issue(self, owner: str, name: str, days: int, kind: str = PLAIN,
              now: float | None = None) -> tuple[str, dict]:
        """Новый ключ. Отдаёт саму строку — её показывают человеку один раз и нигде не хранят."""
        name = " ".join(name.split())
        if not name or len(name) > NAME_MAX:
            raise KeysError(f"назовите ключ — до {NAME_MAX} знаков: например, «Cursor на рабочем ноутбуке»")
        if kind not in DAYS:
            raise KeysError("вид ключа: plain (обычный) или manage (с управлением)")
        if days not in DAYS[kind]:
            raise KeysError("срок ключа с управлением: 7, 30 или 90 дней — дольше нельзя" if kind == MANAGE
                            else "срок ключа: без срока, 90 дней или год")
        now = int(time.time() if now is None else now)
        token = PREFIX + secrets.token_urlsafe(32)
        with self.db.write() as con:
            mine = self.of(owner)
            if len(mine) >= PER_OWNER:
                raise KeysError(f"ключей уже {PER_OWNER} — отзовите ненужные")
            if any(k["name"] == name for k in mine):
                raise KeysError("ключ с таким именем уже есть")
            key = {"id": secrets.token_hex(4), "hash": digest(token), "prefix": token[:SHOWN], "owner": owner,
                   "name": name, "kind": kind, "created": now,
                   "expires": now + days * 86400 if days else None, "used": None}
            con.execute("INSERT INTO keys (id, hash, prefix, owner, name, kind, created, expires, used) "
                        "VALUES (:id, :hash, :prefix, :owner, :name, :kind, :created, :expires, :used)", key)
            return token, self._public(key)

    def revoke(self, key_id: str, owner: str | None = None) -> dict:
        """Гасит ключ. owner — чей он должен быть: человек отзывает только свои,
        администратор сервиса (owner=None) — любой."""
        with self.db.write() as con:
            key = con.execute("SELECT * FROM keys WHERE id = ? AND (?2 IS NULL OR owner = ?2) ORDER BY n LIMIT 1",
                              (key_id, owner)).fetchone()
            if key is None:
                raise KeysError("такого ключа нет")
            con.execute("DELETE FROM keys WHERE n = ?", (key["n"],))
            return self._public(key)

    def drop_owner(self, owner: str) -> int:
        """Человека удалили — его ключи больше ничьи."""
        with self.db.write() as con:
            return con.execute("DELETE FROM keys WHERE owner = ?", (owner,)).rowcount

    def find(self, token: str, now: float | None = None) -> dict | None:
        """Запись действующего ключа по его строке. Заодно отмечает, что ключом пользовались, —
        но не на каждом запросе: отметка — запись в базу, а проверяется ключ при каждом обращении."""
        if not token.startswith(PREFIX):
            return None
        wanted = digest(token)
        now = int(time.time() if now is None else now)
        key = self.db.row("SELECT * FROM keys WHERE hash = ?", wanted)
        if key is None or not hmac.compare_digest(key["hash"], wanted):
            return None
        if key["expires"] is not None and key["expires"] < now:
            return None
        found = self._public(key)
        if key["used"] is None or now - key["used"] >= TOUCH_SEC:
            with self.db.write() as con:
                con.execute("UPDATE keys SET used = ? WHERE n = ?", (now, key["n"]))
            found["used"] = now
        return found
