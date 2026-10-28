"""Несколько обработчиков над одной очередью: сколько записей идёт сразу и что одна
запись двумя обработчиками не идёт. Сама обработка подменена — наружу ничего не уходит."""
import asyncio
import contextlib
import dataclasses
import json
import os
import sqlite3
import subprocess
import types
import unicodedata
from pathlib import Path

import pytest

from app import db, dictionary, ledger, media, pipeline, records, speech, triage, usage, watcher
from app.ledger import Ledger
from tests.test_pipeline import REL as QUIET, silent  # noqa: F401 — запись с подменённым распознаванием
from tests.test_reuse import TRI


@dataclasses.dataclass(frozen=True)
class Cfg:
    """Настройки, сколько их нужно сканеру и обработчикам."""
    inbox: Path
    outbox: Path
    workers: int
    model: str = "deepgram/nova-3"
    outbox_inside_inbox: None = None
    stable_sec: int = 0
    scan_interval_sec: int = 10
    stable_samples: int = 1


class Trail:
    """Журнал событий: здесь важно только, что прогон в него записан."""

    def __init__(self):
        self.runs = []

    def uploader(self, file):
        return ""

    def run(self, record, by, reason):
        self.runs.append((record["file"], reason))


def service(tmp_path, workers):
    cfg = Cfg(inbox=tmp_path / "in", outbox=tmp_path / "out", workers=workers)
    cfg.inbox.mkdir()
    return watcher.Service(cfg=cfg, log=lambda s: None, ledger=Ledger(db.Database(cfg.outbox.parent / "state")), journal=Trail(),
                           speech_model=lambda: "elevenlabs/scribe-v2", tell=lambda record, kind, login, by_hand: None,
                           words=lambda rel: dictionary.merge([]), series=lambda rel: True)


def slow_process(monkeypatch, sec=0.15):
    """Обработка-заглушка: спит и записывает, кто шёл одновременно с кем."""
    seen = types.SimpleNamespace(now=[], most=0, twice=[], order=[], stems={}, models={}, series={})

    async def process(cfg, rel, stem, template, force, manual, log, on_progress=None, neighbours=None, words=None, marked=None, series=False):
        if rel in seen.now:
            seen.twice.append(rel)
        seen.now.append(rel)
        seen.most = max(seen.most, len(seen.now))
        seen.order.append(rel)
        seen.stems[rel] = stem
        seen.series[rel] = series
        log("шаг 1: расшифровка с разделением говорящих — заглушка")
        await on_progress(1, 2)
        await asyncio.sleep(sec)
        seen.now.remove(rel)
        seen.models[rel] = cfg.model
        return {"file": rel, "stem": stem, "status": "ok", "cost": {}, "finished_at": pipeline.now_iso()}

    monkeypatch.setattr(pipeline, "process", process)
    return seen


def drain(svc, timeout=20.0):
    """Запускает обработчики, как это делает сервис, и ждёт, пока очередь разойдётся."""
    async def go():
        workers = [asyncio.create_task(svc.worker_loop()) for _ in range(svc.cfg.workers)]
        try:
            async with asyncio.timeout(timeout):
                while svc.queue or svc.running:
                    await asyncio.sleep(0.02)
        finally:
            for w in workers:
                w.cancel()
    asyncio.run(go())


def test_no_more_than_workers_records_at_once(tmp_path, monkeypatch):
    svc = service(tmp_path, workers=3)
    seen = slow_process(monkeypatch)
    svc.queue = [watcher.QueueItem(path=f"запись {n}.mp4") for n in range(7)]
    drain(svc)
    assert seen.most == 3 and len(seen.order) == 7 and seen.twice == []
    assert len(svc.ledger.records) == 7 and len(svc.journal.runs) == 7 and svc.running == []


def test_one_worker_goes_strictly_one_by_one_in_queue_order(tmp_path, monkeypatch):
    svc = service(tmp_path, workers=1)
    seen = slow_process(monkeypatch, sec=0.02)
    svc.queue = [watcher.QueueItem(path=f"запись {n}.mp4") for n in range(4)]
    drain(svc)
    assert seen.most == 1 and seen.order == [f"запись {n}.mp4" for n in range(4)]


def test_worker_asks_whether_the_folder_of_the_record_is_a_series(tmp_path, monkeypatch):
    svc = service(tmp_path, workers=1)
    asked = []
    svc.series = lambda rel: asked.append(rel) or rel.startswith("Планёрки/")
    seen = slow_process(monkeypatch, sec=0.01)
    svc.queue = [watcher.QueueItem(path=p) for p in ("Планёрки/вкс.mp4", "Разное/вкс.mp4")]
    drain(svc)
    assert asked == ["Планёрки/вкс.mp4", "Разное/вкс.mp4"]
    assert seen.series == {"Планёрки/вкс.mp4": True, "Разное/вкс.mp4": False}


def test_same_record_never_runs_in_two_workers(tmp_path, monkeypatch):
    svc = service(tmp_path, workers=3)
    seen = slow_process(monkeypatch)
    # ручной запуск и пересборка той же записи, что уже стоит в очереди, и ещё чужая запись
    svc.queue = [watcher.QueueItem(path="вкс.mp4"),
                 watcher.QueueItem(path="вкс.mp4", manual=True, reason="manual"),
                 watcher.QueueItem(path="вкс.mp4", manual=True, reason="rebuild"),
                 watcher.QueueItem(path="другая.mp4")]
    drain(svc)
    assert seen.twice == [] and seen.order.count("вкс.mp4") == 3
    assert seen.most == 2                                # вторая запись шла рядом, повторы первой ждали
    assert [reason for file, reason in svc.journal.runs if file == "вкс.mp4"] == ["new", "manual", "rebuild"]
    assert svc.ledger.get("вкс.mp4")["runs"] == 3


