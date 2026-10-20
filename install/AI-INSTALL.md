# «Подьячий»: установка с помощью ИИ-агента

**Для человека.** Положите этот файл в любую папку (если у вас есть архив «Подьячего» — рядом с
ним), откройте в этой папке своего ИИ-агента (Claude Code, Cursor, Codex и подобные) и скажите:
«Установи Подьячий по инструкции AI-INSTALL.md».
Агент спросит, ставить для себя или для организации, и проведёт по всем этапам. Ключ поставщика и
пароли вы впишете в файл сами — в чат их не присылайте.

---

Дальше — для агента. Выполняй этапы по порядку. После каждого шага — его проверка; не прошла —
раздел «Сбои», и дальше не иди, пока она не пройдёт. Говори с человеком по-русски, простыми
словами, без терминов, которые он не спрашивал.

## Правила

**Секреты.** Ключи поставщиков, пароль `admin`, пароли почты и домена человек вписывает в `.env`
сам. Не проси их в чат, не печатай, не читай `.env` целиком (`cat`, `Get-Content`, редактор в
терминале) после того, как в нём появился секрет. Есть ли значение, проверяй командой, которая
отвечает только «да/нет» (ниже). Пароль `admin` можешь сгенерировать сам — тогда пиши его сразу
в `.env`, на экран не выводи, а человеку скажи, где он лежит.

**Что делает только человек:**

- ставит Docker Desktop (нужны права администратора компьютера), перезагружает компьютер,
  включает виртуализацию в BIOS, принимает соглашение Docker;
- получает ключ поставщика и платит поставщику;
- вписывает ключи и пароли в `.env`;
- разрешает команды, которые меняют систему: правило брандмауэра, задание по расписанию. Ты
  показываешь команду и выполняешь её, только получив «да».

**Чего не делать:**

- в личном режиме не открывать сервис в сеть: `BIND` остаётся `127.0.0.1` — без входа сервис
  отдаёт всё любому, кто до него достанет;
- не удалять и не очищать папки с данными (`inbox`, `outbox`, `state`, `backup`, `prompts`) и
  существующий `.env`; не выполнять `docker compose down -v`, `docker system prune`,
  `docker volume rm`;
- не трогать чужие контейнеры, образы и проекты Docker на этом компьютере;
- не отправлять записи на обработку без согласия человека: обработка платная;
- не менять `ACTIONS_MODELS`, `SUMMARY_MODEL` и строки `PROVIDER_*`: значения проверены;
- не править настройки Docker Desktop, WSL и системы без просьбы человека.

**Вопросы — пачкой в начале этапа**, а не по одному. Предлагай ответ по умолчанию: «если не
знаете — оставлю так».

**Оболочка.** Команды даны для PowerShell (Windows) и bash (macOS, Linux). В Windows бери
PowerShell-варианты, даже если у тебя есть Git Bash: в нём нет `lsof` и `ss`, а пути и
переменные `HOME` ведут себя иначе. Состояние оболочки
между командами у многих агентов не сохраняется: переменные (`$Dir`, `$Port`…) задавай в начале
каждой команды. В Windows PowerShell 5.1 нет `&&`; для запросов бери `curl.exe`, а не `curl`;
перед чтением русского текста из `docker` и `curl.exe` выполняй
`[Console]::OutputEncoding = [Text.Encoding]::UTF8`.

**Обозначения.** `$Dir` — папка установки (там `compose.yml`, `.env` и папки данных), по умолчанию
`$HOME\Podyachiy` (Windows) или `$HOME/Podyachiy`. `$Src` — папка с исходниками, если они есть.
`$Port` — порт страницы, по умолчанию `8081`. `$Tag` — версия образа, строка `IMAGE_TAG` в `.env`,
по умолчанию `latest`.

---

## Этап 0. Выбор и вопросы

Спроси одной пачкой:

1. **Для себя или для организации?** Для себя (личный режим) — сервисом пользуетесь только вы,
   на этом компьютере, без входа и паролей. Для организации (корпоративный) — вход по паролю,
   пользователи, папки с правами, доступ с других компьютеров.
2. **Поставщик моделей:** RouterAI (российский, оплата в рублях) или OpenRouter (зарубежный, в
   долларах, нужна зарубежная карта или криптовалюта)? Есть ли уже ключ и деньги на счёте?
   Ключ можно получить и позже — к этапу 3.
3. **Где поставить:** папка по умолчанию `$HOME\Podyachiy` подойдёт? На этом диске должно быть
   свободно хотя бы 10 ГБ плюс место под записи (видео — 0,5–2 ГБ за час). Не облачная папка
   (OneDrive, Dropbox).
4. **Где архив или папка с исходниками «Подьячего»**, если они есть (без них — только готовый
   образ из реестра).

