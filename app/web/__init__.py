"""Страницы и API.

Вход обязателен всегда (кроме личного режима): без него сервис данных не отдаёт — только
оболочки страниц (они пусты, содержимое приходит запросами), проверку живости, «кто я»,
оформление страницы входа и навык для ИИ-клиентов. Человек получает то, что лежит в открытых
ему папках (см. users.py); управление сервисом — администраторам сервиса. Вход — подписанной
кукой со страницы, ключом доступа или логином и паролем в самом запросе; права проверяются
на сервере при каждом запросе.

По HTTP пароли и ключи идут по сети открыто: без прокси с сертификатом сервис годится
только для внутренней сети (см. install/README.md). Вход под учётной записью домена
(directory.py) по HTTP не принимается вовсе: доменный пароль открывает человеку не только
этот сервис.

Пакет собран из модулей: core — настройки и общее состояние, common — общие помощники, access — вход
и права, lifecycle — запуск и фоновая работа, server — приложение и проверки на каждый запрос; адреса —
по разделам, каждый раздел — свой модуль со своим APIRouter.
"""
from fastapi.staticfiles import StaticFiles

from . import (access_keys, access_requests, admin_users, api_v1, branding, browse, dictionaries, folders_access,
               letters, login, pages, processing, prompt_templates, protocol_edit, record_actions, reports, results,
               skill, speakers, tasks, trash)
from .core import STATIC
from .server import app

app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Адреса разделов — в приложение как есть, в том порядке, в каком они шли в прежнем web.py. Не через
# include_router: новые FastAPI кладут в app.routes сам роутер, а не его адреса, а адреса оттуда читают
# описание API (apidoc.build) и проверки.
for part in (pages, browse, api_v1, skill, access_keys, results, record_actions, trash, tasks, protocol_edit, letters,
             login, access_requests, folders_access, admin_users, speakers, dictionaries, reports, branding,
             prompt_templates, processing):
    app.router.routes.extend(part.router.routes)