def test_job_shows_its_own_step_and_progress(tmp_path, monkeypatch):
    svc = service(tmp_path, workers=2)
    slow_process(monkeypatch)
    svc.queue = [watcher.QueueItem(path="а.mp4"), watcher.QueueItem(path="б.mp4", template="lecture", manual=True)]
    shots = []

    async def go():
        workers = [asyncio.create_task(svc.worker_loop()) for _ in range(2)]
        await asyncio.sleep(0.05)
        shots.append(json.loads(json.dumps(svc.snapshot())))
        for w in workers:
            w.cancel()
    asyncio.run(go())
    running = shots[0]["running"]
    assert [(j["file"], j["template"], j["manual"]) for j in running] == [("а.mp4", None, False),
                                                                          ("б.mp4", "lecture", True)]
    assert all(j["step"] == "расшифровка с разделением говорящих" and (j["chunks_done"], j["chunks_total"]) == (1, 2)
               for j in running)
    assert shots[0]["queue"] == []


def test_scanner_leaves_a_record_in_work_alone(tmp_path):
    svc = service(tmp_path, workers=3)
    (svc.cfg.inbox / "вкс.mp4").write_bytes(b"video")
    svc.running.append({"file": "вкс.mp4"})
    svc.scan_once()
    svc.scan_once()
    assert svc.pending == {} and svc.queue == []
    svc.running.clear()                                  # а без отметки «в работе» это был бы новый файл
    svc.scan_once()
    assert list(svc.pending) == ["вкс.mp4"]


def test_processed_record_keeps_its_stem_whatever_the_neighbours(tmp_path, monkeypatch):
    svc = service(tmp_path, workers=1)
    seen = slow_process(monkeypatch, sec=0)
    for name in ("запись.mp4", "запись.mp3", "новая.mp4", "новая.mp3"):
        (svc.cfg.inbox / name).write_bytes(b"x")
    # «запись.mp4» обработана, когда соседки с тем же именем ещё не было
    svc.ledger.put({"file": "запись.mp4", "stem": "запись", "status": "ok"})
    # «запись.mp3» обработана при соседке — и останется «запись.mp3», даже когда соседку уберут
    svc.ledger.put({"file": "запись.mp3", "stem": "запись.mp3", "status": "ok"})
    assert pipeline.stem_for(svc.cfg.inbox, "запись.mp4", {}) == "запись.mp4"      # вычисленное имя разошлось бы
    svc.enqueue_manual("запись.mp4", None, False, "admin", "rebuild")
    drain(svc)
    (svc.cfg.inbox / "запись.mp4").unlink()
    assert pipeline.stem_for(svc.cfg.inbox, "запись.mp3", {}) == "запись"
    svc.enqueue_manual("запись.mp3", None, False, "admin", "rebuild")
    svc.queue.append(watcher.QueueItem(path="новая.mp4"))                    # новой записи имя вычисляется
    drain(svc)
    assert seen.stems == {"запись.mp4": "запись", "запись.mp3": "запись.mp3", "новая.mp4": "новая.mp4"}


def test_rebuild_reuses_the_transcript_found_by_the_stem_from_the_ledger(tmp_path):
    """Настоящий pipeline.process: расшифровка лежит под именем из журнала — в распознавание он не идёт."""
    cfg = types.SimpleNamespace(inbox=tmp_path / "in", outbox=tmp_path / "out", prompts_dir=tmp_path / "нет",
                                model="deepgram/nova-3")
    cfg.inbox.mkdir()
    (cfg.inbox / "запись.mp3").write_bytes(b"x")
    out = cfg.outbox / "запись.mp3" / "transcript"
    out.mkdir(parents=True)
    (out / "запись.mp3.json").write_text(json.dumps(
        {"segments": [{"start": 0, "end": 1, "speaker": "S1", "text": "Добрый день."}]}), encoding="utf-8")
    (out / "запись.mp3.meta.json").write_text(json.dumps(
        {"success": True, "model": cfg.model, "audio_duration_sec": 1.0}), encoding="utf-8")
    lines = []
    # соседки «запись.mp4» уже нет: вычисленное имя было бы «запись», и расшифровка не нашлась бы
    record = asyncio.run(pipeline.process(cfg, "запись.mp3", "запись.mp3", None, False, True, lines.append))
    assert record["stem"] == "запись.mp3" and record["reused_transcript"] and record["api_calls"] == 0
    assert record["status"] == "no_summary"              # шаблонов в тесте нет — до модели дело не дошло
    assert any("беру результат прошлого запуска" in line for line in lines)


def spin(svc, sec=0.3):
    """Обработчики работают столько-то секунд — очередь может и не разойтись."""
    async def go():
        workers = [asyncio.create_task(svc.worker_loop()) for _ in range(svc.cfg.workers)]
        await asyncio.sleep(sec)
        for w in workers:
            w.cancel()
    asyncio.run(go())