Скажи человеку: звук и текст записей уходят поставщику моделей и владельцам моделей, в том числе за
границу; сам сервис бесплатный, обработка часа записи стоит порядка 0,15–0,25 $ через OpenRouter
или 17–30 ₽ через RouterAI. Для организации: Docker Desktop бесплатен для личного пользования и
небольших организаций, крупной нужна подписка Docker; для постоянной работы организации лучше
сервер на Linux (`install/README.md`, разделы 1 и 12).

---

## Этап 1. Docker

**Проверка:**

```powershell
docker version --format "{{.Server.Version}}"; docker compose version
```

```bash
docker version --format '{{.Server.Version}}' && docker compose version
```

Обе ответили номерами — Docker готов, переходи к этапу 2. Иначе:

- **`docker` — неизвестная команда.** Docker не установлен. Это делает человек:
  - Windows 10/11 (64 бита): скачать Docker Desktop с <https://www.docker.com/products/docker-desktop/>
    и запустить установщик (нужны права администратора), галочку «Use WSL 2» оставить, перезагрузиться,
    если попросит. Вместо скачивания можно в PowerShell **от имени администратора**:
    `winget install -e --id Docker.DockerDesktop`. Подробно:
    <https://docs.docker.com/desktop/setup/install/windows-install/>.
  - macOS: Docker Desktop для своего процессора (Apple или Intel) с той же страницы.
  - Linux: Docker Engine и плагин compose — <https://docs.docker.com/engine/install/>; затем
    `sudo usermod -aG docker $USER` и выйти-войти в систему.

  После установки открой новый терминал (старый не знает новых программ) и повтори проверку.
- **Есть клиент, нет сервера** (`error during connect`, `Cannot connect to the Docker daemon`,
  `dockerDesktopLinuxEngine`). Docker Desktop не запущен. Запусти его и подожди до двух минут:

  ```powershell
  Start-Process "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
  for ($i = 0; $i -lt 24; $i++) { docker version --format "{{.Server.Version}}" 2>$null; if ($LASTEXITCODE -eq 0) { break }; Start-Sleep 5 }
  ```

  ```bash
  open -a Docker 2>/dev/null || sudo systemctl start docker
  for i in $(seq 24); do docker version --format '{{.Server.Version}}' 2>/dev/null && break; sleep 5; done
  ```

  При первом запуске Docker Desktop показывает соглашение — его принимает человек. Входить в
  учётную запись Docker не нужно.
- **WSL** (Windows; Docker Desktop пишет про WSL или виртуализацию). Проверь `wsl --status`. Человек
  в PowerShell от имени администратора выполняет `wsl --update` (на старой Windows 10 —
  `wsl --install`) и перезагружает компьютер. «Virtualization support not detected» — виртуализацию
  включают в BIOS; скажи об этом человеку и остановись.

---

## Этап 2. Файлы и образ

### 2.1. Исходники и файлы установки

Нужны два файла из папки `install` комплекта: `compose.yml` и `.env.example`. Они лежат рядом с этой
инструкцией.

Если человек дал **архив**, распакуй его в отдельную папку — не в папку установки:

```powershell
$Src = "$HOME\Podyachiy-source"; New-Item -ItemType Directory -Force $Src | Out-Null
Expand-Archive -LiteralPath "ПУТЬ\К\АРХИВУ.zip" -DestinationPath $Src      # для .tar, .tar.gz: tar -xf "ПУТЬ\К\АРХИВУ.tar" -C $Src
Get-ChildItem $Src -Recurse -Depth 2 -Filter Dockerfile | ForEach-Object { $_.DirectoryName }
```

```bash
SRC="$HOME/Podyachiy-source"; mkdir -p "$SRC"
unzip -q "ПУТЬ/К/АРХИВУ.zip" -d "$SRC"      # для .tar, .tar.gz: tar -xf "ПУТЬ/К/АРХИВУ.tar" -C "$SRC"
find "$SRC" -maxdepth 3 -name Dockerfile -exec dirname {} \;
```

Корень исходников — папка, где лежит `Dockerfile`. В архиве он обычно во вложенной папке
(`Podyachiy-source\podyachiy`): **во всех командах ниже подставляй в `$Src` этот найденный путь**,
а не папку распаковки. Человек уже распаковал архив сам — `$Src` та папка, где у него лежит
`Dockerfile`. **Проверка** — все пункты на месте:

```powershell
$Src = "$HOME\Podyachiy-source"
"Dockerfile","requirements.txt","VERSION","app\web.py","prompts","install\compose.yml","install\.env.example" | ForEach-Object { "$_ : $(Test-Path (Join-Path $Src $_))" }
```

```bash
SRC="$HOME/Podyachiy-source"
for f in Dockerfile requirements.txt VERSION app/web.py prompts install/compose.yml install/.env.example; do [ -e "$SRC/$f" ] && echo "есть $f" || echo "НЕТ $f"; done
```

