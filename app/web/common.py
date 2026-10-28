"""Общие помощники адресов: путь записи, протокол и отметки, пересборка, политика CSP страниц,
адрес клиента, личная папка."""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path, PurePosixPath

from fastapi import Request

from .. import dictionary, external, marks, names, pipeline, registry, summarize
from . import core
from .core import cfg, log, noted, people, words


# Заголовки защиты — на каждый ответ. CSP: сценарии — только файлы самого сервиса (встроенных в
# страницы нет, обработчиков on…= в разметке тоже), стили — свои файлы и атрибуты style (ими
# страницы пользуются), картинки — свои и blob: (предпросмотр логотипа перед загрузкой), звук и
# запросы — только к себе; плагинов нет, внутри чужой страницы сервис не открывается
# (frame-ancestors), форма уходит только сюда. Карту записи — самостоятельный файл со своим
# сценарием внутри — сервис отдаёт с разрешением ровно этого сценария по хешу (_page_policy).
# Referrer-Policy same-origin: в адресах страниц — имена папок и записей, на чужой сайт по ссылке
# из протокола они не уходят ни целиком, ни именем сервера. HSTS — только по HTTPS: схему за прокси
# uvicorn берёт из X-Forwarded-Proto лишь от адресов FORWARDED_ALLOW_IPS (см. _secure); по HTTP
# заголовок бессмыслен, а с поддоменами и списком preload — решение владельца домена, не сервиса.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; "
       "media-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")
HSTS = "max-age=31536000"
# Ответы API — данные людей: браузер не кладёт их в кэш на диске (на общем компьютере их прочёл бы
# следующий). Это и выгрузки (DOCX, PDF, CSV, книга, карта), и звук записи — всё под /api/.
# Ответ, назначивший себе Cache-Control сам, остаётся как есть.
NO_STORE = "no-store"


def _page_policy(page: str) -> str:
    """CSP для страницы со своим встроенным сценарием: разрешён ровно он — по хешу SHA-256."""
    hashes = "".join(f" 'sha256-{base64.b64encode(hashlib.sha256(code.encode()).digest()).decode()}'"
                     for code in re.findall(r"<script>(.*?)</script>", page, flags=re.S))
    return CSP.replace("script-src 'self'", "script-src 'self'" + hashes, 1)


def _parent_of(rel: str) -> str:
    p = PurePosixPath(rel).parent.as_posix()
    return "" if p == "." else p


def _protocol(file: str, record: dict) -> str | None:
    """Протокол записи в Markdown, без служебных пометок сервиса."""
    path = pipeline.protocol_path(cfg, file, record)
    if not path.is_file():
        return None
    return re.sub(r"<!--.*?-->\s*", "", path.read_text(encoding="utf-8"), flags=re.S).strip() + "\n"


def _marked(file: str, record: dict, protocol: str) -> list[dict]:
    """Поручения протокола с отметками «выполнено» и сроками, которые поставил человек."""
    found = marks.merged(external.tasks(protocol), noted.load(file))
    day, now = external.day_of(record), registry.today()
    return [{**task, **registry.state(task, day, now)} for task in found]


def _rebuild_state(file: str, base: Path, stem: str) -> dict:
    """Расходится ли протокол с подписями и со словарём записи и не пересобирается ли он уже.
    Словарь папки сюда не входит: его правка не делает устаревшими все протоколы папки разом."""
    assert core.service is not None
    try:
        built = (base / "summary" / f"{stem}.md").stat().st_mtime
    except OSError:
        built = None
    return {"names_newer": names.names_newer(base, stem),
            "dict_newer": built is not None and words.get(dictionary.RECORDS, file)["changed"] > built,
            # протокол правили вручную: пересборка соберёт его заново, правленый текст уйдёт в версии
            "edited": summarize.edited(base / "summary", stem) if built is not None else None,
            "rebuilding": any(i.path == file for i in core.service.queue) or _in_work(file)}


def _in_work(file: str) -> bool:
    assert core.service is not None
    return any(job["file"] == file for job in list(core.service.running))


def _protocol_state(out_dir: Path, stem: str) -> dict:
    """Отпечаток текущего протокола (version — его передают в base правки) и кто его правил вручную."""
    return {"version": summarize.digest((out_dir / f"{stem}.md").read_bytes()),
            "edited": summarize.edited(out_dir, stem)}


def _client(request: Request) -> str:
    return request.client.host if request.client else "неизвестно"


def _make_home(login: str) -> None:
    """Личная папка пользователя во входной папке."""
    home = people.get(login)["home"]
    if core.upload_ready:
        try:
            (cfg.inbox / home).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log(f"ВНИМАНИЕ: личная папка {home} не создана: {e}")