def test_outcome_the_base_did_not_take_is_kept_and_not_paid_for_again(tmp_path, monkeypatch):
    """База не приняла исход прогона (диск полон, папка состояния отвалилась). Прежде запись
    оставалась без строки журнала, сканер принимал её за новую и платно обрабатывал снова — и так
    по кругу, а расход прогона терялся. Теперь исход ждёт в памяти, запись в обработку не идёт,
    пока он не записан, а записывается он тот самый — с расходом."""
    monkeypatch.setattr(watcher, "SAVE_RETRY_SEC", 0, raising=False)
    svc = service(tmp_path, workers=1)
    lines = []
    svc.log = lines.append
    calls = []

    async def process(cfg, rel, stem, template, force, manual, log, on_progress=None, **kw):
        calls.append((rel, manual))
        return {"file": rel, "stem": stem, "status": "ok", "cost": {"USD": 0.5}, "finished_at": pipeline.now_iso()}
    monkeypatch.setattr(pipeline, "process", process)
    told = []
    svc.journal.uploader = lambda file: "ivanov"
    svc.tell = lambda record, kind, login, by_hand: told.append((record["file"], kind, login))

    base, broken = svc.ledger.db, [True]
    real = base.write

    @contextlib.contextmanager
    def write():
        if broken[0]:
            raise sqlite3.OperationalError("disk I/O error")
        with real() as con:
            yield con
    base.write = write

    (svc.cfg.inbox / "вкс.mp4").write_bytes(b"video")
    svc.queue.append(watcher.QueueItem(path="вкс.mp4"))
    spin(svc)
    assert calls == [("вкс.mp4", False)] and svc.ledger.get("вкс.mp4") is None
    assert sum("исход обработки вкс.mp4 не записан в базу" in line for line in lines) == 1   # сказано один раз
    svc.scan_once()
    svc.scan_once()
    svc.enqueue_manual("вкс.mp4", None, False, "admin", "manual")
    spin(svc)
    assert calls == [("вкс.mp4", False)]                 # ни сканер, ни ручной запуск не платят второй раз
    assert svc.pending == {} and told == []

    broken[0] = False                                    # база снова принимает
    spin(svc)
    first, second = svc.journal.runs[:2]
    assert (first, second) == (("вкс.mp4", "new"), ("вкс.mp4", "manual"))   # исход того прогона — первым
    assert calls == [("вкс.mp4", False), ("вкс.mp4", True)]                 # ручной запуск — следом
    assert svc.ledger.get("вкс.mp4")["cost_total"] == {"USD": 1.0}          # расход первого прогона не потерян
    assert any("исход обработки вкс.mp4 записан в базу" in line for line in lines)
    assert told[0] == ("вкс.mp4", "ready", "ivanov")


def test_failure_outside_the_pipeline_does_not_kill_the_worker(tmp_path, monkeypatch):
    """Сбой после прогона (письмо, отметка в базе) у одной записи: обработчик пишет его в журнал
    работы и берёт следующую. Прежде он молча умирал, и очередь стояла до перезапуска."""
    svc = service(tmp_path, workers=1)
    lines = []
    svc.log = lines.append
    seen = slow_process(monkeypatch, sec=0)

    def uploader(file):
        if file == "а.mp4":
            raise RuntimeError("сломалось")
        return ""
    svc.journal.uploader = uploader
    svc.queue = [watcher.QueueItem(path="а.mp4"), watcher.QueueItem(path="б.mp4")]
    drain(svc, timeout=3)
    assert seen.order == ["а.mp4", "б.mp4"] and svc.running == []
    assert any("сбой обработчика" in line and "сломалось" in line for line in lines)


# ---------- имя папки результатов у новой записи ----------

def results_process(monkeypatch):
    """Обработка-заглушка, которая кладёт протокол в папку результатов, как настоящая."""
    async def process(cfg, rel, stem, template, force, manual, log, on_progress=None, **kw):
        out = cfg.outbox / Path(rel).parent / stem
        (out / "summary").mkdir(parents=True, exist_ok=True)
        (out / "summary" / f"{stem}.md").write_text("# Протокол", encoding="utf-8")
        return {"file": rel, "stem": stem, "status": "ok", "output": str(out), "cost": {},
                "finished_at": pipeline.now_iso()}
    monkeypatch.setattr(pipeline, "process", process)


def test_record_named_like_a_subfolder_does_not_share_its_results_folder(tmp_path, monkeypatch):
    """«Сметы.mp4» рядом с подпапкой «Сметы»: результаты обеих легли бы в одну папку «Сметы», и
    удаление записи уносило бы в корзину результаты подпапки, а удаление подпапки — результаты записи."""
    svc = service(tmp_path, workers=1)
    results_process(monkeypatch)
    (svc.cfg.inbox / "Проекты" / "Сметы").mkdir(parents=True)
    for rel in ("Проекты/Сметы/акт.mp4", "Проекты/Сметы.mp4"):
        (svc.cfg.inbox / rel).write_bytes(b"video")
        svc.queue.append(watcher.QueueItem(path=rel))
    drain(svc)
    assert svc.ledger.get("Проекты/Сметы.mp4")["stem"] == "Сметы.mp4"
    records.remove(svc.cfg, svc, "Проекты/Сметы.mp4", "admin")
    assert (svc.cfg.outbox / "Проекты/Сметы/акт/summary/акт.md").is_file()      # результаты подпапки на месте
    # подпапка, которой на диске уже нет, а её записи в журнале есть, имя тоже занимает
    (svc.cfg.inbox / "Проекты/Сметы/акт.mp4").unlink()
    (svc.cfg.inbox / "Проекты/Сметы").rmdir()
    assert pipeline.stem_for(svc.cfg.inbox, "Проекты/Сметы.mp4", svc.ledger.records) == "Сметы.mp4"


def test_new_record_does_not_take_the_results_of_a_gone_one_with_the_same_name(tmp_path, monkeypatch):
    """Исходник «вкс.wav» убрали с диска руками, а его строка в журнале и результаты «вкс» остались.
    Новая «вкс.mp3» в той же папке взяла бы его готовую расшифровку и протокол."""
    svc = service(tmp_path, workers=1)
    seen = slow_process(monkeypatch, sec=0)
    svc.ledger.put({"file": "Проекты/вкс.wav", "stem": "вкс", "status": "ok"})
    (svc.cfg.inbox / "Проекты").mkdir()
    (svc.cfg.inbox / "Проекты/вкс.mp3").write_bytes(b"audio")
    svc.queue.append(watcher.QueueItem(path="Проекты/вкс.mp3"))
    drain(svc)
    assert seen.stems == {"Проекты/вкс.mp3": "вкс.mp3"}
    assert svc.ledger.get("Проекты/вкс.wav")["stem"] == "вкс"                # у прежней — её имя