### 2.2. Папка установки

Сначала посмотри, нет ли уже установки «Подьячего» на этом компьютере — контейнера из его образа
под любым именем проекта:

```powershell
docker ps -a --format "{{.Names}}  {{.Image}}  {{.Status}}  {{.Ports}}" | Select-String podyachiy
docker ps -a --filter "label=com.docker.compose.project=podyachiy" --format "{{.Names}}"
```

```bash
docker ps -a --format '{{.Names}}  {{.Image}}  {{.Status}}  {{.Ports}}' | grep podyachiy
docker ps -a --filter 'label=com.docker.compose.project=podyachiy' --format '{{.Names}}'
```

Оба ответа пустые — ставь как есть. Есть контейнер — у человека уже стоит сервис: спроси, обновить
его или поставить вторую установку рядом. Её контейнер не трогай. Вторая установка — в своей папке, и
в её `.env` (шаг 2.3) задай свободный порт (шаг 3.1), а если второй ответ не пустой (проект с именем
`podyachiy` уже есть) — ещё и своё имя проекта: `COMPOSE_PROJECT_NAME=podyachiy-2`, иначе
`docker compose up -d` пересоздаст контейнер первой.

Создай папку и положи в неё файлы. Существующий `.env` **не перезаписывай**: в нём ключи.

```powershell
$Dir = "$HOME\Podyachiy"; $Src = "$HOME\Podyachiy-source"
New-Item -ItemType Directory -Force $Dir | Out-Null
Copy-Item "$Src\install\compose.yml" $Dir -Force
if (-not (Test-Path "$Dir\.env")) { Copy-Item "$Src\install\.env.example" "$Dir\.env" } else { "есть .env — оставляю" }
Get-ChildItem -Force $Dir | Select-Object Name, Length
```

```bash
DIR="$HOME/Podyachiy"; SRC="$HOME/Podyachiy-source"
mkdir -p "$DIR" && cp "$SRC/install/compose.yml" "$DIR/"
[ -e "$DIR/.env" ] && echo "есть .env — оставляю" || { cp "$SRC/install/.env.example" "$DIR/.env"; chmod 600 "$DIR/.env"; }
ls -la "$DIR"
```

Без исходников возьми `compose.yml` и `.env.example` из папки, где лежит эта инструкция.

**Проверка:** в `$Dir` есть `compose.yml` и `.env`.

### 2.3. Как менять строки `.env`

Строки, где нет секретов, меняешь ты — этой функцией (вставляй её в начало команды). Она заменяет
строку `ИМЯ=…` (или закомментированную `#ИМЯ=…`), а если такой нет — дописывает в конец; файл
остаётся в UTF-8 без BOM.

```powershell
$Dir = "$HOME\Podyachiy"
function Set-Env([string]$Name, [string]$Value) {
  $p = Join-Path $Dir ".env"; $t = [IO.File]::ReadAllText($p)
  $rx = New-Object Text.RegularExpressions.Regex ("(?m)^#?" + [regex]::Escape($Name) + "=[^\r\n]*")
  $line = "$Name=$Value"
  if ($rx.IsMatch($t)) { $t = $rx.Replace($t, $line.Replace('$', '$$'), 1) } else { $t = $t.TrimEnd() + "`n$line`n" }
  [IO.File]::WriteAllText($p, $t, (New-Object Text.UTF8Encoding $false))
}
Set-Env MODE personal
Select-String -LiteralPath "$Dir\.env" -Pattern '^(MODE|BIND|PORT|IMAGE_TAG)='
```

```bash
DIR="$HOME/Podyachiy"
set_env() { f="$DIR/.env"
  if grep -qE "^#?$1=" "$f"; then
    awk -v k="$1" -v v="$2" '!d && $0 ~ "^#?" k "=" { print k "=" v; d=1; next } { print }' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  else printf '%s=%s\n' "$1" "$2" >> "$f"; fi; chmod 600 "$f"; }
set_env MODE personal
grep -E '^(MODE|BIND|PORT|IMAGE_TAG)=' "$DIR/.env"
```

Выводи на экран только строки без секретов, как в последней команде.

### 2.4. Образ

Сервис — образ `egordoronchenko/podyachiy:$Tag`. Два источника, пробуй по порядку.

**(а) Готовый образ из Docker Hub:**

```powershell
$Tag = "latest"; docker pull "egordoronchenko/podyachiy:$Tag"; "код: $LASTEXITCODE"
```

```bash
TAG=latest; docker pull "egordoronchenko/podyachiy:$TAG"; echo "код: $?"
```

Код 0 — образ скачан, переходи к этапу 3.

