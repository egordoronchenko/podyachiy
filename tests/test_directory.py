"""Вход через домен, сам модуль app/directory.py — без сети: вместо соединения с контроллером
подставлен поддельный каталог (tests/fake_directory.py). Как вход встроен в сервис — адреса,
HTTPS, перепроверка, журнал — в tests/test_domain_login.py."""
import types

import pytest

from app import directory

from .fake_directory import BASE, BIND_DN, BIND_PASSWORD, PASSWORD, URLS, staffed


def make_cfg(tmp_path, **changed):
    base = dict(ldap_urls=URLS, ldap_base_dn=BASE, ldap_bind_dn=BIND_DN, ldap_bind_password=BIND_PASSWORD,
                ldap_user_filter="", ldap_access_group="podyachiy-users",
                ldap_admin_group=f"CN=podyachiy-admins,OU=Groups,{BASE}", ldap_ca_file="", ldap_tls_verify=True,
                ldap_starttls=False, ldap_allow_http=False, ldap_domain="CORP", state_dir=tmp_path)
    base.update(changed)
    return types.SimpleNamespace(**base)


@pytest.fixture
def forest(monkeypatch):
    forest = staffed()
    monkeypatch.setattr(directory, "connect", forest.connect)
    return forest


@pytest.fixture
def domain(forest, tmp_path):
    lines = []
    found = directory.Directory(make_cfg(tmp_path), lines.append)
    found.lines = lines
    return found


def refused(domain, login, password=PASSWORD):
    with pytest.raises(directory.Denied) as caught:
        domain.authenticate(login, password)
    return caught.value.reason


# ---------- экранирование и вид логина ----------

def test_filter_values_are_escaped_by_rfc_4515():
    assert directory.esc("ivanov") == "ivanov"
    assert directory.esc("*)(uid=*") == "\\2a\\29\\28uid=\\2a"
    assert directory.esc("a\\b") == "a\\5cb"
    assert directory.esc("a\0b") == "a\\00b"
    assert directory.esc("Иванов (отдел)") == "Иванов \\28отдел\\29"


@pytest.mark.parametrize("typed", ["ivanov", "IVANOV", " ivanov ", "CORP\\ivanov", "corp\\Ivanov",
                                   "ivanov@corp.test", "Ivanov@CORP.TEST"])
def test_three_forms_of_login_are_one_person(domain, typed):
    assert directory.short_login(typed) == "ivanov"
    person = domain.authenticate(typed, PASSWORD)
    assert (person.login, person.name, person.email) == ("ivanov", "Иванов Иван Иванович", "ivanov@corp.test")


def test_login_with_a_principal_name_that_differs_from_the_short_login(domain, forest):
    forest.user("a.sokolova", "Соколова Алина Олеговна", ["podyachiy-users"], upn="alina@corp.test")
    assert domain.authenticate("alina@corp.test", PASSWORD).login == "a.sokolova"


@pytest.mark.parametrize("typed", ["*)(uid=*", "*", "ivan*", "ivanov)(objectClass=*", "(ivanov)", "ivanov\0", "\0",
                                   "ivanov)(|(sAMAccountName=*", "*@corp.test", "CORP\\*", "admin)(&)"])
def test_injection_into_the_login_finds_nobody(domain, forest, typed):
    """Звёздочка и скобки в логине не становятся частью фильтра: такой «логин» никого не находит,
    а фильтр остаётся правильным (поддельный каталог разбирает его целиком)."""
    assert refused(domain, typed) == "invalid"
    assert forest.filters and all("(sAMAccountName=*)" not in flt for flt in forest.filters)
    # пароль ушёл только на привязку-пустышку к несуществующей записи, не к настоящему человеку
    assert all(dn == BIND_DN or dn not in forest.entries for _, dn, _ in forest.binds)


# ---------- пароль ----------

def test_wrong_password_and_unknown_login_get_the_same_refusal(domain, forest):
    assert refused(domain, "ivanov", "не тот пароль") == "invalid"
    assert refused(domain, "nobody") == "invalid"
    # и для того, и для другого сервис сделал поиск и привязку: по числу обращений их не отличить
    binds = [dn for _, dn, _ in forest.binds if dn != BIND_DN]
    assert len(binds) == 2 and binds[0] == forest.dn("ivanov") and binds[1] not in forest.entries


def test_empty_password_never_reaches_the_directory(domain, forest):
    """Привязка с пустым паролем — анонимная, контроллер отвечает на неё «успех»: пустой пароль
    отклоняется раньше, чем сервис откроет соединение."""
    for login in ("ivanov", "Petrova", "nobody", ""):
        assert refused(domain, login, "") == "invalid"
    assert forest.opened == [] and forest.binds == []
    assert refused(domain, "", PASSWORD) == "invalid" and forest.opened == []