# ---------- имена на диске в разложенной форме (NFD) ----------

def nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


def logged(tmp_path, workers=1):
    svc = service(tmp_path, workers)
    svc.lines = []
    svc.log = svc.lines.append
    return svc


def test_new_file_copied_from_a_mac_gets_the_name_in_one_form_before_it_is_taken(tmp_path):
    """Mac по сети кладёт «Отчёт й/Встреча ё.mp3» разложенными буквами. Страница и база знают имена
    в составной форме (NFC); запись взята под разложенным именем жила бы отдельно от них. Сканер
    приводит имя на диске к составной форме — когда файл докопирован, — и берёт запись уже под ним."""
    svc = logged(tmp_path)
    (svc.cfg.inbox / nfd("Отчёт й")).mkdir()
    (svc.cfg.inbox / nfd("Отчёт й") / nfd("Встреча ё.mp3")).write_bytes(b"audio")
    svc.scan_once()
    assert svc.queue == [] and list(svc.pending) == [nfd("Отчёт й/Встреча ё.mp3")]   # пока копируется — не трогает
    assert os.listdir(svc.cfg.inbox) == [nfd("Отчёт й")]
    svc.scan_once()
    assert [i.path for i in svc.queue] == ["Отчёт й/Встреча ё.mp3"] and svc.pending == {}
    assert os.listdir(svc.cfg.inbox) == ["Отчёт й"] and os.listdir(svc.cfg.inbox / "Отчёт й") == ["Встреча ё.mp3"]
    assert any("составной форме" in line for line in svc.lines)


def test_empty_folder_from_a_mac_gets_the_name_in_one_form(tmp_path):
    svc = logged(tmp_path)
    (svc.cfg.inbox / nfd("Пустая й")).mkdir()
    svc.scan_once()
    assert os.listdir(svc.cfg.inbox) == ["Пустая й"]


def test_name_already_known_to_the_ledger_or_taken_in_the_other_form_stays(tmp_path):
    """Запись, обработанная под разложенным именем до этой версии, на диске не переименовывается:
    её строка журнала, права и словари — под прежним. И файл, рядом с которым уже лежит тот же в
    составной форме, — тоже: это два разных файла."""
    svc = logged(tmp_path)
    known = nfd("Старое й") + "/вкс.mp3"
    (svc.cfg.inbox / nfd("Старое й")).mkdir()
    (svc.cfg.inbox / known).write_bytes(b"audio")
    svc.ledger.put({"file": known, "stem": "вкс", "status": "ok", "output": "x"})
    (svc.cfg.inbox / "Встреча ё.mp3").write_bytes(b"first")
    (svc.cfg.inbox / nfd("Встреча ё.mp3")).write_bytes(b"second")
    svc.ledger.put({"file": "Встреча ё.mp3", "stem": "Встреча ё", "status": "ok", "output": "y"})
    for _ in range(3):
        svc.scan_once()
    assert sorted(os.listdir(svc.cfg.inbox)) == sorted(["Встреча ё.mp3", nfd("Встреча ё.mp3"), nfd("Старое й")])
    assert [i.path for i in svc.queue] == [nfd("Встреча ё.mp3")]
    warned = [line for line in svc.lines if "разложенной форме" in line]
    assert len(warned) == 2                                             # по разу на имя, не на каждом обходе


# ---------- недокопированный файл: копия по сети замерла, размер «устоялся» ----------

def made(tmp_path, name, *args):
    """Настоящая запись на 20 с (ffmpeg из образа сервиса): синус, у видео — ещё и картинка."""
    path = tmp_path / "src" / name
    path.parent.mkdir(exist_ok=True)
    video = ["-f", "lavfi", "-i", "testsrc=d=20:s=64x48:r=5"] if path.suffix in (".mp4", ".mkv") else []
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=20", *video, "-shortest", *args, str(path)],
                   check=True, timeout=120)
    return path.read_bytes()


CASES = [("встреча.mp4", ()), ("быстрая.mp4", ("-movflags", "+faststart")), ("звонок.m4a", ()), ("созвон.mkv", ()),
         ("диктофон.mp3", ("-b:a", "64k")), ("микрофон.wav", ()), ("лекция.flac", ())]