**(б) Сборка из исходников** — если `docker pull` ответил, что образа нет: `failed to resolve
reference … not found`, `pull access denied`, `repository does not exist` или `manifest unknown`, — а
исходники есть (шаг 2.1). Тег — тот же, что
ждёт `compose.yml`: `egordoronchenko/podyachiy:$Tag`.

```powershell
$Src = "$HOME\Podyachiy-source"; $Tag = "latest"
docker build -t "egordoronchenko/podyachiy:$Tag" --build-arg "BUILD_DATE=$(Get-Date -Format yyyy-MM-dd)" "$Src"; "код: $LASTEXITCODE"
```

```bash
SRC="$HOME/Podyachiy-source"; TAG=latest
docker build -t "egordoronchenko/podyachiy:$TAG" --build-arg "BUILD_DATE=$(date +%F)" "$SRC"; echo "код: $?"
```

Сборка идёт 3–10 минут (дай команде до 20 минут; если у тебя предел на время команды — запусти её
в фоне и дождись) и скачивает основу `python:3.12-slim` с Docker Hub, ffmpeg из репозиториев Debian
и библиотеки с PyPI.

Если `IMAGE_TAG` в `.env` не `latest` — собирай с тем же тегом. Если исходников нет, а образа в
реестре нет — попроси у человека архив с исходниками или файл образа (`docker load -i файл.tar`).

**Проверка** (для обоих путей):

```bash
docker image inspect --format "{{.Created}}" egordoronchenko/podyachiy:latest
```

Ответ — дата создания образа. Ошибка `No such image` — образа нет, вернись к началу шага.

---

## Этап 3. Ключ поставщика и основное в `.env`

### 3.1. Строки без секретов — меняешь ты

Функцией из шага 2.3:

| Строка | Личный режим | Для организации |
|---|---|---|
| `MODE` | `personal` | `corporate` |
| `OPENAI_BASE_URL` | `https://routerai.ru/api/v1`, если поставщик RouterAI; для OpenRouter оставить как есть | так же |
| `BIND` | `127.0.0.1` — проверить, что так | `127.0.0.1` до этапа «Доступ из сети» |
| `PORT` | `8081`, если свободен (проверка ниже) | так же |

Свободен ли порт:

```powershell
$Port = 8081; if (@(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Ignore).Count) { "$Port занят" } else { "$Port свободен" }
```

```bash
PORT=8081; if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 || ss -ltn 2>/dev/null | grep -q ":$PORT "; then echo "$PORT занят"; else echo "$PORT свободен"; fi
```

Занят — возьми следующий свободный (8082, 8083…) и запиши его в `PORT`.

### 3.2. Ключ — вписывает человек

Объясни человеку, где взять ключ, если его ещё нет:

- **RouterAI** — <https://routerai.ru>: зарегистрироваться, пополнить баланс в личном кабинете, там же
  создать ключ API.
- **OpenRouter** — <https://openrouter.ai>: зарегистрироваться, пополнить счёт (Settings → Credits),
  создать ключ (Settings → Keys → Create Key). Ключ начинается с `sk-or-` и показывается один раз.
  Ключу можно задать предел расходов.

Открой человеку `.env` в редакторе и попроси вставить ключ после `OPENAI_API_KEY=` — без пробелов и
кавычек — и сохранить:

```powershell
notepad "$HOME\Podyachiy\.env"
```

```bash
open -e "$HOME/Podyachiy/.env" 2>/dev/null || xdg-open "$HOME/Podyachiy/.env"   # или nano — в терминале человека
```

Есть второй ключ другого поставщика — его можно вписать запасным: `BACKUP_BASE_URL` (адрес того
поставщика) ставишь ты, `BACKUP_API_KEY` вписывает человек.

**Проверка** — только «да/нет», сам ключ не выводится:

```powershell
Select-String -LiteralPath "$HOME\Podyachiy\.env" -Pattern '^OPENAI_API_KEY=[^\s"'']{20,}\s*$' -Quiet
```

```bash
grep -Eq "^OPENAI_API_KEY=[^[:space:]\"']{20,}[[:space:]]*$" "$HOME/Podyachiy/.env" && echo True || echo False
```

`False` — ключа нет, он короче 20 знаков, с пробелом или в кавычках: попроси человека проверить
строку. Для OpenRouter ещё: `-Pattern '^OPENAI_API_KEY=sk-or-' -Quiet` должно дать `True` — иначе
ключ, скорее всего, от другого поставщика.

### 3.3. Пароль `admin` — только для организации

Спроси: сгенерировать пароль главного администратора (логин `admin`) или человек впишет свой? Свой —
открой `.env`, пусть впишет после `ADMIN_PASSWORD=` (не короче 12 знаков, без пробелов). Генерация —
пароль сразу в файл, на экран не выводится:

```powershell
$Dir = "$HOME\Podyachiy"
$abc = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
$b = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
$pw = -join ($b | ForEach-Object { $abc[$_ % $abc.Length] })
$p = "$Dir\.env"; $t = [IO.File]::ReadAllText($p)
$t = ([regex]'(?m)^ADMIN_PASSWORD=[^\r\n]*').Replace($t, "ADMIN_PASSWORD=$pw", 1)
[IO.File]::WriteAllText($p, $t, (New-Object Text.UTF8Encoding $false)); Remove-Variable pw
Select-String -LiteralPath $p -Pattern '^ADMIN_PASSWORD=\S{12,}$' -Quiet
```

```bash
DIR="$HOME/Podyachiy"; pw=$(LC_ALL=C tr -dc 'A-HJ-NP-Za-km-z2-9' < /dev/urandom | head -c 24)
awk -v v="$pw" '!d && /^ADMIN_PASSWORD=/ { print "ADMIN_PASSWORD=" v; d=1; next } { print }' "$DIR/.env" > "$DIR/.env.tmp" && mv "$DIR/.env.tmp" "$DIR/.env"; chmod 600 "$DIR/.env"; unset pw
grep -Eq '^ADMIN_PASSWORD=[^[:space:]]{12,}$' "$DIR/.env" && echo True || echo False
```

Скажи человеку: пароль лежит в файле `.env` в папке установки, строка `ADMIN_PASSWORD`; под `admin`
входят только с самого сервера (`install/README.md`, раздел 6). Файл `.env` никому не пересылать.

---

## Этап 4. Запуск и проверка

```powershell
Set-Location "$HOME\Podyachiy"; docker compose up -d; "код: $LASTEXITCODE"
```

```bash
cd "$HOME/Podyachiy" && docker compose up -d; echo "код: $?"
```

**Проверка 1 — контейнер здоров** (до двух минут; первые полминуты состояние `starting`):

```powershell
Set-Location "$HOME\Podyachiy"
for ($i = 0; $i -lt 24; $i++) { $s = docker inspect --format "{{.State.Health.Status}}" (docker compose ps -q podyachiy); if ($s -eq "healthy") { break }; Start-Sleep 5 }; "состояние: $s"
```

```bash
cd "$HOME/Podyachiy" && for i in $(seq 24); do s=$(docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q podyachiy)"); [ "$s" = healthy ] && break; sleep 5; done; echo "состояние: $s"
```

`healthy` — дальше. Контейнер перезапускается или `unhealthy` — смотри журнал (проверка 3) и «Сбои».

**Проверка 2 — сервис отвечает** (порт — свой):

```powershell
curl.exe -sS http://127.0.0.1:8081/api/status
```

```bash
curl -sS http://127.0.0.1:8081/api/status
```

Ответ: `{"ok":true,"version":"…"}`.

**Проверка 3 — журнал запуска:**

```powershell
[Console]::OutputEncoding = [Text.Encoding]::UTF8; Set-Location "$HOME\Podyachiy"; docker compose logs --tail 40 podyachiy
```

```bash
cd "$HOME/Podyachiy" && docker compose logs --tail 40 podyachiy
```

Должны быть строки `база: /data/state/podyachiy.db…`, `podyachiy … запущен`, в личном режиме —
`режим личный (MODE=personal)…`, и через несколько секунд после запуска — итог проверки ключа:

- `проверка ключа: основной поставщик … — ключ принят` — ключ в порядке;
- `ВНИМАНИЕ: проверка ключа: … — ключ не принят (HTTP 401)` — ключ неверный, отозван или от другого
  поставщика: человек исправляет строку в `.env`, ты выполняешь `docker compose up -d` и смотришь
  журнал снова;
- `… поставщик отказал из-за баланса (HTTP 402)` или `предел расходов этого ключа исчерпан` — нужны
  деньги на счёте или новый предел ключа;
- `… поставщик недоступен` — нет связи с поставщиком (интернет, VPN, прокси).

Проверку ключа можно повторить и без перезапуска — тот же запрос, что у кнопки «Проверить ключ»
(бесплатный; ключа в ответе нет). Личный режим:

```powershell
[Console]::OutputEncoding = [Text.Encoding]::UTF8
curl.exe -sS -X POST -H "Content-Type: application/json" -d "{}" http://127.0.0.1:8081/api/admin/providers/check
```

```bash
curl -sS -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8081/api/admin/providers/check
```

Ответ `"ok": true` — ключ принят; иначе у каждого поставщика `text` (что вышло) и `hint` (что делать).
Для организации тот же запрос — с паролем `admin` (проверка 4).

**Проверка 4 — вход (только для организации).** Пароль читается из `.env` в переменную и не выводится:

```powershell
$Dir = "$HOME\Podyachiy"; [Console]::OutputEncoding = [Text.Encoding]::UTF8
$pw = (Select-String -LiteralPath "$Dir\.env" -Pattern '^ADMIN_PASSWORD=(.*)$').Matches[0].Groups[1].Value.Trim()
$auth = "Authorization: Basic " + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("admin:$pw")); Remove-Variable pw
curl.exe -sS -H $auth http://127.0.0.1:8081/api/v1/whoami
```

```bash
DIR="$HOME/Podyachiy"; pw=$(sed -n 's/^ADMIN_PASSWORD=//p' "$DIR/.env" | tr -d '\r')
printf 'header = "Authorization: Basic %s"\n' "$(printf 'admin:%s' "$pw" | base64 | tr -d '\n')" | curl -sS -K - http://127.0.0.1:8081/api/v1/whoami; unset pw
```

Ответ с `"login": "admin"` — пароль работает. `401` — пароль пуст или контейнер не пересоздан после
правки `.env` (`docker compose up -d`).

**Проверка 5 — страница.** Открой её человеку:

```powershell
Start-Process http://localhost:8081
```

```bash
open http://localhost:8081 2>/dev/null || xdg-open http://localhost:8081
```

Личный режим — страница открывается сразу, без входа. Для организации — страница входа: логин
`admin`, пароль из `.env`. Попроси человека открыть «Управление → Обработка» и нажать «Проверить
ключ»: там же «Журнал работы».

**Первая запись — решает человек:** она платная (несколько центов или рублей за короткую запись).
Предложи загрузить запись на две-три минуты кнопкой «Загрузить запись»; через пару минут у неё
будет статус «готово». Сам записи не загружай.

---

## Этап 5. Для организации — этапы по порядку

Только корпоративный путь. Перед каждым этапом спроси: «нужно или пропустить?» — и задай вопросы
этапа пачкой. Подробности — в разделах `install/README.md`: не пересказывай их человеку целиком,
делай по ним. После каждой правки `.env` — `docker compose up -d` и проверка этапа.

| Этап | Вопросы человеку | Что делаешь ты | Проверка | Подробно |
|---|---|---|---|---|
| **Доступ из сети** | с каких компьютеров заходить? Сеть организации доверенная (не гостевой Wi-Fi)? | `BIND=0.0.0.0`, `PUBLIC_URL=http://<адрес-сервера>:<порт>` (адрес — `Get-NetIPAddress -AddressFamily IPv4` / `hostname -I`); показываешь команду брандмауэра из раздела 5.2 и выполняешь её в PowerShell от администратора только после «да». Предупреди: пока нет HTTPS, пароли и протоколы идут по сети открыто | `curl.exe -sS http://<адрес>:<порт>/api/status` с этого компьютера; с другого — человек открывает адрес в браузере | разделы 5, 5.4 |
| **HTTPS** | есть ли имя сервера в DNS организации и сертификат (или берём внутренний центр Caddy)? | `Caddyfile` из `Caddyfile.example`, папка `certs`, раскомментировать блок `caddy` в `compose.yml`, `BIND=127.0.0.1`, `PUBLIC_URL=https://…`. Сертификат и закрытый ключ человек кладёт сам | `docker compose ps` — оба контейнера `Up`; `https://имя-сервера` открывается | раздел 10 |
| **Почта** | адрес почтового сервера, порт, защита, логин; от чьего имени письма; кому писать о заявках; домен почты организации | `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USER`, `SMTP_FROM`, `ADMIN_EMAIL`, `MAIL_DOMAINS` (раскомментировать), `PUBLIC_URL` обязателен. `SMTP_PASSWORD` вписывает человек | человек: «Управление → Пользователи → Почта → Отправить пробное письмо»; в журнале запуска — `почта включена` | раздел 8 |
| **Вход через домен** | только если HTTPS уже работает. Адреса контроллеров, база поиска, служебная учётная запись, группы доступа и администраторов | `LDAP_URL`, `LDAP_BASE_DN`, `LDAP_BIND_DN`, группы, `FORWARDED_ALLOW_IPS` и постоянный адрес прокси по разделу 10а; корневой сертификат — в `state/ldap-ca.pem`. `LDAP_BIND_PASSWORD` вписывает человек | человек: «Пользователи → Проверить подключение»; в журнале запуска — `вход через домен включён` | раздел 10а |
| **Резервная копия по расписанию** | на какой диск класть копии (лучше другой)? Во сколько ночью? Сколько хранить? | `HOST_BACKUP=<папка>`; Windows — файл `backup.cmd` и задание `schtasks` (от администратора, после «да»); Linux — строка `crontab` | один раз вручную: `docker compose exec -T podyachiy python -m app.backup create`, затем `docker compose exec -T podyachiy python -m app.backup verify` — оба с кодом 0, в папке копий появились архив `.tar` и опись `.manifest.json` | раздел 12 |
| **Первый вход, пользователи и папки** | кто будет администраторами? Какие общие папки и кому что открыть? | ничего: это делает человек на странице | человек вошёл под `admin`, завёл себе учётную запись администратора и вошёл под ней | разделы 6, 7 |