def test_the_person_password_goes_only_into_his_own_bind(domain, forest):
    domain.authenticate("ivanov", PASSWORD)
    assert [(dn, password) for _, dn, password in forest.binds] == [(BIND_DN, BIND_PASSWORD),
                                                                    (forest.dn("ivanov"), PASSWORD)]
    assert all(PASSWORD not in flt and BIND_PASSWORD not in flt for flt in forest.filters)


# ---------- состояние учётной записи ----------

@pytest.mark.parametrize("login, reason, words", [
    ("smirnova", "disabled", "отключена"),
    ("zaitsev", "locked", "заблокирована"),
    ("volkov", "password_expired", "срок действия пароля"),
    ("orlov", "must_change", "сменить пароль"),
    ("belov", "account_expired", "срок действия учётной записи"),
])
def test_account_state_is_told_in_plain_words(domain, login, reason, words):
    with pytest.raises(directory.Denied) as caught:
        domain.authenticate(login, PASSWORD)
    assert caught.value.reason == reason and words in caught.value.text
    assert PASSWORD not in caught.value.text


def test_account_state_is_not_told_to_someone_without_the_password(domain):
    """Отключена запись или просрочен пароль — контроллер говорит только назвавшему верный пароль."""
    for login in ("smirnova", "volkov", "orlov", "belov"):
        assert refused(domain, login, "не тот пароль") == "invalid"


@pytest.mark.parametrize("message, reason", [
    ("80090308: LdapErr: DSID-0C090447, comment: AcceptSecurityContext error, data 52e, v3839", "invalid"),
    ("80090308: LdapErr: DSID-0C090447, comment: AcceptSecurityContext error, data 525, v3839", "invalid"),
    ("80090308: LdapErr: DSID-0C090447, comment: AcceptSecurityContext error, data 530, v3839", "hours"),
    ("80090308: LdapErr: DSID-0C090447, comment: AcceptSecurityContext error, data 531, v3839", "workstation"),
    ("80090308: LdapErr: DSID-0C090447, comment: AcceptSecurityContext error, data 532, v3839", "password_expired"),
    ("80090308: LdapErr: DSID-0C090447, comment: AcceptSecurityContext error, data 533, v3839", "disabled"),
    ("80090308: LdapErr: DSID-0C090447, comment: AcceptSecurityContext error, data 701, v3839", "account_expired"),
    ("80090308: LdapErr: DSID-0C090447, comment: AcceptSecurityContext error, data 773, v3839", "must_change"),
    ("80090308: LdapErr: DSID-0C090447, comment: AcceptSecurityContext error, data 775, v3839", "locked"),
    ("80090308: LdapErr: DSID-0C0903A9, comment: AcceptSecurityContext error, data 52E, v1db1", "invalid"),
    ("80090308: LdapErr: DSID-0C090447, comment: AcceptSecurityContext error, data 999, v3839", "invalid"),
    ("", "invalid"), ("Invalid credentials", "invalid"),
])
def test_bind_subcodes_of_active_directory(message, reason):
    assert directory.subcode(message) == reason


# ---------- группы ----------

def test_access_group_admits_direct_and_nested_members_only(domain):
    assert domain.authenticate("ivanov", PASSWORD).admin is False
    assert domain.authenticate("sidorov", PASSWORD).admin is False          # через вложенную группу
    assert refused(domain, "kuznetsov") == "no_access"


def test_admin_group_gives_the_role_and_counts_for_access_through_nesting(domain, forest):
    person = domain.authenticate("petrova", PASSWORD)
    assert person.admin is True and person.login == "petrova"              # в домене логин записан с заглавной
    forest.leave("Petrova", "podyachiy-admins")
    assert refused(domain, "petrova") == "no_access"                        # в доступ она входила только через неё
    forest.entries[forest.dn("podyachiy-users")]["member"].append(forest.dn("Petrova"))
    assert domain.authenticate("petrova", PASSWORD).admin is False


def test_without_groups_anyone_from_the_base_enters_and_role_is_left_alone(forest, tmp_path):
    open_house = directory.Directory(make_cfg(tmp_path, ldap_access_group="", ldap_admin_group=""))
    assert open_house.authenticate("kuznetsov", PASSWORD).admin is None
    with pytest.raises(directory.Denied):
        open_house.authenticate("smirnova", PASSWORD)                        # отключённый не входит и без групп


def test_group_by_name_is_resolved_and_a_missing_group_closes_the_door(forest, tmp_path):
    by_name = directory.Directory(make_cfg(tmp_path, ldap_admin_group="podyachiy-admins"))
    assert by_name.authenticate("petrova", PASSWORD).admin is True
    lost = directory.Directory(make_cfg(tmp_path, ldap_access_group="нет-такой-группы"))
    with pytest.raises(directory.Unavailable, match="нет-такой-группы"):
        lost.authenticate("ivanov", PASSWORD)


