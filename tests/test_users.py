import json
import types

import pytest

from app import db, keys, users
from tests import legacy

HOME = users.HOME_ROOT


def store(tmp_path, admin="секрет-админа"):
    return users.Users(db.Database(tmp_path), admin)


def on_disk(tmp_path) -> str:
    """Всё, что база держит на диске, одним текстом: сам файл и журнал незакрытых записей."""
    return "".join(path.read_bytes().decode("utf-8", "replace") for path in sorted(tmp_path.glob(db.FILE + "*")))


def upgraded(tmp_path, admin="секрет-админа"):
    """Учётные записи установки прежней версии (users.json в tmp_path) после первого запуска с базой."""
    cfg = types.SimpleNamespace(state_dir=tmp_path, inbox=tmp_path / "inbox", outbox=tmp_path / "outbox")
    return users.Users(legacy.start(cfg), admin)


def user(s, login, name="Имя", on=None):
    """Заводит пользователя и раздаёт ему права: папка → (чтение, администрирование)."""
    s.put(login, name, "пароль-1234")
    for folder, (read, manage) in (on or {}).items():
        s.set_access(login, folder, read, manage)
    return s.access(login)


def test_read_on_one_folder_does_not_open_its_subfolders(tmp_path):
    s = store(tmp_path)
    who = user(s, "ivanov", on={"Проекты отдела": ("only", None)})
    assert who.sees("Проекты отдела")
    assert not who.sees("Проекты отдела/Сметы") and not who.sees("Проекты") and not who.sees("")


def test_read_with_subfolders_opens_the_whole_branch(tmp_path):
    s = store(tmp_path)
    who = user(s, "ivanov", on={"Проекты отдела/": ("tree", None), "Обучение\\Курс": ("tree", None)})
    assert who.sees("Проекты отдела") and who.sees("Проекты отдела/Сметы/2026") and who.sees("Обучение/Курс")
    assert not who.sees("Проекты отдела-2") and not who.sees("Обучение") and not who.admin


def test_read_and_admin_are_independent_and_each_has_its_own_scope(tmp_path):
    s = store(tmp_path)
    who = user(s, "lead", on={"Проекты отдела": ("only", "tree"), "Снабжение": ("tree", "only")})
    # администрирование с подпапками: раздаёт доступ по всей ветке и заводит подпапки
    assert who.rules("Проекты отдела") and who.rules("Проекты отдела/Сметы") and who.plants("Проекты отдела/Сметы")
    assert who.sees("Проекты отдела/Сметы")                     # администратор видит то, чем управляет
    # администрирование только папки: раздаёт доступ к ней самой, но не ниже и подпапок не заводит
    assert who.rules("Снабжение") and not who.rules("Снабжение/Заявки")
    assert not who.plants("Снабжение") and who.owns("Снабжение")
    assert who.sees("Снабжение/Заявки")                     # это уже по праву чтения с подпапками
    assert not who.rules("Обучение") and not who.rules("")


def test_everyone_gets_a_personal_folder_and_is_its_master(tmp_path):
    s = store(tmp_path)
    who = user(s, "ivanov", on={"Проекты отдела": ("tree", None)})
    user(s, "petrov")
    assert who.home == f"{HOME}/ivanov" and who.sees(f"{HOME}/ivanov/черновики")
    assert who.plants(f"{HOME}/ivanov") and who.owns(f"{HOME}/ivanov/черновики")
    assert not who.rules(f"{HOME}/ivanov")                  # личную папку другим не раздают
    assert not who.plants("Проекты отдела") and not who.owns("Проекты отдела") and not who.plants(HOME)
    assert not who.sees(f"{HOME}/petrov") and not s.access("petrov").sees(f"{HOME}/ivanov")
    assert who.roots == tuple(sorted((f"{HOME}/ivanov", "Проекты отдела")))


def test_all_folders_can_only_be_given_for_reading(tmp_path):
    s = store(tmp_path)
    who = user(s, "boss", on={"": ("tree", None)})
    assert who.sees("Совещания") and who.sees(f"{HOME}/кто-то") and not who.rules("Совещания")
    for read, manage in (("only", None), (None, "tree"), (None, "only")):
        with pytest.raises(users.UserError):
            s.set_access("boss", "", read, manage)