Не открывай сервис в интернет: он рассчитан только на внутреннюю сеть (раздел 16).

---

## Этап 6. Прочие настройки — оба пути

Спроси одной пачкой: «Остальное можно оставить по умолчанию — значения рабочие. Настроить что-то
из этого?» — и покажи список с пояснением в одну строку. Меняй только то, что человек выбрал.

| Что | Строка `.env` | По умолчанию | Когда менять |
|---|---|---|---|
| Запасной поставщик | `BACKUP_BASE_URL`, `BACKUP_API_KEY` (ключ — человек) | нет | есть ключ второго поставщика: сервис перейдёт на него, если основной не отвечает или отказал |
| Модель распознавания | `SPEECH_MODEL` | `elevenlabs/scribe-v2` | обычно не менять. Можно и позже на странице «Обработка», без перезапуска |
| Язык записей | `LANGUAGE` | `ru` | записи на другом языке (`en`, `de`…) или смешанные (`auto`) |
| Где лежат записи и результаты | `HOST_INBOX`, `HOST_OUTBOX` | папки рядом с `compose.yml` | на этом диске мало места: другой диск, на Windows прямыми слешами — `D:/Podyachiy/inbox`. Сетевая папка допустима |
| Папка базы | `HOST_STATE` | `./state` | не менять. Только локальный диск: на сетевой папке база ненадёжна |
| Папка резервных копий | `HOST_BACKUP` | `./backup` | лучше другой диск |
| Порт | `PORT` | `8081` | занят другой программой |
| Сколько записей обрабатывать сразу | `WORKERS` | `3` | `1` — строго по одной; больше 5 незачем. На деньги не влияет |
| Предел размера записи | `UPLOAD_MAX_GB` | `4` | записи больше 4 ГБ |
| Поручения прошлого совещания в конце протокола | `PREV_TASKS` | `1` | `0`, если в одной папке лежат не связанные совещания |
| Часовой пояс | `TZ` | `Europe/Moscow` | другой пояс |
| Адрес сервиса для ссылок и навыка | `PUBLIC_URL` | пусто | для организации — как только сервис открыт в сеть; в личном режиме не нужен |
| Версия образа | `IMAGE_TAG` | `latest` | закрепить номер, чтобы не обновиться случайно; при сборке из исходников — тот же тег у `docker build` |
| Текст протокола на своей модели (LM Studio) | блок `SUMMARY_*` в `.env` | облако | только если человек сам этого хочет: протокол пишет модель на этом компьютере |

Ещё скажи, чего в `.env` нет:

- корзина: удалённое лежит 30 дней и стирается само — срок не настраивается;
- `ACTIONS_MODELS` — какие модели выписывают поручения; значение проверено, его не меняем;
- на страницах сервиса, без правки `.env`: модель распознавания и проверка ключа — «Управление →
  Обработка»; шаблоны протоколов, свои типы записей, общий словарь терминов — «Управление →
  Настройки»; логотип и название организации — там же, вкладка «Оформление» (только для
  организации); словарь участников и сокращений — кнопка «Словарь» у папки или записи;
  пользователи, папки и права — «Пользователи», «Папки и доступ» (только для организации);
  ключи доступа для ИИ-клиентов и программ каждый выпускает себе сам — меню под своим именем
  (только для организации; в личном режиме ключи не нужны).

После правок: `docker compose up -d`, затем проверки 1–3 этапа 4.

---

## Этап 7. Подключить ИИ-клиента — оба пути

Ты сам и есть ИИ-клиент. Предложи человеку: «Могу научиться работать с сервисом: находить протоколы,
собирать поручения, загружать записи по вашей просьбе. Поставить себе навык?» Согласился — сервис
сам отдаёт инструкцию по установке навыка:

```powershell
[Console]::OutputEncoding = [Text.Encoding]::UTF8; curl.exe -sS http://127.0.0.1:8081/skill
```

```bash
curl -sS http://127.0.0.1:8081/skill
```

Выполни её. Она кладёт файл навыка `SKILL.md` в папку навыков клиента и создаёт в домашней папке
файл `~/.podyachiy` с адресом сервиса.

- **Личный режим:** ключ не нужен. Инструкция сама проверяет подключение запросом `whoami` — в
  ответе `"login": "owner"`.
- **Для организации:** в `~/.podyachiy` нужен ключ доступа. Его человек выпускает себе сам на
  странице — меню под своим именем → «Ключи доступа» — и сам вписывает после `key=`. Под `admin`
  ключ для повседневной работы не выпускать: пусть человек заведёт себе учётную запись (этап 5) и
  выпустит ключ под ней. Проверка — `whoami` из навыка: в ответе его логин.

