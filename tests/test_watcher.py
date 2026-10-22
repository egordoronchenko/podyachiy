"""Несколько обработчиков над одной очередью: сколько записей идёт сразу и что одна
запись двумя обработчиками не идёт. Сама обработка подменена — наружу ничего не уходит."""
import asyncio
import dataclasses
import json
import types
from pathlib import Path

from app import db, dictionary, pipeline, watcher
from app.ledger import Ledger


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
    assert pipeline.stem_for(svc.cfg.inbox, "запись.mp4") == "запись.mp4"      # вычисленное имя разошлось бы
    svc.enqueue_manual("запись.mp4", None, False, "admin", "rebuild")
    drain(svc)
    (svc.cfg.inbox / "запись.mp4").unlink()
    assert pipeline.stem_for(svc.cfg.inbox, "запись.mp3") == "запись"
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