# ---------- контроллеры ----------

def test_second_controller_answers_when_the_first_is_down(domain, forest):
    forest.dead.add(URLS[0])
    assert domain.authenticate("ivanov", PASSWORD).login == "ivanov"
    assert set(forest.opened) == {URLS[1]}
    assert any(URLS[1] in line and URLS[0] in line for line in domain.lines)
    forest.opened.clear()
    domain.authenticate("ivanov", PASSWORD)                 # дальше сервис начинает с ответившего
    assert set(forest.opened) == {URLS[1]}


def test_all_controllers_down_is_unavailable_not_a_refusal(domain, forest):
    forest.dead.update(URLS)
    with pytest.raises(directory.Unavailable, match="ни один контроллер"):
        domain.authenticate("ivanov", PASSWORD)
    with pytest.raises(directory.Unavailable):
        domain.check("ivanov")


def test_rejected_service_account_is_unavailable_and_keeps_its_password_out(domain, forest):
    forest.passwords[BIND_DN] = "другой"
    with pytest.raises(directory.Unavailable) as caught:
        domain.authenticate("ivanov", PASSWORD)
    assert "служебную учётную запись" in str(caught.value)
    assert BIND_PASSWORD not in str(caught.value) and PASSWORD not in str(caught.value)


# ---------- перепроверка ----------

def test_check_confirms_without_a_password_and_sees_changes(domain, forest):
    assert domain.check("ivanov").name == "Иванов Иван Иванович"
    assert all(dn == BIND_DN for _, dn, _ in forest.binds)              # привязки от имени человека нет
    forest.leave("ivanov", "podyachiy-users")
    with pytest.raises(directory.Denied) as caught:
        domain.check("ivanov")
    assert caught.value.reason == "no_access"
    forest.entries[forest.dn("sidorov")]["userAccountControl"] = ["514"]
    with pytest.raises(directory.Denied) as caught:
        domain.check("sidorov")
    assert caught.value.reason == "disabled"
    # срок учётной записи вышел час назад (accountExpires — сотни наносекунд от 1601 года)
    gone_at = int((directory.time.time() - 3600 + directory.FILETIME_EPOCH_SEC) * 10_000_000)
    for value, reason in ((str(gone_at), "account_expired"), ("0", None), (str(directory.NEVER), None)):
        forest.entries[forest.dn("Petrova")]["accountExpires"] = [value]
        if reason:
            with pytest.raises(directory.Denied) as caught:
                domain.check("petrova")
            assert caught.value.reason == reason
        else:
            assert domain.check("petrova").admin is True
    del forest.entries[forest.dn("kuznetsov")]
    with pytest.raises(directory.Denied) as caught:
        domain.check("kuznetsov")
    assert caught.value.reason == "gone"


# ---------- настройки ----------

def test_unencrypted_controller_address_is_refused_by_the_settings(tmp_path):
    plain = ("ldap://dc1.corp.test:389",)
    assert "без шифрования" in directory.Directory(make_cfg(tmp_path, ldap_urls=plain)).problem()
    assert directory.Directory(make_cfg(tmp_path, ldap_urls=plain, ldap_starttls=True)).problem() == ""
    stand = directory.Directory(make_cfg(tmp_path, ldap_urls=plain, ldap_allow_http=True))
    assert stand.problem() == "" and len(stand.warnings()) == 2            # и HTTP, и открытый канал до контроллера
    assert "ldaps://" in directory.Directory(make_cfg(tmp_path, ldap_urls=("dc1.corp.test",))).problem()
    for missing in ("ldap_urls", "ldap_base_dn", "ldap_bind_dn", "ldap_bind_password"):
        assert directory.Directory(make_cfg(tmp_path, **{missing: ()})).problem()
    safe = directory.Directory(make_cfg(tmp_path))
    assert safe.problem() == "" and safe.warnings() == []
    assert "не проверяется" in directory.Directory(make_cfg(tmp_path, ldap_tls_verify=False)).warnings()[0]


def test_root_certificate_is_taken_from_the_setting_or_the_state_folder(tmp_path):
    assert directory.ca_path(make_cfg(tmp_path)) == ""                     # системные центры сертификации
    (tmp_path / directory.CA_FILE).write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    assert directory.ca_path(make_cfg(tmp_path)) == str(tmp_path / directory.CA_FILE)
    assert directory.ca_path(make_cfg(tmp_path, ldap_ca_file="/certs/corp-root.pem")) == "/certs/corp-root.pem"


