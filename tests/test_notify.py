"""Письма о записи: кому и когда положено «запись готова», что берётся в письмо из протокола,
какие адреса годятся в получатели протокола.

Правило проверяется дважды: таблицей на notify.due и прогоном через настоящий обработчик
(watcher.Service) с подменённой обработкой — он решает, кому письмо, и ставит отметку в журнал
обработки. Сама отправка здесь не участвует: её зовёт сервис, см. tests/test_web.py.
"""
import asyncio
import dataclasses
from pathlib import Path

import pytest

from app import db, dictionary, notify, pipeline, watcher
from app.ledger import MAX_FAILURES, Ledger

OK, ERROR, NO_SUMMARY, NO_AUDIO = "ok", "error", "no_summary", "no_audio"


def row(status, failures=0, announced=None):
    return {"file": "вкс.mp4", "status": status, "failures": failures, "announced": announced}


@pytest.mark.parametrize("reason, prev, record, letter", [
    # сервис взял запись сам: первая готовность — письмо, дальше тишина
    ("new", None, row(OK), "ready"),
    ("retry", row(ERROR, 1), row(OK), "ready"),
    ("retry", row(NO_SUMMARY, 2), row(OK), "ready"),
    ("retry", row(ERROR, 1, "ready"), row(OK), None),               # готовность уже сообщали
    ("retry", row(ERROR, 3, "failed"), row(OK), "ready"),           # о сбое писали, а запись всё же готова
    # сбой: пока автоматика повторит — молчим; на последнем сбое подряд — одно письмо
    ("new", None, row(ERROR, 1), None),
    ("retry", row(ERROR, 1), row(ERROR, 2), None),
    ("retry", row(ERROR, 2), row(ERROR, MAX_FAILURES), "failed"),
    ("retry", row(NO_SUMMARY, 2), row(NO_SUMMARY, MAX_FAILURES), "failed"),
    ("retry", row(ERROR, 3, "failed"), row(ERROR, 4), None),        # о сбое уже писали
    ("retry", row(ERROR, 2, "ready"), row(ERROR, 3), None),         # после «готова» о сбоях пересборки не пишем
    # нет звука — повторять нечего, письмо сразу
    ("new", None, row(NO_AUDIO), "failed"),
    ("retry", row(ERROR, 1), row(NO_AUDIO), "failed"),
    # пересборка протокола — никогда
    ("rebuild", row(OK), row(OK), None),
    ("rebuild", row(OK), row(NO_SUMMARY, 1), None),
    ("rebuild", row(NO_SUMMARY, 3, "failed"), row(OK), None),
    # ручной запуск — письмо об исходе этого прогона, что бы ни было раньше
    ("manual", row(OK), row(OK), "ready"),
    ("manual", None, row(ERROR, 1), "failed"),
    ("manual", row(ERROR, 3, "failed"), row(OK), "ready"),
    ("manual", row(OK, 0, "ready"), row(NO_AUDIO), "failed"),
])
def test_which_letter_is_due_after_a_run(reason, prev, record, letter):
    assert notify.due(record, prev, reason) == letter


def test_record_ready_before_letters_appeared_counts_as_announced():
    """У записей, обработанных до появления писем, отметки нет: готовность считается сообщённой."""
    old = {"file": "вкс.mp4", "status": OK}
    assert notify.due(row(OK), {**old, "status": NO_SUMMARY, "announced": "ready"}, "retry") is None
    ledger_row = {"file": "вкс.mp4", "status": NO_SUMMARY, "failures": 1}
    # пересборка старой записи не удалась, повтор её починил — письма нет
    from app.ledger import said
    assert said(old) == "ready" and said(None) is None and said(ledger_row) is None


# ---------- через обработчик ----------

@dataclasses.dataclass(frozen=True)
class Cfg:
    inbox: Path
    outbox: Path
    workers: int = 1
    model: str = "deepgram/nova-3"
    outbox_inside_inbox: None = None
    stable_sec: int = 0
    scan_interval_sec: int = 10
    stable_samples: int = 1


class Trail:
    def __init__(self, uploaded):
        self.uploaded = uploaded

    def uploader(self, file):
        return self.uploaded.get(file, "")

    def run(self, record, by, reason):
        pass


def service(tmp_path, monkeypatch, outcomes, uploaded=None):
    """Обработчик с обработкой-заглушкой: каждый прогон отдаёт следующий исход из outcomes.
    Исход — статус либо исключение (сбой, который pipeline не поймал). svc.told — кому что положено."""
    cfg = Cfg(inbox=tmp_path / "in", outbox=tmp_path / "out")
    cfg.inbox.mkdir()
    told, hands = [], []        # кому что положено; ручной ли это был запуск
    svc = watcher.Service(cfg=cfg, log=lambda s: None, ledger=Ledger(db.Database(cfg.outbox.parent / "state")),
                          journal=Trail({"вкс.mp4": "petrov"} if uploaded is None else uploaded),
                          speech_model=lambda: "elevenlabs/scribe-v2",
                          tell=lambda record, kind, login, by_hand: (told.append((record["file"], kind, login)),
                                                                     hands.append(by_hand)),
                          words=lambda rel: dictionary.merge([]))
    left = list(outcomes)

    async def process(cfg, rel, stem, template, force, manual, log, on_progress=None, neighbours=None, words=None, marked=None):
        status = left.pop(0)
        if isinstance(status, Exception):
            raise status
        # время окончания — давнее: пауза перед автоматическим повтором уже прошла
        return {"file": rel, "stem": stem, "status": status, "cost": {}, "finished_at": "2026-01-01T00:00:00+03:00"}

    monkeypatch.setattr(pipeline, "process", process)
    svc.told, svc.by_hand = told, hands
    return svc


