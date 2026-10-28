"""Работа со звуком (ffmpeg): извлечение дорожки и защита от тишины.

Перенесено из эталонного проекта transcribe-web. Функция обхода папки из
эталона здесь не нужна — её заменил сканер INBOX (app/watcher.py).
Исходный файл только читается: извлечённый звук пишется в OUTBOX.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from pathlib import Path

VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".webm", ".flv", ".ts", ".mpg", ".mpeg", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS


class NoAudioError(Exception):
    pass


# Сколько ffmpeg работает одновременно, сколько бы записей ни шло (WORKERS). Почти вся
# обработка — ожидание провайдера, и машину нагружает только ffmpeg: на гигабайтных
# видео третий процесс упирается в диск и процессор и замедляет все три.
FFMPEG_PARALLEL = 2
_ffmpeg_slots = threading.BoundedSemaphore(FFMPEG_PARALLEL)


# Файл записи — не обязательно то, чем назван: под именем .mp4 может лежать плейлист (HLS, concat),
# и ffmpeg по нему пошёл бы в сеть (http, tcp, rtmp…) или собрал бы вход из кусков других файлов
# протоколами concat:, subfile:, data:. Поэтому входу ffmpeg и ffprobe разрешён только протокол file —
# обычный файл на диске. Формат входа не задаётся: записи приходят в десятке контейнеров, и
# определяет их сам ffmpeg. Формат того, что ffmpeg пишет, назван явно (-f), а не угадан по имени.
LOCAL_ONLY = ["-protocol_whitelist", "file"]


def run_ffmpeg(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Любой запуск ffmpeg — через эту функцию: она ждёт свободного места и разрешает входу
    только файлы на диске. Зовётся из потоков (asyncio.to_thread), поэтому ожидание событийный
    цикл не держит."""
    with _ffmpeg_slots:
        return subprocess.run(["ffmpeg", *LOCAL_ONLY, *args], **kwargs)


def run_ffprobe(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Любой запуск ffprobe — через эту функцию: входу разрешены только файлы на диске."""
    return subprocess.run(["ffprobe", *LOCAL_ONLY, *args], **kwargs)


# Отпечаток содержимого файла записи: по нему сервис узнаёт ту же запись под другим именем и замечает, что
# исходник готовой записи изменился. Читаются начало и конец файла по PRINT_EDGE и размер, а не весь файл:
# часовое видео — гигабайты, часто на сетевой папке, а сличать приходится и при обходе входной папки. Две
# разные записи с одним размером до байта и одинаковыми первыми и последними мегабайтами не встречаются:
# в начале — заголовки контейнера, в конце — оглавление или последние секунды звука. Докопированный файл
# отличается размером, перезаписанный — содержимым концов.
PRINT_EDGE = 4 * 2 ** 20


def fingerprint(path: Path) -> str | None:
    """Отпечаток содержимого файла (sha256 размера, первых и последних PRINT_EDGE байт); None — файл не
    прочитать."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            digest = hashlib.sha256(str(size).encode())
            digest.update(f.read(PRINT_EDGE))
            if size > PRINT_EDGE:
                f.seek(max(PRINT_EDGE, size - PRINT_EDGE))
                digest.update(f.read(PRINT_EDGE))
    except OSError:
        return None
    return digest.hexdigest()


SILENCE_DB = -60.0  # ниже этого порога в дорожке нет сигнала вообще
AUDIO_SUFFIX = ".audio.mp3"   # имя извлечённой дорожки рядом с результатами


def has_audio_stream(path: Path) -> bool:
    proc = run_ffprobe(
        ["-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    return proc.returncode == 0 and proc.stdout.strip() != ""


def max_volume_db(path: Path) -> float:
    """Пиковый уровень дорожки. Цифровая тишина даёт около -91 дБ."""
    proc = run_ffmpeg(
        ["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800,
    )
    for line in (proc.stderr or "").splitlines():
        if "max_volume:" in line:
            try:
                return float(line.split("max_volume:")[1].strip().split()[0])
            except (IndexError, ValueError):
                break
    return 0.0


def existing_audio(out_dir: Path, stem: str) -> Path | None:
    """Звук записи, если он уже есть, — ничего не извлекает.

    Нужно странице: по этому файлу человек переслушивает момент, на который
    ссылается метка времени в протоколе.
    """
    cached = out_dir / f"{stem}{AUDIO_SUFFIX}"
    return cached if cached.is_file() else None


def ensure_audio(src: Path, out_dir: Path, stem: str, force: bool, log) -> Path:
    """Возвращает путь к mp3 для пайплайна.

    Звук любого исходника, в том числе mp3, извлекается в mono mp3 64k и кэшируется
    в out_dir (в OUTBOX): исходник остаётся неприкосновенным, а плеер на странице
    работает и после того, как входную папку почистили.
    """
    cached = out_dir / f"{stem}{AUDIO_SUFFIX}"
    if cached.exists() and not force:
        log(f"звук уже извлечён: {cached.name}")
        _reject_if_silent(cached, log)
        return cached
    if not has_audio_stream(src):
        raise NoAudioError("файл не содержит аудиодорожки")
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = cached.with_suffix(".tmp.mp3")
    log("извлекаю звук ffmpeg → mono mp3 64k")
    proc = run_ffmpeg(
        ["-y", "-i", str(src), "-vn", "-ac", "1", "-b:a", "64k", "-f", "mp3", str(tmp)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["ffmpeg failed"]
        raise RuntimeError(f"ffmpeg: {tail[0]}")
    tmp.replace(cached)
    _reject_if_silent(cached, log)
    return cached


def _reject_if_silent(path: Path, log) -> None:
    """Не платить за расшифровку тишины: модели на ней галлюцинируют чужими
    субтитрами вроде «Продолжение следует...», и за это списываются деньги."""
    peak = max_volume_db(path)
    if peak <= SILENCE_DB:
        raise NoAudioError(f"в дорожке нет звука (пиковый уровень {peak:.0f} дБ)")
    log(f"уровень звука: пик {peak:.0f} дБ")