@pytest.mark.parametrize("cut", ["third", "tail"])
@pytest.mark.parametrize("name, args", CASES, ids=[Path(n).suffix for n, _ in CASES])
def test_file_cut_short_is_not_taken_until_it_is_whole(tmp_path, name, args, cut):
    """Копия по сети замерла на трети или под самый конец: размер и время не меняются, но в файле нет конца. Платно
    распознавать такой нельзя — сканер ждёт, пишет почему, а докопированный берёт как обычно."""
    whole = made(tmp_path, name, *args)
    svc = logged(tmp_path)
    (svc.cfg.inbox / name).write_bytes(whole[:len(whole) * {"third": 33, "tail": 85}[cut] // 100])
    for _ in range(3):
        svc.scan_once()
    assert svc.queue == [] and list(svc.pending) == [name]
    waiting = [line for line in svc.lines if "докопирован не до конца" in line]
    assert len(waiting) == 1 and name in waiting[0]                     # один раз, а не на каждом обходе
    (svc.cfg.inbox / name).write_bytes(whole)                           # копия дошла до конца
    svc.scan_once()
    svc.scan_once()
    assert [i.path for i in svc.queue] == [name]


@pytest.mark.parametrize("name, args", CASES + [("звук.ogg", ()), ("видео-без-звука.mp4", ("-an",))],
                         ids=[Path(n).suffix for n, _ in CASES] + [".ogg", "no-audio"])
def test_whole_files_are_taken_at_once(tmp_path, name, args):
    if name == "видео-без-звука.mp4":
        path = tmp_path / name
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=5:s=64x48:r=5", str(path)],
                       check=True, timeout=120)
        whole = path.read_bytes()
    else:
        whole = made(tmp_path, name, *args)
    svc = logged(tmp_path)
    (svc.cfg.inbox / name).write_bytes(whole)
    svc.scan_once()
    svc.scan_once()
    assert [i.path for i in svc.queue] == [name]
    assert not [line for line in svc.lines if "докопирован не до конца" in line]


def test_two_files_copied_into_one_folder_from_a_mac_are_both_taken(tmp_path):
    """Оба файла папки докопированы: каждый ждал бы, пока докопируется другой, — и ни один бы не пошёл."""
    svc = logged(tmp_path)
    (svc.cfg.inbox / nfd("Отчёт й")).mkdir()
    for name in ("первая.mp3", "вторая.mp3"):
        (svc.cfg.inbox / nfd("Отчёт й") / name).write_bytes(b"audio")
    for _ in range(4):
        svc.scan_once()
    assert sorted(i.path for i in svc.queue) == ["Отчёт й/вторая.mp3", "Отчёт й/первая.mp3"]
    assert os.listdir(svc.cfg.inbox) == ["Отчёт й"] and svc.pending == {}


# ---------- исходник записи переименован, скопирован, изменён или убран мимо сервиса ----------

class Events(Trail):
    """Журнал событий, в котором видно ещё и события: переименование записи сервисом."""

    def __init__(self):
        super().__init__()
        self.events = []

    def write(self, kind, by="", **fields):
        self.events.append({"kind": kind, "by": by, **fields})


def watched(tmp_path):
    """Сканер со словарями и журналом событий, как в сервисе; строки журнала работы — в svc.lines."""
    svc = logged(tmp_path)
    svc.journal = Events()
    svc.dictionaries = dictionary.Store(svc.ledger.db)
    return svc


def processed(svc, rel, data=b"video" * 4000, **fields):
    """Запись обработана так, как её записывает pipeline.process: в журнале — размер исходника, время его
    изменения и отпечаток содержимого; протокол — в папке результатов."""
    path = svc.cfg.inbox / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stem = Path(rel).stem
    out = svc.cfg.outbox / Path(rel).parent / stem
    (out / "summary").mkdir(parents=True, exist_ok=True)
    (out / "summary" / f"{stem}.md").write_text("# Протокол", encoding="utf-8")
    svc.ledger.put({"file": rel, "stem": stem, "status": "ok", "output": str(out), "cost": {"RUB": 30.0},
                    "summary": f"summary/{stem}.md", "source_size": len(data), "source_mtime": path.stat().st_mtime,
                    "source_print": media.fingerprint(path), "finished_at": pipeline.now_iso(), **fields})
    return path


def test_file_renamed_by_hand_in_the_inbox_keeps_its_record_and_is_not_recognised_again(tmp_path):
    """Файл переименовали или перенесли во входной папке руками, мимо сервиса. Журнал ведётся по пути, и
    прежде новый путь был новой записью — платное распознавание заново. Теперь отпечаток содержимого
    совпал с записью, чей исходник пропал: это она же, и она переезжает на новый путь тем же переездом, что
    кнопка «Переименовать» — с результатами, словарём и событием в журнале."""
    svc = watched(tmp_path)
    old = processed(svc, "Проекты/вкс.mp4")
    svc.dictionaries.put(dictionary.RECORDS, "Проекты/вкс.mp4", [{"name": "Ершова Лидия"}], [], "ivanov")
    (svc.cfg.inbox / "Архив").mkdir()
    old.rename(svc.cfg.inbox / "Архив" / "Планёрка.mp4")
    svc.scan_once()
    svc.scan_once()
    assert svc.queue == []                              # в распознавание не идёт
    svc.settle()
    new = "Архив/Планёрка.mp4"
    rec = svc.ledger.get(new)
    assert svc.ledger.get("Проекты/вкс.mp4") is None and rec["stem"] == "Планёрка" and rec["runs"] == 1
    assert rec["cost_total"] == {"RUB": 30.0}           # прогона не было
    assert (svc.cfg.outbox / "Архив/Планёрка/summary/Планёрка.md").is_file()
    assert not (svc.cfg.outbox / "Проекты/вкс").exists()
    assert svc.dictionaries.get(dictionary.RECORDS, new)["people"] == [{"name": "Ершова Лидия"}]
    assert svc.journal.events == [{"kind": "record_move", "by": "", "file": "Проекты/вкс.mp4", "to": new}]
    assert svc.pending == {}
    for _ in range(2):
        svc.scan_once()
        svc.settle()
    assert svc.queue == [] and svc.journal.runs == [] and svc.missing == set()
    assert sum("мимо сервиса" in line for line in svc.lines) == 1


def test_copy_of_a_processed_record_waits_for_the_administrator(tmp_path):
    """Тот же файл ещё раз, в другой папке, а исходник прежней записи на месте — это копия. Распознавать её
    заново за деньги сервис сам не станет: ждёт, пишет почему и что делать, страница показывает её отложенной."""
    svc = watched(tmp_path)
    data = b"audio" * 5000
    processed(svc, "Проекты/вкс.mp4", data)
    (svc.cfg.inbox / "Снабжение").mkdir()
    (svc.cfg.inbox / "Снабжение" / "копия.mp4").write_bytes(data)
    for _ in range(3):
        svc.scan_once()
        svc.settle()
    assert svc.queue == [] and list(svc.pending) == ["Снабжение/копия.mp4"]
    assert svc.pending["Снабжение/копия.mp4"].held
    said = [line for line in svc.lines if "копия записи" in line]
    assert len(said) == 1 and "Проекты/вкс.mp4" in said[0] and "вручную" in said[0]
    assert svc.snapshot()["watching"][0]["held"]
    svc.enqueue_manual("Снабжение/копия.mp4", None, False, "admin", "manual")      # администратор решил: нужна
    assert [i.path for i in svc.queue] == ["Снабжение/копия.mp4"]
    # другой файл того же размера — новая запись, как всегда
    (svc.cfg.inbox / "Снабжение" / "другая.mp4").write_bytes(b"other" * 5000)
    svc.scan_once()
    svc.scan_once()
    assert [i.path for i in svc.queue] == ["Снабжение/копия.mp4", "Снабжение/другая.mp4"]


def test_record_processed_before_fingerprints_gets_one_when_it_is_needed(tmp_path):
    """У записи, обработанной до этой версии, отпечатка нет. Считается он, когда понадобился: пришёл файл
    того же размера, а исходник записи на месте. Файл того же размера, чей исходник пропал, при неизвестном
    отпечатке не сличить — его сервис тоже не распознаёт сам."""
    svc = watched(tmp_path)
    data = b"audio" * 5000
    processed(svc, "Проекты/вкс.mp4", data, source_print=None, source_mtime=None)
    processed(svc, "Проекты/ушла.mp4", b"gone!" * 5000, source_print=None, source_mtime=None).unlink()
    (svc.cfg.inbox / "копия.mp4").write_bytes(data)
    (svc.cfg.inbox / "похожая.mp4").write_bytes(b"other" * 5000)
    for _ in range(2):
        svc.scan_once()
        svc.settle()
    assert svc.queue == [] and sorted(svc.pending) == ["копия.mp4", "похожая.mp4"]
    assert svc.ledger.get("Проекты/вкс.mp4")["source_print"] == media.fingerprint(svc.cfg.inbox / "копия.mp4")
    assert any("похожая.mp4" in line and "Проекты/ушла.mp4" in line and "вручную" in line for line in svc.lines)


def test_changed_and_missing_source_of_a_processed_record_are_noticed_but_not_paid_for(tmp_path):
    """Исходник готовой записи докопировали или перезаписали: автоматически заново сервис его не распознаёт,
    а запоминает «исходник изменился» — для списка записей. Исходник убрали — «исходник отсутствует»."""
    svc = watched(tmp_path)
    grown = processed(svc, "Проекты/дописали.mp4")
    same = processed(svc, "Проекты/перезаписали.mp4")
    touched = processed(svc, "Проекты/тронули.mp4")
    processed(svc, "Проекты/убрали.mp4").unlink()
    with open(grown, "ab") as f:
        f.write(b"tail")
    same.write_bytes(b"VIDEO" * 4000)                   # тот же размер, другое содержимое
    os.utime(same, (same.stat().st_atime, same.stat().st_mtime + 5))
    os.utime(touched, (touched.stat().st_atime, touched.stat().st_mtime + 5))   # время другое, содержимое то же
    for _ in range(3):
        svc.scan_once()
        svc.settle()
    assert svc.queue == []
    state = {rel: rec.get("source_state") for rel, rec in svc.ledger.records.items()}
    assert state == {"Проекты/дописали.mp4": "changed", "Проекты/перезаписали.mp4": "changed",
                     "Проекты/тронули.mp4": None, "Проекты/убрали.mp4": None}
    assert svc.missing == {"Проекты/убрали.mp4"}
    assert svc.ledger.get("Проекты/тронули.mp4")["source_mtime"] == touched.stat().st_mtime
    assert sum("изменился после обработки" in line for line in svc.lines) == 2      # по разу на запись
    # вернули прежний файл — отметки нет
    grown.write_bytes(b"video" * 4000)
    os.utime(grown, (grown.stat().st_atime, svc.ledger.get("Проекты/дописали.mp4")["source_mtime"]))
    svc.scan_once()
    assert svc.ledger.get("Проекты/дописали.mp4").get("source_state") is None


def test_rebuild_from_the_old_transcript_keeps_the_mark_that_the_source_changed(tmp_path, monkeypatch):
    """Пересборка без force берёт прежнюю расшифровку: протокол по-прежнему описывает прежний файл, и отметка
    «исходник изменился» остаётся. Полный прогон заново (force) — уже по новому файлу, отметки нет."""
    svc = watched(tmp_path)
    path = processed(svc, "вкс.mp4", source_state="changed")
    path.write_bytes(b"VIDEO" * 4000)

    async def process(cfg, rel, stem, template, force, manual, log, on_progress=None, **kw):
        src = cfg.inbox / rel
        return {"file": rel, "stem": stem, "status": "ok", "cost": {}, "finished_at": pipeline.now_iso(),
                "reused_transcript": not force, "source_size": src.stat().st_size,
                "source_mtime": src.stat().st_mtime, "source_print": media.fingerprint(src)}
    monkeypatch.setattr(pipeline, "process", process)
    before = svc.ledger.get("вкс.mp4")
    svc.enqueue_manual("вкс.mp4", None, False, "admin", "rebuild")
    drain(svc)
    after = svc.ledger.get("вкс.mp4")
    assert after["source_state"] == "changed" and after["source_print"] == before["source_print"]
    svc.enqueue_manual("вкс.mp4", None, True, "admin", "manual")
    drain(svc)
    after = svc.ledger.get("вкс.mp4")
    assert after.get("source_state") is None and after["source_print"] == media.fingerprint(path)


# ---------- записи и папки под разложенными именами, обработанные до 1.2.0 ----------

def test_records_and_folders_known_under_decomposed_names_move_to_the_composed_form(tmp_path):
    """Записи и папки, обработанные под разложенным именем (так пишет macOS) до этой версии, жили под ним, а
    ссылки страницы теперь в составной форме — такая папка на странице не открывалась. При запуске сервис
    один раз переименовывает их тем же переездом, что кнопки «Переименовать»: исходники, результаты, журнал,
    словари, права, признак серии — всё под составным именем."""
    from app import series, users
    svc = watched(tmp_path)
    people = users.Users(svc.ledger.db, "секрет")
    people.put("ivanov", "Иванов", "пароль-1234")
    people.set_access("ivanov", "Отчёт й", read="tree")         # права выданы со страницы — в составной форме
    svc.people = people
    folder, record = nfd("Отчёт й"), nfd("Встреча ё.mp3")
    processed(svc, f"{folder}/{record}")
    processed(svc, f"{folder}/план.mp3")
    processed(svc, record)
    svc.dictionaries.put(dictionary.FOLDERS, folder, [{"name": "Ершова Лидия"}], [], "ivanov")
    svc.dictionaries.put(dictionary.RECORDS, record, [{"name": "Петров Пётр"}], [], "ivanov")
    series.Store(svc.ledger.db).put(folder, False)
    svc.known_to_nfc()
    assert sorted(svc.ledger.records) == ["Встреча ё.mp3", "Отчёт й/Встреча ё.mp3", "Отчёт й/план.mp3"]
    assert sorted(os.listdir(svc.cfg.inbox)) == ["Встреча ё.mp3", "Отчёт й"]
    assert sorted(os.listdir(svc.cfg.inbox / "Отчёт й")) == ["Встреча ё.mp3", "план.mp3"]
    assert (svc.cfg.outbox / "Отчёт й/Встреча ё/summary/Встреча ё.md").is_file()
    assert (svc.cfg.outbox / "Встреча ё/summary/Встреча ё.md").is_file()
    assert svc.dictionaries.get(dictionary.FOLDERS, "Отчёт й")["people"] == [{"name": "Ершова Лидия"}]
    assert svc.dictionaries.get(dictionary.RECORDS, "Встреча ё.mp3")["people"] == [{"name": "Петров Пётр"}]
    assert not series.Store(svc.ledger.db).on("Отчёт й")
    assert people.access("ivanov").sees("Отчёт й")
    assert sorted(e["kind"] for e in svc.journal.events) == ["folder_rename", "record_rename", "record_rename"]
    svc.known_to_nfc()                                          # второй раз — нечего
    assert len(svc.journal.events) == 3


def test_decomposed_record_next_to_its_composed_twin_stays_and_is_said(tmp_path):
    svc = watched(tmp_path)
    processed(svc, nfd("Встреча ё.mp3"))
    processed(svc, "Встреча ё.mp3", b"other" * 4000)
    svc.known_to_nfc()
    assert sorted(svc.ledger.records) == sorted(["Встреча ё.mp3", nfd("Встреча ё.mp3")])
    assert sum("разложенной форме" in line and "оставляю" in line for line in svc.lines) == 1


# ---------- администратор снимает запись из очереди и отменяет прогон ----------

def test_record_taken_off_the_queue_is_not_queued_again_until_it_changes_or_is_run_by_hand(tmp_path):
    """Снятую из очереди новую запись сканер на следующем обходе взял бы снова — и платно распознал.
    Сервис помнит, что её сняли вручную (и в базе — переживает перезапуск), пока её не запустят вручную
    или файл не изменится: изменённый файл — уже другая запись."""
    svc = watched(tmp_path)
    kept = []
    svc.keep_dropped = lambda entries: kept.append(dict(entries))
    rel = "Проекты/вкс.mp4"
    src = svc.cfg.inbox / rel
    src.parent.mkdir()
    src.write_bytes(b"video" * 4000)
    svc.scan_once()
    svc.scan_once()
    assert [i.path for i in svc.queue] == [rel]
    assert svc.unqueue(rel, "admin") is True
    assert svc.queue == [] and kept[-1][rel]["by"] == "admin" and kept[-1][rel]["size"] == 20000
    for _ in range(3):
        svc.scan_once()
    assert svc.queue == [] and svc.pending == {}
    assert [(d["path"], d["by"]) for d in svc.snapshot()["dropped"]] == [(rel, "admin")]
    assert sum("снята из очереди" in line for line in svc.lines) == 1
    assert svc.unqueue(rel, "admin") is None                    # в очереди её уже нет

    src.write_bytes(b"video" * 4001)                            # файл изменился — это уже другая запись
    svc.scan_once()
    svc.scan_once()
    assert [i.path for i in svc.queue] == [rel] and kept[-1] == {} and svc.snapshot()["dropped"] == []
    assert any("изменился после того, как его сняли из очереди" in line for line in svc.lines)

    svc.unqueue(rel, "admin")
    svc.enqueue_manual(rel, None, False, "admin", "manual")     # ручной запуск — решение администратора
    assert [i.path for i in svc.queue] == [rel] and svc.dropped == {} and kept[-1] == {}


def test_retry_taken_off_the_queue_waits_too_and_a_manual_run_just_leaves_it(tmp_path):
    svc = watched(tmp_path)
    retried, done = "Проекты/план.mp4", "Проекты/итоги.mp4"
    processed(svc, retried, status="error", error="HTTP 502", finished_at="2026-01-01T00:00:00+03:00")
    processed(svc, done, data=b"audio" * 3000)
    svc.scan_once()
    assert [(i.path, i.reason) for i in svc.queue] == [(retried, "retry")]
    assert svc.unqueue(retried, "admin") is True                # повтор после сбоя — тоже платный
    svc.scan_once()
    assert svc.queue == []
    svc.enqueue_manual(done, None, False, "admin", "rebuild")
    assert svc.unqueue(done, "admin") is False                  # готовую сканер сам и так не возьмёт: помнить нечего
    assert svc.queue == [] and list(svc.dropped) == [retried]


def cancelling(cfg, seen, monkeypatch, cancel_at):
    """Обработчик над настоящим pipeline.process; распознавание — по частям, каждая платная;
    администратор нажимает «Отменить прогон» там, где скажет cancel_at."""
    trail = types.SimpleNamespace(runs=[], run=lambda record, by, reason: trail.runs.append((dict(record), by, reason)),
                                  uploader=lambda file: "ivanov")
    told = []
    svc = watcher.Service(cfg=cfg, log=seen.lines.append, ledger=Ledger(db.Database(cfg.outbox.parent / "state")),
                          journal=trail, speech_model=lambda: cfg.model, tell=lambda *a: told.append(a),
                          words=lambda rel: dictionary.merge([]), series=lambda rel: False)
    provider = types.SimpleNamespace(title="RouterAI", currency="RUB")
    monkeypatch.setattr(speech, "audio_duration", lambda path: 3 * speech.CHUNK_SEC)

    async def stt(client, cfg, chunk, log=None, terms=()):
        seen.paid += 1
        usage.add("speech", cfg.model, {"cost": 10.0}, provider)
        if cancel_at == "speech" and seen.paid == 1:
            assert svc.cancel(QUIET, "admin")["by"] == "admin"     # посреди распознавания, пока идёт первая часть
        return {"text": "Добрый день", "words": [{"word": "Добрый", "start": 0.1, "end": 0.4, "speaker": 0},
                                                 {"word": "день", "start": 0.5, "end": 0.8, "speaker": 0}]}

    async def tri(*a, **kw):
        if cancel_at == "triage":
            svc.cancel(QUIET, "admin")
        return dict(TRI)

    monkeypatch.setattr(speech, "call_stt", stt)
    monkeypatch.setattr(triage, "triage_file", tri)
    return svc, trail, told


@pytest.mark.parametrize("before", [None, "ok"])
def test_cancelled_run_stops_before_the_next_paid_request_and_keeps_what_is_paid_for(silent, monkeypatch, before):
    """Прогон отменяют посреди распознавания: идущий запрос поставщику не обрывается (за него могли уже
    списать деньги), следующая часть не заказывается. Оплаченное — в расходах и на диске: повтор берёт
    готовую часть, не платя за неё снова. Исход — «отменено администратором», по расписанию не повторяется,
    письма никому. Готовая прежде запись остаётся готовой: её протокол на месте."""
    cfg, seen = silent
    svc, trail, told = cancelling(cfg, seen, monkeypatch, "speech")
    parts = len(speech.plan_chunks(3 * speech.CHUNK_SEC))
    assert parts > 2
    if before:
        svc.ledger.put({"file": QUIET, "stem": "тишина", "status": "ok", "cost": {"RUB": 5.0}, "summary": "summary/тишина.md",
                        "finished_at": "2026-10-01T10:00:00+03:00"})
        svc.enqueue_manual(QUIET, None, True, "admin", "manual")
    else:
        svc.queue.append(watcher.QueueItem(path=QUIET))
    drain(svc)
    assert seen.paid == 1 and seen.text == 0
    rec = svc.ledger.get(QUIET)
    assert rec[ledger.CANCELLED]["by"] == "admin" and rec["cost"] == {"RUB": 10.0}
    if before:
        assert rec["status"] == "ok" and rec["runs"] == 2 and rec["cost_total"] == {"RUB": 15.0}
    else:
        assert rec["status"] == "error" and "отменил администратор admin" in rec["error"]
    assert not svc.ledger.retry_due(QUIET) and told == []
    (record, by, reason), = trail.runs
    assert record[ledger.CANCELLED]["by"] == "admin" and record["cost"] == {"RUB": 10.0}
    out = cfg.outbox / "Планёрки" / "тишина"
    assert (out / "transcript" / speech.RAW_DIR / "тишина.part01.json").is_file()
    assert not (out / "тишина.error.json").exists() and not (out / f"тишина{ledger.MARKER_SUFFIX}").exists()
    assert any(line.startswith("отменено: ") and "admin" in line for line in seen.lines)
    assert any("автоматически не повторяю" in line for line in seen.lines)

    svc.enqueue_manual(QUIET, None, False, "admin", "manual")   # запустили снова: оплаченная часть — с диска
    drain(svc)
    assert seen.paid == parts and ledger.CANCELLED not in svc.ledger.get(QUIET)


def test_cancel_between_the_text_steps_is_a_cancel_and_not_a_missing_protocol(silent, monkeypatch):
    cfg, seen = silent
    svc, trail, told = cancelling(cfg, seen, monkeypatch, "triage")
    svc.queue.append(watcher.QueueItem(path=QUIET))
    drain(svc)
    rec = svc.ledger.get(QUIET)
    assert rec["status"] == "error" and rec[ledger.CANCELLED]["by"] == "admin"
    assert seen.text == 0                                       # ни поручений, ни протокола не заказывал
    assert svc.cancel(QUIET, "admin") is None                   # прогона больше нет — отменять нечего
