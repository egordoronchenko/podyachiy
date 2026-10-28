"""Раздел «О сервисе»: версия из одного источника, автор и контакты из одного места, тексты лицензий."""
import json
import re
from pathlib import Path

from app import about
from tests.test_web import keyed, running, site, staffed  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_version_comes_from_the_file_and_gets_into_the_image(monkeypatch):
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION) and about.version() == VERSION
    for name in ("Dockerfile",):
        text = (ROOT / name).read_text(encoding="utf-8")
        copied = " ".join(line for line in text.splitlines() if line.startswith("COPY"))
        for file in ("VERSION", "LICENSE", "THIRD_PARTY.md"):
            assert f" {file} " in copied + " ", (name, file)
        assert "ARG BUILD_DATE" in text and "ARG GIT_COMMIT" in text and "APP_BUILD_DATE=$BUILD_DATE APP_COMMIT=$GIT_COMMIT" in text
        assert "git " not in " ".join(line for line in text.splitlines() if line.startswith(("RUN", "CMD")))
    monkeypatch.delenv("APP_BUILD_DATE", raising=False)                      # тесты идут и внутри собранного образа
    monkeypatch.delenv("APP_COMMIT", raising=False)
    assert about.build() == {"date": "", "commit": ""}                       # не заданы при сборке — пусто
    monkeypatch.setenv("APP_BUILD_DATE", "2026-10-11")
    monkeypatch.setenv("APP_COMMIT", "dbeb6c0aaaaaaaaaaaaaaaaaaaaaa")
    assert about.build() == {"date": "2026-10-11", "commit": "dbeb6c0aaaaa"}
    monkeypatch.setattr(about, "ROOT", ROOT / "нет такой папки")
    assert about.version() == "" and about.document("license") is None


def test_contacts_live_in_one_place():
    """Почта и Telegram автора записаны только в app/about.py: страницы берут их из ответов сервиса."""
    assert about.contacts() == {"author": "Доронченко Егор", "email": "mazedastra@gmail.com",
                                "telegram": "@EgorDoronchenko", "telegram_url": "https://t.me/EgorDoronchenko"}
    for path in [*(ROOT / "app").glob("*.py"), *(ROOT / "app" / "static").glob("*.*")]:
        if path.name == "about.py" or path.suffix in (".png", ".woff2"):
            continue
        text = path.read_text(encoding="utf-8")
        assert about.EMAIL not in text and "EgorDoronchenko" not in text, path.name


def test_about_is_shown_to_everyone_who_is_in_and_tells_nothing_private(site, monkeypatch):
    monkeypatch.delenv("APP_BUILD_DATE", raising=False)
    monkeypatch.delenv("APP_COMMIT", raising=False)
    web, admin, ivanov, _ = staffed(site)
    assert site[1].get("/api/about").status_code == 401 and site[1].get("/about").status_code == 200
    for client in (admin, ivanov):
        seen = client.get("/api/about").json()
        assert seen["name"] == "Подьячий" and seen["version"] == VERSION and seen["build"] == {"date": "", "commit": ""}
        assert (seen["author"], seen["email"], seen["telegram_url"]) == (
            "Доронченко Егор", "mazedastra@gmail.com", "https://t.me/EgorDoronchenko")
        assert seen["mode"] == "corporate" and seen["speech_model"] == "deepgram/nova-3"
        assert "license" not in seen
        assert seen["documents"] == {"license": True, "third-party": True}
    dump = json.dumps(ivanov.get("/api/about").json(), ensure_ascii=False)
    for private in ("sk-test", "127.0.0.1", "ivanov", "petrov", str(web.core.cfg.inbox)):
        assert private not in dump, private

    licence = ivanov.get("/api/about/license")
    assert licence.status_code == 200 and licence.headers["content-type"] == "text/plain; charset=utf-8"
    assert licence.text == (ROOT / "LICENSE").read_text(encoding="utf-8") and "Доронченко Егор" in licence.text
    third = ivanov.get("/api/about/third-party")
    assert third.headers["content-type"] == "text/plain; charset=utf-8" and "FFmpeg" in third.text
    assert third.headers["x-content-type-options"] == "nosniff" and "pillow" in third.text
    assert ivanov.get("/api/about/.env").status_code == 404 and ivanov.get("/api/about/VERSION").status_code == 404
    assert site[1].get("/api/about/license").status_code == 401
    robot, _ = keyed(web, ivanov)                                     # ключом — тоже: это чтение
    assert robot.get("/api/about").json()["version"] == VERSION


def test_version_is_in_the_health_check_and_whoami(site):
    web, admin, _, _ = staffed(site)
    health = site[1].get("/api/status").json()
    assert health == {"ok": True, "version": VERSION}                        # формат проверки живости прежний, поле добавлено
    assert admin.get("/api/v1/whoami").json()["version"] == VERSION
    assert any(f"podyachiy {VERSION} запущен" in line for line in web.core.log.tail_from(0)[1])


def test_personal_mode_shows_about_without_login(tmp_path):
    with running(tmp_path, mode="personal") as (web, client):
        seen = client.get("/api/about").json()
        assert seen["mode"] == "personal" and seen["version"] == VERSION
        assert client.get("/api/about/license").status_code == 200
