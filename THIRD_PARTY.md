# Сторонние компоненты

В образе «Подьячего» (`egordoronchenko/podyachiy`) вместе с кодом сервиса лежат сторонние
программы и библиотеки. Они распространяются на условиях своих лицензий; лицензия MIT из файла
`LICENSE` этого репозитория на них не действует.

Перечень снят 10.10.2026 с образа, собранного из этого репозитория командой `docker build .`
(метаданные пакетов Python, `ffmpeg -version`, `ffmpeg -L`, `dpkg-query`). В `requirements.txt`
версии заданы нижней границей, поэтому **в каждой новой сборке версии могут отличаться** —
команды, которыми перечень снимается заново, в конце файла.

Составлено не юристом: это перечень фактов об образе, а не заключение о соблюдении лицензий.

## 1. Пакеты Python

Прямые зависимости (`requirements.txt`): FastAPI, Uvicorn, HTTPX, python-docx, ReportLab,
openpyxl, cryptography, Pillow, ldap3, pyasn1. Остальные пришли вместе с ними. Текст лицензии каждого
пакета лежит в образе, в папке
`/usr/local/lib/python3.12/site-packages/<пакет>-<версия>.dist-info/`.

| Пакет | Версия | Лицензия | Исходники |
|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | https://github.com/fastapi/annotated-doc |
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| anyio | 4.15.1 | MIT | https://github.com/agronholm/anyio |
| certifi | 2026.7.22 | MPL-2.0 | https://github.com/certifi/python-certifi |
| cffi | 2.1.1 | MIT-0 | https://github.com/python-cffi/cffi |
| charset-normalizer | 3.5.2 | MIT | https://github.com/jawah/charset_normalizer |
| click | 8.5.0 | BSD-3-Clause | https://github.com/pallets/click |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |
| et_xmlfile | 2.0.0 | MIT | https://foss.heptapod.net/openpyxl/et_xmlfile |
| fastapi | 0.143.0 | MIT | https://github.com/fastapi/fastapi |
| h11 | 0.16.0 | MIT | https://github.com/python-hyper/h11 |
| httpcore | 1.0.9 | BSD-3-Clause | https://github.com/encode/httpcore |
| httpx | 0.28.1 | BSD-3-Clause | https://github.com/encode/httpx |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| ldap3 | 2.9.1 | LGPL-3.0 | https://github.com/cannatag/ldap3 |
| lxml | 6.1.3 | BSD-3-Clause | https://github.com/lxml/lxml |
| openpyxl | 3.1.5 | MIT | https://foss.heptapod.net/openpyxl/openpyxl |
| opentelemetry-api | 1.45.1 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python |
| pillow | 12.3.0 | MIT-CMU | https://github.com/python-pillow/Pillow |
| pip | 25.0.1 | MIT | https://github.com/pypa/pip |
| pyasn1 | 0.6.4 | BSD-2-Clause | https://github.com/pyasn1/pyasn1 |
| pycparser | 3.11 | BSD-3-Clause | https://github.com/eliben/pycparser |
| pydantic | 2.14.0 | MIT | https://github.com/pydantic/pydantic |
| pydantic_core | 2.50.0 | MIT | https://github.com/pydantic/pydantic-core |
| python-docx | 1.2.0 | MIT | https://github.com/python-openxml/python-docx |
| reportlab | 5.0.1 | BSD-3-Clause | https://hg.reportlab.com/hg-public/reportlab |
| starlette | 1.7.0 | BSD-3-Clause | https://github.com/Kludex/starlette |
| typing-inspection | 0.4.4 | MIT | https://github.com/pydantic/typing-inspection |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| uvicorn | 0.54.0 | BSD-3-Clause | https://github.com/Kludex/uvicorn |