def run(svc, *items):
    """Задания идут по одному, в заданном порядке."""
    async def go():
        for item in items:
            svc.queue.append(item)
            worker = asyncio.create_task(svc.worker_loop())
            async with asyncio.timeout(10):
                while svc.queue or svc.running:
                    await asyncio.sleep(0.01)
            worker.cancel()
    asyncio.run(go())


def auto(reason="new"):
    return watcher.QueueItem(path="вкс.mp4", reason=reason)


def by_hand(by, reason="manual"):
    return watcher.QueueItem(path="вкс.mp4", manual=True, by=by, reason=reason)


def test_author_gets_one_ready_letter_and_rebuilds_send_nothing(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch, [OK, OK, NO_SUMMARY, OK])
    run(svc, auto())
    assert svc.told == [("вкс.mp4", "ready", "petrov")] and svc.ledger.get("вкс.mp4")["announced"] == "ready"
    # хозяин папки подписал говорящих и пересобрал протокол; пересборка упала и починилась повтором
    run(svc, by_hand("ivanov", "rebuild"), by_hand("ivanov", "rebuild"), auto("retry"))
    assert svc.told == [("вкс.mp4", "ready", "petrov")]
    assert svc.ledger.get("вкс.mp4")["runs"] == 4 and svc.ledger.get("вкс.mp4")["announced"] == "ready"


def test_retries_are_silent_until_success(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch, [ERROR, NO_SUMMARY, OK])
    run(svc, auto(), auto("retry"))
    assert svc.told == [] and svc.ledger.get("вкс.mp4")["announced"] is None
    run(svc, auto("retry"))
    assert svc.told == [("вкс.mp4", "ready", "petrov")]


def test_one_failure_letter_when_the_service_gives_up_and_the_admin_run_answers_the_admin(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch, [ERROR, ERROR, RuntimeError("сбой мимо pipeline"), ERROR, OK, NO_SUMMARY, OK])
    run(svc, auto(), auto("retry"))
    assert svc.told == []
    run(svc, auto("retry"))                 # третий сбой подряд (на этот раз мимо pipeline) — автоматика сдалась
    assert svc.ledger.get("вкс.mp4")["failures"] == MAX_FAILURES
    assert svc.told == [("вкс.mp4", "failed", "petrov")] and svc.ledger.get("вкс.mp4")["announced"] == "failed"
    # администратор запускает вручную: письма идут ему, а не автору, — и о сбое, и о готовности
    run(svc, by_hand("boss"), by_hand("boss"))
    assert svc.told[1:] == [("вкс.mp4", "failed", "boss"), ("вкс.mp4", "ready", "boss")]
    # обработчик говорит, что запуск ручной: о сбое запустившему пишется иначе, чем автору
    assert svc.by_hand == [False, True, True]
    # дальше пересборка с повтором: запись уже была готова — автору больше ничего
    run(svc, by_hand("ivanov", "rebuild"), auto("retry"))
    assert len(svc.told) == 3 and svc.ledger.get("вкс.mp4")["status"] == OK


def test_no_audio_is_reported_at_once_and_only_once(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch, [NO_AUDIO])
    run(svc, auto())
    assert svc.told == [("вкс.mp4", "failed", "petrov")] and svc.ledger.get("вкс.mp4")["announced"] == "failed"
    # сканер такую запись сам больше не берёт: она в журнале и повтору не подлежит
    assert not svc.ledger.retry_due("вкс.mp4")


def test_file_put_by_hand_has_no_author_and_no_letter(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch, [OK, OK], uploaded={})
    run(svc, auto())
    assert svc.told == [] and svc.ledger.get("вкс.mp4")["announced"] == "ready"
    # ручной запуск без имени запустившего (личный режим) — тоже некому
    run(svc, by_hand(""))
    assert svc.told == []


def test_manual_first_run_answers_the_launcher_and_the_author_hears_nothing_later(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch, [OK, NO_SUMMARY, OK])
    run(svc, by_hand("boss"))
    assert svc.told == [("вкс.mp4", "ready", "boss")]
    run(svc, by_hand("ivanov", "rebuild"), auto("retry"))
    assert svc.told == [("вкс.mp4", "ready", "boss")]


# ---------- что берётся из протокола ----------

