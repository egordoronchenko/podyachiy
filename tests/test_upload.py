import os

import pytest

from app import upload

GB = 2 ** 30


def test_parts_are_joined_and_the_record_appears_only_when_complete(tmp_path):
    rel = upload.target_rel("Совещания/Отдел", "встреча.mp4", None)
    assert rel == "Совещания/Отдел/встреча.mp4"
    assert upload.received(tmp_path, rel, 10, GB) == 0

    assert upload.append(tmp_path, rel, 10, 0, b"12345") == (5, False)
    assert not (tmp_path / rel).exists()
    assert upload.received(tmp_path, rel, 10, GB) == 5          # с этого места продолжают после обрыва

    assert upload.append(tmp_path, rel, 10, 5, b"67890") == (10, True)
    assert (tmp_path / rel).read_bytes() == b"1234567890"
    assert not list((tmp_path / upload.PARTS_DIR).glob("*.part"))


def test_a_part_out_of_place_is_refused(tmp_path):
    rel = "a.mp3"
    upload.append(tmp_path, rel, 10, 0, b"12345")
    with pytest.raises(upload.UploadError) as e:
        upload.append(tmp_path, rel, 10, 0, b"12345")           # та же часть второй раз
    assert e.value.status == 409
    with pytest.raises(upload.UploadError):
        upload.append(tmp_path, rel, 10, 5, b"678901")          # длиннее заявленного размера


def test_an_existing_record_is_never_replaced(tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"old")
    with pytest.raises(upload.UploadError) as e:
        upload.received(tmp_path, "a.mp3", 10, GB)
    assert e.value.status == 409
    with pytest.raises(upload.UploadError):
        upload.append(tmp_path, "a.mp3", 3, 0, b"new")
    assert (tmp_path / "a.mp3").read_bytes() == b"old"


@pytest.mark.parametrize("folder, name", [
    ("..", "a.mp4"), ("a/../..", "a.mp4"), ("", "../a.mp4"), ("", "a/b.mp4"), ("", "a\\b.mp4"),
    ("", ".hidden.mp4"), (".uploads", "a.mp4"), ("", "notes.txt"), ("", "a.mp4.part"), ("", 'a"b.mp4'),
])
def test_names_that_lead_outside_or_are_not_records_are_refused(folder, name):
    with pytest.raises(upload.UploadError):
        upload.target_rel(folder, name, None)


def test_size_limit_and_results_folder(tmp_path):
    with pytest.raises(upload.UploadError) as e:
        upload.received(tmp_path, "a.mp4", 5 * GB, 4 * GB)
    assert e.value.status == 413
    with pytest.raises(upload.UploadError):
        upload.target_rel("out/x", "a.mp4", upload.Path("out"))


def test_abandoned_parts_are_removed_after_a_week_and_fresh_ones_stay(tmp_path):
    upload.append(tmp_path, "old.mp3", 10, 0, b"1")
    upload.append(tmp_path, "new.mp3", 10, 0, b"1")
    old = upload.part_path(tmp_path, "old.mp3", 10)
    week_ago = old.stat().st_mtime - upload.STALE_SEC - 60
    os.utime(old, (week_ago, week_ago))
    assert upload.sweep(tmp_path) == [old.name]
    assert upload.received(tmp_path, "new.mp3", 10, GB) == 1


def test_cancel_drops_what_was_received(tmp_path):
    upload.append(tmp_path, "a.mp3", 10, 0, b"123")
    upload.cancel(tmp_path, "a.mp3", 10)
    assert upload.received(tmp_path, "a.mp3", 10, GB) == 0


# ---------- загрузка одним запросом ----------

def test_streamed_upload_becomes_a_record_only_when_finished(tmp_path):
    rel = "Совещания/встреча.mp4"
    part = upload.stream_start(tmp_path, rel, 10, GB)
    assert part.parent == tmp_path / upload.PARTS_DIR and part.suffix == ".part"       # сканер такие пропускает
    part.write_bytes(b"1234567890")
    assert not (tmp_path / rel).exists()
    assert upload.stream_finish(tmp_path, rel, part) == 10
    assert (tmp_path / rel).read_bytes() == b"1234567890" and not part.exists()


def test_streamed_upload_checks_size_and_never_replaces_a_record(tmp_path):
    with pytest.raises(upload.UploadError) as too_big:
        upload.stream_start(tmp_path, "a.mp3", 5 * GB, 4 * GB)
    assert too_big.value.status == 413
    empty = upload.stream_start(tmp_path, "a.mp3", None, GB)          # размер клиент мог и не назвать
    empty.write_bytes(b"")
    with pytest.raises(upload.UploadError) as nothing:
        upload.stream_finish(tmp_path, "a.mp3", empty)
    assert nothing.value.status == 400

    (tmp_path / "a.mp3").write_bytes(b"old")
    with pytest.raises(upload.UploadError) as taken:
        upload.stream_start(tmp_path, "a.mp3", 3, GB)
    assert taken.value.status == 409
    late = tmp_path / upload.PARTS_DIR / "stream-x.part"              # запись появилась, пока шло тело
    late.write_bytes(b"new")
    with pytest.raises(upload.UploadError):
        upload.stream_finish(tmp_path, "a.mp3", late)
    assert (tmp_path / "a.mp3").read_bytes() == b"old"


def test_abandoned_stream_is_swept_like_any_other_part(tmp_path):
    part = upload.stream_start(tmp_path, "a.mp3", None, GB)
    part.write_bytes(b"half")
    old = part.stat().st_mtime - upload.STALE_SEC - 1
    os.utime(part, (old, old))
    assert upload.sweep(tmp_path) == [part.name] and not part.exists()