def test_real_connection_is_built_with_certificate_check_and_timeouts(tmp_path, monkeypatch):
    """Что уходит в ldap3: проверка сертификата включена, корневой сертификат — из папки состояния,
    таймауты заданы, ссылки на другие серверы не разыменовываются."""
    seen = {}

    class Conn:
        def __init__(self, server, **kw):
            seen.update(kw, server=server)

        def open(self):
            raise directory.LDAPException("нет сети")

        def unbind(self):
            pass

    monkeypatch.setattr(directory.ldap3, "Connection", Conn)
    (tmp_path / directory.CA_FILE).write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    with pytest.raises(directory.Unreachable, match="нет сети"):
        directory.Link(URLS[0], make_cfg(tmp_path))
    server = seen["server"]
    assert server.ssl and server.port == 636 and server.connect_timeout == directory.CONNECT_SEC
    assert server.tls.validate == directory.ssl.CERT_REQUIRED
    assert server.tls.ca_certs_file == str(tmp_path / directory.CA_FILE)
    assert seen["receive_timeout"] == directory.REPLY_SEC and seen["auto_referrals"] is False
    with pytest.raises(directory.Unreachable):
        directory.Link(URLS[0], make_cfg(tmp_path, ldap_tls_verify=False))
    assert seen["server"].tls.validate == directory.ssl.CERT_NONE


# ---------- проверка подключения ----------

def test_probe_reports_every_step(domain, forest):
    steps = domain.probe()
    titles = [s["title"] for s in steps]
    assert titles == ["Настройки", f"Соединение с {URLS[0]}", f"Служебная учётная запись на {URLS[0]}",
                      f"Соединение с {URLS[1]}", f"Служебная учётная запись на {URLS[1]}",
                      "База поиска", "Группа доступа", "Группа администраторов сервиса"]
    assert all(s["ok"] for s in steps)
    by = {s["title"]: s["text"] for s in steps}
    assert "CN=dc1.corp.test" in by[f"Соединение с {URLS[0]}"] and "CN=Corp Root CA" in by[f"Соединение с {URLS[0]}"]
    assert "01.01.2031" in by[f"Соединение с {URLS[0]}"]
    assert "учётных записей людей — 9" in by["База поиска"]
    assert by["Группа доступа"].endswith("— 8")                             # с вложенными группами
    assert by["Группа администраторов сервиса"].endswith("— 1")


def test_probe_names_the_failed_step_and_what_to_do(forest, tmp_path):
    forest.dead.add(URLS[0])
    steps = directory.Directory(make_cfg(tmp_path, ldap_access_group="нет-такой")).probe()
    failed = {s["title"]: s for s in steps if s["ok"] is False}
    assert set(failed) == {f"Соединение с {URLS[0]}", "Группа доступа"}
    assert "брандмауэр" in failed[f"Соединение с {URLS[0]}"]["hint"] and "нет-такой" in failed["Группа доступа"]["text"]
    forest.dead.clear()
    forest.passwords[BIND_DN] = "другой"
    steps = directory.Directory(make_cfg(tmp_path)).probe()
    assert [s["ok"] for s in steps] == [True, True, False, True, False]     # дальше служебной записи не пошло
    assert "LDAP_BIND_PASSWORD" in steps[2]["hint"]
    steps = directory.Directory(make_cfg(tmp_path, ldap_base_dn="DC=нет")).probe()
    assert steps[0]["ok"] and steps[1]["ok"]
    steps = directory.Directory(make_cfg(tmp_path, ldap_urls=())).probe()
    assert len(steps) == 1 and steps[0]["ok"] is False and "LDAP_URL" in steps[0]["text"]


def test_probe_of_a_person_says_what_would_happen_at_login(domain):
    def last(login, password=PASSWORD):
        return domain.probe(login, password)[-1]

    fine = last("CORP\\petrova")
    assert fine["ok"] and "Петрова Анна Сергеевна" in fine["text"] and "администратор сервиса" in fine["text"]
    assert last("ivanov", "не тот")["ok"] is False and "неверный логин или пароль" in last("ivanov", "не тот")["text"]
    assert "отключена" in last("smirnova")["text"] and "группу доступа" in last("kuznetsov")["text"]
    assert "не найден" in last("nobody")["text"] and "не введён" in last("ivanov", "")["text"]


def test_passwords_are_nowhere_in_the_probe_report_or_the_log(domain, forest):
    reports = [domain.probe("ivanov", PASSWORD), domain.probe("ivanov", "не-тот-пароль-Я1"),
               domain.probe("smirnova", PASSWORD), domain.probe("nobody", PASSWORD)]
    forest.passwords[BIND_DN] = "другой"
    reports.append(domain.probe("ivanov", PASSWORD))
    text = repr(reports) + repr(domain.lines)
    for secret in (PASSWORD, BIND_PASSWORD, "не-тот-пароль-Я1"):
        assert secret not in text