PROTOCOL = """<!-- протокол: модель x -->

# Протокол совещания

**Название:** Планёрка

## 1. Кратко

- Срок **не меняется** [00:01:51], см. [смету](/?open=x) и `ОбменДанными`.
- Склад: стеллажи заказаны,
  приёмка *с понедельника*. [00:02:37]

Итог один.
Вторая строка абзаца.

## 2. Участники

- Иванов

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Иванов Иван** | | | |
| Согласовать смету | до пятницы | [00:10:05] | — |
| Проверить остатки | — | — | — |
"""
RECORD = {"file": "Проекты/Склад/вкс 0610.mp4", "status": "ok", "title": "Планёрка по складу",
          "gist": "Обсудили склад.", "record_date": "2026-10-06", "audio_duration_sec": 5246.6,
          "finished_at": "2026-10-07T09:00:00+03:00"}


def test_brief_is_the_summary_section_as_plain_lines():
    assert notify.brief(PROTOCOL) == [
        "Срок не меняется, см. смету и ОбменДанными.",
        "Склад: стеллажи заказаны, приёмка с понедельника.",
        "Итог один. Вторая строка абзаца."]
    assert notify.brief("# Протокол\n\n## Кратко (Executive Summary)\n\nКоротко.\n") == ["Коротко."]
    assert notify.brief("# Протокол\n\n## 1. Что делаем\n\nТекст.\n") == []
    # длинный раздел в письмо не идёт
    assert notify.brief("## 1. Кратко\n\n" + "- пункт " + "я" * notify.BRIEF_MAX + "\n") == []


def test_about_collects_what_the_letters_say():
    about = notify.about(RECORD, PROTOCOL, "http://podyachiy.test")
    assert (about["title"], about["file_name"], about["day"], about["duration"], about["tasks"]) == (
        "Планёрка по складу", "вкс 0610.mp4", "06.10.2026", "1 ч 27 мин", 2)
    assert len(about["brief"]) == 3
    assert about["url"] == ("http://podyachiy.test/?path=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B%2F%D0%A1%D0%BA%D0%BB%D0%B0%D0%B4"
                            "&open=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B%2F%D0%A1%D0%BA%D0%BB%D0%B0%D0%B4%2F%D0%B2%D0%BA%D1%81%200610.mp4")
    # «Кратко» слишком длинное — вместо него суть записи одной строкой
    long = notify.about(RECORD, "## 1. Кратко\n\n" + "слово " * 500 + "\n", "http://s")
    assert long["brief"] == ["Обсудили склад."] and long["tasks"] == 0
    # без протокола: ссылка на расшифровку либо на папку, имя вместо названия
    bare = {"file": "Проекты/вкс.mp4", "finished_at": "2026-10-07T09:00:00+03:00"}
    no_summary = notify.about({**bare, "status": "no_summary"}, None, "http://s")
    assert no_summary["url"].endswith("&kind=transcript") and no_summary["title"] == "вкс.mp4"
    assert (no_summary["brief"], no_summary["tasks"], no_summary["duration"]) == ([], None, "")
    assert notify.about({**bare, "status": "no_audio"}, None, "http://s")["url"] == (
        "http://s/?path=%D0%9F%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D1%8B")
    assert notify.about({"file": "вкс.mp4", "status": "error"}, None, "http://s")["url"] == "http://s/?path="


# ---------- получатели протокола ----------

def test_recipients_are_split_normalised_and_deduplicated():
    typed = " Ivanov@Example.test, petrov@example.test;\n sidorov@partner.test\tivanov@example.test "
    assert notify.recipients(typed, ()) == ["ivanov@example.test", "petrov@example.test", "sidorov@partner.test"]


@pytest.mark.parametrize("typed, word", [
    ("", "хотя бы один"), (" ,\n; ", "хотя бы один"),
    ("ivanov", "не похоже на адрес"), ("Иванов <ivanov@example.test>", "«Иванов» не похоже"),
    ("a@b", "не похоже"), ("a@example.test\nBcc:x@y.z", "не похоже"),
    (",".join(f"u{n}@example.test" for n in range(notify.RECIPIENTS_MAX + 1)), "не больше 20"),
])
def test_bad_recipients_are_refused_with_the_reason(typed, word):
    with pytest.raises(notify.AddressError, match=word):
        notify.recipients(typed, ())


def test_exactly_the_limit_of_recipients_passes():
    assert len(notify.recipients(" ".join(f"u{n}@example.test" for n in range(notify.RECIPIENTS_MAX)), ())) == 20


def test_domains_limit_where_the_protocol_may_go():
    domains = ("example.test", "corp.example")
    assert notify.recipients("a@example.test, b@CORP.example", domains) == ["a@example.test", "b@corp.example"]
    with pytest.raises(notify.AddressError) as refused:
        notify.recipients("a@example.test, x@gmail.test, y@sub.example.test", domains)
    text = str(refused.value)
    assert "example.test, corp.example" in text and "x@gmail.test, y@sub.example.test" in text and "MAIL_DOMAINS" in text
    assert "a@example.test" not in text.split("не подходят")[1]
    # поддомен и похожее имя — не тот домен
    assert not notify.allowed("a@notexample.test", domains) and notify.allowed("a@anything.test", ())
