"""Страницы сервиса, проверка живости, оформление без входа, «О сервисе», сводка администратору."""
from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, RedirectResponse

from .. import about, backup, providers, speech, templates, users
from . import core
from .access import admin_guard, viewer
from .core import STATIC, base, branded, cfg, hooks, log, look, options, speech_model
from .lifecycle import _backups, _heartbeat, storage, storage_warnings, volume_warnings

router = APIRouter()


@router.get("/")
def index():
    """Главная — обзор готовых результатов по папкам. Только чтение."""
    return FileResponse(STATIC / "index.html")


def _entrance(page: str) -> Response:
    """Страница входа или задания пароля. В личном режиме входа нет — человек попадает на главную."""
    return RedirectResponse("/", status_code=302) if cfg.personal else FileResponse(STATIC / page)


@router.get("/login")
def login_page():
    """Вход, а с неё же — заявка на доступ и «Забыл пароль»."""
    return _entrance("login.html")


@router.get("/set-password")
def set_password_page():
    """Страница, на которую ведёт ссылка из письма: человек задаёт себе пароль."""
    return _entrance("set-password.html")


@router.get("/help")
def help_page():
    """Справка для тех, кто пользуется сервисом: живёт в образе, чтобы за ней
    не нужно было ходить в репозиторий."""
    return FileResponse(STATIC / "help.html")


@router.get("/install-guide")
def install_guide_page():
    """Инструкция по установке и сопровождению — администратору, там же, где справка. Файл собран
    из install/README.md (tools/docs_build.py) со сценарием install-guide.js рядом; данных
    установки в нём нет."""
    return FileResponse(STATIC / "install-guide.html")


@router.get("/about")
def about_page():
    """О сервисе: версия, автор, куда писать. Страница пуста: сведения приходят запросом по входу."""
    return FileResponse(STATIC / "about.html")


@router.get("/api")
def api_page():
    """Раздел «API»: методы для программ и ИИ-клиентов. Страница строит себя из
    /api/v1/openapi.json и, как он, открыта без входа: данных в ней нет."""
    return FileResponse(STATIC / "api.html")


