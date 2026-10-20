"""Лог: stdout, файл в OUTBOX с ротацией и кольцевой буфер для страницы статуса.

Ключ API в лог не попадает: сюда пишутся только имена файлов, модели и цифры.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import time
from collections import deque
from pathlib import Path

LOG_DIR = "_logs"
LOG_FILE = "podyachiy.log"
TAIL = 400
MAX_BYTES = 5_000_000   # ~5 МБ на файл
BACKUPS = 3             # плюс три предыдущих: примерно 20 МБ на всю историю


class Logbook:
    """Одна запись — три адресата: консоль контейнера, файл, страница статуса.

    Файл ведёт RotatingFileHandler: сервис работает годами без присмотра,
    и лог, растущий без предела, однажды займёт диск целиком.

    Пишут сюда с разных сторон: сканер и ffmpeg — из потоков, обработчики — из
    событийного цикла. emit у обработчика logging замок не берёт (его берёт handle),
    а счётчик строк и буфер должны меняться вместе, поэтому замок здесь свой.
    """

    def __init__(self, outbox: Path):
        self.path = outbox / LOG_DIR / LOG_FILE
        self.lines: deque[str] = deque(maxlen=TAIL)
        self.total = 0          # сколько строк прошло за всё время работы
        self._lock = threading.Lock()
        self._file = self._make_handler()

    def _make_handler(self) -> logging.Handler | None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                self.path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            return handler
        except OSError as e:
            # выходная папка может быть недоступна — сервис при этом работать должен
            print(f"не могу писать лог в {self.path}: {e}", flush=True)
            return None

    def __call__(self, line: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        full = f"{stamp} {line}"
        with self._lock:
            self.lines.append(full)
            self.total += 1
            print(full, flush=True, file=sys.stdout)
            if self._file is None:
                return
            try:
                self._file.emit(logging.LogRecord(
                    "podyachiy", logging.INFO, "", 0, full, None, None))
            except Exception as e:  # диск кончился, папка отвалилась — не падаем
                print(f"{stamp} лог в файл больше не пишется: {e}", flush=True)
                self._file = None

    def tail(self, limit: int = TAIL) -> list[str]:
        with self._lock:
            return list(self.lines)[-limit:]

    def tail_from(self, seen: int) -> tuple[int, list[str]]:
        """Строки, которых у клиента ещё нет.

        Возвращает (номер первой отданной строки, строки). Если клиент отстал
        сильнее, чем длина буфера, или сервис перезапускался и счётчик пошёл
        заново, отдаётся весь буфер — клиент увидит несовпадение номера
        и заменит свой список целиком.
        """
        with self._lock:
            first_in_buffer = self.total - len(self.lines)
            if seen < first_in_buffer or seen > self.total:
                return first_in_buffer, list(self.lines)
            return seen, list(self.lines)[len(self.lines) - (self.total - seen):]
