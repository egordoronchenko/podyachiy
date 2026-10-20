"""Обход входной папки и обработка записей несколькими обработчиками.

Задачи работают параллельно:
  сканер — раз в SCAN_INTERVAL_SEC обходит INBOX и решает, какие файлы дописаны;
  обработчики (WORKERS штук) — разбирают одну очередь: каждый берёт запись
  и ведёт её до конца.
Сканер обработчиков не ждёт: пока идут часовые записи, новые файлы продолжают
отслеживаться и накапливаться в очереди.

Почти вся обработка — ожидание ответа провайдера, поэтому несколько записей сразу
машину не нагружают; нагружает только ffmpeg, и его число ограничено отдельно
(media.FFMPEG_PARALLEL). Одна и та же запись двумя обработчиками не идёт никогда:
задание, чья запись уже в работе, ждёт в очереди её окончания.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from . import marks, media, notify, pipeline
from .config import Settings
from .journal import Journal
from .ledger import MAX_FAILURES, RETRY_AFTER_SEC, RETRY_STATUSES, Ledger

# временные файлы копирования: пока идёт передача, имя выглядит так
TEMP_SUFFIXES = {".tmp", ".part", ".crdownload", ".partial", ".!ut"}
TEMP_PREFIXES = ("~$",)


def hidden(rel: str) -> bool:
    """Служебные папки сервиса начинаются с точки: корзина (.trash), недогрузки (.uploads).
    Записи в них — не новые записи: удалённое не должно пойти в обработку заново."""
    return rel.startswith(".") or "/." in rel


def human_size(n: int) -> str:
    return f"{n / 1048576:.1f} МБ" if n >= 1048576 else f"{n / 1024:.0f} КБ"


@dataclass
class Pending:
    """Файл, за которым сканер следит до признания дописанным."""
    size: int
    mtime: float
    stable: int = 1              # сколько замеров подряд совпали
    since: float = 0.0           # monotonic: когда этот замер увидели впервые
    first_seen: str = ""

    def quiet_sec(self) -> float:
        return time.monotonic() - self.since


@dataclass
class QueueItem:
    path: str
    # None — шаблон протокола выберет разбор записи по расшифровке.
    # Своё значение приходит только из админки: администратор видел файл
    # и решил сам.
    template: str | None = None
    force: bool = False
    manual: bool = False
    by: str = ""                 # кто запустил; пусто — сервис взял запись сам
    reason: str = "new"          # new | retry | manual | rebuild — для журнала расходов
    # Модель распознавания именно для этого запуска; пусто — та, что выбрана для новых
    # записей. Задаётся только вместе с force: без него расшифровка переиспользуется.
    model: str = ""


@dataclass
class Service:
    cfg: Settings
    log: object
    ledger: Ledger
    journal: Journal
    # Модель распознавания для новых записей: выбранная администратором на странице либо
    # значение из .env. Спрашивается перед каждой записью — смена действует без перезапуска.
    speech_model: Callable[[], str]
    # Письмо человеку об исходе обработки: (строка журнала обработки, "ready" | "failed", логин).
    # Задаёт сервис (web.py); зовётся из событийного цикла и не должен его задерживать.
    tell: Callable[[dict, str, str, bool], None]        # запись, исход, кому, ручной ли это запуск
    # Действующий словарь записи по её пути: участники и сокращения её самой и её папок
    # (dictionary.Store.effective). Спрашивается перед каждым заданием — правка словаря
    # действует со следующей обработки.
    words: Callable[[str], dict]
    # Отметки и сроки поручений: для раздела «Поручения прошлого совещания» и карты протокола.
    marked: marks.Store | None = None
    pending: dict[str, Pending] = field(default_factory=dict)
    queue: list[QueueItem] = field(default_factory=list)
    # Записи в работе: по словарю на обработчик, занятый делом. Меняется только из
    # событийного цикла; сканер и страницы читают копию.
    running: list[dict] = field(default_factory=list)
    scans: int = 0
    last_scan: str | None = None
    skipped_outbox: bool = False
    warned_changed: set[str] = field(default_factory=set)
    # Обход входной папки и действия с записью (records.py) друг друга исключают: сканер
    # сверяет файлы на диске с журналом обработки по пути, а переименование меняет и то,
    # и другое не одним шагом. Застань сканер середину — принял бы запись за новую.
    scan_lock: threading.Lock = field(default_factory=threading.Lock)

    # ---------- сканер ----------

    def _ignore_reason(self, p: Path) -> str | None:
        if p.name.startswith(TEMP_PREFIXES):
            return "временный файл"
        if p.suffix.lower() in TEMP_SUFFIXES:
            return "недокопированный файл"
        if p.suffix.lower() not in media.MEDIA_EXTS:
            return "не медиафайл"
        return None

    def _under_outbox(self, rel: str) -> bool:
        """OUTBOX может оказаться внутри INBOX — тогда сканер обязан
        пропускать собственные результаты, иначе они пойдут по кругу."""
        inside = self.cfg.outbox_inside_inbox
        if inside is None:
            return False
        return Path(rel).is_relative_to(inside)

    def _warn_if_replaced(self, rel: str, size: int) -> None:
        """Журнал ведётся по путям: файл, перезаписанный под тем же именем,
        автоматика повторно не возьмёт. Сказать об этом надо один раз."""
        rec = self.ledger.get(rel) or {}
        known = rec.get("source_size")
        if known is None or known == size or rel in self.warned_changed:
            return
        self.warned_changed.add(rel)
        self.log(f"{rel}: размер изменился ({known} → {size} байт), но файл уже есть "
                 f"в журнале — автоматически повторно не обрабатываю. Нужно — "
                 f"запустите вручную с перезаписью")

    def scan_once(self) -> None:
        with self.scan_lock:
            self._scan()

    def _scan(self) -> None:
        self.scans += 1
        self.last_scan = pipeline.now_iso()
        seen: set[str] = set()
        try:
            candidates = sorted(self.cfg.inbox.rglob("*"))
        except OSError as e:
            self.log(f"входная папка недоступна: {e}")
            return

        # Сначала очередь, потом работа: обработчик заносит запись в работу раньше, чем
        # убирает из очереди, и при таком порядке чтения она не выпадет из обоих списков.
        busy = {i.path for i in list(self.queue)} | {j["file"] for j in list(self.running)}

        for p in candidates:
            try:
                if not p.is_file():
                    continue
                rel = p.relative_to(self.cfg.inbox).as_posix()
            except (OSError, ValueError):
                continue

            if self._under_outbox(rel):
                if not self.skipped_outbox:
                    self.skipped_outbox = True
                    self.log("выходная папка лежит внутри входной — "
                             "собственные результаты в обработку не беру")
                continue
            if self._ignore_reason(p) or hidden(rel):
                continue
            if rel in busy:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size == 0:
                continue

            if self.ledger.is_processed(rel):
                self.pending.pop(rel, None)
                if self.ledger.retry_due(rel):
                    rec = self.ledger.get(rel) or {}
                    self.queue.append(QueueItem(path=rel, reason="retry"))
                    self.log(f"повтор после сбоя ({int(rec.get('failures') or 1) + 1}-я попытка "
                             f"из {MAX_FAILURES}): {rel} — прошлый раз: "
                             f"{(rec.get('error') or rec.get('status'))[:150]}")
                else:
                    self._warn_if_replaced(rel, st.st_size)
                continue

            seen.add(rel)
            prev = self.pending.get(rel)
            if prev is None:
                self.pending[rel] = Pending(size=st.st_size, mtime=st.st_mtime,
                                            since=time.monotonic(),
                                            first_seen=self.last_scan)
                self.log(f"новый файл: {rel} ({human_size(st.st_size)}) — "
                         f"жду, пока допишется")
                continue
            if prev.size == st.st_size and prev.mtime == st.st_mtime:
                prev.stable += 1
            else:
                prev.size, prev.mtime = st.st_size, st.st_mtime
                prev.stable, prev.since = 1, time.monotonic()

            # решение по фактически прошедшему времени, а не по числу замеров:
            # требование — размер и mtime не менялись STABLE_SEC
            if prev.quiet_sec() >= self.cfg.stable_sec:
                self.pending.pop(rel, None)
                self.queue.append(QueueItem(path=rel))
                self.log(f"файл дописан (не менялся {prev.quiet_sec():.0f} с, "
                         f"совпавших замеров подряд: {prev.stable}, {human_size(st.st_size)}): "
                         f"{rel} → в очередь (в очереди {len(self.queue)})")

        # файл исчез или переехал — забываем накопленную стабильность
        for rel in [r for r in self.pending if r not in seen]:
            self.pending.pop(rel, None)

    async def scan_loop(self) -> None:
        self.log(f"сканер запущен: {self.cfg.inbox} → {self.cfg.outbox}, "
                 f"опрос раз в {self.cfg.scan_interval_sec} с, файл считается дописанным, "
                 f"когда размер и время изменения не менялись {self.cfg.stable_sec} с "
                 f"(совпавших замеров подряд нужно {self.cfg.stable_samples})")
        while True:
            try:
                await asyncio.to_thread(self.scan_once)
            except Exception as e:  # сканер не должен умирать ни при чём
                self.log(f"сбой сканера (продолжаю): {e!r}")
            await asyncio.sleep(self.cfg.scan_interval_sec)

    # ---------- обработчики ----------

    def enqueue_manual(self, path: str, template: str | None, force: bool,
                       by: str, reason: str, model: str = "") -> None:
        """Запуск человеком: в начало очереди — его возьмёт первый освободившийся
        обработчик."""
        self.queue.insert(0, QueueItem(path=path, template=template,
                                       force=force, manual=True, by=by, reason=reason, model=model))
        self.log(f"ручной запуск ({by}): {path}, "
                 f"шаблон {template or 'по разбору записи'}, "
                 f"force={'да' if force else 'нет'}"
                 + (f", модель распознавания {model}" if model else "") + " → в начало очереди")

    def _take(self) -> tuple[QueueItem, dict] | None:
        """Следующее задание из очереди — и сразу отметка, что запись в работе.

        Задание, чья запись уже идёт у другого обработчика, остаётся в очереди и ждёт
        её окончания: так пересборка после подписей, запрошенная посреди прогона, идёт
        следом за ним, а не одновременно с ним по тем же файлам. Обработчики за это
        время берут другие записи.

        Без await: между выбором и отметкой другой обработчик вклиниться не может.
        """
        in_work = {j["file"] for j in self.running}
        for item in list(self.queue):
            if item.path in in_work:
                continue
            if (not item.force and not item.manual and self.ledger.is_processed(item.path)
                    and not self.ledger.retry_due(item.path)):
                self.queue.remove(item)
                continue
            job = {
                "file": item.path,
                "template": item.template,
                "force": item.force,
                "manual": item.manual,
                "model": item.model,
                "started_at": pipeline.now_iso(),
                # то же числом и без округления: по нему решают, правили ли подписи и словарь
                # после начала прогона — нужна ли следом ещё одна пересборка (web._queue_rebuild)
                "began": time.time(),
                "chunks_done": 0,
                "chunks_total": 0,
                "step": "",
            }
            # в работу — раньше, чем из очереди: сканер читает оба списка из своего потока
            self.running.append(job)
            self.queue.remove(item)
            return item, job
        return None

    async def worker_loop(self) -> None:
        while True:
            taken = self._take()
            if taken is None:
                await asyncio.sleep(1)
                continue
            await self._process(*taken)

    async def _process(self, item: QueueItem, job: dict) -> None:
        # строки нескольких записей идут в лог вперемешку — у каждой подпись, чья она
        tag = f"[{Path(item.path).name}] " if self.cfg.workers > 1 else ""

        def job_log(line: str) -> None:
            # Шаг для страницы берём из тех же строк, что идут в лог: при
            # пересборке расшифровка не трогается, и без этого страница
            # всё время показывала бы «готовлю звук».
            if line.startswith("шаг "):
                job["step"] = line.split(" — ")[0].split(": ", 1)[-1]
                job["chunks_done"] = job["chunks_total"] = 0
            self.log(tag + line)

        async def on_progress(done: int, total: int) -> None:
            job["chunks_done"] = done
            job["chunks_total"] = total

        prev = self.ledger.get(item.path)
        stem = pipeline.stem_of(self.cfg.inbox, item.path, prev)
        try:
            record = await pipeline.process(
                replace(self.cfg, model=item.model or self.speech_model()), item.path, stem, item.template, item.force,
                item.manual, job_log, on_progress,
                neighbours=self.ledger.history(limit=len(self.ledger.records)),
                words=self.words(item.path), marked=self.marked)
            # исход записи и её расход — одной транзакцией: строка журнала обработки без
            # строки расходов означала бы прогон, которого нет в отчёте
            with self.ledger.db.write():
                self.ledger.put(record)
                self.journal.run(record, item.by, item.reason)
        except Exception as e:
            # сюда не должно доходить: pipeline.process сам ловит всё
            self.log(f"необработанный сбой на {item.path} (продолжаю обход): {e!r}")
            record = {
                "file": item.path, "stem": stem, "status": "error", "error": str(e)[:500],
                "started_at": job["started_at"],
                "finished_at": pipeline.now_iso(), "cost": {},
                "manual": item.manual, "force": item.force,
            }
            try:
                self.ledger.put(record)
            except Exception as again:
                # база не принимает запись (диск полон, папка состояния отвалилась): обработчик
                # живёт дальше, а запись сканер возьмёт снова — расшифровка на диске переиспользуется
                self.log(f"ВНИМАНИЕ: исход обработки {item.path} не записан в базу: {again!r}")
        finally:
            # после записи в журнал: иначе сканер успел бы принять запись за новую
            self.running.remove(job)
        self._say_retry_plan(item.path)
        self._announce(item, record, prev)

    def _announce(self, item: QueueItem, record: dict, prev: dict | None) -> None:
        """Письмо о том, чем кончился прогон, — кому и когда, решает notify.due. Само письмо
        уходит не отсюда: обработчик только сообщает, что и кому положено."""
        kind = notify.due(record, prev, item.reason)
        if kind is None:
            return
        if item.reason == "manual":
            to = item.by                                    # запустил человек — ему и ответ
        else:
            self.ledger.announce(item.path, kind)
            to = self.journal.uploader(item.path)           # автор загрузки; у файла, положенного руками, его нет
        if to:
            self.tell(record, kind, to, item.reason == "manual")

    def _say_retry_plan(self, rel: str) -> None:
        """После сбоя — что будет дальше: повторит ли автоматика и когда."""
        rec = self.ledger.get(rel) or {}
        if rec.get("status") not in RETRY_STATUSES:
            return
        failures = int(rec.get("failures") or 1)
        if failures >= MAX_FAILURES:
            self.log(f"{rel}: сбоев подряд {failures} — автоматически больше не повторяю, "
                     f"запустите вручную из админки"
                     + (" без галки force: расшифровка переиспользуется"
                        if rec.get("status") == "no_summary" else ""))
        else:
            self.log(f"{rel}: повторю автоматически через "
                     f"{RETRY_AFTER_SEC * failures // 60} мин")

    # ---------- сводка для страницы статуса ----------

    def inbox_files(self) -> list[dict]:
        """Все медиафайлы входной папки — для выбора администратором,
        включая уже обработанные."""
        out: list[dict] = []
        try:
            candidates = sorted(self.cfg.inbox.rglob("*"))
        except OSError:
            return out
        for p in candidates:
            try:
                if not p.is_file():
                    continue
                rel = p.relative_to(self.cfg.inbox).as_posix()
                if hidden(rel):
                    continue
            except (OSError, ValueError):
                continue
            if self._under_outbox(rel) or self._ignore_reason(p):
                continue
            rec = self.ledger.get(rel)
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            out.append({
                "path": rel,
                "size": size,
                "status": (rec or {}).get("status"),
                "finished_at": (rec or {}).get("finished_at"),
                "cost": (rec or {}).get("cost"),
            })
        return out

    def snapshot(self) -> dict:
        return {
            "running": list(self.running),
            "queue": [{"path": i.path, "template": i.template, "force": i.force,
                       "manual": i.manual, "model": i.model} for i in self.queue],
            "watching": [
                {"path": rel, "size": p.size, "stable": p.stable,
                 "needed": self.cfg.stable_samples,
                 "quiet_sec": round(p.quiet_sec()), "stable_sec": self.cfg.stable_sec,
                 "first_seen": p.first_seen}
                for rel, p in sorted(self.pending.items())
            ],
            "scans": self.scans,
            "last_scan": self.last_scan,
        }
