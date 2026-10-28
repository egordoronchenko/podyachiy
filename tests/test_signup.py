"""Заявки на доступ (таблица requests базы) и ограничение частоты открытых форм."""
import pytest

from app import db, signup, users


def stores(tmp_path):
    base = db.Database(tmp_path)
    return signup.Requests(base), users.Users(base, "секрет-админа")


def test_request_is_saved_normalised_and_survives_a_restart(tmp_path):
    claims, people = stores(tmp_path)
    claim = claims.add("  Сидоров   Пётр  Ильич ", " Sidorov@Example.RU ", " Sidorov ", "  Отдел продаж, протоколы планёрок ", people)
    assert claim["name"] == "Сидоров Пётр Ильич" and claim["email"] == "sidorov@example.ru"
    assert claim["login"] == "sidorov" and claim["comment"] == "Отдел продаж, протоколы планёрок" and claim["status"] == "pending"
    again = signup.Requests(db.Database(tmp_path))
    assert again.pending() == [claim] and again.get(claim["id"]) == claim


@pytest.mark.parametrize("name, email, login, comment, field", [
    ("", "a@b.ru", "sidorov", "", "name"),
    ("Я" * 121, "a@b.ru", "sidorov", "", "name"),
    ("Сидоров", "", "sidorov", "", "email"),
    ("Сидоров", "не почта", "sidorov", "", "email"),
    ("Сидоров", "a@b", "sidorov", "", "email"),
    ("Сидоров", "a@b.ru, c@d.ru", "sidorov", "", "email"),
    ("Сидоров", "a@b.ru\nBcc: x@y.ru", "sidorov", "", "email"),
    ("Сидоров", "a" * 250 + "@b.ru", "sidorov", "", "email"),
    ("Сидоров", "a@b.ru", "Сидоров", "", "login"),
    ("Сидоров", "a@b.ru", "admin", "", "login"),
    ("Сидоров", "a@b.ru", "s", "", "login"),
    ("Сидоров", "a@b.ru", "s" * 33, "", "login"),
    ("Сидоров", "a@b.ru", "sidorov", "ы" * 501, "comment"),
])
def test_bad_fields_are_refused_and_the_field_is_named(tmp_path, name, email, login, comment, field):
    claims, people = stores(tmp_path)
    with pytest.raises(signup.RequestError) as refused:
        claims.add(name, email, login, comment, people)
    assert refused.value.status == 400 and refused.value.field == field
    assert claims.pending() == [] and claims.all() == []


def test_repeated_request_is_refused(tmp_path):
    claims, people = stores(tmp_path)
    claims.add("Сидоров", "sidorov@example.ru", "sidorov", "", people)
    for email, login in (("other@example.ru", "sidorov"), ("sidorov@example.ru", "other")):
        with pytest.raises(signup.RequestError) as refused:
            claims.add("Сидоров", email, login, "", people)
        assert refused.value.status == 409 and "уже подана" in refused.value.detail
    assert len(claims.pending()) == 1


def test_no_more_waiting_requests_than_the_limit(tmp_path, monkeypatch):
    claims, people = stores(tmp_path)
    monkeypatch.setattr(signup, "PENDING_MAX", 2)
    for n in (1, 2):
        claims.add("Имя", f"user{n}@example.ru", f"user{n}", "", people)
    with pytest.raises(signup.RequestError) as refused:
        claims.add("Имя", "user3@example.ru", "user3", "", people)
    assert refused.value.status == 503
    claims.decide(claims.pending()[0]["id"], signup.REJECTED, "admin")      # место освободилось
    claims.add("Имя", "user3@example.ru", "user3", "", people)


def test_decided_request_is_marked_and_cannot_be_decided_twice(tmp_path):
    claims, people = stores(tmp_path)
    first = claims.add("Сидоров", "sidorov@example.ru", "sidorov", "", people)
    second = claims.add("Петров", "petrov@example.ru", "petrov", "", people)
    done = claims.decide(first["id"], signup.APPROVED, "admin", login="sidorov.p")
    assert done["status"] == "approved" and done["decided_by"] == "admin" and done["login"] == "sidorov.p"
    assert claims.decide(second["id"], signup.REJECTED, "chief")["status"] == "rejected"
    assert claims.pending() == []
    for claim_id in (first["id"], "нет-такой"):
        with pytest.raises(signup.RequestError) as refused:
            claims.decide(claim_id, signup.REJECTED, "admin")
        assert refused.value.status == 404
    saved = signup.Requests(db.Database(tmp_path)).all()
    assert [(r["status"], r["decided_by"]) for r in saved] == [("approved", "admin"), ("rejected", "chief")]
    # отклонённому можно подать заявку заново
    assert claims.add("Петров", "petrov@example.ru", "petrov", "", people)["status"] == "pending"


