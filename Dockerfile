FROM python:3.12-slim

# tzdata — чтобы TZ из .env работал и время в логах было местным, а не UTC
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# Шаблоны и словарь лежат в образе как исходные. Рабочая папка /app/prompts
# монтируется с хоста: при первом запуске сервис кладёт в неё недостающие файлы,
# дальше их правят на хосте без пересборки образа.
COPY prompts ./prompts_default
# Версия сервиса (раздел «О сервисе», /api/status) и тексты лицензий — их сервис отдаёт вошедшим.
COPY VERSION LICENSE THIRD_PARTY.md ./

# Дата сборки и короткий идентификатор коммита — для раздела «О сервисе». Задаются при сборке:
#   docker build --build-arg BUILD_DATE=$(date +%F) --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) .
# Не заданы — поля пусты; к git из контейнера сервис не обращается.
ARG BUILD_DATE=""
ARG GIT_COMMIT=""
ENV APP_BUILD_DATE=$BUILD_DATE APP_COMMIT=$GIT_COMMIT

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8080"]
