import os
import unicodedata

import pytest

from app import upload, users

GB = 2 ** 30
ME = ("ivanov", "1761000000000")          # чья загрузка: логин и время изменения файла у человека


def test_parts_are_joined_and_the_record_appears_only_when_complete(tmp_path):
    rel = upload.target_rel("Совещания/Отдел", "встреча.mp4", None)
    assert rel == "Совещания/Отдел/встреча.mp4"
    assert upload.received(tmp_path, rel, 10, GB, *ME) == 0

    assert upload.append(tmp_path, rel, 10, 0, b"12345", *ME) == (5, False)
    assert not (tmp_path / rel).exists()
    assert upload.received(tmp_path, rel, 10, GB, *ME) == 5          # с этого места продолжают после обрыва

    assert upload.append(tmp_path, rel, 10, 5, b"67890", *ME) == (10, True)
    assert (tmp_path / rel).read_bytes() == b"1234567890"
    assert not list((tmp_path / upload.PARTS_DIR).glob("*.part"))


@pytest.mark.parametrize("other", [("petrov", ME[1]), (ME[0], "1761000999000")], ids=["other-person", "other-file"])
def test_two_uploads_of_one_name_and_size_are_not_glued_together(tmp_path, other):
    """Два файла с одним именем и размером в одну папку: двое людей или один человек — два разных файла
    (у них разное время изменения). Раньше принятое находилось по имени и размеру, и второй
    продолжал первый: запись из двух половин."""
    rel = "Совещания/встреча.mp4"
    assert upload.append(tmp_path, rel, 10, 0, b"AAAAA", *ME) == (5, False)
    assert upload.received(tmp_path, rel, 10, GB, *other) == 0          # своё — с начала, чужое не продолжает
    assert upload.append(tmp_path, rel, 10, 0, b"BBBBBBBBBB", *other) == (10, True)
    assert (tmp_path / rel).read_bytes() == b"BBBBBBBBBB"
    with pytest.raises(upload.UploadError) as e:
        upload.append(tmp_path, rel, 10, 5, b"AAAAA", *ME)                # первому — «имя уже занято», не склейка
    assert e.value.status == 409
    upload.cancel(tmp_path, rel, 10, *ME)
    assert not list((tmp_path / upload.PARTS_DIR).glob("*.part"))


def test_a_part_out_of_place_is_refused(tmp_path):
    rel = "a.mp3"
    upload.append(tmp_path, rel, 10, 0, b"12345", *ME)
    with pytest.raises(upload.UploadError) as e:
        upload.append(tmp_path, rel, 10, 0, b"12345", *ME)           # та же часть второй раз
    assert e.value.status == 409
    with pytest.raises(upload.UploadError):
        upload.append(tmp_path, rel, 10, 5, b"678901", *ME)          # длиннее заявленного размера


def test_an_existing_record_is_never_replaced(tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"old")
    with pytest.raises(upload.UploadError) as e:
        upload.received(tmp_path, "a.mp3", 10, GB, *ME)
    assert e.value.status == 409
    with pytest.raises(upload.UploadError):
        upload.append(tmp_path, "a.mp3", 3, 0, b"new", *ME)
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
        upload.received(tmp_path, "a.mp4", 5 * GB, 4 * GB, *ME)
    assert e.value.status == 413
    with pytest.raises(upload.UploadError):
        upload.target_rel("out/x", "a.mp4", upload.Path("out"))


def test_abandoned_parts_are_removed_after_a_week_and_fresh_ones_stay(tmp_path):
    upload.append(tmp_path, "old.mp3", 10, 0, b"1", *ME)
    upload.append(tmp_path, "new.mp3", 10, 0, b"1", *ME)
    old = upload.part_path(tmp_path, "old.mp3", 10, *ME)
    week_ago = old.stat().st_mtime - upload.STALE_SEC - 60
    os.utime(old, (week_ago, week_ago))
    assert upload.sweep(tmp_path) == [old.name]
    assert upload.received(tmp_path, "new.mp3", 10, GB, *ME) == 1


def test_cancel_drops_what_was_received(tmp_path):
    upload.append(tmp_path, "a.mp3", 10, 0, b"123", *ME)
    upload.cancel(tmp_path, "a.mp3", 10, *ME)
    assert upload.received(tmp_path, "a.mp3", 10, GB, *ME) == 0


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


# ---------- папку переименовали или удалили, пока шла загрузка ----------

def test_upload_finished_after_its_folder_was_renamed_or_deleted_does_not_bring_the_folder_back(tmp_path):
    """Прежде последняя часть заводила папку заново — под старым именем, рядом с переименованной,
    или на месте удалённой в корзину. Теперь папка появляется в начале загрузки, а в конце её
    отсутствие — отказ."""
    rel = "Проекты/вкс.mp3"
    assert upload.append(tmp_path, rel, 10, 0, b"12345", *ME) == (5, False)
    assert (tmp_path / "Проекты").is_dir()                          # папка новой загрузки — с первой части
    (tmp_path / "Проекты").rename(tmp_path / "Архив")                # папку переименовали посреди загрузки
    with pytest.raises(upload.UploadError) as e:
        upload.append(tmp_path, rel, 10, 5, b"67890", *ME)
    assert e.value.status == 409 and "переименовали или удалили" in e.value.detail
    assert not (tmp_path / "Проекты").exists() and not list(tmp_path.rglob("вкс.mp3"))
    assert not list((tmp_path / upload.PARTS_DIR).glob("*.part"))   # докачать её уже некуда — принятое не держим

    part = upload.stream_start(tmp_path, "Снабжение/итоги.mp3", None, GB)
    assert (tmp_path / "Снабжение").is_dir()
    part.write_bytes(b"123")
    (tmp_path / "Снабжение").rmdir()                                 # папку удалили, пока шло тело
    with pytest.raises(upload.UploadError) as e:
        upload.stream_finish(tmp_path, "Снабжение/итоги.mp3", part)
    assert e.value.status == 409 and not (tmp_path / "Снабжение").exists()


# ---------- одна буква — два написания (NFC и NFD) ----------

def nfd(text: str) -> str:
    """Как имена присылает macOS: «й» и «ё» разложены на букву и знак над ней."""
    return unicodedata.normalize("NFD", text)


def test_paths_from_people_come_in_one_form_whatever_the_computer_sent():
    """«Отчёт й.mp3» с Mac и с Windows — одно имя: иначе рядом две «одинаковые» записи или папки, а
    права, выданные на одну, не действуют на другую."""
    assert nfd("Отчёт й") != "Отчёт й"                                  # разные строки — разные имена на диске
    assert upload.target_rel(nfd("Проекты/Отчёт й"), nfd("Встреча ё.mp3"), None) == "Проекты/Отчёт й/Встреча ё.mp3"
    assert upload.folder_rel(nfd("Проекты/Отчёт й"), None) == "Проекты/Отчёт й"
    assert upload.folder_arg(nfd("Отчёт й"), None) == "Отчёт й"
    assert users.norm_folder(nfd("/Проекты//Отчёт й/")) == "Проекты/Отчёт й"


def test_upload_of_the_same_name_in_the_other_form_is_the_same_record(tmp_path):
    rel = upload.target_rel("Проекты", "Встреча ё.mp3", None)
    assert upload.append(tmp_path, rel, 3, 0, b"abc", *ME) == (3, True)
    with pytest.raises(upload.UploadError) as e:
        upload.received(tmp_path, upload.target_rel("Проекты", nfd("Встреча ё.mp3"), None), 3, GB, *ME)
    assert e.value.status == 409