@router.get("/brand/{version}/{name}")
def brand_file(version: str, name: str):
    """Логотип организации: полный, малый для меню, значок вкладки. Без входа — его показывает
    и страница входа. В адресе метка версии: файл по этому адресу не меняется никогда, поэтому
    браузеру разрешено хранить его год; новый логотип получает новый адрес. Отдаётся только
    PNG, который сервис собрал сам (brand.py), с запретом угадывать тип содержимого."""
    path = look.file(version, name) if branded() else None
    if path is None or not path.is_file():
        raise HTTPException(404, "такого файла нет")
    return FileResponse(path, media_type="image/png", headers={
        "X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/tasks")
def tasks_page():
    """Поручения всех протоколов одной таблицей. Страница пуста: строки приходят запросом."""
    return FileResponse(STATIC / "tasks.html")


@router.get("/admin")
def admin_page():
    """Состояние сервиса и управление — администратору. Сама страница пуста:
    всё, что на ней показано, приходит запросами, которые сервер проверяет."""
    return FileResponse(STATIC / "admin.html")


@router.get("/api/status")
def status():
    """Проверка, что сервис жив, — для Docker. Без входа, поэтому без данных: только номер версии."""
    return {"ok": True, "version": about.version()}


def _brand_public() -> dict:
    """Оформление для любой страницы, включая страницу входа: название организации и адреса
    картинок. Пусто — стандартное: наш логотип и надпись."""
    if not branded():
        return {"name": "", "logo": None}
    seen = look.view()
    return {"name": seen["name"],
            "logo": {kind: f"/brand/{seen['logo']}/{file}" for kind, file in
                     (("full", "logo.png"), ("small", "logo-small.png"), ("icon", "favicon.png"))}
            if seen["logo"] else None}


@router.get("/api/brand")
def brand_public():
    """Без входа: это то, что человек и так видит на странице входа."""
    return _brand_public()


@router.get("/api/about")
def about_service(who: users.Access = Depends(viewer)):
    """О сервисе — всем вошедшим: версия, режим, автор и как с ним связаться. Из настроек
    здесь только то, что нужно в обращении к автору: режим и модели. Адресов, логинов,
    названий записей и ключей нет."""
    return {"name": about.NAME, "tagline": about.TAGLINE, "version": about.version(), "build": about.build(),
            **about.contacts(),
            "mode": "personal" if cfg.personal else "corporate",
            "speech_model": speech_model(), "summary_model": cfg.summary_model,
            "documents": {name: about.document(name) is not None for name in about.DOCUMENTS}}


@router.get("/api/about/{name}")
def about_document(name: str, who: users.Access = Depends(viewer)):
    """Лицензия сервиса (license) и перечень сторонних компонентов с их лицензиями (third-party) —
    тексты, которые едут внутри образа."""
    if name not in about.DOCUMENTS:
        raise HTTPException(404, "такого документа нет: license или third-party")
    found = about.document(name)
    if found is None:
        raise HTTPException(404, "этого файла в образе нет — он кладётся при сборке образа")
    text, kind = found
    return Response(text, media_type=f"{kind}; charset=utf-8", headers={"X-Content-Type-Options": "nosniff"})


@router.get("/api/admin/status", dependencies=[Depends(admin_guard)])
def admin_status(log_from: int = 0, history_version: int = -1):
    """Сводка для страницы администрирования. Ключа API и путей папок на диске сервера здесь
    нет и быть не может.

    В ней имена всех записей, журнал работы и расходы, поэтому она только администратору.

    Страница опрашивает этот метод каждые несколько секунд, поэтому лог и
    история отдаются приростом: клиент сообщает, что у него уже есть, и
    получает только новое. Иначе каждая открытая вкладка тянула бы всю
    историю обработки и весь хвост лога по кругу.
    """
    assert core.service is not None
    log_start, log_lines = log.tail_from(max(log_from, 0))
    fresh_history = core.service.ledger.version != history_version
    totals, history = core.service.ledger.totals(), core.service.ledger.history() if fresh_history else None
    if history is not None:
        # output — папка результатов записи на диске сервера: путей диска в ответе нет
        history = [{k: v for k, v in row.items() if k != "output"} for row in history]
    return {
        "config": {
            "scan_interval_sec": cfg.scan_interval_sec,
            "stable_sec": cfg.stable_sec,
            "workers": cfg.workers,
            "speech_model": speech_model(),
            "summary_model": cfg.summary_model,
            "language": cfg.language,
            "key_present": bool(cfg.api_key),
            "admin_enabled": bool(cfg.admin_password),
            "mode": "расшифровка с разделением говорящих одним вызовом",
        },
        # база и резервная копия: версия схемы, размер, на чём лежит папка состояния (тип файловой
        # системы без путей), предупреждения и когда команда копии отработала последний раз
        "storage": {"schema": base.version, "bytes": base.size(), "fs": storage["type"], "kind": storage["kind"],
                    "warnings": storage_warnings, "backup": backup.last(base)},
        # тома установки: не подключены с хоста, мало места — по проверке при запуске и раз в сутки
        "volumes": {"warnings": volume_warnings},
        # копия из самого сервиса: расписание, следующее время, идущая копия, последняя неудача
        "backups": _backups(),
        # webhook «запись готова»: включён ли, куда (без пути и токенов), сколько доставок ждёт, последняя
        # доставка и последний сбой
        "webhook": hooks.state(),
        # последняя отметка «сервис жив» в базе
        "heartbeat": _heartbeat(),
        "templates": {k: v["title"] for k, v in templates.types(cfg.prompts_dir).items()},
        "speech": _speech_choice(),
        "providers": _providers(),
        "now": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        **core.service.snapshot(),
        "totals": totals,
        "history": history,
        "history_version": core.service.ledger.version,
        "log": log_lines,
        "log_start": log_start,
        "log_total": log.total,
    }


def _speech_choice() -> dict:
    """Модель распознавания для новых записей: что действует, откуда взято и из чего выбирать."""
    return {"models": speech.models(), "current": speech_model(), "env": cfg.model,
            "chosen": options.speech_model()}


def _providers() -> dict:
    """Какие поставщики настроены — без ключей — и когда запрос последний раз уходил запасному."""
    backup, text = cfg.backup_provider, cfg.text_provider
    return {"main": cfg.speech_provider.public(),
            "backup": backup.public() if backup else None,
            # у текстовых этапов свой адрес — тогда запасной их не подхватывает
            "text": text.public() if cfg.summary_base_url != cfg.base_url else None,
            **providers.state()}