**Проверка работы навыка** — список записей (пустой список — тоже правильный ответ) функцией из
раздела «Подключение» файла `SKILL.md`: запрос `/api/v1/tree` или `/api/v1/records`. Ответ `401` —
ключ не вписан или вписан с ошибкой: попроси человека проверить файл, сам файл не выводи.

Навык для организации работает и с других компьютеров, если задан `PUBLIC_URL` (адрес попадает в
навык) — подробно `install/README.md`, раздел 9.

---

## Сбои

| Что видно | Причина | Что делать |
|---|---|---|
| `docker` — неизвестная команда | Docker не установлен или терминал открыт до установки | этап 1; новый терминал |
| `error during connect`, `Cannot connect to the Docker daemon`, `dockerDesktopLinuxEngine` | Docker Desktop не запущен | запустить и подождать (этап 1) |
| Docker Desktop пишет про WSL, `WSL 2 is not installed`, `Virtualization support not detected` | нет WSL 2 или выключена виртуализация | человек: `wsl --update` от администратора и перезагрузка; виртуализация — в BIOS |
| `failed to resolve reference … not found`, `pull access denied`, `repository does not exist`, `manifest unknown` | образа с таким тегом в реестре нет | путь (б) — сборка из исходников; нет исходников — попросить архив |
| `TLS handshake timeout`, `i/o timeout`, `connection reset` при `docker pull` или `docker build` | нет доступа к Docker Hub (сеть, прокси, блокировка) | проверить интернет и VPN; прокси для Docker человек задаёт в Docker Desktop → Settings → Resources → Proxies; либо образ файлом: на компьютере с доступом `docker save -o podyachiy.tar egordoronchenko/podyachiy:latest`, здесь `docker load -i podyachiy.tar` |
| Сборка падает на `apt-get` или `pip install` | нет доступа к репозиториям Debian или PyPI | то же: сеть, прокси, VPN |
| `port is already allocated`, `address already in use`, `bind: An attempt was made to access a socket` | порт занят | свободный порт в `PORT` (шаг 3.1), `docker compose up -d` |
| `ОТКАЗ ЗАПУСКА: …` в журнале, контейнер перезапускается | сервис назвал причину сам | по тексту причины; таблица — `install/README.md`, раздел 14 |
| `INBOX_DIR и OUTBOX_DIR указывают на одну папку` | `HOST_INBOX` и `HOST_OUTBOX` совпадают | развести по разным папкам |
| Проверка ключа: `ключ не принят (HTTP 401)` | ключ неверный, отозван или от другого поставщика (`OPENAI_BASE_URL`) | человек исправляет ключ; ты сверяешь адрес поставщика; `docker compose up -d` |
| Проверка ключа: `HTTP 402`, `предел расходов этого ключа исчерпан` | нет денег или исчерпан предел ключа | человек пополняет счёт или поднимает предел |
| Проверка ключа: `поставщик недоступен` | нет связи с поставщиком из контейнера | интернет, VPN, прокси; сервис при этом работает |
| Страница просит войти в личном режиме | `MODE` не `personal` или контейнер не пересоздан | проверить строку `MODE`, `docker compose up -d` |
| `401` на `whoami` под `admin` | `ADMIN_PASSWORD` пуст или контейнер не пересоздан | проверить «да/нет» (шаг 3.3), `docker compose up -d` |
| Страница не открывается с другого компьютера (организация) | `BIND=127.0.0.1` или закрыт брандмауэр | этап 5, «Доступ из сети» |

Если сбой не из таблицы — `docker compose logs --tail 100 podyachiy`, страница «Управление →
Обработка» и `install/README.md`, раздел 14. Не пробуй «починить» удалением папок и базы.

---

## Этап 8. Итог для человека

Скажи коротко:

- адрес сервиса (`http://localhost:<порт>`; для организации — и сетевой адрес) и папку установки;
- где лежат данные (`inbox`, `outbox`, `state`, `backup`, `prompts` в папке установки) и что папку
  `state` удалять нельзя — без неё все записи пойдут в обработку заново, за деньги;
- как остановить и запустить: `docker compose stop` / `docker compose start` в папке установки;
  сервис сам запускается вместе с Docker Desktop;
- резервная копия: `docker compose exec podyachiy python -m app.backup create`;
- как обновить: готовый образ — `docker compose pull`, затем `docker compose up -d`; из исходников —
  новый архив, та же команда `docker build`, затем `docker compose up -d`;
- что не выполнено и почему (пропущенные этапы, непроверенный ключ);
- где читать дальше: для себя — `install/personal.md`, для организации — `install/README.md`;
  как пользоваться — «Справка» в меню сервиса.