def test_access_is_taken_back_and_changing_one_right_keeps_the_other(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov", on={"Проекты отдела": ("tree", "only")})
    s.set_access("ivanov", "Проекты отдела", read="none")
    who = s.access("ivanov")
    assert who.rules("Проекты отдела") and not who.sees("Проекты отдела/Сметы")
    s.set_access("ivanov", "Проекты отдела", manage="none")
    assert not s.access("ivanov").sees("Проекты отдела")
    with pytest.raises(users.UserError):
        s.set_access("ivanov", "Проекты отдела", read="всё")
    with pytest.raises(users.UserError):
        s.set_access("nobody", "Проекты отдела", read="tree")


def test_holders_show_own_rights_and_what_comes_from_above(tmp_path):
    s = store(tmp_path)
    user(s, "lead", "Руководитель", on={"Проекты отдела": (None, "tree")})
    user(s, "ivanov", "Иванов", on={"Проекты отдела": ("tree", None)})
    user(s, "petrov", "Петров", on={"Проекты отдела/Сметы": ("only", "only"), "Проекты отдела": ("only", None)})
    user(s, "boss", "Директор", on={"": ("tree", None)})
    held = {u["login"]: u for u in s.holders("Проекты отдела/Сметы/")}
    assert held["petrov"] == {"login": "petrov", "name": "Петров", "admin": False,
                              "read": "only", "read_via": None, "upload": "none", "upload_via": None,
                              "manage": "only", "manage_via": None}
    assert held["ivanov"]["read"] == "none" and held["ivanov"]["read_via"] == "Проекты отдела"
    assert held["lead"]["manage"] == "none" and held["lead"]["manage_via"] == "Проекты отдела"
    assert held["boss"]["read_via"] == "все папки"


def test_passwords_are_stored_hashed_and_survive_reload(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov", on={"А": ("tree", None)})
    assert "пароль-1234" not in on_disk(tmp_path) and "scrypt$" in on_disk(tmp_path)
    again = store(tmp_path)
    assert again.verify("Ivanov ", "пароль-1234") and not again.verify("ivanov", "не тот")
    assert not again.verify("nobody", "пароль-1234")
    assert again.access("ivanov").sees("А/Б")                  # права тоже пережили перезапуск


def test_admin_enters_with_the_env_password_and_may_everything(tmp_path):
    s = store(tmp_path)
    assert s.verify("admin", "секрет-админа") and not s.verify("admin", "")
    who = s.access("admin")
    assert who.admin and who.sees("что угодно") and who.rules("Совещания") and who.plants("")
    assert not store(tmp_path, admin="").verify("admin", "")


def test_several_service_admins(tmp_path):
    s = store(tmp_path)
    s.put("chief", "Второй админ", "пароль-1234", admin=True)
    user(s, "ivanov")
    who = s.access("chief")
    assert who.admin and who.sees(f"{HOME}/ivanov") and who.rules("Совещания") and who.plants("Новая общая")
    assert who.home == f"{HOME}/chief" and s.by_token(s.token("chief")).admin
    s.put("chief", "Второй админ")                             # галочку сняли
    assert not s.access("chief").admin and not s.access("chief").sees(f"{HOME}/ivanov")
    assert s.access("admin").admin                             # главный остаётся всегда


@pytest.mark.parametrize("login, password", [("admin", "пароль-1234"), ("Иванов", "пароль-1234"),
                                             ("a", "пароль-1234"), ("ivanov", "коротко"), ("ivanov", "")])
def test_bad_logins_and_weak_passwords_are_refused(tmp_path, login, password):
    with pytest.raises(users.UserError):
        store(tmp_path).put(login, "Имя", password)


def test_editing_a_user_keeps_password_and_rights(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov", on={"А": ("tree", "only")})
    s.put("ivanov", "Иванов Иван")
    who = s.access("ivanov")
    assert s.verify("ivanov", "пароль-1234") and who.name == "Иванов Иван"
    assert who.sees("А/Б") and who.rules("А")


def test_session_token_works_until_expiry_password_change_or_removal(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov")
    token = s.token("ivanov", now=1000)
    assert s.by_token(token, now=1000 + 3600).login == "ivanov"
    assert store(tmp_path).by_token(token, now=2000).login == "ivanov"      # перезапуск сервиса
    assert s.by_token(token, now=1000 + users.SESSION_SEC + 1) is None      # истекла
    assert s.by_token(token[:-2] + "00", now=2000) is None                  # подделали подпись
    assert s.by_token("мусор", now=2000) is None

    s.set_password("ivanov", "пароль-1234", "новый-пароль-5678")
    assert s.by_token(token, now=2000) is None                              # пароль сменили
    fresh = s.token("ivanov", now=1000)
    s.delete("ivanov")
    assert s.by_token(fresh, now=2000) is None                              # пользователя удалили


def test_changing_own_password_requires_the_current_one(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov")
    with pytest.raises(users.UserError):
        s.set_password("ivanov", "не тот", "новый-пароль-5678")
    with pytest.raises(users.UserError):
        s.set_password("ivanov", "пароль-1234", "мало")


def test_users_file_of_the_previous_layout_is_read_as_rights_with_subfolders(tmp_path):
    old = {"users": {"ivanov": {"name": "Иванов", "password": users.hash_password("пароль-1234"),
                                "home": f"{HOME}/ivanov", "admin": False,
                                "folders": ["Проекты отдела"], "manages": ["Снабжение"]}}}
    (tmp_path / "users.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    who = upgraded(tmp_path).access("ivanov")
    assert who.sees("Проекты отдела/Сметы") and who.rules("Снабжение/Заявки") and not who.rules("Проекты отдела")


# ---------- почта, пользователь без пароля, ссылка «задать пароль» ----------

def invited(s, login="sidorov", email="sidorov@example.ru"):
    s.put(login, "Сидоров", email=email, invited=True)
    return login


def test_invited_user_has_no_password_and_cannot_log_in(tmp_path):
    s = store(tmp_path)
    invited(s)
    assert s.users["sidorov"]["password"] == "" and s.public()[0]["has_password"] is False
    for password in ("", "пароль-1234", "scrypt$$", "$$"):
        assert not s.verify("sidorov", password), password
    with pytest.raises(users.UserError):
        s.set_password("sidorov", "", "новый-пароль-5678")          # сменить «пустой» пароль нельзя
    with pytest.raises(users.UserError):
        s.put("petrov", "Петров")                                    # без приглашения пароль обязателен
    assert store(tmp_path).access("sidorov").home == f"{HOME}/sidorov"


def test_email_is_kept_checked_and_unique(tmp_path):
    s = store(tmp_path)
    s.put("ivanov", "Иванов", "пароль-1234", email=" Ivanov@Example.RU ")
    assert s.users["ivanov"]["email"] == "ivanov@example.ru" and s.by_email("ivanov@example.ru") == "ivanov"
    s.put("ivanov", "Иванов Иван")                                   # почту не называли — она остаётся
    assert s.public()[0]["email"] == "ivanov@example.ru"
    s.put("ivanov", "Иванов Иван", email="")                         # стёрли
    assert s.users["ivanov"]["email"] == "" and s.by_email("") is None
    s.put("ivanov", "Иванов", email="ivanov@example.ru")
    for bad in ("не почта", "a@b", "a b@c.ru", "a@b.ru,c@d.ru", "a@b.ru\nBcc: x@y.ru", "<a@b.ru>"):
        with pytest.raises(users.UserError):
            s.put("ivanov", "Иванов", email=bad)
    with pytest.raises(users.UserError, match="другого пользователя"):
        s.put("petrov", "Петров", "пароль-1234", email="IVANOV@example.ru")
    assert "petrov" not in s.users


def test_users_file_without_email_is_read(tmp_path):
    old = {"users": {"ivanov": {"name": "Иванов", "password": users.hash_password("пароль-1234"),
                                "home": f"{HOME}/ivanov", "admin": False, "read": {}, "manage": {}}}}
    (tmp_path / "users.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    s = upgraded(tmp_path)
    assert s.public()[0]["email"] == "" and s.public()[0]["has_password"] and s.verify("ivanov", "пароль-1234")


def test_link_sets_the_password_once(tmp_path):
    s = store(tmp_path)
    invited(s)
    token = s.issue_link("sidorov", now=1000)
    saved = on_disk(tmp_path)
    assert token not in saved and users._link_hash(token) in saved and len(token) >= 43     # в базе только хеш
    assert store(tmp_path).link_owner(token, now=2000) == "sidorov"  # переживает перезапуск
    with pytest.raises(users.UserError, match="короче"):
        s.use_link(token, "мало", now=2000)
    assert s.link_owner(token, now=2000) == "sidorov"                # слабый пароль ссылку не гасит
    assert s.use_link(token, "пароль-из-письма", now=2000) == "sidorov"
    assert s.verify("sidorov", "пароль-из-письма") and "link" not in s.users["sidorov"]
    assert s.link_owner(token, now=2000) is None
    with pytest.raises(users.UserError, match="недействительна"):
        s.use_link(token, "другой-пароль-1", now=2000)               # второй раз не сработает
    assert s.verify("sidorov", "пароль-из-письма")


def test_link_expires_after_48_hours(tmp_path):
    s = store(tmp_path)
    invited(s)
    token = s.issue_link("sidorov", now=1000)
    assert users.LINK_SEC == 48 * 3600
    assert s.link_owner(token, now=1000 + users.LINK_SEC) == "sidorov"
    assert s.link_owner(token, now=1000 + users.LINK_SEC + 1) is None
    with pytest.raises(users.UserError, match="срок вышел"):
        s.use_link(token, "пароль-из-письма", now=1000 + users.LINK_SEC + 1)
    assert not s.verify("sidorov", "пароль-из-письма")


def test_new_link_cancels_the_previous_one_and_wrong_strings_fit_nobody(tmp_path):
    s = store(tmp_path)
    invited(s)
    user(s, "ivanov")
    first = s.issue_link("sidorov", now=1000)
    second = s.issue_link("sidorov", now=1100)
    assert first != second and s.link_owner(first, now=1200) is None and s.link_owner(second, now=1200) == "sidorov"
    for junk in ("", "мусор", second[:-1], second + "x"):
        assert s.link_owner(junk, now=1200) is None
    with pytest.raises(users.UserError):
        s.issue_link("nobody")
    assert s.issue_link("ivanov", now=1200) != second and s.link_owner(second, now=1200) == "sidorov"


def test_reset_link_keeps_the_old_password_until_used_and_ends_old_sessions(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov")
    session = s.token("ivanov", now=1000)
    token = s.issue_link("ivanov", now=1000)
    assert s.verify("ivanov", "пароль-1234") and s.by_token(session, now=1100).login == "ivanov"
    s.use_link(token, "новый-пароль-5678", now=1100)
    assert s.verify("ivanov", "новый-пароль-5678") and not s.verify("ivanov", "пароль-1234")
    assert s.by_token(session, now=1200) is None


def test_changing_the_password_any_other_way_cancels_the_link(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov")
    token = s.issue_link("ivanov", now=1000)
    s.put("ivanov", "Иванов Иван")                                   # правка имени ссылку не трогает
    assert s.link_owner(token, now=1100) == "ivanov"
    s.set_password("ivanov", "пароль-1234", "новый-пароль-5678")
    assert s.link_owner(token, now=1100) is None
    token = s.issue_link("ivanov", now=1000)
    s.put("ivanov", "Иванов", "пароль-от-админа")                    # пароль задал администратор
    assert s.link_owner(token, now=1100) is None


def test_store_is_safe_to_change_from_several_threads(tmp_path):
    """Права раздают из событийного цикла, пароли меняют в потоках: без замка сохранения
    затирали бы друг друга и спотыкались об общий временный файл."""
    import threading
    s = store(tmp_path)
    for n in range(8):
        s.put(f"user{n}", "Имя", "пароль-1234")
    failures = []

    def work(n):
        try:
            for i in range(25):
                s.set_access(f"user{n}", f"Папка {i}", "tree")
                s.issue_link(f"user{n}")
        except Exception as e:      # любая ошибка потока — провал теста
            failures.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert failures == []
    saved = store(tmp_path).users                               # с диска, другим соединением
    assert all(len(saved[f"user{n}"]["read"]) == 25 and "link" in saved[f"user{n}"] for n in range(8))


# ---------- ключи доступа ----------

def test_key_is_shown_once_and_only_its_hash_is_stored(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov", "Иванов", on={"Проекты": ("tree", None)})
    token, key = s.issue_key("ivanov", "  Cursor   на ноутбуке ", 90, now=1000)
    assert token.startswith("stg_") and len(token) > 40
    assert key == {"id": key["id"], "owner": "ivanov", "name": "Cursor на ноутбуке", "kind": "plain",
                   "prefix": token[:12], "created": 1000, "expires": 1000 + 90 * 86400, "used": None}
    saved = on_disk(tmp_path)
    assert token not in saved and token[12:] not in saved
    stored, = [dict(row) for row in s.db.rows("SELECT * FROM keys")]
    assert len(stored.pop("hash")) == 64 and stored.pop("n") == 1 and stored == key and keys.digest(token) in saved
    assert s.keys("ivanov") == [key] and "hash" not in s.keys("ivanov")[0]

    who = users.Users(db.Database(tmp_path), "секрет-админа").by_key(token, now=2000)       # и после перезапуска
    assert who.login == "ivanov" and (who.key, who.key_kind) == ("Cursor на ноутбуке", "plain") and not who.admin
    assert who.sees("Проекты/Сметы") and who.sees(f"{HOME}/ivanov") and not who.sees("Снабжение")
    assert (s.access("ivanov").key, s.access("ivanov").key_kind) == ("", "")    # сам человек — без пометки
    assert s.issue_key("ivanov", "по умолчанию — на год", now=0)[1]["expires"] == 365 * 86400


def test_key_stops_working_when_expired_revoked_or_the_user_is_removed(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov")
    user(s, "petrov")
    year, _ = s.issue_key("ivanov", "на год", 365, now=0)
    forever, kept = s.issue_key("ivanov", "без срока", 0, now=0)
    other, others = s.issue_key("petrov", "чужой", 0, now=0)
    assert kept["expires"] is None
    assert s.by_key(year, now=365 * 86400) and s.by_key(year, now=365 * 86400 + 1) is None
    assert s.by_key(forever, now=10 ** 10).login == "ivanov"

    with pytest.raises(users.UserError):
        s.revoke_key(others["id"], "ivanov")                # человек отзывает только свои
    assert s.revoke_key(kept["id"], "ivanov")["name"] == "без срока"
    assert s.by_key(forever) is None and [k["name"] for k in s.keys("ivanov")] == ["на год"]
    with pytest.raises(users.UserError):
        s.revoke_key(kept["id"], "ivanov")
    s.delete("ivanov")                                      # удаление пользователя гасит его ключи — и в базе
    assert s.by_key(year, now=1) is None and s.keys("ivanov") == []
    assert [k["owner"] for k in store(tmp_path).vault.everyone()] == ["petrov"]
    assert s.by_key(other, now=1).login == "petrov"
    assert s.revoke_key(others["id"])["owner"] == "petrov"  # администратор сервиса — любой
    assert s.by_key(other) is None


def test_wrong_strings_fit_no_key_and_bad_requests_are_refused(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov")
    token, _ = s.issue_key("ivanov", "ключ")
    for junk in ("", "stg_", token[:-1], token + "x", token[4:], "секрет-админа", "пароль-1234"):
        assert s.by_key(junk) is None, junk
    with pytest.raises(users.UserError):
        s.issue_key("nobody", "ключ")
    for name, days in (("", 0), ("   ", 0), ("я" * 61, 0), ("ключ", 0), ("другой", 30), ("другой", 7)):
        with pytest.raises(users.UserError):
            s.issue_key("ivanov", name, days)
    for n in range(keys.PER_OWNER - 1):
        s.issue_key("ivanov", f"ключ {n}")
    with pytest.raises(users.UserError):
        s.issue_key("ivanov", "лишний")


def test_builtin_admin_issues_keys_though_he_has_no_account(tmp_path):
    s = store(tmp_path)
    token, key = s.issue_key("admin", "Claude Code", 90)
    assert key["owner"] == "admin" and "admin" not in s.users
    who = users.Users(db.Database(tmp_path), "секрет-админа").by_key(token)
    assert who.login == "admin" and who.admin and who.key == "Claude Code" and who.key_kind == "plain"
    assert [k["name"] for k in s.keys("admin")] == ["Claude Code"]
    # пароль администратора убрали из .env — его ключ не действует, как и он сам
    assert users.Users(db.Database(tmp_path), "").by_key(token) is None
    with pytest.raises(users.UserError):
        users.Users(db.Database(tmp_path), "").issue_key("admin", "ещё")


def test_manage_key_is_only_for_service_admins_and_lives_ninety_days_at_most(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov")
    with pytest.raises(users.UserError, match="администратор"):
        s.issue_key("ivanov", "управление", 30, manage=True)
    for days in (0, 365, 180):
        with pytest.raises(users.UserError, match="90"):
            s.issue_key("admin", "управление", days, manage=True)
    token, key = s.issue_key("admin", "управление", 90, manage=True, now=0)
    assert key["kind"] == "manage" and key["expires"] == 90 * 86400
    who = s.by_key(token, now=1)
    assert who.admin and who.key_kind == "manage"
    assert s.by_key(token, now=90 * 86400 + 1) is None
    # администратора разжаловали — ключ остаётся его ключом, но прав администратора за ним больше нет
    s.put("ivanov", "Иванов", admin=True)
    was_admin, _ = s.issue_key("ivanov", "управление", 7, manage=True)
    s.put("ivanov", "Иванов", admin=False)
    assert not s.by_key(was_admin).admin and s.by_key(was_admin).key_kind == "manage"


def test_last_use_is_written_at_most_once_in_a_few_minutes(tmp_path, monkeypatch):
    s = store(tmp_path)
    user(s, "ivanov")
    token, _ = s.issue_key("ivanov", "ключ", now=0)
    writes = lambda: s.db.value("SELECT total_changes()")       # сколько строк это соединение изменило в базе
    before = writes()
    for now in (100, 101, 100 + keys.TOUCH_SEC - 1):
        assert s.by_key(token, now=now).login == "ivanov"
    assert writes() - before == 1 and s.keys("ivanov")[0]["used"] == 100
    s.by_key(token, now=100 + keys.TOUCH_SEC)
    assert writes() - before == 2 and s.keys("ivanov")[0]["used"] == 100 + keys.TOUCH_SEC


def test_keys_survive_editing_the_user_and_a_password_change(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov")
    token, _ = s.issue_key("ivanov", "ключ")
    s.put("ivanov", "Иванов И. И.", "новый-пароль-1", admin=True)
    s.set_password("ivanov", "новый-пароль-1", "ещё-один-пароль")
    who = s.by_key(token)
    assert who.name == "Иванов И. И." and who.admin and who.key == "ключ"


def test_users_file_without_keys_is_read(tmp_path):
    (tmp_path / "users.json").write_text(json.dumps({"users": {"ivanov": {
        "name": "Иванов", "email": "", "password": users.hash_password("пароль-1234"),
        "home": f"{HOME}/ivanov", "admin": False, "read": {}, "manage": {}}}}), encoding="utf-8")
    s = upgraded(tmp_path)
    assert s.keys("ivanov") == [] and s.issue_key("ivanov", "ключ")[0].startswith("stg_")


def old_file(tmp_path, token="stg_" + "a" * 43):
    """users.json прежнего вида: ключи лежат в учётных записях."""
    old = {"id": "0a1b2c3d", "name": "Cursor", "prefix": token[:12], "hash": keys.digest(token),
           "created": 1000, "expires": None, "used": 2000}
    account = {"name": "Иванов", "email": "", "password": users.hash_password("пароль-1234"),
               "home": f"{HOME}/ivanov", "admin": False, "read": {"Проекты": "tree"}, "manage": {},
               "notify_ready": True}
    (tmp_path / "users.json").write_text(json.dumps({"users": {
        "ivanov": {**account, "keys": [old]},
        "petrov": {**account, "name": "Петров", "home": f"{HOME}/petrov", "keys": []}}}), encoding="utf-8")
    return token, old


def test_keys_kept_inside_the_accounts_move_to_the_keys_table(tmp_path):
    token, old = old_file(tmp_path)
    s = upgraded(tmp_path)
    moved, = [dict(row) for row in s.db.rows("SELECT * FROM keys")]
    assert moved == {**old, "owner": "ivanov", "kind": "plain", "n": 1}
    assert s.users["ivanov"]["read"] == {"Проекты": "tree"}
    who = s.by_key(token, now=3000)
    assert who.login == "ivanov" and who.key == "Cursor" and who.sees("Проекты/Сметы")
    assert s.keys("ivanov")[0]["used"] == 3000 and s.keys("petrov") == []
    # второй запуск ничего не переносит: прежние файлы убраны, база та же
    assert not (tmp_path / "users.json").exists()
    again = upgraded(tmp_path)
    assert len(again.vault.everyone()) == 1 and again.by_key(token, now=3001).login == "ivanov"


def test_key_that_lies_both_in_the_account_and_in_the_keys_file_is_not_doubled(tmp_path):
    """Прежний перенос ключей из users.json в keys.json мог оборваться между двумя файлами:
    ключ лежит в обоих. В базу он попадает один раз."""
    token, old = old_file(tmp_path)
    (tmp_path / "keys.json").write_text(json.dumps({"keys": [{**old, "owner": "ivanov", "kind": "plain"}]}),
                                        encoding="utf-8")
    s = upgraded(tmp_path)
    assert [k["id"] for k in s.vault.everyone()] == ["0a1b2c3d"] and s.by_key(token).login == "ivanov"


def test_admin_sees_every_key_with_its_owner_and_marks(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov", "Иванов")
    s.issue_key("ivanov", "свежий", 365, now=0)
    s.issue_key("ivanov", "истёкший", 90, now=0)
    used, _ = s.issue_key("ivanov", "в работе", 0, now=0)
    s.issue_key("admin", "управление", 90, manage=True, now=0)
    day = 86400
    s.by_key(used, now=100 * day)
    listed = s.all_keys(now=100 * day)
    assert [(k["owner"], k["owner_name"], k["name"], k["kind"], k["mark"]) for k in listed] == [
        ("admin", "Администратор", "управление", "manage", "expired"),
        ("ivanov", "Иванов", "свежий", "plain", "stale"),           # 100 дней ни разу не использован
        ("ivanov", "Иванов", "истёкший", "plain", "expired"),
        ("ivanov", "Иванов", "в работе", "plain", "")]
    assert all("hash" not in k for k in listed)
    assert [k["mark"] for k in s.all_keys(now=10 * day)] == ["", "", "", ""]


# ---------- раздельные права: чтение, загрузка, администрирование ----------

def test_reading_does_not_let_upload_and_upload_includes_reading(tmp_path):
    s = store(tmp_path)
    reader = user(s, "reader", on={"Проекты": ("tree", None)})
    assert reader.sees("Проекты") and reader.sees("Проекты/Сметы")
    assert not reader.uploads("Проекты") and not reader.uploads("Проекты/Сметы") and not reader.uploader

    s.put("loader", "Имя", "пароль-1234")
    s.set_access("loader", "Проекты", upload="tree")
    loader = s.access("loader")
    assert loader.sees("Проекты") and loader.sees("Проекты/Сметы")         # загрузка включает чтение
    assert loader.uploads("Проекты") and loader.uploads("Проекты/Сметы") and loader.uploader
    assert not loader.owns("Проекты") and not loader.rules("Проекты") and not loader.plants("Проекты")
    assert not loader.uploads("Снабжение") and not loader.uploads("") and not loader.sees("Снабжение")
    assert loader.roots == tuple(sorted((f"{HOME}/loader", "Проекты")))


def test_upload_on_one_folder_does_not_reach_its_subfolders(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov", on={"Проекты": ("tree", None)})
    s.set_access("ivanov", "Проекты", upload="only")
    who = s.access("ivanov")
    assert who.uploads("Проекты") and not who.uploads("Проекты/Сметы") and who.sees("Проекты/Сметы")
    s.set_access("ivanov", "Проекты", upload="none")
    assert not s.access("ivanov").uploads("Проекты") and s.access("ivanov").sees("Проекты")      # чтение осталось


def test_administration_includes_upload(tmp_path):
    s = store(tmp_path)
    who = user(s, "lead", on={"Проекты": (None, "only")})
    assert who.uploads("Проекты") and who.uploader and not who.uploads("Проекты/Сметы")
    whole = user(s, "chief", on={"Проекты": (None, "tree")})
    assert whole.uploads("Проекты/Сметы")
    assert s.access("admin").uploads("") and s.access("admin").uploads(f"{HOME}/lead") and s.access("admin").uploader


def test_home_folder_upload_is_a_separate_mark_of_the_account(tmp_path):
    s = store(tmp_path)
    who = user(s, "ivanov")
    home = f"{HOME}/ivanov"
    assert who.sees(home) and who.owns(home) and who.plants(home)          # папка есть, хозяин в ней он
    assert not who.uploads(home) and not who.uploads(home + "/Архив") and not who.uploader
    s.put("ivanov", "Иванов", home_upload=True)
    who = s.access("ivanov")
    assert who.uploads(home) and who.uploads(home + "/Архив") and who.uploader
    assert not who.uploads(f"{HOME}/petrov") and not who.uploads(HOME)
    s.put("ivanov", "Иванов И. И.")                                        # правка без признака его не трогает
    assert s.access("ivanov").home_upload and s.public()[0]["home_upload"] is True
    s.put("ivanov", "Иванов", home_upload=False)
    assert not s.access("ivanov").uploads(home)
    # администрирование общей папки загрузки в личную не даёт
    s.set_access("ivanov", "Проекты", manage="tree")
    assert s.access("ivanov").uploads("Проекты") and not s.access("ivanov").uploads(home)


def test_all_folders_cannot_be_given_for_upload(tmp_path):
    s = store(tmp_path)
    user(s, "boss", on={"": ("tree", None)})
    for scope in ("tree", "only"):
        with pytest.raises(users.UserError):
            s.set_access("boss", "", upload=scope)
    # отказ не оставляет запрос сделанным наполовину
    with pytest.raises(users.UserError):
        s.set_access("boss", "", read="none", upload="tree")
    assert s.access("boss").sees("Совещания") and not s.access("boss").uploads("Совещания")
    with pytest.raises(users.UserError):
        s.set_access("boss", "Проекты", upload="всё")


def test_holders_and_folder_moves_know_the_upload_right(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov", "Иванов", on={"Проекты": ("only", None)})
    s.set_access("ivanov", "Проекты", upload="tree")
    s.set_access("ivanov", "Проекты/Сметы", upload="only")
    held, = s.holders("Проекты/Сметы")
    assert (held["upload"], held["upload_via"], held["read"], held["read_via"]) == ("only", "Проекты", "none", None)
    assert s.public()[0]["upload"] == {"Проекты": "tree", "Проекты/Сметы": "only"}
    s.rename_folder("Проекты", "Проекты отдела")
    assert s.users["ivanov"]["upload"] == {"Проекты отдела": "tree", "Проекты отдела/Сметы": "only"}
    s.drop_folder("Проекты отдела/Сметы")
    assert s.users["ivanov"]["upload"] == {"Проекты отдела": "tree"}
    assert users.Users(db.Database(tmp_path), "секрет-админа").access("ivanov").uploads("Проекты отдела/Архив")     # пережило перезапуск


def test_old_users_file_gives_reading_only(tmp_path):
    """Файл до раздельных прав: чтение там включало загрузку. После обновления чтение — только
    чтение, а загрузки нет ни в общих папках, ни в личной, пока её не выдадут."""
    (tmp_path / "users.json").write_text(json.dumps({"users": {
        "ivanov": {"name": "Иванов", "email": "", "password": users.hash_password("пароль-1234"),
                   "home": f"{HOME}/ivanov", "admin": False, "read": {"Проекты": "tree", "": "tree"},
                   "manage": {"Снабжение": "only"}, "notify_ready": True, "source": "local"},
        # совсем старый вид: права списками
        "petrov": {"name": "Петров", "password": users.hash_password("пароль-1234"), "home": f"{HOME}/petrov",
                   "admin": False, "folders": ["Проекты"], "manages": []}}}), encoding="utf-8")
    s = upgraded(tmp_path)
    ivanov, petrov = s.access("ivanov"), s.access("petrov")
    assert ivanov.sees("Проекты") and ivanov.sees("Кадры") and not ivanov.uploads("Проекты") and not ivanov.uploads("Кадры")
    assert not ivanov.uploads(f"{HOME}/ivanov") and ivanov.sees(f"{HOME}/ivanov")
    assert ivanov.uploads("Снабжение") and ivanov.uploader             # администрирование загрузку включает
    assert petrov.sees("Проекты/Сметы") and not petrov.uploads("Проекты") and not petrov.uploader
    assert s.users["ivanov"]["upload"] == {} and s.users["petrov"]["home_upload"] is False
    s.set_access("petrov", "Проекты", upload="only")                   # и дальше права пишутся в новом виде
    saved = store(tmp_path).users
    assert saved["petrov"]["upload"] == {"Проекты": "only"} and saved["ivanov"]["home_upload"] is False


def test_key_uploads_only_where_its_owner_may(tmp_path):
    s = store(tmp_path)
    user(s, "ivanov", on={"Проекты": ("tree", None)})
    token, _ = s.issue_key("ivanov", "ключ")
    assert s.by_key(token).sees("Проекты") and not s.by_key(token).uploads("Проекты") and not s.by_key(token).uploader
    s.set_access("ivanov", "Проекты", upload="tree")
    assert s.by_key(token).uploads("Проекты/Сметы")                     # право выдали — ключ его получил сразу