def test_old_decided_requests_are_pushed_out(tmp_path, monkeypatch):
    claims, people = stores(tmp_path)
    monkeypatch.setattr(signup, "DECIDED_KEPT", 2)
    for n in range(4):
        claim = claims.add("Имя", f"user{n}@example.ru", f"user{n}", "", people)
        claims.decide(claim["id"], signup.REJECTED, "admin")
    assert [r["login"] for r in claims.all()] == ["user2", "user3"]


def test_throttle_counts_per_key_inside_the_window():
    limit = signup.Throttle(3, window_sec=3600)
    assert [limit.allow("10.0.0.1", now=t) for t in (0, 10, 20, 30)] == [True, True, True, False]
    assert limit.full("10.0.0.1", now=40) and not limit.full("10.0.0.2", now=40)
    assert limit.allow("10.0.0.2", now=40)                      # у другого адреса свой счёт
    assert not limit.allow("10.0.0.1", now=3599)                # первая попытка ещё в окне
    assert limit.allow("10.0.0.1", now=3601)                    # вышла из окна — место есть
    assert not limit.full("10.0.0.1", now=3 * 3600) and "10.0.0.1" not in limit._hits   # отжившее не копится


def test_full_does_not_count_an_attempt():
    limit = signup.Throttle(1)
    assert not limit.full("a", now=0) and not limit.full("a", now=1)
    assert limit.allow("a", now=2) and limit.full("a", now=3)


def test_taken_login_and_known_email_get_the_answer_of_any_request(tmp_path):
    """Заявка не подсказывает, какие логины и почты заведены. Логин занят — заявка ложится
    администратору как обычная: он поправит логин при одобрении, а человек узнает логин из письма.
    Почта уже у учётной записи — заявку не сохраняем (Known): владельцу ящика сервис напишет, что
    учётная запись есть (обращаясь по имени из учётной записи, а не из заявки: текст заявки
    в чужое письмо не попадает), заявителю ответ тот же, что всем. Без почты в сервисе написать некому —
    заявка ложится администратору, и он разберётся сам."""
    claims, people = stores(tmp_path)
    people.put("ivanov", "Иванов", "пароль-1234", email="ivanov@example.ru")
    claim = claims.add("Иванов Иван", "new@example.ru", "Ivanov", "", people)
    assert claim["login"] == "ivanov" and [c["id"] for c in claims.pending()] == [claim["id"]]
    with pytest.raises(signup.Known) as known:
        claims.add("Иван", " Ivanov@Example.ru ", "ivan", "", people)
    assert (known.value.login, known.value.email) == ("ivanov", "ivanov@example.ru")
    assert len(claims.pending()) == 1
    quiet = claims.add("Иван", "ivanov@example.ru", "ivan", "", people, email_needed=False)
    assert quiet["email"] == "ivanov@example.ru" and len(claims.pending()) == 2


def test_request_email_is_taken_only_from_the_mail_domains(tmp_path):
    """MAIL_DOMAINS задан — заявку принимают только с почтой этих доменов: ссылка на пароль после
    одобрения уйдёт на неё. Домен сверяется целиком, без поддоменов."""
    claims, people = stores(tmp_path)
    for email in ("x@gmail.test", "x@sub.example.ru"):
        with pytest.raises(signup.RequestError) as refused:
            claims.add("Сидоров", email, "sidorov", "", people, domains=("example.ru", "corp.example"))
        assert refused.value.status == 400 and refused.value.field == "email"
        assert "example.ru, corp.example" in refused.value.detail
    assert claims.add("Сидоров", "Sidorov@Corp.Example", "sidorov", "", people, domains=("example.ru", "corp.example"))
    assert claims.add("Петров", "", "petrov", "", people, email_needed=False, domains=("example.ru",))
