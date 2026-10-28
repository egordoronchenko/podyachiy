"""Вход под учётной записью домена в работающем сервисе: страница входа, HTTPS, автозаведение,
группы, перепроверка сессий и ключей, журнал. Каталог поддельный (tests/fake_directory.py),
сети нет. Сам модуль directory.py проверяет tests/test_directory.py."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import directory, users

from . import test_web as tw
from .fake_directory import BASE, BIND_DN, BIND_PASSWORD, PASSWORD, URLS, staffed

LOCAL_PASSWORD = tw.PASSWORD
ENV = {
    "LDAP_URL": ", ".join(URLS), "LDAP_BASE_DN": BASE, "LDAP_BIND_DN": BIND_DN,
    "LDAP_BIND_PASSWORD": BIND_PASSWORD, "LDAP_ACCESS_GROUP": "podyachiy-users",
    "LDAP_ADMIN_GROUP": f"CN=podyachiy-admins,OU=Groups,{BASE}", "LDAP_DOMAIN": "CORP",
}
WRONG = "неверный логин или пароль"


@pytest.fixture
def forest(monkeypatch):
    forest = staffed()
    monkeypatch.setattr(directory, "connect", forest.connect)
    return forest


@pytest.fixture
def office(tmp_path, forest):
    """Сервис с настроенным входом через домен. Отдаёт модуль, поддельный каталог и браузер,
    открывший сервис по HTTPS."""
    with tw.running(tmp_path, extra=ENV) as (web, _):
        yield web, forest, https(web)


def https(web):
    return TestClient(web.app, base_url="https://testserver")


def sign(client, login, password=PASSWORD):
    return client.post("/api/login", json={"login": login, "password": password})


def entered(web, login, password=PASSWORD):
    client = https(web)
    assert sign(client, login, password).status_code == 200
    return client


def events(web, kind):
    return [e for e in web.core.trail.read() if e["kind"] == kind]


def everything_written(web):
    """Всё, что сервис записал: журнал работы, журнал действий, учётные записи."""
    state = web.core.cfg.state_dir
    files = [p.read_text(encoding="utf-8") for p in state.rglob("*") if p.is_file() and p.suffix in (".json", ".jsonl")]
    return "\n".join(web.core.log.tail_from(0)[1]) + "\n".join(files)


# ---------- вход и автозаведение ----------

def test_first_login_creates_the_user_with_a_home_folder(office):
    web, forest, browser = office
    answer = sign(browser, "ivanov")
    assert answer.status_code == 200 and answer.json() == {"login": "ivanov", "name": "Иванов Иван Иванович",
                                                           "admin": False}
    me = browser.get("/api/me").json()
    assert me["login"] == "ivanov" and me["domain_account"] and me["home"] == f"{tw.HOME}/ivanov"
    assert (web.core.cfg.inbox / tw.HOME / "ivanov").is_dir()
    saved = web.core.people.users["ivanov"]
    assert saved["source"] == "domain" and saved["password"] == "" and saved["email"] == "ivanov@corp.test"
    assert saved["checked"] >= int(time.time()) - 5 and saved["read"] == {} and not saved["admin"]
    assert browser.get("/api/browse").status_code == 200
    assert [(e["by"], e["name"], e["admin"]) for e in events(web, "domain_user_new")] == [
        ("ivanov", "Иванов Иван Иванович", False)]
    assert [(e["by"], e.get("source")) for e in events(web, "login")] == [("ivanov", "domain")]
    listed = tw.enter(web, "admin", tw.ADMIN_PASSWORD).post("/api/admin/users").json()["users"]
    assert [(u["login"], u["source"], u["has_password"], bool(u["checked"])) for u in listed] == [
        ("ivanov", "domain", False, True)]


@pytest.mark.parametrize("typed", ["CORP\\Ivanov", "ivanov@corp.test", "  IVANOV "])
def test_any_form_of_the_login_enters_the_same_account(office, typed):
    web, _, browser = office
    assert sign(browser, typed).json()["login"] == "ivanov"
    assert sorted(web.core.people.users) == ["ivanov"]


def test_next_login_updates_name_and_mail_from_the_domain(office):
    web, forest, browser = office
    sign(browser, "ivanov")
    entry = forest.entries[forest.dn("ivanov")]
    entry["displayName"], entry["mail"] = ["Иванов-Петров Иван Иванович"], ["i.ivanov@corp.test"]
    assert sign(https(web), "ivanov").json()["name"] == "Иванов-Петров Иван Иванович"
    assert web.core.people.users["ivanov"]["email"] == "i.ivanov@corp.test"
    assert len(events(web, "domain_user_new")) == 1


def test_wrong_and_empty_password_and_unknown_login_look_the_same(office):
    web, forest, browser = office
    answers = [sign(browser, "ivanov", "не тот"), sign(browser, "nobody"), sign(browser, "ivanov", ""),
               sign(browser, "admin", "не тот")]
    assert {(a.status_code, a.json()["detail"]) for a in answers} == {(401, WRONG)}
    assert web.core.people.users == {} and users.COOKIE not in browser.cookies
    assert all(password for _, _, password in forest.binds)             # пустой пароль в каталог не уходил


@pytest.mark.parametrize("login, words", [
    ("smirnova", "отключена в домене"), ("zaitsev", "заблокирована в домене"),
    ("volkov", "срок действия пароля"), ("orlov", "сменить пароль"), ("kuznetsov", "не входит в группу доступа"),
])
def test_refusals_of_the_domain_are_told_in_plain_words(office, login, words):
    web, _, browser = office
    answer = sign(browser, login)
    assert answer.status_code == 401 and words in answer.json()["detail"]
    assert web.core.people.users == {}                                       # отказ никого не заводит
    failed = events(web, "login_failed")
    assert failed[-1]["by"] == login and words in failed[-1]["reason"]


def test_nested_group_member_enters(office):
    web, _, browser = office
    assert sign(browser, "sidorov").status_code == 200 and not web.core.people.users["sidorov"]["admin"]


# ---------- роль по группе ----------

def test_admin_group_gives_the_role_and_takes_it_away(office):
    web, forest, browser = office
    assert sign(browser, "petrova").json()["admin"] is True
    assert browser.get("/api/admin/status").status_code == 200
    assert events(web, "domain_user_new")[0]["admin"] is True
    forest.leave("Petrova", "podyachiy-admins")
    forest.entries[forest.dn("podyachiy-users")]["member"].append(forest.dn("Petrova"))
    assert sign(https(web), "petrova").json()["admin"] is False
    assert [(e["by"], e["admin"]) for e in events(web, "domain_role")] == [("petrova", False)]
    # и прежняя сессия, выданная администратору, больше ничего не открывает
    assert browser.get("/api/admin/status").status_code == 403


def test_role_of_a_domain_user_is_not_set_by_hand_when_the_group_rules(office):
    web, _, browser = office
    sign(browser, "ivanov")
    admin = tw.enter(web, "admin", tw.ADMIN_PASSWORD)
    refused = admin.post("/api/admin/users/save", json={"login": "ivanov", "name": "Иванов", "admin": True})
    assert refused.status_code == 400 and "группа домена" in refused.json()["detail"]
    kept = admin.post("/api/admin/users/save", json={"login": "ivanov", "name": "Другое имя", "admin": False})
    assert kept.status_code == 200 and web.core.people.users["ivanov"]["name"] == "Иванов Иван Иванович"
    with_password = admin.post("/api/admin/users/save", json={"login": "ivanov", "password": "новый-пароль-1"})
    assert with_password.status_code == 400 and web.core.people.users["ivanov"]["password"] == ""


# ---------- локальный или доменный ----------

def test_local_password_never_goes_to_the_directory(office):
    """Логин, занятый в сервисе локально, проверяется только в сервисе: ни верный, ни неверный
    пароль локального пользователя и admin в каталог не уходит."""
    web, forest, browser = office
    admin = tw.enter(web, "admin", tw.ADMIN_PASSWORD)
    admin.post("/api/admin/users/save", json={"login": "local1", "name": "Местный", "password": LOCAL_PASSWORD})
    assert sign(https(web), "local1", LOCAL_PASSWORD).status_code == 200
    for login, password in (("local1", "не тот"), ("admin", "не тот"), ("admin", tw.ADMIN_PASSWORD),
                            ("CORP\\local1", LOCAL_PASSWORD), ("local1@corp.test", LOCAL_PASSWORD),
                            ("CORP\\admin", tw.ADMIN_PASSWORD)):
        sign(https(web), login, password)
    assert forest.opened == [] and forest.binds == []
    # с именем домена локальный логин не проверяется нигде — даже с верным паролем
    assert sign(https(web), "CORP\\local1", LOCAL_PASSWORD).json()["detail"] == WRONG


def test_domain_password_is_never_compared_with_a_local_hash(office, monkeypatch):
    web, _, browser = office
    compared = []
    monkeypatch.setattr(users, "check_password", lambda password, stored: compared.append(password) or False)
    assert sign(browser, "ivanov").status_code == 200
    assert sign(https(web), "ivanov", "не тот").status_code == 401
    assert sign(https(web), "nobody", PASSWORD).status_code == 401
    assert compared == []


def test_local_account_with_a_domain_login_stays_local_until_the_admin_binds_it(office):
    web, forest, _ = office
    admin = entered(web, "admin", tw.ADMIN_PASSWORD)
    admin.post("/api/admin/users/save", json={"login": "ivanov", "name": "Иванов (местный)", "password": LOCAL_PASSWORD})
    admin.post("/api/folder", json={"dir": "Проекты"})
    admin.post("/api/access", json={"dir": "Проекты", "login": "ivanov", "read": "tree"})
    # доменный пароль к локальной записи не подходит и в домен не уходит; локальный — подходит
    assert sign(https(web), "ivanov", PASSWORD).status_code == 401 and forest.binds == []
    local = entered(web, "ivanov", LOCAL_PASSWORD)
    assert web.core.people.users["ivanov"]["source"] == "local"
    # администратор привязывает запись к домену: пароль сервиса стирается, права остаются
    bound = admin.post("/api/admin/users/domain", json={"login": "ivanov"})
    assert bound.status_code == 200
    saved = web.core.people.users["ivanov"]
    assert (saved["source"], saved["password"], saved["read"]) == ("domain", "", {"Проекты": "tree"})
    assert saved["name"] == "Иванов Иван Иванович"
    assert local.get("/api/browse").status_code == 401                  # вход по прежнему паролю погашен
    assert sign(https(web), "ivanov", LOCAL_PASSWORD).status_code == 401
    assert sign(https(web), "ivanov", PASSWORD).status_code == 200
    assert [(e["by"], e["login"]) for e in events(web, "domain_bind")] == [("admin", "ivanov")]
    # привязать можно только того, кого домен подтверждает
    admin.post("/api/admin/users/save", json={"login": "kuznetsov", "password": LOCAL_PASSWORD})
    admin.post("/api/admin/users/save", json={"login": "stranger", "password": LOCAL_PASSWORD})
    assert "группу доступа" in admin.post("/api/admin/users/domain", json={"login": "kuznetsov"}).json()["detail"]
    assert admin.post("/api/admin/users/domain", json={"login": "stranger"}).status_code == 400
    assert admin.post("/api/admin/users/domain", json={"login": "ivanov"}).status_code == 404   # уже доменная
    assert web.core.people.users["kuznetsov"]["source"] == "local"


def test_principal_name_that_resolves_to_a_local_login_is_a_conflict(office):
    """Человек вошёл по user@domain, а его короткий логин в сервисе занят локальной записью."""
    web, forest, browser = office
    forest.user("a.sokolova", "Соколова Алина Олеговна", ["podyachiy-users"], upn="alina@corp.test")
    tw.enter(web, "admin", tw.ADMIN_PASSWORD).post(
        "/api/admin/users/save", json={"login": "a.sokolova", "name": "Местная", "password": LOCAL_PASSWORD})
    answer = sign(browser, "alina@corp.test")
    assert answer.status_code == 403 and "локальная учётная запись" in answer.json()["detail"]
    assert web.core.people.users["a.sokolova"]["source"] == "local" and users.COOKIE not in browser.cookies


def test_domain_login_that_does_not_fit_the_service_is_refused(office):
    web, forest, browser = office
    forest.user("Иванова", "Иванова Ольга", ["podyachiy-users"])
    answer = sign(browser, "Иванова")
    assert answer.status_code == 403 and "латинские буквы" in answer.json()["detail"] and web.core.people.users == {}


# ---------- только по HTTPS ----------

def test_over_http_the_domain_login_is_refused_and_the_password_goes_nowhere(office):
    web, forest, _ = office
    plain = TestClient(web.app)                                         # http://testserver
    me = plain.get("/api/me").json()["domain"]
    assert me["on"] is False and me["blocked"] is True
    answer = sign(plain, "ivanov")
    assert answer.status_code == 401 and "https://" in answer.json()["detail"] and "HTTPS" in answer.json()["detail"]
    assert forest.opened == [] and web.core.people.users == {}
    # отказ слово в слово тот же для локального логина: по тексту не узнать, какой логин локальный
    assert sign(plain, "admin", "не тот").json()["detail"] == answer.json()["detail"]
    assert sign(plain, "admin", tw.ADMIN_PASSWORD).status_code == 200  # локальный вход по HTTP работает, как раньше
    assert "HTTP" in events(web, "login_failed")[0]["reason"]


def test_https_localhost_and_a_trusted_proxy_are_accepted(office):
    web, forest, browser = office
    assert browser.get("/api/me").json()["domain"] == {"on": True, "blocked": False, "name": "CORP", "https": ""}
    assert sign(browser, "ivanov").status_code == 200
    assert "secure" in sign(https(web), "ivanov").headers["set-cookie"].lower()
    # с самого сервера
    assert sign(TestClient(web.app, client=("127.0.0.1", 50000)), "ivanov").status_code == 200
    assert sign(TestClient(web.app, client=("10.0.0.7", 50000)), "ivanov").status_code == 401
    # за прокси: uvicorn берёт схему из X-Forwarded-Proto только от доверенного адреса
    forwarded = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "10.0.0.7"}
    trusted = TestClient(ProxyHeadersMiddleware(web.app, trusted_hosts="172.18.0.5"), client=("172.18.0.5", 40000))
    assert trusted.post("/api/login", json={"login": "ivanov", "password": PASSWORD}, headers=forwarded).status_code == 200
    stranger = TestClient(ProxyHeadersMiddleware(web.app, trusted_hosts="172.18.0.5"), client=("10.0.0.7", 40000))
    lied = stranger.post("/api/login", json={"login": "sidorov", "password": PASSWORD}, headers=forwarded)
    assert lied.status_code == 401 and "sidorov" not in web.core.people.users     # заголовок от постороннего не в счёт


def test_stand_exception_allows_http_and_is_loud_about_it(tmp_path, forest):
    with tw.running(tmp_path, extra={**ENV, "LDAP_ALLOW_HTTP": "1"}) as (web, client):
        assert sign(client, "ivanov").status_code == 200
        assert any("НЕБЕЗОПАСНО" in line and "LDAP_ALLOW_HTTP" in line for line in web.core.log.tail_from(0)[1])
        view = tw.enter(web, "admin", tw.ADMIN_PASSWORD).get("/api/admin/domain").json()
        assert view["on"] and len(view["warnings"]) == 1 and view["params"]["allow_http"]


# ---------- что выключает вход через домен ----------

def test_personal_mode_has_no_domain_login(tmp_path, forest):
    with tw.running(tmp_path, mode="personal", extra=ENV) as (web, client):
        assert client.get("/api/me").json()["domain"] == {"on": False}
        assert sign(https(web), "ivanov").status_code == 401 and forest.opened == []
        assert client.get("/api/admin/domain").status_code == 404
        assert client.post("/api/admin/domain/check", json={}).status_code == 404


def test_unencrypted_controller_address_switches_the_login_off(tmp_path, forest):
    with tw.running(tmp_path, extra={**ENV, "LDAP_URL": "ldap://dc1.corp.test:389"}) as (web, _):
        assert sign(https(web), "ivanov").status_code == 401 and forest.opened == []
        view = tw.enter(web, "admin", tw.ADMIN_PASSWORD).get("/api/admin/domain").json()
        assert not view["on"] and "без шифрования" in view["why_off"]
        assert any("вход через домен выключен" in line for line in web.core.log.tail_from(0)[1])


def test_not_configured_service_behaves_as_before(tmp_path, forest):
    with tw.running(tmp_path) as (web, client):
        assert client.get("/api/me").json()["domain"]["on"] is False
        assert sign(https(web), "ivanov").json()["detail"] == WRONG and forest.opened == []
        view = tw.enter(web, "admin", tw.ADMIN_PASSWORD).get("/api/admin/domain").json()
        assert not view["on"] and not view["configured"] and "LDAP_URL" in view["why_off"]


# ---------- контроллеры ----------

def test_first_controller_down_login_goes_through_the_second(office):
    web, forest, browser = office
    forest.dead.add(URLS[0])
    assert sign(browser, "ivanov").status_code == 200 and set(forest.opened) == {URLS[1]}


def test_all_controllers_down_local_admin_still_enters(office):
    web, forest, browser = office
    forest.dead.update(URLS)
    answer = sign(browser, "ivanov")
    assert answer.status_code == 503 and "домен сейчас не отвечает" in answer.json()["detail"]
    assert sign(https(web), "admin", tw.ADMIN_PASSWORD).status_code == 200
    assert any("домен недоступен" in line for line in web.core.log.tail_from(0)[1])


# ---------- перебор ----------

def test_failed_domain_logins_are_limited_per_login(office):
    """В домен уходит не больше четырёх неверных паролей на логин за 15 минут: сервис не
    должен блокировать людям учётные записи домена."""
    web, forest, browser = office
    for _ in range(4):
        assert sign(browser, "ivanov", "не тот").status_code == 401
    tried = len(forest.binds)
    held = sign(browser, "CORP\\Ivanov", "не тот")
    assert held.status_code == 429 and "15 минут" in held.json()["detail"]
    assert sign(browser, "ivanov").status_code == 429                  # и верный пароль ждёт: иначе это был бы оракул
    assert len(forest.binds) == tried
    assert sign(browser, "sidorov").status_code == 200                 # другого человека это не касается
    assert sign(browser, "admin", tw.ADMIN_PASSWORD).status_code == 200


def test_failed_domain_logins_are_limited_per_address(office, monkeypatch):
    web, forest, browser = office
    monkeypatch.setattr(web.login, "domain_fails_by_address", web.core.signup.Throttle(3, 900))
    for n in range(3):
        assert sign(browser, f"user{n}", "не тот").status_code == 401
    assert sign(browser, "ivanov").status_code == 429


# ---------- перепроверка сессии и ключа ----------

def test_session_is_rechecked_and_a_removed_person_loses_access(office, monkeypatch):
    web, forest, browser = office
    sign(browser, "ivanov")
    searches = len(forest.filters)
    assert browser.get("/api/browse").status_code == 200 and len(forest.filters) == searches   # недавно подтверждён
    forest.leave("ivanov", "podyachiy-users")
    assert browser.get("/api/browse").status_code == 200               # до перепроверки домен не спрашивают
    monkeypatch.setattr(web.access, "RECHECK_SEC", 0)
    refused = browser.get("/api/browse")
    assert refused.status_code == 401 and "группу доступа" in refused.json()["detail"]
    me = browser.get("/api/me").json()
    assert me["login"] is None and "группу доступа" in me["notice"]
    assert web.core.people.users["ivanov"]["checked"] == 0
    assert [e["by"] for e in events(web, "domain_recheck_denied")] == ["ivanov"]       # один раз, не на каждый запрос
    # вернули в группу — человек входит заново, и прежняя сессия снова действует
    forest.entries[forest.dn("podyachiy-users")]["member"].append(forest.dn("ivanov"))
    assert sign(https(web), "ivanov").status_code == 200 and browser.get("/api/browse").status_code == 200


def test_disabled_account_and_lost_admin_role_are_seen_at_recheck(office, monkeypatch):
    web, forest, _ = office
    ivanov, petrova = entered(web, "ivanov"), entered(web, "petrova")
    monkeypatch.setattr(web.access, "RECHECK_SEC", 0)
    forest.entries[forest.dn("ivanov")]["userAccountControl"] = ["514"]
    assert "отключена" in ivanov.get("/api/browse").json()["detail"]
    forest.leave("Petrova", "podyachiy-admins")
    forest.entries[forest.dn("podyachiy-users")]["member"].append(forest.dn("Petrova"))
    assert petrova.get("/api/admin/status").status_code == 403 and petrova.get("/api/browse").status_code == 200
    assert events(web, "domain_role")[-1]["admin"] is False


def test_access_key_of_a_domain_user_stops_with_him(office, monkeypatch):
    web, forest, browser = office
    sign(browser, "ivanov")
    token = browser.post("/api/keys", json={"name": "Cursor", "days": 365}).json()["key"]
    robot = TestClient(web.app, headers={"Authorization": f"Bearer {token}"})    # ключ — и по HTTP, как раньше
    assert robot.get("/api/v1/whoami").json()["login"] == "ivanov"
    monkeypatch.setattr(web.access, "RECHECK_SEC", 0)
    assert robot.get("/api/v1/whoami").status_code == 200
    forest.entries[forest.dn("ivanov")]["userAccountControl"] = ["514"]
    assert robot.get("/api/v1/whoami").status_code == 401
    assert robot.get("/api/v1/records").status_code == 401


def test_controller_outage_keeps_people_for_a_limited_time(office, monkeypatch):
    web, forest, browser = office
    sign(browser, "ivanov")
    monkeypatch.setattr(web.access, "RECHECK_SEC", 0)
    monkeypatch.setattr(web.access, "RETRY_SEC", 0)
    forest.dead.update(URLS)
    assert browser.get("/api/browse").status_code == 200               # домен молчит — верим последнему подтверждению
    with web.core.base.write() as con:
        con.execute("UPDATE users SET checked = ? WHERE login = 'ivanov'", (int(time.time()) - web.access.GRACE_SEC - 60,))
    late = browser.get("/api/browse")
    assert late.status_code == 401 and "домен недоступен" in late.json()["detail"]
    assert tw.enter(web, "admin", tw.ADMIN_PASSWORD).get("/api/browse").status_code == 200
    forest.dead.clear()
    assert browser.get("/api/browse").status_code == 200               # домен ответил — человек снова работает
    assert web.core.people.users["ivanov"]["checked"] >= int(time.time()) - 5
    assert any("домен снова отвечает" in line for line in web.core.log.tail_from(0)[1])


def test_outage_is_not_asked_again_on_every_request(office, monkeypatch):
    web, forest, browser = office
    sign(browser, "ivanov")
    web.access.domain_seen.clear()                                             # как после перезапуска сервиса
    forest.dead.update(URLS)
    assert browser.get("/api/browse").status_code == 200
    forest.dead.clear()
    forest.opened.clear()
    assert browser.get("/api/browse").status_code == 200 and forest.opened == []     # повтор — не раньше RETRY_SEC


def test_restart_asks_the_domain_again(tmp_path, forest):
    with tw.running(tmp_path, extra=ENV) as (web, _):
        browser = entered(web, "ivanov")
        cookie = browser.cookies[users.COOKIE]
    forest.leave("ivanov", "podyachiy-users")
    with tw.running(tmp_path, extra=ENV) as (web, _):
        again = https(web)
        again.cookies.set(users.COOKIE, cookie)
        assert again.get("/api/browse").status_code == 401


def test_domain_login_switched_off_ends_domain_sessions(tmp_path, forest):
    with tw.running(tmp_path, extra=ENV) as (web, _):
        cookie = entered(web, "ivanov").cookies[users.COOKIE]
    with tw.running(tmp_path) as (web, _):                              # LDAP_URL из .env убрали
        again = https(web)
        again.cookies.set(users.COOKIE, cookie)
        refused = again.get("/api/browse")
        assert refused.status_code == 401 and "выключен" in refused.json()["detail"]


# ---------- то, что доменной записи не положено ----------

def test_basic_auth_with_a_domain_password_is_refused(office):
    web, forest, browser = office
    sign(browser, "ivanov")
    binds = len(forest.binds)
    for login in ("ivanov", "CORP\\ivanov", "sidorov"):
        answer = https(web).get("/api/v1/records", auth=(login, PASSWORD))
        assert answer.status_code == 401 and "ключом доступа" in answer.json()["detail"]
    assert len(forest.binds) == binds                                   # в домен такой запрос не ходит вовсе
    assert https(web).get("/api/v1/records", auth=("admin", tw.ADMIN_PASSWORD)).status_code == 200


def test_domain_user_has_no_password_in_the_service(tmp_path, forest):
    mail_on = {"SMTP_HOST": "127.0.0.1", "SMTP_PORT": "9", "SMTP_FROM": "podyachiy@corp.test",
               "PUBLIC_URL": "https://podyachiy.corp.test", "SMTP_SECURITY": "none"}
    with tw.running(tmp_path, extra={**ENV, **mail_on}) as (web, _):
        browser = entered(web, "ivanov")
        changed = browser.post("/api/password", json={"old": PASSWORD, "new": "новый-пароль-1"})
        assert changed.status_code == 400 and "в домене" in changed.json()["detail"]
        admin = tw.enter(web, "admin", tw.ADMIN_PASSWORD)
        link = admin.post("/api/admin/users/link", json={"login": "ivanov"})
        assert link.status_code == 400 and "в домене" in link.json()["detail"]
        assert https(web).post("/api/forgot", json={"login": "ivanov"}).status_code == 200      # ответ как всем
        assert "link" not in web.core.people.users["ivanov"] and events(web, "link_sent") == []
        assert browser.get("/api/me").json()["domain"]["https"] == "https://podyachiy.corp.test"
        # убрали из сервиса — при следующем входе заведётся снова
        assert admin.post("/api/admin/users/delete", json={"login": "ivanov"}).status_code == 200
        assert browser.get("/api/browse").status_code == 401
        assert sign(https(web), "ivanov").status_code == 200 and len(events(web, "domain_user_new")) == 2


def test_users_file_of_the_previous_kind_is_read_as_local(tmp_path, forest):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "users.json").write_text(json.dumps({"users": {"petrov": {
        "name": "Петров", "email": "", "password": users.hash_password(LOCAL_PASSWORD), "home": f"{tw.HOME}/petrov",
        "admin": False, "read": {}, "manage": {}, "notify_ready": True}}}), encoding="utf-8")
    with tw.running(tmp_path, extra=ENV) as (web, _):
        assert web.core.people.users["petrov"]["source"] == "local"
        assert entered(web, "petrov", LOCAL_PASSWORD).get("/api/me").json()["domain_account"] is False


# ---------- страница администратора ----------

def test_admin_sees_the_state_and_parameters_without_the_password(office):
    web, _, _ = office
    admin = entered(web, "admin", tw.ADMIN_PASSWORD)
    view = admin.get("/api/admin/domain").json()
    assert view["on"] and view["why_off"] == "" and view["secure"] and view["warnings"] == []
    assert view["params"]["urls"] == list(URLS) and view["params"]["bind_dn"] == BIND_DN
    assert view["params"]["bind_password"] is True and view["params"]["domain"] == "CORP"
    assert view["recheck_min"] == 15 and view["grace_hours"] == 4 and view["fails"] == {"limit": 4, "minutes": 15}
    assert BIND_PASSWORD not in admin.get("/api/admin/domain").text
    assert tw.enter(web, "admin", tw.ADMIN_PASSWORD).get("/api/admin/domain").json()["secure"] is False     # по HTTP


def test_connection_check_reports_steps_and_creates_nobody(office):
    web, forest, _ = office
    admin = entered(web, "admin", tw.ADMIN_PASSWORD)
    report = admin.post("/api/admin/domain/check", json={}).json()
    assert report["ok"] and [s["title"] for s in report["steps"]][-3:] == [
        "База поиска", "Группа доступа", "Группа администраторов сервиса"]
    person = admin.post("/api/admin/domain/check", json={"login": "CORP\\ivanov", "password": PASSWORD}).json()
    assert person["ok"] and "Иванов Иван Иванович" in person["steps"][-1]["text"]
    assert web.core.people.users == {}                                       # проверка никого не заводит
    wrong = admin.post("/api/admin/domain/check", json={"login": "ivanov", "password": "не-тот-пароль-Я1"}).json()
    assert not wrong["ok"] and WRONG in wrong["steps"][-1]["text"]
    forest.dead.update(URLS)
    down = admin.post("/api/admin/domain/check", json={}).json()
    assert not down["ok"] and all(s["hint"] for s in down["steps"] if s["ok"] is False)
    assert [(e["by"], e["ok"], e.get("login")) for e in events(web, "domain_check")] == [
        ("admin", True, None), ("admin", True, "ivanov"), ("admin", False, "ivanov"), ("admin", False, None)]


# ---------- пароли нигде не остаются ----------

def test_passwords_never_reach_the_log_the_journal_or_the_answers(office, monkeypatch):
    web, forest, browser = office
    typed_wrong, checked_wrong = "неверный-пароль-Ю3", "проверочный-пароль-Э2"
    admin = entered(web, "admin", tw.ADMIN_PASSWORD)
    answers = [sign(browser, "ivanov"), sign(https(web), "ivanov", typed_wrong), sign(https(web), "smirnova"),
               sign(https(web), "kuznetsov"), sign(TestClient(web.app), "sidorov"),
               admin.post("/api/admin/domain/check", json={"login": "ivanov", "password": PASSWORD}),
               admin.post("/api/admin/domain/check", json={"login": "ivanov", "password": checked_wrong}),
               https(web).get("/api/v1/records", auth=("ivanov", PASSWORD)),
               admin.get("/api/admin/domain"), admin.post("/api/admin/users"), admin.get("/api/admin/journal")]
    forest.passwords[BIND_DN] = "сменили"                               # служебную запись контроллер больше не принимает
    answers += [sign(https(web), "sidorov"), admin.post("/api/admin/domain/check", json={})]
    forest.dead.update(URLS)
    monkeypatch.setattr(web.access, "RECHECK_SEC", 0)
    answers += [sign(https(web), "sidorov"), browser.get("/api/browse"), admin.get("/api/admin/status")]
    seen = everything_written(web) + "\n".join(a.text for a in answers)
    assert "ivanov" in seen and "вход через домен" in seen              # журнал и лог не пусты
    for secret in (PASSWORD, BIND_PASSWORD, typed_wrong, checked_wrong):
        assert secret not in seen
