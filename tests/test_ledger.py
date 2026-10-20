import datetime

from app import db, ledger


def ago(sec: float) -> str:
    moment = datetime.datetime.now().astimezone() - datetime.timedelta(seconds=sec)
    return moment.isoformat(timespec="seconds")


def put(book: ledger.Ledger, status: str, finished_sec_ago: float) -> None:
    book.put({"file": "a.mp4", "status": status, "finished_at": ago(finished_sec_ago)})


def test_failure_retried_after_pause(tmp_path):
    book = ledger.Ledger(db.Database(tmp_path))
    put(book, "error", 60)
    assert not book.retry_due("a.mp4")
    put(book, "error", ledger.RETRY_AFTER_SEC + 1)
    # второй сбой подряд — пауза вдвое длиннее
    assert not book.retry_due("a.mp4")
    book.records["a.mp4"]["finished_at"] = ago(2 * ledger.RETRY_AFTER_SEC + 1)
    assert book.retry_due("a.mp4")


def test_gives_up_after_max_failures(tmp_path):
    book = ledger.Ledger(db.Database(tmp_path))
    for _ in range(ledger.MAX_FAILURES):
        put(book, "no_summary", 10 * ledger.RETRY_AFTER_SEC)
    assert not book.retry_due("a.mp4")


def test_success_and_silence_are_final(tmp_path):
    book = ledger.Ledger(db.Database(tmp_path))
    put(book, "error", 10 * ledger.RETRY_AFTER_SEC)
    put(book, "ok", 10 * ledger.RETRY_AFTER_SEC)
    assert book.records["a.mp4"]["failures"] == 0
    assert not book.retry_due("a.mp4")
    put(book, "no_audio", 10 * ledger.RETRY_AFTER_SEC)
    assert not book.retry_due("a.mp4")


def test_first_processing_moment_survives_reruns_and_renames(tmp_path):
    """По нему запись стоит в ряду совещаний папки: пересборка и перенос его не меняют."""
    book = ledger.Ledger(db.Database(tmp_path))
    book.put({"file": "a.mp4", "status": "error", "finished_at": "2026-10-01T10:00:00+03:00"})
    book.put({"file": "a.mp4", "status": "ok", "finished_at": "2026-10-09T10:00:00+03:00"})
    assert book.get("a.mp4")["first_done_at"] == "2026-10-01T10:00:00+03:00"
    book.rekey("a.mp4", dict(book.get("a.mp4"), file="b.mp4"))
    book.put({"file": "b.mp4", "status": "ok", "finished_at": "2026-10-10T10:00:00+03:00"})
    assert book.get("b.mp4")["first_done_at"] == "2026-10-01T10:00:00+03:00"
    # запись из журнала прежнего образца: первым считается то, что в нём было
    book.records["old.mp4"] = {"file": "old.mp4", "status": "ok", "finished_at": "2026-09-01T10:00:00+03:00"}
    book.put({"file": "old.mp4", "status": "ok", "finished_at": "2026-10-10T10:00:00+03:00"})
    assert ledger.Ledger(db.Database(tmp_path)).get("old.mp4")["first_done_at"] == "2026-09-01T10:00:00+03:00"


def test_spending_adds_up_by_currency_and_old_rows_count_as_dollars(tmp_path):
    book = ledger.Ledger(db.Database(tmp_path))
    # строка прежнего вида: одно число без валюты
    book.records["a.mp4"] = {"file": "a.mp4", "status": "ok", "cost": 0.5, "cost_total": 1.5, "runs": 3}
    book.put({"file": "a.mp4", "status": "ok", "cost": {"RUB": 12.0}, "finished_at": ago(1)})
    book.put({"file": "a.mp4", "status": "ok", "cost": {"RUB": 1.0, "USD": 0.25}, "finished_at": ago(0)})
    book.put({"file": "b.mp4", "status": "ok", "cost": {}, "finished_at": ago(0)})
    assert book.get("a.mp4")["cost_total"] == {"RUB": 13.0, "USD": 1.75} and book.get("a.mp4")["runs"] == 5
    assert book.get("b.mp4")["cost_total"] == {}
    again = ledger.Ledger(db.Database(tmp_path))
    assert again.totals()["cost"] == {"RUB": 13.0, "USD": 1.75}