Тексты лицензий: [MIT](https://opensource.org/license/mit),
[MIT-0](https://opensource.org/license/mit-0),
[BSD-2-Clause](https://opensource.org/license/bsd-2-clause),
[BSD-3-Clause](https://opensource.org/license/bsd-3-clause),
[Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0),
[MPL-2.0](https://www.mozilla.org/MPL/2.0/),
[PSF-2.0](https://docs.python.org/3/license.html#psf-license),
[LGPL-3.0](https://www.gnu.org/licenses/lgpl-3.0.html).

Зачем они сервису:

- `fastapi`, `uvicorn`, `starlette`, `pydantic` — страницы и API; `httpx` — запросы к поставщику моделей.
- `python-docx` (с ним `lxml`) и `reportlab` (с ним `pillow`, `charset-normalizer`) — выгрузка
  протокола в DOCX и PDF. `pillow` ещё проверяет и уменьшает логотип организации.
- `openpyxl` (с ним `et_xmlfile`) — отчёт о расходах книгой Excel. В `et_xmlfile` часть кода взята
  из стандартной библиотеки Python: рядом с его лицензией MIT лежит текст лицензии PSF (`LICENCE.python`).
- `cryptography` (с ним `cffi`, `pycparser`) — разбор сертификата контроллера домена при входе
  через домен. Используется на условиях BSD-3-Clause (лицензия двойная, на выбор). Его колесо
  несёт собранную библиотеку OpenSSL (Apache-2.0).
- `ldap3` (с ним `pyasn1`) — вход под учётной записью домена: обращение к Active Directory по LDAP.
- `certifi` (MPL-2.0) используется без изменений; его исходный текст доступен по ссылке выше.

Колёса `lxml` и `pillow` несут в себе собранные библиотеки (libxml2 и libxslt — MIT; zlib,
libjpeg-turbo, libpng, FreeType и другие — разрешительные лицензии); их тексты лежат в тех же
папках `dist-info`.

### ldap3 и LGPL

`ldap3` распространяется под **GNU LGPL версии 3**. Как он используется:

- без изменений, как отдельная библиотека: сервис вызывает её через `import`, её код в код
  сервиса не копируется;
- в образе она лежит обычными файлами `.py` в `/usr/local/lib/python3.12/site-packages/ldap3/`;
- библиотеку можно заменить своей версией (положить другие файлы в `site-packages` или
  пересобрать образ с другой версией в `requirements.txt`) — сервис продолжит с ней работать;
- текст лицензии лежит в образе в папке `dist-info` пакета, исходные тексты — по ссылке выше.

Последний выпуск `ldap3` — 2.9.1 (2021 год). Библиотека берёт из `pyasn1` имена, объявленные
устаревшими, поэтому в `requirements.txt` версия `pyasn1` ограничена сверху (`<0.7`): поднимать
границу — только после проверки входа через домен.

Только для разработки и в образ не входит: `pytest` (MIT), `pypdf` (BSD-3-Clause; тесты читают
им собранный PDF) — `requirements-dev.txt`.

## 2. FFmpeg

Вызывается сервисом как отдельная программа — для извлечения звука и замера уровня;
с кодом сервиса не компонуется.

| | |
|---|---|
| Версия | 7.1.5, пакет Debian `ffmpeg 7:7.1.5-0+deb13u1` |
| Сборка | Debian 13 (trixie), gcc 14; в параметрах сборки `--enable-gpl`, без `--enable-version3` и без `--enable-nonfree` |
| Лицензия сборки | **GNU GPL версии 2 или более поздней** — так сообщает `ffmpeg -L` в образе |
| Вместе с ним | `libx264`, `libx265` (GPL-2+), `libmp3lame` (LGPL) и другие библиотеки кодеков — пакетами Debian со своими лицензиями |
| Текст лицензии | в образе: `/usr/share/common-licenses/GPL-2`; в сети: https://www.gnu.org/licenses/old-licenses/gpl-2.0.html |
| Сведения об авторских правах | в образе: `/usr/share/doc/ffmpeg/copyright` |
| Исходные тексты | пакет исходников Debian: https://packages.debian.org/source/trixie/ffmpeg и https://sources.debian.org/src/ffmpeg/ ; архив точных версий: https://snapshot.debian.org/package/ffmpeg/ ; исходный проект: https://ffmpeg.org/download.html |

Получить исходники именно этой сборки: в системе Debian 13 с включёнными источниками
`deb-src` — `apt-get source ffmpeg=7:7.1.5-0+deb13u1`.

Образ содержит FFmpeg в двоичном виде, без изменений, из пакета Debian. Текст GPL лежит
в образе; исходные тексты доступны по ссылкам выше.

## 3. Базовый образ

| Компонент | Версия | Лицензия | Где смотреть |
|---|---|---|---|
| Образ `python:3.12-slim` | Python 3.12.15 на Debian 13.7 (trixie) | содержимое — по лицензиям входящих пакетов | https://hub.docker.com/_/python , https://github.com/docker-library/python |
| Python | 3.12.15 | PSF-2.0 | https://docs.python.org/3/license.html , https://www.python.org/downloads/source/ |
| Пакеты Debian (в образе 292, вместе с зависимостями FFmpeg) | Debian 13 | у каждого своя: GPL, LGPL, BSD, MIT и другие | в образе: `/usr/share/doc/<пакет>/copyright`; тексты — `/usr/share/common-licenses/`; исходники — https://sources.debian.org/ |
| `tzdata` | 2026c | общественное достояние | https://www.iana.org/time-zones |

Среди пакетов Debian есть распространяемые под GPL и LGPL (например, `bash`, `coreutils`,
`glibc`). Они входят в образ без изменений; исходные тексты каждого доступны на
https://sources.debian.org/ и командой `apt-get source <пакет>`.

## 4. Шрифты

| | |
|---|---|
| Шрифт | Vertograd — надпись «Подьячий» рядом с логотипом (славянские буквицы) |
| Файл | `app/static/fonts/Vertograd.woff2` и `site/assets/fonts/Vertograd.woff2` — поднабор: русская кириллица и знаки ударения, 43 КБ; собран из `fonts/otf/Vertograd-Regular.otf` репозитория авторов, состояние на 07.08.2025, коммит `695f7f7` |
| Авторы | © 2025 The Vertograd Project Authors: Александр Андреев, Влад Дорош (Slavonic Computing Initiative) |
| Лицензия | SIL Open Font License 1.1 |
| Текст лицензии | рядом со шрифтом: `app/static/fonts/Vertograd-OFL.txt`, `site/assets/fonts/Vertograd-OFL.txt`; в сети: https://openfontlicense.org/ |
| Исходники | https://github.com/slavonic/Vertograd |

OFL разрешает включать шрифт в состав программы и урезать его до нужных знаков; отдельно сам
шрифт продавать нельзя. Зарезервированного названия у Vertograd нет, поэтому урезанный файл
назван так же.

| | |
|---|---|
| Шрифт | DejaVu Sans (обычный, жирный, наклонный, жирный наклонный) и DejaVu Sans Mono, версия 2.37 — текст протокола в PDF; встраивается в каждый выгруженный файл теми знаками, которые в нём есть |
| Файлы | `app/static/fonts/DejaVuSans.ttf`, `DejaVuSans-Bold.ttf`, `DejaVuSans-Oblique.ttf`, `DejaVuSans-BoldOblique.ttf`, `DejaVuSansMono.ttf` — без изменений, из пакетов Debian `fonts-dejavu-core` и `fonts-dejavu-extra` |
| Авторы | © 2003 Bitstream, Inc. (Bitstream Vera); изменения DejaVu — общественное достояние |
| Лицензия | Bitstream Vera Fonts Copyright (разрешительная) |
| Текст лицензии | рядом со шрифтами: `app/static/fonts/DejaVu-LICENSE.txt` |
| Исходники | https://dejavu-fonts.github.io/ |

Лицензия разрешает распространять шрифт в составе программы и встраивать его в документы;
продавать сам шрифт отдельно нельзя. Изменённый шрифт нельзя называть «Bitstream» или «Vera».

## 5. Чего в образе нет

- Прокси Caddy (Apache-2.0) в образ сервиса не входит: при включении HTTPS администратор
  сам скачивает официальный образ `caddy:2` — https://github.com/caddyserver/caddy .
- Модели распознавания и текстовые модели — внешние услуги поставщика, а не часть программы.

## Как снять перечень заново

Пакеты Python — имя, версия, лицензия:

```bash
docker run --rm egordoronchenko/podyachiy:latest pip list --format=freeze
docker run --rm egordoronchenko/podyachiy:latest sh -c "pip show -v fastapi uvicorn httpx | grep -E '^(Name|Version|License)'"
```

FFmpeg — версия, параметры сборки, лицензия:

```bash
docker run --rm egordoronchenko/podyachiy:latest sh -c "ffmpeg -version | head -3; ffmpeg -L"
```

Пакеты Debian с версиями:

```bash
docker run --rm egordoronchenko/podyachiy:latest dpkg-query -W
```
