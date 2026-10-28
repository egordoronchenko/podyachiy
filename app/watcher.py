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
import json
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from . import folders, marks, media, notify, pipeline, records, upload, webhook
from .config import Settings
from .journal import Journal
from .ledger import CANCELLED, MAX_FAILURES, RETRY_AFTER_SEC, RETRY_STATUSES, SOURCE_CHANGED, SOURCE_STATE, Ledger

# временные файлы копирования: пока идёт передача, имя выглядит так
TEMP_SUFFIXES = {".tmp", ".part", ".crdownload", ".partial", ".!ut"}
TEMP_PREFIXES = ("~$",)
# Исход прогона, который база не приняла, пробуем записать снова не чаще этого
SAVE_RETRY_SEC = 30
# Недокопированный файл (cut_short): чем он выдаёт себя ffmpeg — у mp4 и mov, у mkv и webm
CUT_MARKS = ("partial file", "File ended prematurely")
CUT_TAIL_SEC = 30            # с какого места до конца звука читать файл, чтобы найти его настоящий конец
CUT_GAP_SEC = 2.0            # насколько звук в файле может кончаться раньше, чем обещает заголовок
MP4_HEADS = {b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"}    # первый блок файла mp4 и mov (m4a, m4v)


def cut_short(path: Path) -> str | None:
    """Чем файл похож на недокопированный, или None.

    Устойчивость размера этого не видит: копия по сети, замершая посреди файла, тоже стоит на месте.
    Платить за распознавание обрубка нельзя, а взятый в обработку файл автоматика второй раз не
    возьмёт. Проверки — бесплатные и только те, что на целых файлах не срабатывают:
      - заголовок WAV называет размер файла, а на диске его меньше;
      - файл mp4 (по первому блоку) ffmpeg не открывает: оглавление пишется в конце, у обрубка
        его нет;
      - ffmpeg сам говорит, что файл кончился раньше времени (CUT_MARKS): у mp4 с оглавлением в
        начале нет данных, на которые оно указывает, у mkv и webm файл обрывается посреди блока;
      - заголовок дорожки называет её длительность (mp4, mp3 с заголовком Xing, flac, wma), а звук
        в файле кончается раньше больше чем на CUT_GAP_SEC. Длительность, которую ffmpeg лишь
        прикинул по битрейту, в счёт не идёт.
    Потоки без длины в заголовке (ogg, aac, mp3 без Xing, ts) так не проверить — у них обрубок
    неотличим от короткой записи. Файл, который ffmpeg не разобрал вовсе, — не сюда: обработка
    скажет «нет звука», денег это не стоит."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return None
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        declared = int.from_bytes(head[4:8], "little") + 8
        if declared not in (8, 0xFFFFFFFF + 8) and declared > size:
            return f"по заголовку WAV в файле {declared} байт, а в нём {size}"

    def probe(*args: str) -> subprocess.CompletedProcess | None:
        try:
            return media.run_ffprobe([*args, str(path)], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return None

    def mark(said: str) -> str | None:
        return next((f"ffmpeg: «{m}»" for m in CUT_MARKS if m in said), None)

    head_info = probe("-v", "warning", "-select_streams", "a:0", "-show_entries", "format=duration:stream=duration",
                      "-of", "json")
    if head_info is None:
        return None
    if found := mark(head_info.stderr or ""):
        return found
    if head_info.returncode != 0 and head[4:8] in MP4_HEADS:
        # mp4 пишется с оглавлением в конце: у обрубка его нет, и ffmpeg файл не открывает вовсе.
        # Файл, который mp4 только назван, сюда не попадает: первого блока контейнера у него нет
        said = (head_info.stderr or "").strip().splitlines()
        return "ffmpeg не открывает файл mp4" + (f": «{said[0].split('] ', 1)[-1]}»" if said else "")
    try:
        info = json.loads(head_info.stdout) if head_info.returncode == 0 else {}
    except ValueError:
        info = {}
    if not info.get("streams"):
        return None                     # не разобран или звука нет — это скажет обработка
    own = info["streams"][0].get("duration")
    try:
        length = float(own or info.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return None
    tail = probe("-v", "error", "-select_streams", "a:0", "-read_intervals", f"{max(0.0, length - CUT_TAIL_SEC)}%",
                 "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0")
    if tail is None:
        return None
    if found := mark(tail.stderr or ""):
        return found
    if own is None or "Estimating duration from bitrate" in (head_info.stderr or ""):
        return None
    last = 0.0
    for line in tail.stdout.split():
        start, _, took = line.partition(",")
        try:
            last = max(last, float(start) + (float(took) if took and took != "N/A" else 0.0))
        except ValueError:
            continue
    if length - last > CUT_GAP_SEC:
        return f"по заголовку звук длится {length:.0f} с, а в файле он кончается на {last:.0f} с"
    return None


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
    since: float = 0.0           # monotonic: когда этот замер увидели впервые
    first_seen: str = ""
    cut: str | None = None       # чем файл в этом замере похож на недокопированный (cut_short); None — не проверен
    # почему файл ждёт решения администратора, а не идёт в обработку сам: это копия готовой записи или её же
    # исходник под другим именем (Service._twin); "" — проверен, это новая запись; None — не проверен
    held: str | None = None

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
    # Отмечена ли папка записи как серия совещаний — по пути записи (series.Store.on её папки):
    # только тогда в конец протокола дописываются незакрытые поручения предыдущей записи папки.
    # Спрашивается перед каждым заданием — переключатель действует со следующей обработки.
    series: Callable[[str], bool]
    # Отметки и сроки поручений: для раздела «Поручения прошлого совещания» и карты протокола.
    marked: marks.Store | None = None
    pending: dict[str, Pending] = field(default_factory=dict)
    queue: list[QueueItem] = field(default_factory=list)
    # Записи в работе: по словарю на обработчик, занятый делом. Меняется только из
    # событийного цикла; сканер и страницы читают копию.
    running: list[dict] = field(default_factory=list)
    # Исходы прогонов, которые база не приняла (диск полон, папка состояния отвалилась): по
    # пути записи — (задание, строка журнала обработки, строка до прогона, писать ли прогон в
    # журнал событий). Пока исход здесь, запись в обработку не идёт: строки журнала у неё нет, и
    # сканер счёл бы её новой — платный прогон заново, по кругу, а расход первого пропал бы.
    # Обработчики пробуют записать их снова (_save_unsaved). Живут в памяти: перезапуск
    # сервиса до записи их теряет.
    unsaved: dict[str, tuple] = field(default_factory=dict)
    unsaved_at: float = 0.0      # monotonic: когда последний раз пробовали записать
    scans: int = 0
    last_scan: str | None = None
    skipped_outbox: bool = False
    warned_form: set[str] = field(default_factory=set)      # имена в разложенной форме, о которых уже сказано
    # Записи журнала обработки, чьих исходников последний обход во входной папке не нашёл: для списка записей
    # («исходник отсутствует»). До первого обхода пусто.
    missing: set[str] = field(default_factory=set)
    # Исходники, переименованные во входной папке мимо сервиса, которые нашёл обход: новый путь → запись.
    # Переезжают в событийном цикле (settle), а не в потоке обхода.
    adopt: dict[str, str] = field(default_factory=dict)
    # Для переезда записей и папок тем же путём, что у кнопок «Переименовать» (records.py, folders.py): права
    # пользователей (users.Users) и словари (dictionary.Store). Задаёт сервис (web.py).
    people: object | None = None
    dictionaries: object | None = None
    # Записи, которые администратор снял из очереди (unqueue): путь → {size, mtime, by, at}. Сканер их сам не
    # берёт — иначе снятая новая запись тут же вернулась бы в очередь и ушла в платное распознавание, — пока
    # файл не изменится или запись не запустят вручную. Хранятся в базе (keep_dropped — записать весь список;
    # задаёт сервис): перезапуск их не забывает.
    dropped: dict[str, dict] = field(default_factory=dict)
    keep_dropped: Callable[[dict], None] | None = None
    # Webhook «запись готова»: (строка журнала обработки, событие webhook.READY | webhook.FAILED). Когда событие
    # положено, решает webhook.due; задаёт сервис (web.py), зовётся из событийного цикла и не должен его держать:
    # само событие только ложится в очередь доставки.
    hook: Callable[[dict, str], None] | None = None
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

    def _check_source(self, rel: str, p: Path, st) -> None:
        """Исходник готовой записи изменился с тех пор, как его обработали (докопировали, перезаписали)?
        Журнал ведётся по путям: такой файл автоматика заново не возьмёт — распознавание снова стоило бы
        денег, — а запомнит, что исходник изменился (SOURCE_STATE): это видно в списке записей. Вернули
        прежний файл — отметка снимается. Сличаются размер и время изменения; время другое при том же
        размере — ещё и отпечаток: файл могли только тронуть. Строка журнала работы — при смене состояния."""
        rec = self.ledger.get(rel) or {}
        size, mtime = rec.get("source_size"), rec.get("source_mtime")
        if size is None:
            return                      # строка давних версий: каким был исходник, не записано
        changed = rec.get(SOURCE_STATE) == SOURCE_CHANGED
        same = size == st.st_size and mtime in (None, st.st_mtime)
        if not same and not changed and size == st.st_size and rec.get("source_print"):
            same = media.fingerprint(p) == rec["source_print"]
            if same:
                self.ledger.amend(rel, source_mtime=st.st_mtime)
        if same != changed:
            return
        if same:
            self.ledger.amend(rel, **{SOURCE_STATE: None})
            self.log(f"{rel}: исходник снова тот, что был при обработке")
            return
        self.ledger.amend(rel, **{SOURCE_STATE: SOURCE_CHANGED})
        how = (f"размер {size} → {st.st_size} байт" if size != st.st_size
               else "размер тот же, а содержимое другое" if rec.get("source_print") else "другое время изменения")
        self.log(f"{rel}: исходник изменился после обработки ({how}) — протокол и расшифровка сделаны по прежнему "
                 f"файлу. Автоматически заново не распознаю: это стоило бы денег. Нужно по новому файлу — запустите "
                 f"запись вручную с перезаписью (force)")

    def _twin(self, rel: str, p: Path, size: int, busy: set[str]) -> str | None:
        """Не известна ли журналу обработки эта запись под другим путём. Журнал ведётся по путям, и файл,
        переименованный или скопированный во входной папке мимо сервиса, был бы для него новой записью —
        платное распознавание заново. Сличаются размер до байта, а у совпавших — отпечаток содержимого
        (media.fingerprint):
          - совпал с записью, чей исходник пропал с прежнего пути, — это она же, переименованная: запись
            с результатами переезжает на новый путь (settle) тем же переездом, что у кнопки «Переименовать»;
          - совпал с записью, чей исходник на месте, — копия. Сам сервис её не распознаёт: та же запись
            за те же деньги второй раз, а результаты копии разошлись бы с прежними (подписи, отметки,
            словари). Ждёт решения администратора: нужна и здесь — ручной запуск, нет — убрать файл;
          - у записи без отпечатка (обработана до версии 1.2.0) он считается сейчас, если её исходник на
            месте и того же размера; исходника нет — не сличить, и файл тоже ждёт решения.
        Отдаёт, почему файл ждёт (для страницы), "" — это новая запись, None — решать на следующем обходе:
        прежняя запись сейчас в работе."""
        same = [rec for file, rec in list(self.ledger.records.items()) if file != rel and rec.get("source_size") == size]
        if not same:
            return ""
        mine = media.fingerprint(p)
        if mine is None:
            return ""
        copy = unsure = None
        for rec in same:
            file = rec["file"]
            there = self.cfg.inbox / file
            on_disk = there.is_file()
            known = rec.get("source_print")
            if (known is None and on_disk and file not in busy and rec.get(SOURCE_STATE) != SOURCE_CHANGED
                    and pipeline._size_of(there) == size):
                known = media.fingerprint(there)            # обработана до отпечатков: считаем, раз понадобился
                if known:
                    self.ledger.amend(file, source_print=known)
            if known is None:
                if not on_disk:
                    unsure = unsure or file
                continue
            if known != mine:
                continue
            if on_disk:
                copy = copy or file
                continue
            if file in busy:
                return None
            self.adopt[rel] = file
            return f"это запись {file}, переименованная мимо сервиса, — переносится"
        if copy:
            self.log(f"{rel}: копия записи {copy} (то же содержимое, её исходник на месте) — в обработку не беру: "
                     f"распознавание той же записи снова стоило бы денег. Нужна и здесь — запустите её вручную; "
                     f"не нужна — уберите файл")
            return f"копия записи {copy} — запустите вручную или уберите файл"
        if unsure:
            self.log(f"{rel}: размер до байта тот же, что у записи {unsure}, чей исходник пропал, а отпечатка "
                     f"содержимого у неё нет (обработана до версии 1.2.0) — в обработку не беру. Если это она же под "
                     f"новым именем — верните файлу прежнее имя и переименуйте запись на странице: результаты переедут "
                     f"без распознавания; если другая — запустите её вручную")
            return f"того же размера, что запись {unsure} без исходника, — проверьте и запустите вручную"
        return ""

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
        busy = ({i.path for i in list(self.queue)} | {j["file"] for j in list(self.running)}
                | set(list(self.unsaved)))
        present: set[str] = set()       # все файлы входной папки: чьих исходников нет (missing)
        if self._folders_to_nfc(candidates, busy):
            try:
                candidates = sorted(self.cfg.inbox.rglob("*"))
            except OSError as e:
                self.log(f"входная папка недоступна: {e}")
                return

        for p in candidates:
            try:
                if not p.is_file():
                    continue
                rel = p.relative_to(self.cfg.inbox).as_posix()
            except (OSError, ValueError):
                continue
            present.add(rel)

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
            if rel in self.dropped and self._still_dropped(rel, st):
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
                    self._check_source(rel, p, st)
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
            if (prev.size, prev.mtime) != (st.st_size, st.st_mtime):
                prev.size, prev.mtime = st.st_size, st.st_mtime
                prev.since, prev.cut, prev.held = time.monotonic(), None, None

            # решение по прошедшему времени: размер и mtime не менялись STABLE_SEC
            if prev.quiet_sec() >= self.cfg.stable_sec:
                if prev.cut is None:
                    prev.cut = cut_short(p) or ""
                    if prev.cut:
                        self.log(f"{rel}: похоже, файл докопирован не до конца ({prev.cut}) — в обработку не беру, "
                                 f"жду, пока он изменится. Если запись такая и есть — запустите её вручную")
                if prev.cut:
                    continue
                if prev.held is None:
                    prev.held = self._twin(rel, p, st.st_size, busy)
                if prev.held != "":
                    continue                    # ждёт решения администратора или переезда записи (settle)
                taken = self._file_to_nfc(rel, busy)
                if taken is None:
                    continue                    # рядом ещё копируется другой файл той же папки — следующим обходом
                self.pending.pop(rel, None)
                self.queue.append(QueueItem(path=taken))
                self.log(f"файл дописан (не менялся {prev.quiet_sec():.0f} с, {human_size(st.st_size)}): "
                         f"{taken} → в очередь (в очереди {len(self.queue)})")

        # файл исчез или переехал — забываем накопленную стабильность
        for rel in [r for r in self.pending if r not in seen]:
            self.pending.pop(rel, None)
            self.adopt.pop(rel, None)
        self.missing = {file for file in list(self.ledger.records) if file not in present}

    def settle(self) -> None:
        """Переезды записей, найденные обходом (_twin), — уже не в его потоке, а в событийном цикле: там же
        живут обработчики и ручной запуск, и переезд, как у кнопки «Переименовать», идёт без await под
        замком сканера (подробно — в records.py). Замок занят действием со страницы — до следующего обхода."""
        if not self.adopt or not self.scan_lock.acquire(blocking=False):
            return
        try:
            for new, old in list(self.adopt.items()):
                del self.adopt[new]
                self._adopt(old, new)
        finally:
            self.scan_lock.release()

    def _adopt(self, old: str, new: str) -> None:
        """Запись old, чей исходник переименовали мимо сервиса в new, — на новый путь: результаты, строка
        журнала, отметки поручений, словарь записи и событие в журнале (records.adopt). Не вышло — файл ждёт
        решения администратора, распознавания нет."""
        kind = "record_rename" if old.rpartition("/")[0] == new.rpartition("/")[0] else "record_move"

        def also(target: str) -> None:
            if self.dictionaries is not None:
                self.dictionaries.move_record(old, target)
            self.journal.write(kind, "", file=old, to=target)

        try:
            records.adopt(self.cfg, self, old, new, also=also)
        except Exception as e:
            why = e.detail if isinstance(e, records.RecordError) else (getattr(e, "strerror", None) or str(e))
            held = f"похоже, это запись {old}, переименованная мимо сервиса, но её результаты не перенести ({why})"
            if (watch := self.pending.get(new)) is not None:
                watch.held = held
            self.log(f"{new}: {held} — в обработку не беру. Верните файлу прежнее имя или запустите его вручную")
            return
        self.pending.pop(new, None)
        self.log(f"{old} → {new}: исходник записи переименован во входной папке мимо сервиса (содержимое то же) — "
                 f"запись с результатами переехала следом, заново не распознаю")

    # ---------- имена в разложенной форме (NFD) ----------
    # Страница, база и права знают имена в составной форме (upload.nfc): так их приводит всё, что
    # приходит от людей. А Mac, копируя по сети, кладёт «й» и «ё» разложенными — и запись под таким
    # именем жила бы отдельно от того, что о ней знают страница и права. Сканер приводит к составной
    # форме имена новых файлов и папок на диске, о которых журнал обработки ещё ничего не знает.
    # Записанное в журнал под разложенным именем остаётся как есть: его строка журнала, права и
    # словари — под прежним именем.

    def _form_kept(self, rel: str, why: str) -> None:
        if rel not in self.warned_form:
            self.warned_form.add(rel)
            self.log(f"{rel}: имя в разложенной форме Unicode (так пишет macOS), {why} — оставляю как есть")

    def _nfc_block(self, rel: str, busy: set[str], own: str = "") -> str | None:
        """Почему имя rel сейчас нельзя привести к составной форме: "wait" — внутри ещё копируется
        или обрабатывается файл (own — тот, ради которого и переименовываем); текст — насовсем; None — можно."""
        old, new = self.cfg.inbox / rel, self.cfg.inbox / Path(rel).with_name(upload.nfc(Path(rel).name))
        if new.exists() and not folders.same_place(old, new):      # тот же файл — у тома, что форм не различает
            return f"а рядом уже есть «{new.name}» в составной форме"
        inside = lambda path: path == rel or path.startswith(rel + "/")
        if any(inside(path) for path in self.ledger.records):
            return "а журнал обработки знает записи под ним"
        # ждать — пока внутри копируется файл; докопированные соседи (ждут того же) не держат
        copying = [path for path, seen in self.pending.items() if seen.quiet_sec() < self.cfg.stable_sec]
        if any(inside(path) and path != own for path in [*copying, *busy]):
            return "wait"
        return None

    def known_to_nfc(self) -> None:
        """Разовое приведение при запуске: записи и папки, которые журнал обработки знает под именем в
        разложенной форме (обработаны так до версии 1.2.0), — к составной. Ссылки страницы и права — в
        составной форме, и такая папка на странице не открывалась бы. Переезд — тот же, что у кнопок
        «Переименовать» (folders.to_nfc, records.rename): исходники, результаты, журнал, отметки, права,
        словари, признак серии и событие в журнале. Сначала — самая верхняя часть пути в разложенной
        форме; не вышло (рядом то же имя в составной форме) — остаётся как есть, строкой в журнале работы."""
        kept: set[str] = set()
        while True:
            todo = sorted({here for file in list(self.ledger.records) if (here := self._decomposed(file))} - kept)
            if not todo:
                return
            for here in todo:
                try:
                    new = self._to_nfc(here)
                except Exception as e:
                    kept.add(here)
                    why = (e.detail if isinstance(e, (records.RecordError, folders.FolderError))
                           else getattr(e, "strerror", None) or str(e))
                    self._form_kept(here, f"а переименовать не вышло ({why})")
                    continue
                self.log(f"{here}: имя приведено к составной форме Unicode (разложенным его пишет macOS) — {new}; "
                         "результаты, журнал, права и словари — под новым именем")

    def _to_nfc(self, here: str) -> str:
        """Запись или папку here (последняя часть пути — в разложенной форме) — в составную. Новый путь."""
        if here in self.ledger.records:
            def moved(target: str) -> None:
                if self.dictionaries is not None:
                    self.dictionaries.move_record(here, target)
                self.journal.write("record_rename", "", file=here, to=target)
            return records.rename(self.cfg, self, here, upload.nfc(Path(here).name), also=moved)

        def renamed(target: str) -> None:
            if self.dictionaries is not None:
                self.dictionaries.move_folder(here, target)
            self.journal.write("folder_rename", "", folder=here, to=target)
        return folders.to_nfc(self.cfg, self, self.people, here, also=renamed)

    @staticmethod
    def _decomposed(rel: str) -> str | None:
        """Самая верхняя часть пути в разложенной форме — путём до неё включительно; None — таких нет."""
        parts = rel.split("/")
        for n, part in enumerate(parts, 1):
            if not unicodedata.is_normalized("NFC", part):
                return "/".join(parts[:n])
        return None

    def _rename_nfc(self, rel: str) -> str:
        new = Path(rel).with_name(upload.nfc(Path(rel).name)).as_posix()
        folders.move(self.cfg.inbox / rel, self.cfg.inbox / new)
        self.log(f"{rel}: имя приведено к составной форме Unicode (разложенным его пишет macOS) — {new}")
        return new

    def _folders_to_nfc(self, candidates: list[Path], busy: set[str]) -> bool:
        """Папки в разложенной форме без файлов внутри — к составной. Папка с файлами — вместе с ними,
        когда они докопированы (_file_to_nfc): посреди копии папку не переименовывают. Переименовано ли
        что-нибудь."""
        moved: list[str] = []
        for p in candidates:
            try:
                rel = p.relative_to(self.cfg.inbox).as_posix()
                if unicodedata.is_normalized("NFC", p.name) or not p.is_dir():
                    continue
            except (OSError, ValueError):
                continue
            if hidden(rel) or self._under_outbox(rel) or any(rel.startswith(m + "/") for m in moved):
                continue
            why = self._nfc_block(rel, busy)
            if why and why != "wait":
                self._form_kept(rel, why)
                continue
            try:
                if why == "wait" or any(c.is_file() for c in p.rglob("*")):
                    continue                    # файлы в ней — когда докопируются (_file_to_nfc)
                self._rename_nfc(rel)
            except OSError as e:
                self._form_kept(rel, f"а переименовать не вышло ({e.strerror or e})")
                continue
            moved.append(rel)
        return bool(moved)

    def _file_to_nfc(self, rel: str, busy: set[str]) -> str | None:
        """Докопированный файл — под именем в составной форме, вместе с папками в разложенной над ним.
        Отдаёт путь, под которым запись идёт в очередь; None — подождать: в той же папке ещё
        копируется другой файл. Докопированные соседи по такой папке найдутся под её новым именем
        следующим обходом."""
        parts = rel.split("/")
        for n in range(1, len(parts) + 1):
            here = "/".join(parts[:n])
            if unicodedata.is_normalized("NFC", parts[n - 1]):
                continue
            why = self._nfc_block(here, busy, own=rel)
            if why == "wait":
                return None
            if why:
                self._form_kept(here, why)
                return "/".join(parts)
            try:
                new = self._rename_nfc(here)
            except OSError as e:
                self._form_kept(here, f"а переименовать не вышло ({e.strerror or e})")
                return "/".join(parts)
            parts[n - 1] = new.rsplit("/", 1)[-1]
            rel = "/".join(parts)
        return "/".join(parts)

    async def scan_loop(self) -> None:
        self.log(f"сканер запущен: {self.cfg.inbox} → {self.cfg.outbox}, "
                 f"опрос раз в {self.cfg.scan_interval_sec} с, файл считается дописанным, "
                 f"когда размер и время изменения не менялись {self.cfg.stable_sec} с")
        while True:
            try:
                await asyncio.to_thread(self.scan_once)
                self.settle()
            except Exception as e:  # сканер не должен умирать ни при чём
                self.log(f"сбой сканера (продолжаю): {e!r}")
            await asyncio.sleep(self.cfg.scan_interval_sec)

    # ---------- обработчики ----------

    # ---------- снять из очереди, отменить прогон ----------

    def _set_dropped(self, entries: dict) -> None:
        """Список снятых из очереди — сначала в базу, потом в память: не записался — не изменился."""
        if self.keep_dropped is not None:
            self.keep_dropped(entries)
        self.dropped = entries

    def _forget_dropped(self, rel: str) -> None:
        if rel in self.dropped:
            self._set_dropped({k: v for k, v in self.dropped.items() if k != rel})

    def _still_dropped(self, rel: str, st) -> bool:
        """Снятая из очереди запись та же — размер и время изменения те же: сканер её не берёт. Файл
        изменился — это уже другая запись: отметка снимается, и дальше файл идёт как любой."""
        seen = self.dropped[rel]
        if (seen["size"], seen["mtime"]) == (st.st_size, st.st_mtime):
            return True
        self._forget_dropped(rel)
        self.log(f"{rel}: файл изменился после того, как его сняли из очереди, — дальше как с любым файлом")
        return False

    def unqueue(self, path: str, by: str) -> bool | None:
        """Администратор снимает запись из очереди — все её задания там; идущий прогон не трогается
        (его отменяет cancel). None — в очереди её нет. True — сервис запомнил, что её сняли вручную:
        иначе сканер взял бы её снова (новая запись, повтор после сбоя). False — помнить нечего: сам
        сканер её и так не возьмёт (готовая запись, у которой сняли пересборку)."""
        items = [i for i in list(self.queue) if i.path == path]
        if not items:
            return None
        remember = not self.ledger.is_processed(path) or self.ledger.retry_due(path)
        if remember:
            try:
                st = (self.cfg.inbox / path).stat()
            except OSError:
                remember = False                # файла нет — сканер его и не увидит
            else:
                self._set_dropped({**self.dropped, path: {"size": st.st_size, "mtime": st.st_mtime, "by": by,
                                                          "at": pipeline.now_iso()}})
        for item in items:
            self.queue.remove(item)
        self.log(f"{path}: снята из очереди вручную ({by})"
                 + (" — сама в обработку не пойдёт, пока файл не изменится или её не запустят вручную" if remember else ""))
        return remember

    def cancel(self, path: str, by: str) -> dict | None:
        """Администратор отменяет идущий прогон записи: тот остановится перед следующим платным запросом
        (pipeline.checkpoint), идущий запрос поставщику доработает. Отдаёт отметку {by, at}; None — запись
        сейчас не обрабатывается. Отменить уже отменяемое — та же, первая отметка."""
        for job in list(self.running):
            if job["file"] == path:
                if not job.get("cancel"):
                    job["cancel"] = {"by": by, "at": pipeline.now_iso()}
                    self.log(f"{path}: {by} просит отменить прогон — остановлю перед следующим платным запросом; "
                             f"идущий запрос поставщику не обрываю: за него могли уже списать деньги")
                return job["cancel"]
        return None

    @staticmethod
    def _cancelled(record: dict, prev: dict | None) -> dict:
        """Отменённый прогон — не исход записи. Запись, исход которой уже был (готова, без протокола, без
        звука), им и остаётся — её результаты на месте; от прогона у неё — его расход и отметка об отмене.
        Новая запись и запись после сбоя — с исходом «отменено» (ошибка с отметкой CANCELLED): по расписанию
        такая не повторяется (ledger.retry_due)."""
        if not prev or prev.get("status") in (None, "error"):
            return record
        spent = {k: record[k] for k in ("started_at", "cost", "cost_details", "cost_models", "cost_providers",
                                        "api_calls") if k in record}
        return {**prev, **spent, CANCELLED: record[CANCELLED]}

    def enqueue_manual(self, path: str, template: str | None, force: bool,
                       by: str, reason: str, model: str = "") -> None:
        """Запуск человеком: в начало очереди — его возьмёт первый освободившийся
        обработчик. Снятая прежде из очереди запись этим снова разрешена."""
        self._forget_dropped(path)
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

        Задание по записи, чей исход база ещё не приняла (unsaved), тоже ждёт: и ручной запуск —
        до записи исхода прежнего прогона.

        Без await: между выбором и отметкой другой обработчик вклиниться не может.
        """
        in_work = {j["file"] for j in self.running} | set(self.unsaved)
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
            try:
                self._save_unsaved()
                taken = self._take()
                if taken is None:
                    await asyncio.sleep(1)
                    continue
                await self._process(*taken)
            except Exception as e:  # обработчик, как и сканер, не должен умирать ни при чём
                self.log(f"сбой обработчика (продолжаю): {e!r}")
                await asyncio.sleep(1)

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
        pipeline.halt.set(lambda: job.get("cancel"))        # отменил ли администратор (cancel)
        try:
            stem = (pipeline.stem_of(self.cfg.inbox, item.path, prev) if prev
                    else pipeline.stem_for(self.cfg.inbox, item.path, self.ledger.records))
            run = True
            try:
                record = await pipeline.process(
                    replace(self.cfg, model=item.model or self.speech_model()), item.path, stem, item.template,
                    item.force, item.manual, job_log, on_progress,
                    neighbours=self.ledger.history(limit=len(self.ledger.records)),
                    words=self.words(item.path), marked=self.marked,
                    series=self.series(item.path))
            except Exception as e:
                # сюда не должно доходить: pipeline.process сам ловит всё
                self.log(f"необработанный сбой на {item.path} (продолжаю обход): {e!r}")
                record = {
                    "file": item.path, "stem": stem, "status": "error", "error": str(e)[:500],
                    "started_at": job["started_at"],
                    "finished_at": pipeline.now_iso(), "cost": {},
                    "manual": item.manual, "force": item.force,
                }
                run = False
            if record.get(CANCELLED):
                record = self._cancelled(record, prev)
            self._source_kept(record, prev)
            saved = self._save(item, record, prev, run)
        finally:
            # после записи в журнал или в unsaved: иначе сканер успел бы принять запись за новую
            self.running.remove(job)
        if saved:
            self._after(item, record, prev)

    @staticmethod
    def _source_kept(record: dict, prev: dict | None) -> None:
        """Пересборка по прежней расшифровке описывает прежний файл. Если исходник с тех пор изменился,
        в строке остаётся, каким он был при распознавании, и отметка «исходник изменился»: иначе прогон
        выдал бы старый протокол за протокол нового файла. Полный прогон (force) расшифровку не берёт."""
        if not prev or not record.get("reused_transcript"):
            return
        if (prev.get(SOURCE_STATE) == SOURCE_CHANGED
                or prev.get("source_size") not in (None, record.get("source_size"))
                or prev.get("source_print") not in (None, record.get("source_print"))):
            record.update({name: prev.get(name) for name in ("source_size", "source_mtime", "source_print")})
            record[SOURCE_STATE] = SOURCE_CHANGED

    def _save(self, item: QueueItem, record: dict, prev: dict | None, run: bool) -> bool:
        """Исход прогона — в базу. Исход записи и её расход — одной транзакцией: строка журнала
        обработки без строки расходов означала бы прогон, которого нет в отчёте. Не приняла
        база — исход остаётся в unsaved, тот самый, с расходом; записать его снова пробуют
        обработчики (_save_unsaved)."""
        kept = dict(record)                 # put дописывает в строку счётчики — для повтора нужна прежняя
        try:
            with self.ledger.db.write():
                self.ledger.put(record)
                if run:
                    self.journal.run(record, item.by, item.reason)
        except Exception as e:
            if item.path not in self.unsaved:
                self.log(f"ВНИМАНИЕ: исход обработки {item.path} не записан в базу ({e!r}). Он и расход прогона "
                         f"ждут в памяти сервиса, а запись в обработку снова не идёт, пока исход не записан: "
                         f"пробую записать раз в {SAVE_RETRY_SEC} с. Проверьте место на диске и папку состояния; "
                         f"перезапуск сервиса до записи исход потеряет")
            self.unsaved[item.path] = (item, kept, prev, run)
            self.unsaved_at = time.monotonic()
            return False
        if self.unsaved.pop(item.path, None) is not None:
            self.log(f"исход обработки {item.path} записан в базу")
        return True

    def _save_unsaved(self) -> None:
        """Исходы, которые база не приняла, — ещё раз, не чаще раза в SAVE_RETRY_SEC."""
        if not self.unsaved or time.monotonic() - self.unsaved_at < SAVE_RETRY_SEC:
            return
        self.unsaved_at = time.monotonic()
        for item, record, prev, run in list(self.unsaved.values()):
            record = dict(record)
            if self._save(item, record, prev, run):
                self._after(item, record, prev)

    def _after(self, item: QueueItem, record: dict, prev: dict | None) -> None:
        """Исход записан: что будет дальше и кому письмо. Об отменённом прогоне писем нет: что дальше,
        решил администратор."""
        if record.get(CANCELLED):
            self.log(f"{item.path}: прогон отменён — автоматически не повторяю; нужна — запустите вручную")
            return
        self._say_retry_plan(item.path)
        self._hook(item, record, prev)
        self._announce(item, record, prev)

    def _hook(self, item: QueueItem, record: dict, prev: dict | None) -> None:
        """Событие webhook об исходе прогона, если оно положено (webhook.due). Сбой очереди доставки
        (база не приняла) исход записи не трогает: он уже записан — строка в журнал работы."""
        kind = webhook.due(record, prev, item.reason) if self.hook else None
        if kind is None:
            return
        try:
            self.hook(record, kind)
        except Exception as e:
            self.log(f"ВНИМАНИЕ: событие webhook по записи {item.path} не поставлено в очередь доставки: {e!r}")

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
                {"path": rel, "size": p.size, "quiet_sec": round(p.quiet_sec()), "stable_sec": self.cfg.stable_sec,
                 "first_seen": p.first_seen,
                 # почему в обработку не идёт сам: похож на недокопированный, копия готовой записи…
                 "held": f"похоже, докопирован не до конца: {p.cut}" if p.cut else p.held or ""}
                for rel, p in sorted(self.pending.items())
            ],
            # снятые из очереди вручную: сами в обработку не идут (unqueue); файла уже нет — не показываются
            "dropped": [{"path": rel, "by": d["by"], "at": d["at"]} for rel, d in sorted(self.dropped.items())
                        if (self.cfg.inbox / rel).is_file()],
            "scans": self.scans,
            "last_scan": self.last_scan,
        }
