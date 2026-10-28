"""Демонстрационные данные для пробной установки: выдуманная компания, её совещания,
пользователи, поручения и расходы. Звука нет, поставщикам ничего не отправляется.

Записи появляются так же, как после настоящей обработки: расшифровка, разбор, подписи
говорящих, протокол, карта, отметки у поручений, строки журнала обработки и журнала
расходов — теми же функциями приложения, только текст расшифровки и протокола взят
отсюда, а не от моделей. Исходник каждой записи во входной папке — заглушка в несколько байт
(PLACEHOLDER): звука в ней нет, а список записей не помечает исходник отсутствующим.
У каждой записи два прогона, как в жизни: первый — сам сервис, второй — пересборка
протокола после того, как человек подписал говорящих.

Запуск — в образе сервиса, при остановленном сервисе (журнал обработки он держит ещё и в памяти),
над установкой, которую сервис этой версии уже запускал: состояние лежит в базе (app/db.py),
и заводит её сервис:

    cd <папка установки>
    docker compose stop
    docker compose run --rm --no-deps -e PYTHONPATH=/app \
        -v "<папка репозитория>/tools:/tools:ro" -v "<папка установки>:/install" \
        <имя сервиса из compose.yml> python /tools/demo_seed.py /install/ДЕМО-входы.md
    docker compose start

Единственный параметр — куда записать логины, пароли и ключи доступа: пароли создаются
заново при каждом запуске и больше нигде не остаются. Повторный запуск возвращает
демонстрационные записи и пользователей к исходному виду; остальное не трогает.
Даты привязаны к 10.10.2026: просроченные и ближайшие сроки считаются от этого дня.
"""
from __future__ import annotations

import datetime
import json
import random
import re
import secrets
import shutil
import sys
from pathlib import Path, PurePosixPath

from app import (db, dictionary, external, journal, ledger, marks, migrate, mindmap, names, pipeline, registry,
                 series, speech, summarize, templates, triage, usage, users)
from app.config import Settings

RATE = 2.6                  # слов в секунду — темп обычной речи
SPEECH_PER_HOUR = 0.40      # распознавание, долларов за час звука
LINE = re.compile(r"(\d)(?: (\w+))?: (.+)")     # реплика: «2 a5: текст» — голос, метка для протокола, слова

# Логин, ФИО, администратор ли сервиса, роль словами — для файла со входами.
PEOPLE: list[tuple[str, str, bool, str]] = []
# Общие папки и кто их завёл; права: (логин, папка, чтение, администрирование).
FOLDERS: list[tuple[str, str]] = []
GRANTS: list[tuple[str, str, str | None, str | None, str | None]] = []
# Папки, где выключен признак «серия совещаний»: (папка, кто выключил). У остальных он включён, как
# у любой новой папки, — в конец протокола идут незакрытые поручения предыдущей записи папки.
LOOSE: list[tuple[str, str]] = []
# Кому разрешена загрузка в личную папку. Остальные в своей папке только читают.
HOME_UPLOAD: set[str] = set()
# Ключи доступа: (логин, название, срок в днях, с управлением ли).
KEYS: list[tuple[str, str, int, bool]] = []
RECORDS: list[dict] = []
# Словари папок: (папка, кто вписал, когда, участники, сокращения) — по строке на человека и термин,
# в том же виде, в каком их вписывают на странице.
DICTIONARIES: list[tuple[str, str, str, str, str]] = []


def stamp(text: str, sec: int = 0) -> str:
    moment = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M") + datetime.timedelta(seconds=sec)
    return moment.astimezone().isoformat(timespec="seconds")


def later(moment: str, sec: float) -> str:
    return (datetime.datetime.fromisoformat(moment) + datetime.timedelta(seconds=sec)).isoformat(timespec="seconds")


def span(sec: float) -> str:
    minutes = round(sec / 60)
    return f"{minutes // 60} ч {minutes % 60:02d} мин" if minutes >= 60 else f"{minutes} мин"


# Исходник демонстрационной записи: не звук, а метка, что запись выдумана. Размер её и пишется в строку
# журнала обработки (source_size) — с ним сервис сличает исходник на диске.
PLACEHOLDER = "демонстрационная запись: звука нет\n".encode("utf-8")


def out_dir(cfg: Settings, rec: dict) -> Path:
    file = PurePosixPath(rec["file"])
    return cfg.outbox / file.parent / file.stem


# ---------- прошлый запуск ----------

def about_demo(event: dict) -> bool:
    """Строка журнала событий о демонстрационных людях, папках и записях."""
    logins = {p[0] for p in PEOPLE}
    roots = {f.split("/")[0] for f, _ in FOLDERS if not f.startswith(users.HOME_ROOT)}
    roots |= {f"{users.HOME_ROOT}/{login}" for login in logins}
    return (any(event.get(k) in logins for k in ("by", "login", "owner"))
            or any(external.inside(str(event.get(k) or "\0"), root) for k in ("file", "folder", "to") for root in roots))


def forget_journal(trail: journal.Journal) -> None:
    trail.replace([event for event in trail.read() if not about_demo(event)])


def sort_journal(trail: journal.Journal) -> None:
    """События — по времени: строки этого запуска встают между теми, что были раньше."""
    trail.replace(sorted(trail.read(), key=lambda event: datetime.datetime.fromisoformat(event["at"])))


# ---------- расшифровка, разбор, протокол ----------

def spoken(rec: dict, rng: random.Random) -> tuple[list[dict], dict[str, str], float]:
    """Слова записи с временем каждого, метки времени реплик для протокола и длина звука.
    Реплик здесь меньше, чем в настоящем совещании такой длины, — между ними паузы."""
    replies = [LINE.fullmatch(ln.strip()).groups() for ln in rec["talk"].strip().splitlines()]
    assert all(a[0] != b[0] for a, b in zip(replies, replies[1:])), f"{rec['file']}: две реплики одного голоса подряд"
    pause = (rec["minutes"] * 60 - sum(len(text.split()) for _, _, text in replies) / RATE) / len(replies)
    assert pause > 1, f"{rec['file']}: текста больше, чем времени"
    words, anchors, now = [], {}, rng.uniform(3, 9)
    for voice, anchor, text in replies:
        if anchor:
            assert anchor not in anchors, f"{rec['file']}: метка {anchor} повторяется"
            anchors[anchor] = speech.hms(now)
        for word in text.split():
            words.append({"speaker": f"S{voice}", "start": round(now, 2), "end": round(now + 0.8 / RATE, 2),
                          "text": word})
            now += 1 / RATE
        now += pause * rng.uniform(0.5, 1.5)
    return words, anchors, round(words[-1]["end"] + rng.uniform(4, 15), 2)


def write_transcript(cfg: Settings, rec: dict, words: list[dict], duration: float, rng: random.Random) -> dict:
    """Файлы расшифровки — те же, что пишет speech.transcribe_one."""
    base, stem = out_dir(cfg, rec), PurePosixPath(rec["file"]).stem
    folder = base / "transcript"
    folder.mkdir(parents=True)
    segments = speech.segments_from_words(words)
    voices = sorted({s["speaker"] for s in segments}, key=speech._label_order)
    text = speech.to_text(segments)
    unlabeled = round(rng.uniform(0, 0.02), 3)
    (folder / f"{stem}.txt").write_text(text, encoding="utf-8")
    (folder / f"{stem}.srt").write_text(speech.to_srt(words), encoding="utf-8")
    (folder / f"{stem}.json").write_text(json.dumps({
        "mode": pipeline.MODE, "model": cfg.model, "language": cfg.language,
        "timestamps": "точные: время каждого слова приходит от модели распознавания",
        "speakers": [{"id": s, "description": ""} for s in voices], "unlabeled": unlabeled,
        "speakers_unreliable": False, "crowded_meeting": len(voices) > speech.CROWDED_MEETING,
        "chunks": 1, "cuts": [], "segments": segments, "text": text}, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {"success": True, "mode": pipeline.MODE, "model": cfg.model,
            "wall_clock_sec": round(duration / rng.uniform(22, 30), 2), "audio_duration_sec": duration, "chunks": 1,
            "segments": len(segments), "speakers": len(voices),
            "speaker_changes": sum(1 for a, b in zip(segments, segments[1:]) if a["speaker"] != b["speaker"]),
            "unlabeled": unlabeled, "speakers_unreliable": False,
            "crowded_meeting": len(voices) > speech.CROWDED_MEETING, "source_audio": str(base / f"{stem}.mp3")}
    (folder / f"{stem}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def write_triage(cfg: Settings, rec: dict, kinds: dict, rng: random.Random) -> dict:
    """Разбор записи: тип, название и догадки, кто есть кто, — через разборщик приложения."""
    base, stem = out_dir(cfg, rec), PurePosixPath(rec["file"]).stem
    data = triage.parse(json.dumps({
        "type": rec["kind"], "type_reason": rec["why"], "confidence": triage.SURE, "title": rec["title"],
        "date": rec["date"], "gist": rec["gist"], "topics": rec["topics"],
        "speakers": [{"label": label, "name": guess, "role": role}
                     for label, (_, guess, role) in rec["voices"].items()]}), kinds)
    data.update(templates=sorted(kinds), model=cfg.summary_model,
                source_chars=len((base / "transcript" / f"{stem}.txt").read_text(encoding="utf-8")),
                wall_clock_sec=round(rng.uniform(6, 14), 2), usage={})
    (base / "transcript" / f"{stem}{triage.FILE_SUFFIX}").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def write_protocol(cfg: Settings, rec: dict, md: str, book: ledger.Ledger, started: str, rng: random.Random) -> dict:
    """Протокол, карта и сведения о нём — как в summarize.summarize_file; в конец дописываются
    незакрытые поручения предыдущей записи папки, какими они были на этот момент."""
    base, stem = out_dir(cfg, rec), PurePosixPath(rec["file"]).stem
    who, _, _ = names.for_protocol(base, stem)
    source, known = summarize.load_source(base, stem, who)
    used, unknown = summarize.check_marks(md, known)
    assert not unknown, f"{rec['file']}: меток нет в расшифровке — {unknown}"
    noted = marks.Store(book.db)
    tail = pipeline.previous_tasks(cfg, rec["file"], book.history(limit=len(book.records)), rec["date"], started,
                                   lambda line: None, noted,
                                   series=series.Store(book.db).on(PurePosixPath(rec["file"]).parent.as_posix()))
    folder = base / "summary"
    folder.mkdir()
    target = folder / f"{stem}.md"
    target.write_text(f"<!-- протокол: модель {cfg.summary_model}, шаблон {rec['kind']}, "
                      f"источник transcript/{stem}.json -->\n\n" + md.strip() + "\n" + (f"\n{tail}" if tail else ""),
                      encoding="utf-8")
    mindmap.write_for(target, noted.load(rec["file"]))
    meta = {"success": True, "model": cfg.summary_model, "template": rec["kind"],
            "source": f"transcript/{stem}.json", "source_chars": len(source), "summary_chars": len(md),
            "timestamps": used, "timestamps_unknown": 0, "wall_clock_sec": round(rng.uniform(35, 80), 2), "usage": {}}
    (folder / f"{stem}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def spent(cfg: Settings, rec: dict, kinds: dict, rng: random.Random, first: bool) -> dict:
    """Расход прогона — тем же накопителем, что у обработки. Пересборка платит только за
    поручения и протокол: расшифровка и разбор берутся готовые."""
    usage.reset()
    if first:
        usage.add("speech", cfg.model, {"cost": round(rec["minutes"] / 60 * SPEECH_PER_HOUR * rng.uniform(0.97, 1.03), 6)},
                  cfg.speech_provider)
        usage.add("triage", cfg.summary_model, {"cost": round(rng.uniform(0.002, 0.005), 6)}, cfg.text_provider)
    if kinds[rec["kind"]].get("actions"):
        usage.add("actions", cfg.summary_model, {"cost": round(rng.uniform(0.012, 0.03), 6)}, cfg.text_provider)
    usage.add("summary", cfg.summary_model, {"cost": round(rng.uniform(0.02, 0.06), 6)}, cfg.text_provider)
    return usage.snapshot()


def row(cfg: Settings, rec: dict, kinds: dict, state: dict, started: str, wall: float, first: bool) -> dict:
    """Строка журнала обработки — с теми же полями, что собирает pipeline.process."""
    stem = PurePosixPath(rec["file"]).stem
    paid = spent(cfg, rec, kinds, state["rng"], first)
    heard, tri, protocol = state["heard"], state["triage"], state["protocol"]
    signed = sum(1 for name, _, _ in rec["voices"].values() if name)
    return {
        "file": rec["file"], "stem": stem, "output": str(out_dir(cfg, rec)), "mode": pipeline.MODE,
        "template": rec["kind"], "template_auto": True, "manual": not first, "force": False,
        "source_size": len(PLACEHOLDER), "started_at": started, "finished_at": later(started, wall),
        "duration_sec": round(wall, 2), "audio_duration_sec": heard["audio_duration_sec"],
        "cost": paid["cost"], "status": "ok", "error": None, "reused_transcript": not first,
        "speech_model": heard["model"], "speakers": heard["speakers"], "segments": heard["segments"],
        "unlabeled": heard["unlabeled"], "speakers_unreliable": False, "crowded_meeting": heard["crowded_meeting"],
        "transcript": f"{pipeline.MODE}/{stem}.txt", "summary": f"summary/{stem}.md", "reused_triage": not first,
        "template_title": templates.load_template(cfg.prompts_dir, rec["kind"])[0],
        "title": tri["title"], "gist": tri["gist"], "record_date": tri["date"], "topics": tri["topics"],
        # первый прогон знает только догадки разбора, пересборка — ещё и подписи человека
        "named_speakers": (sum(1 for s in tri["speakers"] if s["name"]) if first
                           else signed + sum(1 for name, guess, _ in rec["voices"].values() if not name and guess)),
        "type_confidence": tri["confidence"], "type_reason": tri["type_reason"], "type_unsure": triage.unsure(tri),
        "summary_chars": protocol["chars"], "timestamps": protocol["marks"], "timestamps_unknown": 0,
        "cost_details": paid["by_stage"], "cost_models": paid["by_model"], "cost_providers": paid["by_provider"],
        "api_calls": paid["calls"],
    }


# ---------- события: всё, что произошло, в порядке времени ----------

def plan(cfg: Settings, kinds: dict, book: ledger.Ledger, trail: journal.Journal, people: users.Users,
         words: dictionary.Store) -> list:
    """(когда, что сделать) — заведение пользователей и папок, загрузки, прогоны, подписи, отметки."""
    events: list[tuple[str, object]] = []
    setup = iter(range(0, 10 ** 6, 97))        # секунды между действиями администратора при настройке

    def then(when: str, act) -> None:
        events.append((when, act))

    for login, name, admin, _ in PEOPLE:
        def create(login=login, name=name, admin=admin):
            people.put(login, name, PASSWORDS[login], admin, home_upload=login in HOME_UPLOAD)
            (cfg.inbox / users.HOME_ROOT / login).mkdir(parents=True, exist_ok=True)
            trail.write("user_create", users.ADMIN, login=login, admin=admin, password=True,
                        **({"home_upload": True} if login in HOME_UPLOAD else {}))
        then(stamp("2026-08-24 10:05", next(setup)), create)
    for folder, by in FOLDERS:
        def make(folder=folder, by=by):
            (cfg.inbox / folder).mkdir(parents=True, exist_ok=True)
            series.Store(book.db).put(folder, True)          # новая папка — серия, как в сервисе
            trail.write("folder_create", by, folder=folder)
        then(stamp("2026-08-24 10:05", next(setup)), make)
    for folder, by in LOOSE:
        def loose(folder=folder, by=by):
            series.Store(book.db).put(folder, False)
            trail.write("series", by, folder=folder, on=False)
        then(stamp("2026-08-24 10:05", next(setup)), loose)
    for login, folder, read, upload, manage in GRANTS:
        def grant(login=login, folder=folder, read=read, upload=upload, manage=manage):
            people.set_access(login, folder, read, manage, upload)
            trail.write("access", users.ADMIN, folder=folder, login=login, read=read, upload=upload, manage=manage)
        then(stamp("2026-08-24 10:05", next(setup)), grant)

    for folder, by, when, who, terms in DICTIONARIES:
        def note(folder=folder, by=by, who=who, terms=terms):
            saved = words.put(dictionary.FOLDERS, folder, dictionary.parse_people(who),
                              dictionary.parse_terms(terms), by)
            trail.write("dictionary", by, folder=folder, people=len(saved["people"]), terms=len(saved["terms"]))
        then(stamp(when), note)

    for rec in RECORDS:
        state = {"rng": random.Random(rec["file"])}
        rng = state["rng"]
        loader, loaded = rec["uploaded"]
        signer, signed_at = rec["signed"]
        uploaded = stamp(loaded, rng.randrange(60))
        signed = stamp(signed_at, rng.randrange(60))
        # сервис ждёт, пока файл перестанет меняться, потом обрабатывает: тем дольше, чем длиннее запись
        first_start, first_wall = later(uploaded, rng.uniform(35, 50)), rec["minutes"] * rng.uniform(2.4, 3.6) + 70
        again_start, again_wall = later(signed, rng.uniform(1, 4)), rng.uniform(45, 110)

        def upload(rec=rec, state=state, loader=loader):
            state["size"] = int(rec["minutes"] * state["rng"].uniform(0.45, 0.6) * 2 ** 20
                                * (9 if rec["file"].endswith(".mp4") else 1))
            trail.write("upload", loader, file=rec["file"], size=state["size"])

        def first_run(rec=rec, state=state, started=first_start, wall=first_wall):
            words, anchors, duration = spoken(rec, state["rng"])
            state["heard"] = write_transcript(cfg, rec, words, duration, state["rng"])
            state["triage"] = write_triage(cfg, rec, kinds, state["rng"])
            md = rec["protocol"].replace("{dur}", span(duration))
            for anchor, clock in anchors.items():
                md = md.replace("{%s}" % anchor, f"[{clock}]")
            assert "{" not in md, f"{rec['file']}: в протоколе осталась метка {md[md.index('{'):][:12]}"
            state["md"], state["protocol"] = md, {"chars": len(md), "marks": len(set(summarize.MARK_RE.findall(md)))}
            source = cfg.inbox / rec["file"]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(PLACEHOLDER)
            record = row(cfg, rec, kinds, state, started, wall, first=True)
            with book.db.write():
                book.put(record)
                trail.run(record, "", "new")

        def sign(rec=rec, signer=signer):
            names.save(out_dir(cfg, rec), PurePosixPath(rec["file"]).stem,
                       {"parts": {"1": {label: name for label, (name, _, _) in rec["voices"].items() if name}}, "lines": {}})
            trail.write("names", signer, file=rec["file"])

        def rebuild(rec=rec, state=state, signer=signer, started=again_start, wall=again_wall):
            write_protocol(cfg, rec, state["md"], book, started, state["rng"])
            record = row(cfg, rec, kinds, state, started, wall, first=False)
            with book.db.write():
                book.put(record)
                trail.run(record, signer, "rebuild")
            ledger.write_marker(out_dir(cfg, rec), record["stem"], book.get(rec["file"]))

        then(uploaded, upload)
        then(later(first_start, first_wall), first_run)
        then(signed, sign)
        then(later(again_start, again_wall), rebuild)

        for when, by, part, what, value in rec["marks"]:
            def mark(rec=rec, by=by, part=part, what=what, value=value):
                path = pipeline.protocol_path(cfg, rec["file"], book.get(rec["file"]))
                tasks = external.tasks(path.read_text(encoding="utf-8"))
                found = [t for t in tasks if part in t["task"]]
                assert len(found) == 1, f"{rec['file']}: поручение «{part}» — найдено {len(found)}"
                key = marks.key(found[0]["who"], found[0]["task"])
                noted = marks.Store(book.db)
                if what == "done":
                    task, _ = noted.put(rec["file"], tasks, key, True, by, value)
                    trail.write("task_done", by, file=rec["file"], task=task["task"][:120], owner=task["who"] or "")
                else:
                    task, _, was = noted.put_due(rec["file"], tasks, key, value, by)
                    trail.write("task_due", by, file=rec["file"], task=task["task"][:120], owner=task["who"] or "",
                                was=was or task["due_text"] or "", now=task["due"])
            assert stamp(when) > later(again_start, again_wall), f"{rec['file']}: отметка «{part}» раньше протокола"
            then(stamp(when, rng.randrange(60)), mark)
    return events


PASSWORDS: dict[str, str] = {}


def main(notes: Path) -> None:
    cfg = Settings.from_env()
    kinds = templates.types(cfg.prompts_dir)
    PASSWORDS.update({login: secrets.token_urlsafe(9) for login, *_ in PEOPLE})

    if not (cfg.state_dir / db.FILE).is_file():
        sys.exit(f"базы {cfg.state_dir / db.FILE} нет: сначала запустите сервис этой версии — он заведёт базу"
                 + (", а прежние файлы состояния перенесёт в неё" if migrate.found_names(cfg) else "")
                 + "; потом остановите его и запустите этот сценарий")
    base = db.Database(cfg.state_dir)
    trail = journal.Journal(base)
    forget_journal(trail)
    people = users.Users(base)
    for login, *_ in PEOPLE:
        people.delete(login)                    # вместе с правами и ключами доступа
    book = ledger.Ledger(base)
    for rec in RECORDS:
        if book.get(rec["file"]):
            book.forget(rec["file"])            # вместе с отметками поручений
        shutil.rmtree(out_dir(cfg, rec), ignore_errors=True)
    words = dictionary.Store(base)
    noted = marks.Store(base)

    real_now = journal.now_iso
    for when, act in sorted(plan(cfg, kinds, book, trail, people, words), key=lambda e: e[0]):
        journal.now_iso = lambda when=when: when        # время события — в журнале и в отметках поручений
        act()
    journal.now_iso = real_now
    for rec in RECORDS:                                 # карта — уже с отметками «выполнено» и сроками
        mindmap.write_for(pipeline.protocol_path(cfg, rec["file"], book.get(rec["file"])), noted.load(rec["file"]))

    issued = []
    for login, name, days, manage in KEYS:
        token, key = people.issue_key(login, name, days, manage)
        trail.write("key_issue", login, key=key["name"], key_kind=key["kind"], expires=key["expires"])
        issued.append((login, name, "с управлением" if manage else "обычный", days, token))
    sort_journal(trail)

    address = cfg.public_url or "адрес сервиса"
    notes.write_text("\n".join([
        "# Демонстрационные входы", "",
        f"Адрес: {address}", "",
        "Всё выдумано: компания, люди, поставщики, системы, совещания. Даты привязаны к 10.10.2026.",
        "Пароль встроенного администратора (admin) здесь не записан и не менялся.", "",
        "| Логин | Кто | Что ему открыто | Пароль |", "|---|---|---|---|",
        *(f"| {login} | {name} | {role} | `{PASSWORDS[login]}` |" for login, name, _, role in PEOPLE), "",
        "## Ключи доступа", "",
        "Заголовок запроса: `Authorization: Bearer <ключ>`.", "",
        "| Чей | Название | Вид | Срок, дней | Ключ |", "|---|---|---|---|---|",
        *(f"| {login} | {name} | {kind} | {days} | `{token}` |" for login, name, kind, days, token in issued), "",
        "## Чего в демонстрации нет", "",
        "- Звука: проигрыватель на странице записи не появляется, метки времени никуда не перематывают.",
        "- Исходных файлов: на их месте заглушки без звука. Кнопку «Пересобрать протокол» и повторный запуск",
        "  не нажимать — запись уйдёт в ошибку. Вернуть её — запустить наполнение заново.", "",
        "## Наполнить заново", "",
        "Команды — в начале файла `tools/demo_seed.py` в репозитории. Пароли и ключи при этом меняются,",
        "этот файл переписывается.", ""]), encoding="utf-8")

    found, _ = registry.rows([book.get(rec["file"]) for rec in RECORDS],
                             lambda r: pipeline.protocol_path(cfg, r["file"], r), registry.Cache(), now="2026-10-10",
                             marked=noted)
    runs = [e for e in trail.read() if e["kind"] == "run" and about_demo(e)]
    print(f"записей {len(RECORDS)}, папок с записями {len({PurePosixPath(r['file']).parent for r in RECORDS})}, "
          f"пользователей {len(PEOPLE)}, ключей {len(issued)}")
    print(f"поручений {len(found)}: закрыто {sum(not t['open'] for t in found)}, "
          f"просрочено {sum(t['overdue'] for t in found)}, "
          f"открыто со сроком {sum(t['open'] and not t['overdue'] and bool(t['due_date']) for t in found)}, "
          f"открыто без срока {sum(t['open'] and not t['due_date'] for t in found)}, "
          f"на неподписанных {sum(t['unsigned'] for t in found)}")
    print(f"прогонов {len(runs)}, потрачено {usage.text({'USD': round(sum(r['cost'].get('USD', 0) for r in runs), 2)})}, "
          f"на запись от {min(usage.amounts(book.get(r['file'])['cost_total'])['USD'] for r in RECORDS):.2f} "
          f"до {max(usage.amounts(book.get(r['file'])['cost_total'])['USD'] for r in RECORDS):.2f} $")
    base.close()
    print(f"входы записаны: {notes}")


# ====================================================================================
# Данные. Компания, люди, поставщики и системы выдуманы.
# ====================================================================================

PEOPLE += [
    ("a.melnikov", "Мельников Артём Вадимович", True, "администратор сервиса: видит всё, расходы, пользователей"),
    ("m.orlova", "Орлова Марина Сергеевна", False,
     "администратор папки «Снабжение» с подпапками (загружает, раздаёт чтение и загрузку); "
     "читает «Проекты/Склад» — без загрузки"),
    ("i.belousov", "Белоусов Игорь Николаевич", False, "администратор папки «Проекты» с подпапками"),
    ("e.safonova", "Сафонова Елена Павловна", False,
     "читатель с загрузкой: читает «Проекты» с подпапками и загружает в «Проекты/Склад» с подпапками; "
     "«ИТ» — только чтение самой папки; администратор «Проекты/Склад/Обучение»"),
    ("d.kuznetsov", "Кузнецов Дмитрий Олегович", False,
     "только читатель: «Снабжение/Планёрки» (только эта папка) и «Проекты/Склад» с подпапками; "
     "кнопки «Загрузить запись» у него нет — ни в общих папках, ни в личной"),
    ("v.sokolova", "Соколова Вера Игоревна", False,
     "личная папка с подпапкой «Заказчики» — в ней она владелец и загружать ей туда разрешено; "
     "«Снабжение/Планёрки» читает, но не загружает"),
]

HOME_UPLOAD |= {"a.melnikov", "m.orlova", "i.belousov", "v.sokolova"}

FOLDERS += [
    ("Снабжение", users.ADMIN), ("Снабжение/Планёрки", users.ADMIN), ("Снабжение/Поставщики", users.ADMIN),
    ("Проекты", users.ADMIN), ("Проекты/Склад", users.ADMIN), ("Проекты/Склад/Обучение", users.ADMIN),
    ("ИТ", users.ADMIN), ("ИТ/Обучение", users.ADMIN),
    (f"{users.HOME_ROOT}/v.sokolova/Заказчики", "v.sokolova"),
]

GRANTS += [
    # логин, папка, чтение, загрузка, администрирование
    ("m.orlova", "Снабжение", None, None, users.TREE),
    ("m.orlova", "Проекты/Склад", users.TREE, None, None),
    ("i.belousov", "Проекты", None, None, users.TREE),
    ("e.safonova", "Проекты", users.TREE, None, None),
    ("e.safonova", "Проекты/Склад", None, users.TREE, None),
    ("e.safonova", "ИТ", users.ONLY, None, None),
    ("e.safonova", "Проекты/Склад/Обучение", None, None, users.ONLY),
    ("d.kuznetsov", "Снабжение/Планёрки", users.ONLY, None, None),
    ("d.kuznetsov", "Проекты/Склад", users.TREE, None, None),
    ("v.sokolova", "Снабжение/Планёрки", users.ONLY, None, None),
]

# в «ИТ» лежат несвязанные совещания — планёрка отдела и разбор сбоя: это не серия
LOOSE += [("ИТ", "a.melnikov")]

KEYS += [
    ("m.orlova", "Демо: ИИ-клиент на рабочем ноутбуке", 90, False),
    ("a.melnikov", "Демо: выгрузка в корпоративный портал", 30, True),
]

DICTIONARIES += [
    ("Снабжение", "m.orlova", "2026-08-25 09:40", """
Орлова Марина Сергеевна — начальник отдела снабжения
Гришин Павел Андреевич (Паша) — закупки сырья
Тимофеева Ольга Викторовна — закупки расходных материалов, договоры
Валиев Ринат Ильдарович — логистика
Кузнецов Дмитрий Олегович — начальник склада
""", """
Арктур-Пак — поставщик картона
лайнер — картон для плоских слоёв гофрокартона
флютинг — бумага для гофрированного слоя
стрейч-плёнка
"""),
    ("Проекты/Склад", "i.belousov", "2026-08-26 16:10", """
Белоусов Игорь Николаевич — руководитель проекта
Кузнецов Дмитрий Олегович — начальник склада
Сафонова Елена Павловна — аналитик
Мельников Артём Вадимович — ИТ, инфраструктура
""", """
ТСД — терминал сбора данных
адресное хранение
весогабариты — вес и размеры единицы товара в справочнике
"""),
]

# Запись: file — путь, под которым она значится (исходника нет); kind — тип шаблона;
# voices — метка → (ФИО, подписанное человеком, или None; имя по догадке разбора; роль);
# uploaded и signed — кто и когда загрузил и подписал говорящих (за подписью идёт пересборка);
# talk — реплики; protocol — протокол, метки {a1} заменяются временем реплики с этой меткой;
# marks — что люди потом отметили у поручений: (когда, кто, часть текста, done | due, пояснение | дата).

SUPPLY = {
    "S1": ("Орлова Марина Сергеевна", "Марина Сергеевна", "ведёт планёрку, начальник отдела снабжения"),
    "S2": ("Гришин Павел Андреевич", "Павел", "закупки сырья"),
    "S3": ("Тимофеева Ольга Викторовна", "Ольга", "закупки расходных материалов, договоры"),
    "S4": ("Валиев Ринат Ильдарович", "Ринат", "логистика"),
    "S5": ("Кузнецов Дмитрий Олегович", "Дмитрий", "начальник склада"),
}

RECORDS.append(dict(
    file="Снабжение/Планёрки/2026-09-21 Планёрка снабжения.m4a", kind="meeting", minutes=34,
    title="Планёрка отдела снабжения 21 сентября", date="2026-09-21",
    why="несколько человек по очереди докладывают и получают поручения",
    gist="Недопоставка лайнера, запрос поставщика на повышение цены, претензия по стрейч-плёнке, "
         "договор на клей, опоздания перевозчика и новое окно приёмки сырья.",
    topics=["поставки картона", "цена на четвёртый квартал", "претензия по плёнке", "договор на клей",
            "перевозчики", "окно приёмки", "протечка кровли"],
    voices=SUPPLY, uploaded=("m.orlova", "2026-09-21 10:52"), signed=("m.orlova", "2026-09-21 12:15"),
    talk="""
1 a0: Коллеги, доброе утро, начинаем планёрку. Сегодня двадцать первое сентября, повестка обычная: сырьё, расходники, логистика, склад. Павел, начни с картона.
2 a1: Доброе утро. По картону картина такая: «Арктур-Пак» отгрузил на прошлой неделе сто двадцать тонн лайнера из ста сорока по графику. Двадцать тонн они перенесли на эту среду, говорят, была остановка машины на профилактику.
1: То есть недопоставка двадцать тонн. Производству этого хватит до среды?
2 a2: Хватит впритык. По расчёту на складе лайнера на шесть рабочих дней, так что до среды доживаем, но запаса почти не остаётся.
5 a3: Подтверждаю, у меня по остаткам сто восемь тонн лайнера и сорок одна тонна флютинга. Флютинга как раз много, его можно не торопить.
1 a4: Хорошо. Павел, тогда возьми с «Арктур-Пака» письменное подтверждение даты отгрузки, не на словах. Срок — завтра до обеда.
2: Понял, запрошу сегодня же, завтра до обеда будет письмо.
1: Что по ценам на четвёртый квартал? Они обещали прислать новое предложение.
2 a5: Прислали в пятницу. Просят плюс шесть процентов к текущей цене лайнера с первого октября. Аргумент — подорожала макулатура. Я считаю, что шесть — это запрос с запасом, реально можно сойтись на трёх-четырёх.
1 a6: Шесть процентов мы не согласуем. Позиция такая: до конца года держим цену не выше плюс трёх с половиной процентов, иначе выносим объём на тендер. Это им и передай.
2: Передам. Мне для переговоров нужна справка, сколько мы у них выбрали за девять месяцев.
3 a7: Справку по объёмам я сделаю, у меня выгрузка из «Гроссбуха» почти собрана. К среде пришлю.
1: Договорились. Ольга, раз уж ты начала, давай по расходникам.
3 a8: По расходникам два вопроса. Первый — стрейч-плёнка. «Литера-Полимер» поставил партию, в которой намотка короче заявленной: на рулоне написано триста метров, по факту склад намерил двести семьдесят.
5 a9: Да, мы перемерили десять рулонов из разных паллет, везде от двухсот шестидесяти пяти до двухсот семидесяти пяти метров. Акт расхождения я составил в четверг.
3 a10: Я по этому акту готовлю претензию. Сумма недовложения по партии получается около сорока двух тысяч рублей. Претензию отправлю до пятницы.
1 a11: Отправляй. И пока претензию не закроют, новые заказы «Литера-Полимеру» не размещаем, это решение. Второй вопрос?
3 a12: Второй — клей для склейки короба. Текущего запаса на три недели, договор с «Тесьма-Трейд» заканчивается тридцатого сентября, а новую редакцию они до сих пор не подписали.
1 a13: Это плохо. Ольга, продли договор хотя бы допсоглашением на месяц, срок — до двадцать пятого сентября. Без клея линия встанет.
3: Сделаю, допсоглашение лежит у юристов с прошлой недели, потороплю.
1: Ринат, логистика.
4 a14: По логистике. За неделю сорок шесть рейсов, из них четыре с опозданием больше двух часов, и все четыре — перевозчик «Четыре оси». У них не хватает машин с боковой загрузкой.
1: Четыре опоздания из сорока шести — это почти девять процентов. Что предлагаешь?
4 a15: Предлагаю завести второго перевозчика на направление в Заречный. Я запросил три коммерческих предложения, два уже пришли, третье жду.
1 a16: Хорошо, сравнение по трём предложениям покажи на следующей планёрке, двадцать восьмого.
4: Покажу. И ещё: склад просил сдвинуть окно приёмки.
5 a17: Да, прошу принимать машины с сырьём до шестнадцати часов, а не до восемнадцати. После шестнадцати у меня одна смена и на приёмке, и на отгрузке, не успеваем, фуры стоят в очереди.
4 a18: Перевозчикам это неудобно, но предупредить можно. Только нужна дата, с которой правило начинает работать.
1 a19: Решаем так: с первого октября приёмка сырья до шестнадцати ноль-ноль. Ринат, уведоми перевозчиков письменно на этой неделе.
4: Принято.
5 a20: И последнее от склада. В четвёртой зоне течёт кровля над стеллажами с лайнером, два рулона уже подмочило. Заявку в хозяйственную службу я подал, ответа пока нет.
1 a21: Кровлю я подниму сама с директором по производству. Дмитрий, а ты пока перемести рулоны из-под протечки, сегодня же.
5: Переместим до конца смены.
2 a22: Марина Сергеевна, ещё вопрос: будем ли мы в октябре пробовать лайнер второго поставщика? Образцы лежат с августа.
1 a23: Вопрос правильный, но ответа у меня нет: технологи до сих пор не дали заключение по образцам. Вернёмся, когда оно появится.
2: Понял, ждём технологов.
1 a24: Всё, коллеги, спасибо. Следующая планёрка двадцать восьмого в десять.
""",
    protocol="""
# Протокол совещания

**Название:** Планёрка отдела снабжения 21 сентября
**Дата:** 21.09.2026 (из имени файла)
**Длительность:** {dur}
**Вёл:** Орлова Марина Сергеевна

## 1. Кратко
- «Арктур-Пак» недопоставил 20 тонн лайнера, перенёс на среду; запаса лайнера на шесть рабочих дней.
- Поставщик просит +6 % к цене лайнера с 1 октября; позиция отдела — не выше +3,5 % до конца года, иначе тендер.
- В партии стрейч-плёнки «Литера-Полимера» намотка короче заявленной; готовится претензия примерно на 42 тысячи рублей, новые заказы приостановлены.
- Договор на клей с «Тесьма-Трейд» истекает 30 сентября, запас клея — на три недели.
- У перевозчика «Четыре оси» четыре опоздания из 46 рейсов; ищут второго перевозчика на Заречный.
- С 1 октября сырьё принимают до 16:00.

## 2. Участники

| Участник | Роль на совещании |
|---|---|
| Орлова Марина Сергеевна | вела планёрку, давала поручения |
| Гришин Павел Андреевич | доложил по картону и ценам |
| Тимофеева Ольга Викторовна | доложила по расходным материалам |
| Валиев Ринат Ильдарович | доложил по логистике |
| Кузнецов Дмитрий Олегович | остатки, приёмка, состояние склада |

Упоминали, но не выступали: директор по производству, технологи, юристы.

## 3. Что докладывали

**Поставки картона — Гришин** {a1}
- «Арктур-Пак» отгрузил 120 тонн лайнера из 140 по графику; 20 тонн перенесены на среду из-за остановки машины на профилактику.
- Лайнера на складе хватает на шесть рабочих дней, запаса почти нет. {a2}
- Остатки по данным склада: 108 тонн лайнера и 41 тонна флютинга; флютинга достаточно. {a3}

**Цена на четвёртый квартал — Гришин** {a5}
- Поставщик в пятницу прислал предложение: +6 % к цене лайнера с 1 октября, причина — подорожала макулатура.
- По оценке Гришина, реально сойтись на 3–4 %.

**Расходные материалы — Тимофеева** {a8}
- Стрейч-плёнка «Литера-Полимера»: на рулоне заявлено 300 метров, по факту около 270.
- Склад перемерил десять рулонов из разных паллет: от 265 до 275 метров; акт расхождения составлен в четверг. {a9}
- Сумма недовложения по партии — около 42 тысяч рублей. {a10}
- Клей для склейки короба: запас на три недели, договор с «Тесьма-Трейд» заканчивается 30 сентября, новая редакция не подписана. {a12}

**Логистика — Валиев** {a14}
- За неделю 46 рейсов, четыре с опозданием больше двух часов — все у перевозчика «Четыре оси»: не хватает машин с боковой загрузкой.
- Запрошены три коммерческих предложения от других перевозчиков на направление в Заречный, два получены. {a15}

**Склад — Кузнецов** {a17}
- После 16:00 на приёмке и отгрузке работает одна смена, машины стоят в очереди.
- В четвёртой зоне течёт кровля над стеллажами с лайнером, подмочено два рулона; заявка в хозяйственную службу подана, ответа нет. {a20}

## 4. Решения

| Решение | В записи | Кто предложил | Кто согласился |
|---|---|---|---|
| Повышение цены лайнера на 6 % не согласовывать; до конца года — не выше +3,5 %, иначе объём выносится на тендер | {a6} | Орлова | Гришин |
| Пока претензия по стрейч-плёнке не закрыта, новые заказы «Литера-Полимеру» не размещать | {a11} | Орлова | Тимофеева |
| С 1 октября сырьё принимается до 16:00 вместо 18:00 | {a19} | Кузнецов | Орлова, Валиев |

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Тимофеева Ольга Викторовна** | | | |
| Подготовить справку по объёмам закупок у «Арктур-Пака» за девять месяцев | к среде | {a7} | выгрузка почти собрана |
| Отправить «Литера-Полимеру» претензию по недовложению стрейч-плёнки | до пятницы | {a10} | в работе |
| Продлить договор с «Тесьма-Трейд» допсоглашением на месяц | до 25 сентября | {a13} | допсоглашение у юристов |
| **Гришин Павел Андреевич** | | | |
| Получить от «Арктур-Пака» письменное подтверждение даты отгрузки 20 тонн лайнера | завтра до обеда | {a4} | — |
| Передать «Арктур-Паку» позицию по цене: не выше +3,5 % до конца года | — | {a6} | — |
| **Валиев Ринат Ильдарович** | | | |
| Показать сравнение трёх предложений перевозчиков на направление в Заречный | на планёрке 28 сентября | {a16} | два предложения из трёх получены |
| Письменно уведомить перевозчиков о приёмке сырья до 16:00 с 1 октября | на этой неделе | {a19} | — |
| **Кузнецов Дмитрий Олегович** | | | |
| Переместить рулоны лайнера из-под протечки в четвёртой зоне | сегодня | {a21} | — |
| **Орлова Марина Сергеевна** | | | |
| Поднять вопрос ремонта кровли в четвёртой зоне с директором по производству | — | {a21} | — |

Прочерк в колонке «Срок» — срок на совещании не назвали.
Следующая планёрка — 28 сентября в 10:00.

## 6. Открытые вопросы
- **Лайнер второго поставщика.** Гришин спросил, будут ли в октябре пробовать лайнер второго поставщика: образцы лежат с августа {a22}. Ответа нет — технологи не дали заключение по образцам {a23}.

## 7. Риски
- Запаса лайнера хватает только до среды: если «Арктур-Пак» снова сдвинет отгрузку, производство останется без сырья — Гришин {a2}.
- Без продления договора на клей линия склейки встанет через три недели — Орлова {a13}.
- Протечка кровли в четвёртой зоне портит рулоны лайнера — Кузнецов {a20}.
""",
    marks=[
        ("2026-09-21 17:40", "m.orlova", "Переместить рулоны", "done", "Перенесли в третью зону до конца смены"),
        ("2026-09-22 09:10", "m.orlova", "Продлить договор", "due", "2026-09-25"),
        ("2026-09-22 12:20", "m.orlova", "письменное подтверждение", "done", "Письмо получено, отгрузка в среду 23.09"),
        ("2026-09-23 16:05", "m.orlova", "справку по объёмам", "done", ""),
        ("2026-09-25 15:30", "m.orlova", "претензию по недовложению", "done", "Отправлена 25.09, исх. № 214"),
        ("2026-09-28 13:05", "m.orlova", "сравнение трёх предложений", "done", "Показано на планёрке 28.09"),
        ("2026-09-29 11:45", "m.orlova", "уведомить перевозчиков", "done", "Письма разосланы 24.09, двое подтвердили"),
        ("2026-09-30 17:15", "m.orlova", "Продлить договор", "done", "Допсоглашение подписано 30.09"),
        ("2026-10-01 16:40", "m.orlova", "позицию по цене", "done", "Сошлись на +3,5 % до 31.12"),
        ("2026-10-05 13:50", "m.orlova", "ремонта кровли", "done", "Подрядчик был 02.10, капремонт — в ноябре"),
    ],
))

RECORDS.append(dict(
    file="Снабжение/Планёрки/2026-09-28 Планёрка снабжения.m4a", kind="meeting", minutes=37,
    title="Планёрка отдела снабжения 28 сентября", date="2026-09-28",
    why="несколько человек по очереди докладывают и получают поручения",
    gist="Выбор второго перевозчика, дефицит поддонов, превышение плана закупок на октябрь, "
         "подмоченный лайнер и подготовка к инвентаризации расходников.",
    topics=["цена лайнера", "второй перевозчик", "поддоны", "план закупок на октябрь", "инвентаризация",
            "ремонт кровли"],
    voices=SUPPLY, uploaded=("m.orlova", "2026-09-28 10:55"), signed=("m.orlova", "2026-09-28 12:05"),
    talk="""
1 b0: Доброе утро, коллеги. Двадцать восьмое сентября, начинаем. Сначала коротко по прошлым поручениям, потом новое. Павел, картон и цена.
2 b1: Двадцать тонн лайнера пришли в среду, двадцать третьего, как и обещали в письме. По цене: «Арктур-Пак» опустился с шести до четырёх процентов, на три с половиной пока не идут. Встречу назначили на четверг, первое октября, у них в офисе.
1 b2: На четверг езжай с цифрами по объёму. Справку Ольга тебе прислала?
2: Прислала в среду, за девять месяцев мы выбрали у них тысячу сто шестьдесят тонн, это весомый аргумент.
1: Хорошо. Ольга, претензия и договор на клей.
3 b3: Претензию «Литера-Полимеру» отправила в пятницу, срок ответа по договору — десять рабочих дней. По клею хуже: юристы вернули допсоглашение с правками только в пятницу вечером, к двадцать пятому не успели. Подпишем до тридцатого, это последний день действующего договора.
1 b4: То есть срок сорван на пять дней. До тридцатого — крайний срок, Ольга, дальше двигать некуда.
3: Понимаю, сегодня отправлю «Тесьма-Трейд» согласованную редакцию.
1: Ринат, перевозчики.
4 b5: Сравнение готово. Три предложения: «Четыре оси» держат тридцать восемь рублей за километр, «Тракт-Сервис» даёт сорок один рубль, но у них шесть машин с боковой загрузкой, и «Грузовой двор» — тридцать шесть рублей, но боковой загрузки нет совсем.
5 b6: Без боковой загрузки нам не подходит, рулоны на рампе с торца не снять, только краном, а кран один.
4 b7: Поэтому предлагаю «Тракт-Сервис» на пробный месяц, десять рейсов на Заречный. Три рубля разницы на километр — это примерно восемнадцать тысяч в месяц на этих десяти рейсах.
1 b8: Согласна. Берём «Тракт-Сервис» на пробный месяц с пятого октября, десять рейсов, потом смотрим статистику опозданий. Ринат, договор на тебе, до пятого он должен быть подписан.
4 b9: Сделаю. По окну приёмки: уведомления разослал в четверг, два перевозчика подтвердили, «Четыре оси» молчат.
1 b10: Добейся от них подтверждения, иначе первого октября получим фуру в семнадцать тридцать и скандал.
4: Добьюсь, позвоню их диспетчеру сегодня.
1: Дмитрий, склад.
5 b11: Рулоны из-под протечки перенесли в тот же день. Подмочены два рулона, это тонна четыреста лайнера, в работу они не годятся, нужно списывать. Кровлю никто не чинил.
1 b12: По кровле: директор по производству обещал прислать подрядчика до второго октября. Списание оформи до пятницы, акт передай в бухгалтерию.
5 b13: Оформлю. И новая проблема — поддоны. На складе осталось триста штук, расход сто двадцать в день. То есть в четверг отгружать будет не на чем.
2 b14: Поддоны закажу сегодня. Обычная партия — две тысячи штук, поставщик возит по тысяче за машину, первая может быть в среду.
1 b15: Заказывай сегодня же, это приоритет номер один на неделю. Если среда сорвётся, сразу говори мне.
2: Принято.
5 b16: Ещё инвентаризация расходников, она назначена на второе и третье октября. Мне от снабжения нужны два человека в комиссию, иначе считать будем неделю.
1 b17: Двоих назначу до среды, фамилии пришлю тебе письмом.
3 b18: И последнее от меня — план закупок на октябрь. Бюджет восемнадцать миллионов четыреста тысяч, заявок от подразделений набралось на девятнадцать миллионов девятьсот. Превышение полтора миллиона.
1 b19: Что именно даёт превышение?
3 b20: В основном запчасти к вырубному прессу, семьсот тысяч, и увеличенная заявка на гофроклей. Сводную таблицу по статьям я сделаю до тридцатого.
1 b21: Сделай, а что резать — я буду решать с финансовым директором, сама я полтора миллиона не срежу. Пока вопрос открыт.
4 b22: Марина Сергеевна, если режут транспорт, пробный месяц с «Тракт-Сервисом» под угрозой.
1 b23: Транспорт не трогаем, это я обещаю. Всё, спасибо, следующая планёрка пятого октября.
""",
    protocol="""
# Протокол совещания

**Название:** Планёрка отдела снабжения 28 сентября
**Дата:** 28.09.2026 (из имени файла)
**Длительность:** {dur}
**Вёл:** Орлова Марина Сергеевна

## 1. Кратко
- Недопоставленные 20 тонн лайнера пришли 23 сентября; «Арктур-Пак» снизил запрос по цене с 6 до 4 %, встреча — 1 октября.
- Допсоглашение по клею к 25 сентября не подписано, крайний срок — 30 сентября.
- Вторым перевозчиком на Заречный на пробный месяц выбран «Тракт-Сервис»: 10 рейсов с 5 октября.
- Поддонов осталось 300 при расходе 120 в день — заказ на 2 000 штук размещается сегодня.
- Заявки на октябрь превышают бюджет закупок на 1,5 млн рублей; что сокращать, не решено.
- Подмочено 1,4 тонны лайнера, их списывают; кровлю не чинили.

## 2. Участники

| Участник | Роль на совещании |
|---|---|
| Орлова Марина Сергеевна | вела планёрку, принимала решения |
| Гришин Павел Андреевич | картон, цена, поддоны |
| Тимофеева Ольга Викторовна | претензия, договор, план закупок |
| Валиев Ринат Ильдарович | сравнение перевозчиков |
| Кузнецов Дмитрий Олегович | склад, поддоны, инвентаризация |

Упоминали, но не выступали: директор по производству, финансовый директор, юристы.

## 3. Что докладывали

**Картон и цена — Гришин** {b1}
- 20 тонн лайнера пришли в среду, 23 сентября.
- «Арктур-Пак» снизил запрос с 6 до 4 %, на 3,5 % пока не соглашается; встреча назначена на четверг, 1 октября.
- За девять месяцев у поставщика выбрано 1 160 тонн.

**Претензия и договор на клей — Тимофеева** {b3}
- Претензия «Литера-Полимеру» отправлена в пятницу, срок ответа — десять рабочих дней.
- Юристы вернули допсоглашение с «Тесьма-Трейд» с правками в пятницу вечером; к 25 сентября подписать не успели.

**Перевозчики — Валиев** {b5}
- «Четыре оси» — 38 рублей за километр; «Тракт-Сервис» — 41 рубль, шесть машин с боковой загрузкой; «Грузовой двор» — 36 рублей, боковой загрузки нет.
- Разница в 3 рубля на километр — около 18 тысяч рублей в месяц на десяти рейсах. {b7}
- Уведомления о приёмке до 16:00 разосланы; два перевозчика подтвердили, «Четыре оси» не ответили. {b9}

**Склад — Кузнецов** {b11}
- Рулоны из-под протечки перенесены; подмочены два рулона, 1,4 тонны лайнера, в работу не годятся.
- Поддонов осталось 300 штук, расход — 120 в день; в четверг отгружать будет не на чем. {b13}
- Инвентаризация расходников назначена на 2 и 3 октября, в комиссию нужны два человека от снабжения. {b16}

**План закупок на октябрь — Тимофеева** {b18}
- Бюджет — 18,4 млн рублей, заявок — на 19,9 млн; превышение 1,5 млн.
- Основное превышение — запчасти к вырубному прессу (700 тысяч) и увеличенная заявка на гофроклей. {b20}

## 4. Решения

| Решение | В записи | Кто предложил | Кто согласился |
|---|---|---|---|
| «Грузовой двор» не рассматривать: без боковой загрузки рулоны на рампе не снять | {b6} | Кузнецов | Валиев |
| «Тракт-Сервис» — второй перевозчик на Заречный на пробный месяц с 5 октября, 10 рейсов; дальше — по статистике опозданий | {b8} | Валиев | Орлова |
| Расходы на транспорт при сокращении плана закупок не трогать | {b23} | Орлова | — |

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Гришин Павел Андреевич** | | | |
| Заказать 2 000 поддонов | сегодня | {b15} | приоритетная |
| Провести встречу с «Арктур-Паком» по цене лайнера на четвёртый квартал | в четверг, 1 октября | {b2} | — |
| **Тимофеева Ольга Викторовна** | | | |
| Подписать допсоглашение с «Тесьма-Трейд» о продлении договора на клей | до 30 сентября | {b4} | перенесено с 25 сентября |
| Свести план закупок на октябрь в таблицу по статьям | до 30 сентября | {b20} | — |
| **Валиев Ринат Ильдарович** | | | |
| Подписать договор с «Тракт-Сервисом» на пробный месяц | до 5 октября | {b8} | — |
| Получить от «Четырёх осей» подтверждение приёмки сырья до 16:00 | — | {b10} | уведомление отправлено, ответа нет |
| **Орлова Марина Сергеевна** | | | |
| Назначить двух сотрудников снабжения в инвентаризационную комиссию | до среды | {b17} | — |
| Согласовать с финансовым директором, что сокращать в плане закупок на 1,5 млн рублей | — | {b21} | — |
| **Кузнецов Дмитрий Олегович** | | | |
| Оформить списание 1,4 тонны подмоченного лайнера и передать акт в бухгалтерию | до пятницы | {b12} | — |
| **Не участвовали в совещании** | | | |
| Директор по производству: прислать подрядчика на ремонт кровли в четвёртой зоне | до 2 октября | {b12} | обещано |

Прочерк в колонке «Срок» — срок на совещании не назвали.
Следующая планёрка — 5 октября.

## 6. Открытые вопросы
- **Что сокращать в плане закупок.** Превышение 1,5 млн рублей; Орлова одна решить не может и выносит вопрос на финансового директора {b21}. Валиев предупредил, что сокращение транспорта ставит под угрозу пробный месяц с новым перевозчиком {b22}.

## 7. Риски
- Если поставка поддонов в среду сорвётся, в четверг остановятся отгрузки — Кузнецов {b13}.
- «Четыре оси» не подтвердили новое окно приёмки: 1 октября возможен конфликт из-за машины, приехавшей после 16:00, — Орлова {b10}.
- 30 сентября — последний день действующего договора на клей — Тимофеева {b3}.
""",
    marks=[
        ("2026-09-28 16:30", "m.orlova", "Заказать 2 000 поддонов", "done", "Заказ размещён, поставка 30.09 и 02.10 по тысяче"),
        ("2026-09-29 09:30", "m.orlova", "Свести план закупок", "due", "2026-09-30"),
        ("2026-09-29 09:32", "m.orlova", "Оформить списание", "due", "2026-10-02"),
        ("2026-09-30 10:20", "m.orlova", "Назначить двух сотрудников", "done", ""),
        ("2026-09-30 17:20", "m.orlova", "Подписать допсоглашение", "done", "Подписано 30.09, договор продлён до 31.10"),
        ("2026-10-01 11:10", "m.orlova", "Свести план закупок", "done", "Таблица прислана 01.10, на день позже срока"),
        ("2026-10-01 16:45", "m.orlova", "Провести встречу", "done", "Встреча прошла, цена +3,5 % до конца года"),
        ("2026-10-02 15:10", "m.orlova", "договор с «Тракт-Сервисом»", "done", "Подписан 02.10"),
        ("2026-10-05 12:30", "m.orlova", "что сокращать в плане", "done", "Перенесли запчасти к прессу на ноябрь"),
        ("2026-10-05 12:32", "m.orlova", "прислать подрядчика", "done", "Подрядчик был 02.10, сделан временный ремонт"),
    ],
))

RECORDS.append(dict(
    file="Снабжение/Планёрки/2026-10-05 Планёрка снабжения.m4a", kind="meeting", minutes=41,
    title="Планёрка отдела снабжения 5 октября", date="2026-10-05",
    why="несколько человек по очереди докладывают и получают поручения",
    gist="Цена лайнера зафиксирована до конца года, ответ на претензию по плёнке, счёт перевозчика за простой, "
         "недостача скотча по итогам инвентаризации и подготовка тендера на картон.",
    topics=["цена лайнера", "тендер на картон", "компенсация по претензии", "простой машин", "инвентаризация",
            "списание лайнера", "отгрузки в ноябре"],
    voices=SUPPLY, uploaded=("m.orlova", "2026-10-05 11:02"), signed=("m.orlova", "2026-10-05 13:40"),
    talk="""
1 c0: Доброе утро. Пятое октября, начинаем. Павел, поддоны и цена.
2 c1: Поддоны пришли, обе машины, тридцатого и второго, по тысяче штук, дефицит закрыт. По цене с «Арктур-Паком» договорились в четверг: плюс три с половиной процента до тридцать первого декабря, допсоглашение они подписали в пятницу.
1 c2: Отлично, это ровно наша позиция. А на следующий год что?
2 c3: На следующий год они хотят вернуться к разговору в ноябре. Я предлагаю не ждать, а готовить тендер на картон на двадцать седьмой год. Список участников могу собрать до шестнадцатого октября, у меня на примете четыре комбината.
1 c4: Готовь список до шестнадцатого, обсудим на планёрке девятнадцатого. Ольга, плёнка.
3 c5: «Литера-Полимер» ответил на претензию в четверг. Недовложение признают на шестьдесят процентов суммы, деньгами возвращать не хотят, предлагают компенсировать товаром — четырнадцать рулонов плёнки.
5 c6: Четырнадцать рулонов я приму только с перемером каждого. Второй раз верить этикетке не буду.
1 c7: Согласна. Решение такое: компенсацию товаром принимаем, но с перемером при приёмке. Запрет на новые заказы снимаем после того, как компенсация придёт и сойдётся по метражу.
3 c8: Тогда я отвечу им сегодня и запрошу дату поставки. И ещё по «Тесьма-Трейд»: договор продлили, но за третий квартал у нас с ними расхождение во взаиморасчётах на тридцать одну тысячу. Сверку проведу до тринадцатого октября.
1: Хорошо. Ринат.
4 c9: «Тракт-Сервис» — договор подписан второго, первый рейс сегодня, машина на погрузке с восьми утра. По «Четырём осям» плохо: окно приёмки они так и не подтвердили, и второго октября две их машины приехали в семнадцать тридцать.
5 c10: Я их не принял, как и решали. Машины простояли до утра.
4 c11: И теперь «Четыре оси» выставили нам простой — восемнадцать тысяч рублей за две машины. Говорят, письменного согласия на новое окно они не давали.
1 c12: Платить я не собираюсь, уведомление у них было за неделю. Но юридически надо посмотреть, что в договоре написано про изменение условий приёмки. Этим займусь сама с юристом на этой неделе. Ринат, счёт пока не акцептуй.
4: Понял, держу без акцепта.
3 c13: Марина Сергеевна, в договоре, насколько помню, изменение графика приёмки — по соглашению сторон. Одного уведомления может не хватить.
1 c14: Вот поэтому и иду к юристу. Пока вопрос открыт. Дмитрий, инвентаризация.
5 c15: Инвентаризацию расходников провели второго и третьего. Недостача скотча — триста десять рулонов, это примерно сорок шесть тысяч рублей. И излишек угловой защиты, сто восемьдесят штук.
1: Откуда недостача?
5 c16: Причина понятная: в ночную смену скотч в цех выдают без накладных, просто берут с паллеты. Днём всё по документам, ночью — как придётся.
1 c17: Значит, вводим выдачу по накладным и в ночную смену. Дмитрий, с какого числа сможешь?
5 c18: С двенадцатого октября. Мне нужно назначить ответственного кладовщика в ночь и напечатать бланки.
3 c19: А я тогда скорректирую заявку на скотч на октябрь с учётом недостачи, до конца недели.
1 c20: Да, скорректируй. Дмитрий, что со списанием лайнера? Срок был в пятницу.
5 c21: Не оформлено. Бухгалтерия не берёт акт без подписи технолога, а технолог была на больничном. Вышла сегодня. Подпишу у неё и сдам до девятого октября.
1 c22: До девятого, и больше не переносим. Что с кровлей?
5 c23: Подрядчик был второго, залатал временно. Капитальный ремонт обещают в ноябре. Если пойдут сильные дожди, потечёт снова.
1 c24: Поняла, держи рулоны подальше от этого места. Ринат, у тебя было про ноябрь.
4 c25: Да, в ноябре длинные выходные, и перевозчики уже просят график отгрузок заранее. Мне нужен план от отдела продаж, соберу и сведу до четырнадцатого октября.
1 c26: Собирай. И последнее: план закупок мы сократили на полтора миллиона, запчасти к вырубному прессу перенесли на ноябрь, финансовый директор согласовал. Всё, спасибо, следующая планёрка двенадцатого.
""",
    protocol="""
# Протокол совещания

**Название:** Планёрка отдела снабжения 5 октября
**Дата:** 05.10.2026 (из имени файла)
**Длительность:** {dur}
**Вёл:** Орлова Марина Сергеевна

## 1. Кратко
- Цена лайнера у «Арктур-Пака» зафиксирована: +3,5 % до 31 декабря; на 2027 год готовится тендер.
- «Литера-Полимер» признал претензию на 60 % и предлагает компенсацию товаром — 14 рулонов; принимают с перемером.
- «Четыре оси» выставили 18 тысяч рублей за простой двух машин, приехавших после 16:00; платить не собираются, вопрос у юриста.
- Инвентаризация расходников: недостача 310 рулонов скотча (около 46 тысяч рублей) — в ночную смену его выдают без накладных.
- Списание подмоченного лайнера не оформлено, новый срок — 9 октября.
- План закупок на октябрь сокращён на 1,5 млн рублей: запчасти к вырубному прессу перенесены на ноябрь.

## 2. Участники

| Участник | Роль на совещании |
|---|---|
| Орлова Марина Сергеевна | вела планёрку, принимала решения |
| Гришин Павел Андреевич | поддоны, цена, тендер |
| Тимофеева Ольга Викторовна | претензия, сверка, заявка на скотч |
| Валиев Ринат Ильдарович | перевозчики, график отгрузок |
| Кузнецов Дмитрий Олегович | инвентаризация, списание, кровля |

Упоминали, но не выступали: финансовый директор, юрист, технолог, отдел продаж.

## 3. Что докладывали

**Поддоны и цена лайнера — Гришин** {c1}
- Поддоны пришли двумя машинами, 30 сентября и 2 октября, по тысяче штук; дефицит закрыт.
- С «Арктур-Паком» договорились: +3,5 % до 31 декабря, допсоглашение подписано в пятницу.
- О цене на следующий год поставщик хочет говорить в ноябре. {c3}

**Претензия по стрейч-плёнке — Тимофеева** {c5}
- «Литера-Полимер» ответил в четверг: признаёт недовложение на 60 % суммы, деньгами не возвращает, предлагает 14 рулонов плёнки.
- С «Тесьма-Трейд» договор продлён, но за третий квартал расхождение во взаиморасчётах — 31 тысяча рублей. {c8}

**Перевозчики — Валиев** {c9}
- Договор с «Тракт-Сервисом» подписан 2 октября, первый рейс — сегодня.
- «Четыре оси» новое окно приёмки не подтвердили; 2 октября две их машины приехали в 17:30, склад их не принял, машины простояли до утра. {c10}
- Перевозчик выставил простой — 18 тысяч рублей за две машины. {c11}
- Перевозчики просят заранее график отгрузок на ноябрь из-за длинных выходных. {c25}

**Инвентаризация и склад — Кузнецов** {c15}
- Инвентаризация расходников прошла 2 и 3 октября: недостача скотча — 310 рулонов, около 46 тысяч рублей; излишек угловой защиты — 180 штук.
- Причина недостачи: в ночную смену скотч в цех выдают без накладных. {c16}
- Акт списания лайнера бухгалтерия не принимает без подписи технолога; технолог вышла с больничного сегодня. {c21}
- Кровлю 2 октября залатали временно, капитальный ремонт обещан в ноябре. {c23}

**План закупок — Орлова** {c26}
- План закупок на октябрь сокращён на 1,5 млн рублей: запчасти к вырубному прессу перенесены на ноябрь, финансовый директор согласовал.

## 4. Решения

| Решение | В записи | Кто предложил | Кто согласился |
|---|---|---|---|
| Компенсацию от «Литера-Полимера» принять товаром, каждый рулон перемерить при приёмке | {c7} | Кузнецов | Орлова |
| Запрет на новые заказы «Литера-Полимеру» снять после того, как компенсация придёт и сойдётся по метражу | {c7} | Орлова | Тимофеева |
| Счёт «Четырёх осей» за простой не оплачивать до заключения юриста | {c12} | Орлова | Валиев |
| Скотч в цех выдаётся по накладным и в ночную смену | {c17} | Орлова | Кузнецов |

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Тимофеева Ольга Викторовна** | | | |
| Ответить «Литера-Полимеру» о согласии на компенсацию товаром и запросить дату поставки | сегодня | {c8} | — |
| Провести сверку взаиморасчётов с «Тесьма-Трейд» за третий квартал | до 13 октября | {c8} | расхождение 31 тысяча рублей |
| Скорректировать заявку на скотч на октябрь с учётом недостачи | до конца недели | {c19} | — |
| **Кузнецов Дмитрий Олегович** | | | |
| Получить подпись технолога на акте списания 1,4 тонны лайнера и сдать акт в бухгалтерию | до 9 октября | {c21} | перенесено с 2 октября |
| Назначить ответственного кладовщика и начать выдачу скотча по накладным в ночную смену | с 12 октября | {c18} | — |
| **Валиев Ринат Ильдарович** | | | |
| Собрать у отдела продаж план отгрузок на ноябрь и свести график для перевозчиков | до 14 октября | {c25} | — |
| Не акцептовать счёт «Четырёх осей» за простой до решения юриста | — | {c12} | — |
| **Гришин Павел Андреевич** | | | |
| Собрать список участников тендера на картон на 2027 год | до 16 октября | {c4} | на примете четыре комбината |
| **Орлова Марина Сергеевна** | | | |
| Проверить с юристом, что в договоре с «Четырьмя осями» сказано об изменении условий приёмки | на этой неделе | {c12} | — |

Прочерк в колонке «Срок» — срок на совещании не назвали.
Следующая планёрка — 12 октября; список участников тендера обсуждают 19 октября.

## 6. Открытые вопросы
- **Кто платит за простой машин «Четырёх осей».** Орлова считает, что уведомления за неделю достаточно {c12}; Тимофеева напомнила, что по договору график приёмки меняется по соглашению сторон и одного уведомления может не хватить {c13}. Решения нет до разговора с юристом {c14}.

## 7. Риски
- При сильных дождях кровля в четвёртой зоне потечёт снова: ремонт временный — Кузнецов {c23}.
- Если юрист подтвердит позицию перевозчика, 18 тысяч рублей за простой придётся оплатить — Тимофеева {c13}.
""",
    marks=[
        ("2026-10-05 16:20", "m.orlova", "Ответить «Литера-Полимеру»", "done", "Ответ отправлен, компенсацию привезут 15.10"),
        ("2026-10-06 09:15", "m.orlova", "подпись технолога", "due", "2026-10-09"),
        ("2026-10-06 09:16", "m.orlova", "сверку взаиморасчётов", "due", "2026-10-13"),
        ("2026-10-06 09:18", "m.orlova", "план отгрузок на ноябрь", "due", "2026-10-14"),
        ("2026-10-09 17:05", "m.orlova", "заявку на скотч", "done", "Заявка увеличена на 310 рулонов"),
    ],
))

RECORDS.append(dict(
    file="Снабжение/Поставщики/2026-09-10 Оценка поставщиков за август.mp3", kind="meeting", minutes=26,
    title="Оценка поставщиков за август и образцы нового лайнера", date="2026-09-10",
    why="участники обсуждают рабочие вопросы и договариваются о правилах и поручениях",
    gist="Первый рейтинг поставщиков по новой методике, порог 75 баллов и что делать с теми, кто ниже; "
         "испытания образцов лайнера нового поставщика откладываются до конца сентября.",
    topics=["рейтинг поставщиков", "порог оценки", "план исправления", "образцы лайнера", "лаборатория"],
    voices={
        "S1": ("Орлова Марина Сергеевна", "Марина Сергеевна", "ведёт встречу, начальник отдела снабжения"),
        "S2": ("Тимофеева Ольга Викторовна", "Ольга", "считала рейтинг поставщиков"),
        "S3": ("Гришин Павел Андреевич", "Павел", "закупки сырья"),
        "S4": ("Шубина Наталья Петровна", "Наталья Петровна", "технолог, испытания образцов"),
    },
    uploaded=("m.orlova", "2026-09-10 15:20"), signed=("m.orlova", "2026-09-11 09:30"),
    talk="""
1 d0: Коллеги, сегодня не планёрка, а отдельный разговор: оценка поставщиков за август и образцы нового лайнера. Наталья Петровна с нами от технологов. Ольга, начни с рейтинга.
2 d1: Мы первый раз посчитали рейтинг по новой методике. Три критерия: сроки поставки — сорок баллов, качество — сорок, документы — двадцать. «Арктур-Пак» набрал девяносто два балла, «Тесьма-Трейд» — восемьдесят восемь, «Литера-Полимер» — семьдесят один.
1: Почему у «Литера-Полимера» так низко?
2 d2: Две поставки из пяти с опозданием больше трёх дней и одна партия с браком намотки. По документам тоже минус: дважды присылали счёт-фактуру с ошибкой в количестве.
3 d3: По «Арктур-Паку» уточню: девяносто два — это с учётом одной задержки в августе на два дня. Без неё было бы девяносто семь.
4 d4: По качеству добавлю от себя. У «Арктур-Пака» влажность лайнера в августе была в норме во всех двенадцати партиях, от семи до восьми с половиной процентов. Замечаний нет.
1 d5: Хорошо. Теперь главный вопрос: что мы делаем с поставщиком, у которого низкий балл? Сейчас в методике этого нет.
2 d6: Предлагаю порог семьдесят пять баллов. Ниже порога — поставщик получает письмо и обязан прислать план исправления за десять рабочих дней. Два месяца подряд ниже порога — ищем замену.
3 d7: Семьдесят пять — нормально. Только считать не надо, если поставок за месяц было меньше трёх, иначе одна задержка всё рушит.
1 d8: Принимаем оба предложения: порог семьдесят пять баллов, и оцениваем, только если за месяц было три поставки и больше. Ольга, тогда «Литера-Полимеру» уходит запрос плана исправления, до понедельника.
2: Отправлю до понедельника.
1 d9: Методику с порогом я утвержу приказом, чтобы это были не устные договорённости. Сделаю на следующей неделе.
3 d10: И карточки поставщиков в «Гроссбухе» надо обновить: там до сих пор старые контакты и нет поля для рейтинга. Я обновлю, но не быстро, их сорок с лишним.
1: Обновляй по мере сил, срок не ставлю. Наталья Петровна, образцы.
4 d11: Образцы лайнера от «Ковыль-Картона» лежат у нас с двадцатого августа, три рулона разной плотности. На вид хорошие, но без испытаний я заключение не дам. Нужны торцевое сжатие и продавливание.
1: Что мешает испытать?
4 d12: Очередь в лаборатории. Пресс один, и он до конца сентября занят плановыми испытаниями готовой продукции. Реально могу закончить к тридцатому сентября.
3 d13: Тридцатое — это поздно для октябрьской закупки. Заявку на октябрь я подаю двадцать пятого.
4 d14: Быстрее не получится, если только не отдать образцы во внешнюю лабораторию. Это дней пять и тысяч двадцать пять.
1 d15: Двадцать пять тысяч на внешнюю лабораторию я сейчас не найду. Наталья Петровна, испытывайте у себя, срок — тридцатое сентября. Павел, значит, пробная партия у «Ковыль-Картона» не раньше ноября.
3 d16: Понял. Тогда вопрос: по какой цене они дадут пробную партию в ноябре? В августе обещали на четыре процента ниже «Арктур-Пака».
1 d17: Не знаю, и они, думаю, тоже пока не знают. Спросим, когда будет заключение. Всё, спасибо.
""",
    protocol="""
# Протокол совещания

**Название:** Оценка поставщиков за август и образцы нового лайнера
**Дата:** 10.09.2026 (из имени файла)
**Длительность:** {dur}
**Вёл:** Орлова Марина Сергеевна

## 1. Кратко
- Впервые посчитан рейтинг поставщиков по новой методике: «Арктур-Пак» — 92 балла, «Тесьма-Трейд» — 88, «Литера-Полимер» — 71.
- Введён порог 75 баллов: ниже — поставщик присылает план исправления, два месяца подряд ниже — ищут замену.
- «Литера-Полимер» получает запрос плана исправления.
- Образцы лайнера «Ковыль-Картона» испытают только к 30 сентября: пресс в лаборатории занят.
- Пробная партия нового лайнера — не раньше ноября.

## 2. Участники

| Участник | Роль на совещании |
|---|---|
| Орлова Марина Сергеевна | вела встречу, принимала решения |
| Тимофеева Ольга Викторовна | доложила рейтинг, предложила порог |
| Гришин Павел Андреевич | уточнения по сырью и срокам закупки |
| Шубина Наталья Петровна | качество лайнера, испытания образцов |

## 3. Что докладывали

**Рейтинг поставщиков за август — Тимофеева** {d1}
- Критерии: сроки поставки — 40 баллов, качество — 40, документы — 20.
- «Арктур-Пак» — 92, «Тесьма-Трейд» — 88, «Литера-Полимер» — 71.
- У «Литера-Полимера» две поставки из пяти с опозданием больше трёх дней, одна партия с браком намотки, дважды счёт-фактура с ошибкой в количестве. {d2}
- У «Арктур-Пака» одна задержка на два дня; без неё было бы 97 баллов — уточнил Гришин. {d3}

**Качество лайнера — Шубина** {d4}
- Влажность лайнера «Арктур-Пака» в августе в норме во всех двенадцати партиях: от 7 до 8,5 %.

**Образцы лайнера «Ковыль-Картона» — Шубина** {d11}
- Три рулона разной плотности лежат с 20 августа; нужны испытания на торцевое сжатие и продавливание.
- Пресс в лаборатории один и до конца сентября занят плановыми испытаниями готовой продукции. {d12}
- Внешняя лаборатория — около пяти дней и 25 тысяч рублей. {d14}

## 4. Решения

| Решение | В записи | Кто предложил | Кто согласился |
|---|---|---|---|
| Порог рейтинга — 75 баллов: ниже — план исправления за десять рабочих дней, два месяца подряд ниже — поиск замены | {d6} | Тимофеева | Орлова, Гришин |
| Поставщик оценивается, только если за месяц было три поставки и больше | {d7} | Гришин | Орлова |
| Образцы во внешнюю лабораторию не отдавать: денег на это нет | {d15} | Орлова | — |
| Пробная партия лайнера «Ковыль-Картона» — не раньше ноября | {d15} | Орлова | Гришин |

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Тимофеева Ольга Викторовна** | | | |
| Отправить «Литера-Полимеру» запрос плана исправления | до понедельника | {d8} | — |
| **Орлова Марина Сергеевна** | | | |
| Утвердить приказом методику оценки поставщиков с порогом 75 баллов | на следующей неделе | {d9} | — |
| **Гришин Павел Андреевич** | | | |
| Обновить карточки поставщиков в «Гроссбухе»: контакты и поле для рейтинга | — | {d10} | по мере сил, карточек больше сорока |
| **Шубина Наталья Петровна** | | | |
| Испытать образцы лайнера «Ковыль-Картона» на торцевое сжатие и продавливание и дать заключение | до 30 сентября | {d15} | очередь в лаборатории |

Прочерк в колонке «Срок» — срок на совещании не назвали.

## 6. Открытые вопросы
- **Цена пробной партии «Ковыль-Картона».** В августе поставщик обещал цену на 4 % ниже «Арктур-Пака»; сохранится ли она в ноябре, неизвестно {d16}. Спросят, когда будет заключение технолога {d17}.

## 7. Риски
- Заключение по образцам придёт после подачи октябрьской заявки (25 сентября): закупка нового лайнера сдвигается минимум на месяц — Гришин {d13}.
""",
    marks=[
        ("2026-09-11 10:05", "m.orlova", "Испытать образцы", "due", "2026-09-30"),
        ("2026-09-14 12:40", "m.orlova", "запрос плана исправления", "done", "Письмо ушло 14.09"),
        ("2026-09-18 15:25", "m.orlova", "Утвердить приказом", "done", "Приказ № 87 от 18.09"),
    ],
))

RECORDS.append(dict(
    file="Проекты/Склад/2026-08-27 Старт проекта адресного хранения.mp4", kind="meeting", minutes=48,
    title="Старт проекта адресного хранения на складе в Заречном", date="2026-08-27",
    why="участники договариваются о плане проекта и распределяют работы",
    gist="Стартовая встреча проекта: этапы и бюджет, состояние склада, справочник без весогабаритов, "
         "терминалы, покрытие вай-фай и ритм встреч.",
    topics=["этапы проекта", "бюджет", "справочник номенклатуры", "терминалы сбора данных", "вай-фай на складе",
            "интеграция с учётной системой"],
    voices={
        "S1": ("Белоусов Игорь Николаевич", "Игорь Николаевич", "руководитель проекта, ведёт встречу"),
        "S2": ("Кузнецов Дмитрий Олегович", "Дмитрий", "начальник склада"),
        "S3": ("Сафонова Елена Павловна", "Елена", "аналитик, описывает процессы"),
        "S4": ("Мельников Артём Вадимович", "Артём", "ИТ, сеть и интеграция"),
        "S5": ("Валиев Ринат Ильдарович", "", "логистика"),
        "S6": ("Ткаченко Роман Евгеньевич", "Роман Евгеньевич", "руководитель проекта со стороны подрядчика"),
    },
    uploaded=("i.belousov", "2026-08-27 12:10"), signed=("i.belousov", "2026-08-27 16:30"),
    talk="""
1 e0: Коллеги, добрый день. Сегодня у нас стартовая встреча проекта адресного хранения на складе в Заречном. С нами Роман Евгеньевич, руководитель проекта со стороны подрядчика, который ставит нам систему «Стеллаж-7». Цель простая: с первого декабря склад работает по адресам, а не по памяти кладовщиков.
6 e1: Добрый день. Коротко о плане. Четыре этапа: обследование — до восемнадцатого сентября, настройка системы — до тридцатого октября, обучение — в октябре, опытная эксплуатация — весь ноябрь. С первого декабря — промышленная работа.
1 e2: Бюджет проекта утверждён — шесть миллионов восемьсот тысяч, из них лицензии и работы подрядчика четыре девятьсот, оборудование — миллион девятьсот. Дмитрий, расскажи, что у нас на складе сейчас.
2 e3: Сейчас четыре тысячи двести паллетомест в шести зонах. Адресов нет, товар ставим куда свободно, ищем по памяти и по тетради. Средний поиск паллеты — двенадцать минут, в сезон доходит до получаса. Пересорт по последней инвентаризации — три и две десятых процента.
6 e4: Это типичная картина. После внедрения поиск обычно падает до двух-трёх минут, пересорт — ниже половины процента. Но это при условии, что справочник номенклатуры чистый.
3 e5: Вот про справочник. В «Гроссбухе» у нас одиннадцать тысяч позиций, и у тридцати процентов нет весогабаритных характеристик. Это примерно три тысячи триста позиций, которые система не сможет разместить автоматически.
2 e6: Промерить три тысячи триста позиций — это работа не на неделю. Если дам двух человек, по сто позиций в день, выходит полтора месяца.
1 e7: Значит, начинать надо сейчас. Дмитрий, весогабариты на тебе, срок — пятнадцатое октября, чтобы к концу настройки они были в системе. И выдели в рабочую группу двух кладовщиков, которые знают склад лучше всех.
2 e22: Двоих назову до понедельника. С весогабаритами попробуем, но обещать пятнадцатое твёрдо не могу.
6 e8: Теперь оборудование. Нужны терминалы сбора данных, по нашей оценке четырнадцать штук: десять на смену плюс четыре в запас и на зарядку. Срок поставки у производителя — шесть недель.
1 e9: Шесть недель — значит, заказывать надо до конца следующей недели. Терминалы закажу я, счёт пришлите.
4 e10: По моей части главный риск — вай-фай. Склад строили без расчёта на сеть, точек доступа восемь, и я точно знаю, что в дальних рядах пятой зоны связи нет. Нужно обследование покрытия с замерами.
6 e11: Без устойчивого вай-фая терминалы работать не будут, это критично. Нам нужна карта покрытия до конца обследования.
4 e12: Обследование покрытия сделаю до одиннадцатого сентября, с замерами в каждом ряду.
1: Хорошо. Елена, процессы.
3 e13: Я опишу, как сейчас устроены приёмка, размещение и отбор: кто что делает и какие документы оформляет. Это основа для настройки. Срок — к концу обследования, восемнадцатое сентября.
5 e14: От логистики добавлю. Системе надо знать типы машин и окна доставки, иначе она не спланирует ворота. У нас три типа машин и два окна, но есть исключения по перевозчикам.
6 e15: Да, это нужно на этапе обследования. Пришлите списком, в любом виде.
5 e16: Пришлю до пятого сентября.
1 e17: Роман Евгеньевич, от вас нужен детальный план-график с датами и ответственными с обеих сторон.
6 e18: План-график пришлю до третьего сентября. И сразу скажу про интеграцию: обмен с «Гроссбухом» — остатки и заказы — делаем мы, но доступ к тестовой базе нужен с вашей стороны.
4 e19: Тестовую базу поднимем, это несложно. Вопрос в другом: кто будет сопровождать обмен после запуска — вы или мы?
6 e20: Это предмет отдельного договора на сопровождение, в текущий он не входит.
1 e21: Этот вопрос сейчас не решим, вернёмся ближе к ноябрю. Давайте договоримся о ритме: статус проекта раз в три недели по четвергам, следующая встреча — семнадцатого сентября. Всем спасибо.
""",
    protocol="""
# Протокол совещания

**Название:** Старт проекта адресного хранения на складе в Заречном
**Дата:** 27.08.2026 (из имени файла)
**Длительность:** {dur}
**Вёл:** Белоусов Игорь Николаевич

## 1. Кратко
- Цель проекта: с 1 декабря склад в Заречном работает по адресам в системе «Стеллаж-7».
- Этапы: обследование — до 18 сентября, настройка — до 30 октября, обучение — в октябре, опытная эксплуатация — ноябрь.
- Бюджет — 6,8 млн рублей: 4,9 млн — лицензии и работы подрядчика, 1,9 млн — оборудование.
- У 3 300 позиций из 11 000 нет весогабаритов — их нужно промерить до 15 октября.
- Главный технический риск — покрытие вай-фай на складе; обследование до 11 сентября.
- Статус проекта — раз в три недели по четвергам, следующая встреча 17 сентября.

## 2. Участники

| Участник | Роль на совещании |
|---|---|
| Белоусов Игорь Николаевич | руководитель проекта, вёл встречу |
| Ткаченко Роман Евгеньевич | руководитель проекта от подрядчика |
| Кузнецов Дмитрий Олегович | начальник склада |
| Сафонова Елена Павловна | аналитик, процессы и справочник |
| Мельников Артём Вадимович | ИТ: сеть и тестовая база |
| Валиев Ринат Ильдарович | логистика: машины и окна доставки |

## 3. Что докладывали

**План проекта — Ткаченко** {e1}
- Четыре этапа: обследование — до 18 сентября, настройка системы — до 30 октября, обучение — в октябре, опытная эксплуатация — весь ноябрь; с 1 декабря — промышленная работа.
- По опыту подрядчика, после внедрения поиск паллеты падает до двух-трёх минут, пересорт — ниже 0,5 %, если справочник номенклатуры чистый. {e4}
- Нужны 14 терминалов сбора данных: десять на смену и четыре в запас и на зарядку; срок поставки — шесть недель. {e8}
- Обмен остатками и заказами с «Гроссбухом» делает подрядчик, доступ к тестовой базе — со стороны заказчика. {e18}

**Бюджет — Белоусов** {e2}
- Утверждено 6,8 млн рублей: лицензии и работы подрядчика — 4,9 млн, оборудование — 1,9 млн.

**Склад сейчас — Кузнецов** {e3}
- 4 200 паллетомест в шести зонах, адресов нет, товар ищут по памяти и по тетради.
- Средний поиск паллеты — 12 минут, в сезон до получаса; пересорт по последней инвентаризации — 3,2 %.
- Двое сотрудников промеряют около ста позиций в день: на 3 300 позиций — полтора месяца. {e6}

**Справочник номенклатуры — Сафонова** {e5}
- В «Гроссбухе» 11 000 позиций, у 30 % нет весогабаритных характеристик — около 3 300 позиций система не разместит автоматически.

**Сеть — Мельников** {e10}
- На складе восемь точек доступа; в дальних рядах пятой зоны связи нет.

**Логистика — Валиев** {e14}
- Три типа машин и два окна доставки, есть исключения по перевозчикам; системе это нужно для планирования ворот.

## 4. Решения

| Решение | В записи | Кто предложил | Кто согласился |
|---|---|---|---|
| Промер весогабаритов начинается сейчас, не дожидаясь конца обследования | {e7} | Белоусов | Кузнецов |
| Статус проекта — раз в три недели по четвергам; следующая встреча 17 сентября | {e21} | Белоусов | — |

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Кузнецов Дмитрий Олегович** | | | |
| Промерить весогабариты у 3 300 позиций без характеристик | до 15 октября | {e7} | срок под вопросом |
| Назвать двух кладовщиков в рабочую группу проекта | до понедельника | {e22} | — |
| **Мельников Артём Вадимович** | | | |
| Обследовать покрытие вай-фай на складе с замерами в каждом ряду | до 11 сентября | {e12} | — |
| Поднять тестовую базу «Гроссбуха» для подрядчика | — | {e19} | — |
| **Белоусов Игорь Николаевич** | | | |
| Заказать 14 терминалов сбора данных | до конца следующей недели | {e9} | ждёт счёт от подрядчика |
| **Сафонова Елена Павловна** | | | |
| Описать текущие процессы приёмки, размещения и отбора | до 18 сентября | {e13} | — |
| **Валиев Ринат Ильдарович** | | | |
| Прислать подрядчику список типов машин и окон доставки | до 5 сентября | {e16} | — |
| **Ткаченко Роман Евгеньевич** | | | |
| Прислать детальный план-график проекта с датами и ответственными | до 3 сентября | {e18} | — |

Прочерк в колонке «Срок» — срок на совещании не назвали.
Следующая встреча — 17 сентября, статус проекта.

## 6. Открытые вопросы
- **Кто сопровождает обмен с «Гроссбухом» после запуска.** Мельников спросил, чья это зона {e19}; Ткаченко ответил, что это отдельный договор на сопровождение и в текущий он не входит {e20}. Решили вернуться ближе к ноябрю {e21}.

## 7. Риски
- Без устойчивого вай-фая терминалы работать не будут — Ткаченко {e11}; в пятой зоне связи уже нет — Мельников {e10}.
- Промер 3 300 позиций может не уложиться в срок 15 октября — Кузнецов {e22}.
- Терминалы поставляются шесть недель: при позднем заказе не успеют к обучению — Белоусов {e9}.
""",
    marks=[
        ("2026-08-28 09:40", "i.belousov", "Промерить весогабариты", "due", "2026-10-15"),
        ("2026-08-31 11:20", "i.belousov", "Назвать двух кладовщиков", "done", ""),
        ("2026-09-03 17:10", "i.belousov", "план-график", "done", "План-график получен 03.09"),
        ("2026-09-04 15:35", "i.belousov", "Заказать 14 терминалов", "done", "Счёт оплачен 04.09, поставка к 16.10"),
        ("2026-09-04 15:37", "i.belousov", "тестовую базу", "done", ""),
        ("2026-09-07 10:15", "i.belousov", "типов машин", "done", "Прислал 07.09, на день позже"),
        ("2026-09-11 18:00", "i.belousov", "Обследовать покрытие", "done", "9 точек из 34 без связи, отчёт в папке проекта"),
        ("2026-09-15 16:20", "i.belousov", "Описать текущие процессы", "done", "Описание на 23 страницах разослано"),
    ],
))

RECORDS.append(dict(
    file="Проекты/Склад/2026-09-17 Статус проекта.mp4", kind="meeting", minutes=39,
    title="Статус проекта адресного хранения 17 сентября", date="2026-09-17",
    why="участники докладывают о ходе работ и получают поручения",
    gist="Итоги обследования: девять точек без связи, отбор по заказам вместо волнового, "
         "отставание по весогабаритам, заказ этикеток и поставка терминалов.",
    topics=["покрытие вай-фай", "волновой отбор", "техническое задание", "весогабариты", "этикетки ячеек",
            "терминалы"],
    voices={
        "S1": ("Белоусов Игорь Николаевич", "Игорь Николаевич", "руководитель проекта, ведёт встречу"),
        "S2": ("Кузнецов Дмитрий Олегович", "Дмитрий", "начальник склада"),
        "S3": ("Сафонова Елена Павловна", "Елена", "аналитик"),
        "S4": ("Мельников Артём Вадимович", "Артём", "ИТ, сеть"),
        "S5": (None, "Тимур", "инженер внедрения со стороны подрядчика"),
    },
    uploaded=("i.belousov", "2026-09-17 15:45"), signed=("i.belousov", "2026-09-18 10:10"),
    talk="""
1 f0: Добрый день, второй статус по проекту. Романа Евгеньевича сегодня нет, от подрядчика на связи Тимур, инженер внедрения. Идём по плану-графику. Артём, вай-фай.
4 f1: Обследование покрытия закончил одиннадцатого. Замеряли в тридцати четырёх точках, в девяти связи нет или она ниже порога. Это вся пятая зона и дальний край шестой. Чтобы закрыть, нужно шесть дополнительных точек доступа и кабель до них.
1: Сколько это стоит и сколько по времени?
4 f2: Двести восемьдесят тысяч вместе с монтажом. По времени — две недели после оплаты счёта. Счёт у меня есть, он сейчас на согласовании в финансах.
1 f3: В бюджете оборудования на это запас есть. Артём, срок — девятое октября, точки должны быть смонтированы и проверены.
4 f21: Если счёт оплатят на этой неделе, успею.
5 f4: От нас подтверждаю: в пятой зоне терминал на тесте терял связь каждые две-три минуты. Пока покрытия нет, опытную эксплуатацию в этой зоне начинать нельзя.
1: Принято. Елена, процессы.
3 f5: Описание готово, двадцать три страницы, отправила всем во вторник. Главное расхождение с подрядчиком — отбор. Они предлагают волновой отбор, когда заказы собираются пачкой, а склад привык собирать по одному заказу.
5 f6: Волновой отбор даёт прирост производительности процентов на двадцать, но требует зоны комплектации. У вас её сейчас нет.
2 f7: И места под неё нет. Чтобы сделать зону комплектации, надо освободить ряд в третьей зоне, а он занят сезонным товаром до декабря.
1 f8: Тогда решение такое: стартуем с отбора по заказам, как привыкли. Волны — это второй этап, после нового года, когда освободится ряд. Тимур, это не ломает настройку?
5 f9: Нет, отбор по заказам проще. Мне тогда нужно утверждённое техническое задание на настройку, чтобы не переделывать.
3 f10: Техническое задание согласую с вами до двадцать пятого сентября, основа есть — моё описание.
1: Хорошо. Дмитрий, весогабариты и ячейки.
2 f11: Весогабариты — промерили девятьсот позиций из трёх тысяч трёхсот. Идём медленнее плана: один из двух человек неделю был на отгрузках. По ячейкам: нужно четыре тысячи двести этикеток с адресами, типография делает за десять рабочих дней.
1 f12: Этикетки заказывай до двадцать второго, чтобы в начале октября начать клеить. А по весогабаритам — что нужно, чтобы уложиться в пятнадцатое октября?
2 f13: Третий человек хотя бы на две недели. Или весы с автоматической передачей данных, сейчас всё записываем руками и потом вбиваем.
4 f14: Весы с передачей данных есть в цехе, одни. Могу узнать, дадут ли на время.
1 f15: Узнай. Тимур, от вас по плану была схема топологии склада в системе.
5 f16: Да, схема зон, рядов и ячеек. Она в работе, пришлю до двадцать четвёртого сентября. Мне для неё не хватает точных размеров ячеек в шестой зоне, там нестандартные стеллажи.
2 f17: Размеры шестой зоны сниму завтра и пришлю.
1 f18: По терминалам: счёт оплачен четвёртого сентября, четырнадцать штук, поставка обещана к шестнадцатому октября. Это впритык к обучению, но пока в графике.
3 f19: Игорь Николаевич, вопрос про обучение. Кого учим первыми — кладовщиков или начальников смен? От этого зависит программа.
1 f20: Не готов ответить, надо обсудить с Романом Евгеньевичем. Вернёмся на следующем статусе. Я к понедельнику доложу директору о ходе проекта. Следующая встреча — восьмого октября.
""",
    protocol="""
# Протокол совещания

**Название:** Статус проекта адресного хранения 17 сентября
**Дата:** 17.09.2026 (из имени файла)
**Длительность:** {dur}
**Вёл:** Белоусов Игорь Николаевич

## 1. Кратко
- Обследование вай-фай: в 9 точках из 34 связи нет — вся пятая зона и край шестой; нужно шесть точек доступа за 280 тысяч рублей.
- Стартуют с отбора по заказам; волновой отбор — второй этап, после нового года.
- Весогабариты промерены у 900 позиций из 3 300, темп ниже плана.
- Этикетки для 4 200 ячеек заказывают до 22 сентября.
- Терминалы оплачены 4 сентября, поставка к 16 октября — впритык к обучению.
- Кого учить первыми, не решено.

## 2. Участники

| Участник | Роль на совещании |
|---|---|
| Белоусов Игорь Николаевич | руководитель проекта, вёл встречу |
| Мельников Артём Вадимович | итоги обследования сети |
| Сафонова Елена Павловна | описание процессов, техническое задание |
| Кузнецов Дмитрий Олегович | весогабариты, ячейки |
| Тимур (S5) | инженер внедрения подрядчика |

Упоминали, но не выступали: Роман Евгеньевич (руководитель проекта от подрядчика), директор.

## 3. Что докладывали

**Покрытие вай-фай — Мельников** {f1}
- Обследование закончено 11 сентября: замеры в 34 точках, в девяти связи нет или она ниже порога — вся пятая зона и дальний край шестой.
- Нужно шесть дополнительных точек доступа и кабель до них: 280 тысяч рублей с монтажом, две недели после оплаты; счёт на согласовании в финансах. {f2}
- На тесте подрядчика терминал в пятой зоне терял связь каждые две-три минуты — подтвердил Тимур (S5). {f4}

**Процессы и отбор — Сафонова** {f5}
- Описание текущих процессов готово, 23 страницы, разослано во вторник.
- Главное расхождение с подрядчиком: он предлагает волновой отбор, склад привык собирать по одному заказу.
- Волновой отбор даёт около 20 % прироста производительности, но требует зоны комплектации — пояснил Тимур (S5). {f6}

**Весогабариты и ячейки — Кузнецов** {f11}
- Промерено 900 позиций из 3 300; один из двух сотрудников неделю был на отгрузках.
- Для ячеек нужно 4 200 этикеток с адресами, типография делает за десять рабочих дней.
- Места под зону комплектации нет: ряд в третьей зоне занят сезонным товаром до декабря. {f7}

**Терминалы — Белоусов** {f18}
- Счёт оплачен 4 сентября, 14 штук, поставка обещана к 16 октября.

## 4. Решения

| Решение | В записи | Кто предложил | Кто согласился |
|---|---|---|---|
| Стартовать с отбора по заказам; волновой отбор — второй этап, после нового года, когда освободится ряд в третьей зоне | {f8} | Белоусов | Тимур (S5), Кузнецов |
| Опытную эксплуатацию в пятой зоне не начинать, пока нет покрытия | {f4} | Тимур (S5) | Белоусов |

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Кузнецов Дмитрий Олегович** | | | |
| Заказать 4 200 этикеток с адресами ячеек | до 22 сентября | {f12} | — |
| Снять размеры ячеек в шестой зоне и прислать подрядчику | завтра | {f17} | — |
| **Мельников Артём Вадимович** | | | |
| Смонтировать и проверить шесть дополнительных точек доступа в пятой и шестой зонах | до 9 октября | {f3} | счёт на согласовании в финансах |
| Узнать, дадут ли из цеха весы с передачей данных на время промера | — | {f14} | — |
| **Сафонова Елена Павловна** | | | |
| Согласовать с подрядчиком техническое задание на настройку | до 25 сентября | {f10} | основа есть |
| **Белоусов Игорь Николаевич** | | | |
| Доложить директору о ходе проекта | к понедельнику | {f20} | — |
| **S5 (Тимур, инженер подрядчика)** | | | |
| Прислать схему топологии склада: зоны, ряды, ячейки | до 24 сентября | {f16} | в работе, ждёт размеры шестой зоны |

Прочерк в колонке «Срок» — срок на совещании не назвали.
Следующая встреча — 8 октября.

## 6. Открытые вопросы
- **Кого учить первыми.** Сафонова спросила, с кого начинать обучение — с кладовщиков или с начальников смен: от этого зависит программа {f19}. Белоусов не готов ответить без руководителя проекта подрядчика; вернутся на следующем статусе {f20}.
- **Как уложиться с весогабаритами в 15 октября.** Кузнецов просит третьего человека на две недели или весы с передачей данных {f13}; ни то, ни другое не выделено.

## 7. Риски
- Точки доступа успеют к 9 октября, только если счёт оплатят на этой неделе — Мельников {f21}.
- Терминалы приходят 16 октября, впритык к обучению — Белоусов {f18}.

## Комментарии
- **Кто говорит** — S5 в разговоре называют Тимуром, фамилия не звучала; человек не подписан.
""",
    marks=[
        ("2026-09-18 10:40", "i.belousov", "шесть дополнительных точек", "due", "2026-10-09"),
        ("2026-09-18 16:30", "i.belousov", "Снять размеры ячеек", "done", ""),
        ("2026-09-21 12:05", "i.belousov", "Доложить директору", "done", "Доклад принят без замечаний"),
        ("2026-09-21 17:45", "i.belousov", "Заказать 4 200 этикеток", "done", "Заказ размещён 21.09, тираж будет 05.10"),
        ("2026-09-23 11:30", "i.belousov", "весы с передачей данных", "done", "Не дали: весы заняты в цехе до конца октября"),
        ("2026-09-25 18:10", "i.belousov", "техническое задание", "done", "ТЗ подписано обеими сторонами 25.09"),
        ("2026-09-29 17:20", "i.belousov", "схему топологии", "done", "Пришла 29.09, на пять дней позже срока"),
    ],
))

RECORDS.append(dict(
    file="Проекты/Склад/2026-10-08 Статус проекта.mp4", kind="meeting", minutes=44,
    title="Статус проекта адресного хранения 8 октября", date="2026-10-08",
    why="участники докладывают о ходе работ, спорят о сроке и получают поручения",
    gist="Настройка выполнена на 60 %, ошибка в обмене заказами, точки доступа сорваны на неделю, "
         "весогабариты отстают; дату запуска 1 декабря не двигают, контрольная точка — 30 октября.",
    topics=["настройка системы", "обмен заказами", "точки доступа", "весогабариты", "обучение", "дата запуска"],
    voices={
        "S1": ("Белоусов Игорь Николаевич", "Игорь Николаевич", "руководитель проекта, ведёт встречу"),
        "S2": ("Кузнецов Дмитрий Олегович", "Дмитрий", "начальник склада"),
        "S3": ("Сафонова Елена Павловна", "", "аналитик, обучение"),
        "S4": ("Мельников Артём Вадимович", "Артём", "ИТ, сеть и обмен"),
        "S5": ("Ткаченко Роман Евгеньевич", "Роман Евгеньевич", "руководитель проекта со стороны подрядчика"),
    },
    uploaded=("i.belousov", "2026-10-08 15:50"), signed=("i.belousov", "2026-10-09 09:20"),
    talk="""
1 g0: Добрый день. Третий статус, до запуска меньше двух месяцев. Роман Евгеньевич, начните с настройки.
5 g1: Настройка системы выполнена примерно на шестьдесят процентов. Топология склада заведена, приёмка и размещение настроены и проверены на тестовых данных. Отбор по заказам — в работе, закончим к двадцать третьему октября. Схему топологии Тимур прислал с опозданием, двадцать девятого, но сейчас она в системе.
1: Что отстаёт?
5 g2: Отстаёт интеграция. Обмен остатками с «Гроссбухом» работает, а обмен заказами — с ошибкой: «Гроссбух» отдаёт количество в коробках, а «Стеллаж-7» ждёт штуки. На тесте заказ на десять коробок превратился в десять штук.
4 g3: Это на нашей стороне, в выгрузке не учтён коэффициент упаковки. Исправлю до шестнадцатого октября, там ещё надо проверить позиции, у которых коэффициент не заполнен.
1 g4: Сколько таких позиций?
4 g5: Около четырёхсот. Их придётся заполнять вручную, это вопрос к складу.
2 g6: Четыреста позиций заполним вместе с весогабаритами, это те же люди. По весогабаритам — две тысячи сто из трёх тысяч трёхсот. К пятнадцатому не успеваю, реально — к двадцать третьему.
1 g7: Двадцать третье — это уже на краю. Дмитрий, пятнадцатое остаётся целью, двадцать третье — крайний срок, и после него мы начинаем опытную эксплуатацию с неполным справочником. Артём, точки доступа.
4 g8: Плохие новости. Счёт оплатили только шестого октября, финансы держали его три недели. Поставка — четырнадцатого, монтаж — пятнадцатого и шестнадцатого. То есть срок девятое октября сорван на неделю.
1 g9: Причину я понял, но вывод один: шестнадцатое — последний день, потому что в этот же день приезжают терминалы. Монтаж пятнадцатого и шестнадцатого, без переносов.
4 g21: Монтажников я уже забронировал на эти дни.
2 g10: По ячейкам. Этикетки получили пятого, наклеено полторы тысячи из четырёх тысяч двухсот. Клеим в две смены, закончим к девятнадцатому октября.
3 g11: По обучению. Договорились с Романом Евгеньевичем: сначала начальники смен, потом кладовщики. Три группы, двадцатого, двадцать первого и двадцать второго октября, по четыре часа. Расписание по людям составлю до понедельника.
5 g12: От нас на обучение приедут два тренера. И к двадцатому октября пришлю тестовые сценарии для опытной эксплуатации, тридцать-сорок сценариев по всем операциям.
1 g13: Хорошо. Теперь главный вопрос: дата запуска. У нас сорваны точки доступа, отстают весогабариты и интеграция. Двигаем первое декабря или нет?
5 g14: Моё мнение: пока не двигать. Запас в графике съеден, но критический путь цел, если терминалы придут шестнадцатого. Если терминалы задержатся хотя бы на неделю, первое декабря не удержать.
2 g15: Я бы подстраховался и сдвинул на пятнадцатое декабря. В декабре у нас пик отгрузок, запускаться в пик на сырой системе опасно.
1 g16: Аргумент понимаю, но пятнадцатое декабря — это ещё глубже в пик. Решение: дату первое декабря не двигаем. Ставим контрольную точку тридцатого октября: если к ней нет терминалов, покрытия или справочника, переносим запуск на январь, а не на середину декабря.
3 g17: Тогда к тридцатому нужен список критериев, по которым мы решаем, чтобы не спорить на месте.
1 g18: Согласен, критерии подготовлю сам и разошлю заранее. И остался вопрос сопровождения обмена после запуска: Роман Евгеньевич, коммерческое предложение на сопровождение от вас так и не пришло.
5 g19: Пришлю, но не раньше ноября: сначала надо понять объём по итогам опытной эксплуатации.
1 g20: Значит, вопрос остаётся открытым. Следующий статус — двадцать девятого октября, накануне контрольной точки. Спасибо.
""",
    protocol="""
# Протокол совещания

**Название:** Статус проекта адресного хранения 8 октября
**Дата:** 08.10.2026 (из имени файла)
**Длительность:** {dur}
**Вёл:** Белоусов Игорь Николаевич

## 1. Кратко
- Настройка «Стеллаж-7» выполнена примерно на 60 %; отбор по заказам закончат к 23 октября.
- В обмене заказами ошибка: коробки приходят как штуки; исправление — до 16 октября, у 400 позиций не заполнен коэффициент упаковки.
- Точки доступа сорваны на неделю: счёт оплатили только 6 октября, монтаж 15–16 октября.
- Весогабариты: 2 100 из 3 300; цель — 15 октября, крайний срок — 23 октября.
- Обучение — 20, 21 и 22 октября, три группы: сначала начальники смен, потом кладовщики.
- Дату запуска 1 декабря не двигают; контрольная точка — 30 октября, при провале запуск переносится на январь.

## 2. Участники

| Участник | Роль на совещании |
|---|---|
| Белоусов Игорь Николаевич | руководитель проекта, вёл встречу |
| Ткаченко Роман Евгеньевич | руководитель проекта от подрядчика |
| Мельников Артём Вадимович | обмен с «Гроссбухом», точки доступа |
| Кузнецов Дмитрий Олегович | весогабариты, этикетки |
| Сафонова Елена Павловна | обучение |

Упоминали, но не выступали: Тимур (инженер подрядчика), финансовая служба.

## 3. Что докладывали

**Настройка системы — Ткаченко** {g1}
- Выполнено около 60 %: топология заведена, приёмка и размещение настроены и проверены на тестовых данных.
- Отбор по заказам в работе, срок — 23 октября.
- Схема топологии пришла с опозданием, 29 сентября, сейчас она в системе.
- На обучение приедут два тренера. {g12}

**Интеграция — Ткаченко, Мельников** {g2}
- Обмен остатками работает; в обмене заказами «Гроссбух» отдаёт коробки, а «Стеллаж-7» ждёт штуки: заказ на десять коробок стал десятью штуками.
- Причина на стороне заказчика: в выгрузке не учтён коэффициент упаковки. {g3}
- Коэффициент не заполнен примерно у 400 позиций, заполнять придётся вручную. {g5}

**Весогабариты и ячейки — Кузнецов** {g6}
- Промерено 2 100 позиций из 3 300; к 15 октября не успевает, реальный срок — 23 октября.
- Этикетки получены 5 октября, наклеено 1 500 из 4 200, клеят в две смены. {g10}

**Точки доступа — Мельников** {g8}
- Счёт оплатили 6 октября: финансы держали его три недели. Поставка — 14 октября, монтаж — 15 и 16 октября.

**Обучение — Сафонова** {g11}
- Сначала учат начальников смен, потом кладовщиков; три группы — 20, 21 и 22 октября, по четыре часа.

## 4. Решения

| Решение | В записи | Кто предложил | Кто согласился |
|---|---|---|---|
| Дату запуска 1 декабря не двигать | {g16} | Ткаченко | Белоусов |
| Контрольная точка — 30 октября: если нет терминалов, покрытия или справочника, запуск переносится на январь, а не на середину декабря | {g16} | Белоусов | — |
| Первыми учат начальников смен, затем кладовщиков | {g11} | Сафонова, Ткаченко | Белоусов |
| После 23 октября опытная эксплуатация начинается и с неполным справочником | {g7} | Белоусов | — |

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Кузнецов Дмитрий Олегович** | | | |
| Закончить промер весогабаритов | до 15 октября, крайний срок — 23 октября | {g7} | 2 100 из 3 300 |
| Заполнить коэффициент упаковки у 400 позиций | — | {g6} | — |
| Доклеить этикетки на ячейки | до 19 октября | {g10} | 1 500 из 4 200 |
| **Мельников Артём Вадимович** | | | |
| Исправить в выгрузке заказов из «Гроссбуха» пересчёт коробок в штуки | до 16 октября | {g3} | — |
| Смонтировать шесть точек доступа в пятой и шестой зонах | 15–16 октября | {g9} | монтажники забронированы |
| **Сафонова Елена Павловна** | | | |
| Составить расписание обучения трёх групп по людям | до понедельника | {g11} | — |
| **Ткаченко Роман Евгеньевич** | | | |
| Прислать тестовые сценарии опытной эксплуатации | к 20 октября | {g12} | — |
| **Белоусов Игорь Николаевич** | | | |
| Подготовить и разослать критерии для контрольной точки 30 октября | — | {g18} | — |

Прочерк в колонке «Срок» — срок на совещании не назвали.
Следующая встреча — 29 октября, накануне контрольной точки.

## 6. Открытые вопросы
- **Сдвигать ли запуск.** Кузнецов предлагал перенести запуск на 15 декабря: в декабре пик отгрузок, запускаться на сырой системе опасно {g15}. Белоусов оставил 1 декабря до контрольной точки {g16}; возражение склада не снято.
- **Сопровождение обмена после запуска.** Коммерческое предложение подрядчика не пришло {g18}; Ткаченко пришлёт его не раньше ноября, по итогам опытной эксплуатации {g19}.

## 7. Риски
- Если терминалы задержатся хотя бы на неделю, 1 декабря не удержать — Ткаченко {g14}.
- Запас в графике съеден: любой следующий срыв двигает запуск — Ткаченко {g14}.
- Запуск попадает на декабрьский пик отгрузок — Кузнецов {g15}.
""",
    marks=[
        ("2026-10-09 10:00", "i.belousov", "Смонтировать шесть точек", "due", "2026-10-16"),
        ("2026-10-09 10:02", "i.belousov", "Доклеить этикетки", "due", "2026-10-19"),
        ("2026-10-09 16:30", "i.belousov", "расписание обучения", "done", "Расписание разослано 09.10"),
    ],
))

RECORDS.append(dict(
    file="ИТ/2026-09-03 Планёрка ИТ-отдела.m4a", kind="meeting", minutes=27,
    title="Планёрка ИТ-отдела 3 сентября", date="2026-09-03",
    why="участники докладывают о состоянии дел и получают поручения",
    gist="Рост просроченных заявок за август, принтеры этикеток на складе, тест восстановления из резервной "
         "копии дольше регламента, сертификат почты и старые коммутаторы.",
    topics=["заявки за август", "принтеры этикеток", "резервные копии", "сертификат почты", "коммутаторы",
            "антивирус"],
    voices={
        "S1": ("Мельников Артём Вадимович", "", "начальник ИТ-отдела, ведёт планёрку"),
        "S2": ("Крылов Степан Юрьевич", "Степан", "системный администратор"),
        "S3": ("Назарова Алина Тимуровна", "Алина", "первая линия поддержки"),
    },
    uploaded=("a.melnikov", "2026-09-03 11:40"), signed=("a.melnikov", "2026-09-03 14:05"),
    talk="""
1 h0: Коллеги, планёрка, третье сентября. Три темы: заявки за август, резервные копии и хвосты по железу. Алина, заявки.
3 h1: За август через «Заявочник» пришло четыреста двенадцать заявок. Закрыто в срок триста пятьдесят одна, просрочена шестьдесят одна, это почти пятнадцать процентов. В июле было девять.
1: Почему выросло?
3 h2: Две причины. Первая — отпуска, я две недели была одна на первой линии. Вторая — склад: оттуда тридцать восемь заявок по принтерам этикеток, и почти все одинаковые — закончилась лента или её неправильно вставили.
2 h3: По принтерам этикеток подтверждаю, я на склад ходил раз шесть. Там не поломки, там никто не умеет менять ленту, кроме одного кладовщика, а он работает через смену.
1 h4: Значит, нужна инструкция с картинками, которую можно повесить рядом с принтером. Алина, сделаешь?
3 h5: Сделаю, но сначала разберу просроченные. Шестьдесят одну заявку закрою или переназначу до десятого сентября.
1 h6: Хорошо, просроченные — до десятого, инструкция — после, срок не ставлю. Степан, резервные копии.
2 h7: В прошлую пятницу провёл плановый тест восстановления «Гроссбуха» из копии. Восстановился, данные целы, но заняло пять часов сорок минут. По нашему же регламенту цель — четыре часа.
1: В чём узкое место?
2 h8: В хранилище. Копии лежат на медленном массиве, чтение идёт со скоростью около шестидесяти мегабайт в секунду. Если перенести свежие копии на быстрый массив, там есть полтора терабайта свободных, должны уложиться часа в три.
1 h9: Переноси и повтори тест. Срок — восемнадцатое сентября, результат покажи цифрой.
2 h10: Сделаю. И ещё по срокам: сертификат почтового сервера истекает второго октября. Продлю до двадцать пятого сентября, чтобы не в последний день.
1 h11: Да, в прошлом году продлевали в последний день, и почта полдня ругалась. Теперь железо. Два коммутатора в серверной работают с две тысячи пятнадцатого года, на одном уже сыпятся ошибки на портах.
2 h12: На втором тоже начались, с августа. Я порты переткнул, но это временно. Менять надо оба, это примерно четыреста двадцать тысяч.
1 h13: Бюджет на замену буду согласовывать сам, в этом квартале денег на это не заложено. Когда получится — не знаю.
3 h14: А если один из них умрёт до согласования?
2 h15: Тогда половина второго этажа останется без сети, пока я не перекину на резервный. Резервный есть, но он на двадцать четыре порта, а нужно сорок восемь.
1 h16: Риск понятен, буду давить на финансы. Ещё вопрос без ответа: антивирус. Лицензия до декабря, и продлевать ли её или менять продукт, мы так и не решили.
2 h17: Я бы менял, нынешний тормозит старые машины в бухгалтерии. Но сравнения у меня нет.
1 h18: Вот когда будет сравнение, тогда и поговорим. Пока оставляем как есть. Всё, спасибо.
""",
    protocol="""
# Протокол совещания

**Название:** Планёрка ИТ-отдела 3 сентября
**Дата:** 03.09.2026 (из имени файла)
**Длительность:** {dur}
**Вёл:** Мельников Артём Вадимович

## 1. Кратко
- За август 412 заявок, 61 просрочена (почти 15 %, в июле было 9 %): отпуска и 38 однотипных заявок со склада по принтерам этикеток.
- Тест восстановления «Гроссбуха» из копии занял 5 часов 40 минут при цели 4 часа; копии переносят на быстрый массив.
- Сертификат почтового сервера истекает 2 октября, продлят до 25 сентября.
- Два коммутатора в серверной 2015 года дают ошибки на портах; замена — около 420 тысяч рублей, денег в квартале нет.
- Продлевать антивирус или менять продукт — не решено.

## 2. Участники

| Участник | Роль на совещании |
|---|---|
| Мельников Артём Вадимович | вёл планёрку, давал поручения |
| Крылов Степан Юрьевич | резервные копии, сеть, сертификат |
| Назарова Алина Тимуровна | заявки и первая линия |

## 3. Что докладывали

**Заявки за август — Назарова** {h1}
- Через «Заявочник» пришло 412 заявок; в срок закрыта 351, просрочена 61 — почти 15 %, в июле было 9 %.
- Причины: две недели одна на первой линии из-за отпусков и 38 заявок со склада по принтерам этикеток — закончилась лента или её неправильно вставили. {h2}
- По словам Крылова, на складе ленту умеет менять один кладовщик, и он работает через смену. {h3}

**Резервные копии — Крылов** {h7}
- Плановый тест восстановления «Гроссбуха» прошёл: данные целы, но заняло 5 часов 40 минут при цели по регламенту 4 часа.
- Узкое место — хранилище: копии лежат на медленном массиве, чтение около 60 мегабайт в секунду. {h8}
- На быстром массиве свободно 1,5 терабайта; с него восстановление должно занять около трёх часов.

**Сертификат почты — Крылов** {h10}
- Сертификат почтового сервера истекает 2 октября.

**Коммутаторы — Мельников, Крылов** {h11}
- Два коммутатора в серверной работают с 2015 года; ошибки на портах есть на обоих, на втором — с августа. {h12}
- Замена обоих — примерно 420 тысяч рублей.
- Резервный коммутатор есть, но на 24 порта при нужных 48. {h15}

## 4. Решения

| Решение | В записи | Кто предложил | Кто согласился |
|---|---|---|---|
| Сначала разобрать просроченные заявки, инструкция для склада — после | {h6} | Назарова | Мельников |
| Антивирус пока оставить как есть: к вопросу вернуться, когда будет сравнение продуктов | {h18} | Мельников | — |

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Крылов Степан Юрьевич** | | | |
| Перенести свежие копии «Гроссбуха» на быстрый массив и повторить тест восстановления | до 18 сентября | {h9} | — |
| Продлить сертификат почтового сервера | до 25 сентября | {h10} | истекает 2 октября |
| **Назарова Алина Тимуровна** | | | |
| Закрыть или переназначить 61 просроченную заявку | до 10 сентября | {h5} | — |
| Сделать инструкцию с картинками по замене ленты в принтере этикеток | — | {h6} | после разбора просроченных заявок |
| **Мельников Артём Вадимович** | | | |
| Согласовать бюджет на замену двух коммутаторов в серверной | — | {h13} | в этом квартале денег нет |

Прочерк в колонке «Срок» — срок на совещании не назвали.

## 6. Открытые вопросы
- **Антивирус.** Лицензия действует до декабря; продлевать её или менять продукт, не решили {h16}. Крылов за замену: нынешний тормозит старые машины в бухгалтерии, но сравнения продуктов нет {h17}.

## 7. Риски
- Если один из коммутаторов откажет до замены, половина второго этажа останется без сети: резервный вдвое меньше — Крылов {h15}.
- При аварии «Гроссбух» сейчас восстанавливается почти шесть часов вместо четырёх — Крылов {h7}.
""",
    marks=[
        ("2026-09-10 17:30", "a.melnikov", "просроченную заявку", "done", "Закрыто 54, переназначено 7"),
        ("2026-09-17 19:05", "a.melnikov", "повторить тест восстановления", "done", "Повторный тест 17.09: 3 часа 10 минут"),
        ("2026-09-24 12:15", "a.melnikov", "Продлить сертификат", "done", "Продлён до 02.10.2027"),
    ],
))

RECORDS.append(dict(
    file="ИТ/2026-10-01 Разбор сбоя 29 сентября.m4a", kind="meeting", minutes=33,
    title="Разбор сбоя учётной системы 29 сентября", date="2026-10-01",
    why="участники разбирают происшествие и договариваются, что изменить",
    gist="Учётная система была недоступна 2 часа 35 минут из-за переполнения диска журналов; оповещения "
         "мониторинга никто не прочитал. Вводятся дежурства и новый порядок оповещений.",
    topics=["хронология сбоя", "журналы базы данных", "оповещения мониторинга", "дежурства", "работа склада без системы",
            "учения"],
    voices={
        "S1": ("Мельников Артём Вадимович", "", "начальник ИТ-отдела, ведёт разбор"),
        "S2": ("Крылов Степан Юрьевич", "Степан", "системный администратор"),
        "S3": ("Назарова Алина Тимуровна", "Алина", "первая линия поддержки"),
        "S4": ("Кузнецов Дмитрий Олегович", "Дмитрий Олегович", "начальник склада, пострадавшая сторона"),
    },
    uploaded=("a.melnikov", "2026-10-01 12:20"), signed=("a.melnikov", "2026-10-01 15:10"),
    talk="""
1 k0: Коллеги, собрались по одному поводу: сбой двадцать девятого сентября. Дмитрия Олеговича я позвал как пострадавшую сторону. Цель — не найти виноватого, а понять, что меняем. Степан, хронология.
2 k1: В понедельник, двадцать девятого, в девять двенадцать «Гроссбух» перестал принимать документы. В одиннадцать сорок семь работу восстановили. Итого два часа тридцать пять минут простоя. Причина — закончилось место на диске, где лежат журналы базы данных.
1: Почему оно закончилось именно сейчас?
2 k2: В пятницу вечером бухгалтерия запустила закрытие квартала, это тяжёлая операция, журналы за выходные выросли с сорока до ста восьмидесяти гигабайт. Диск на двести. Автоочистка журналов была настроена раз в неделю, по средам.
1 k3: А мониторинг? «Филин» же должен был предупредить.
2 k4: «Филин» предупредил. Первое оповещение — в субботу, двадцать шестого, в четырнадцать ноль-ноль, когда диск заполнился на восемьдесят пять процентов. Всего их было семь. Но все оповещения уходят в общий почтовый ящик отдела, а его в выходные никто не читает.
3 k5: Я в понедельник в восемь тридцать открыла ящик, там было сто сорок писем от «Филина» за выходные, включая обычные. Критические в этой куче я не выделила, честно говоря.
1 k6: Это не твоя вина, это устройство оповещений. Дмитрий Олегович, что было на складе?
4 k7: Склад с девяти двенадцати отгружал по бумажным накладным. За это время ушло семнадцать отгрузок. Потом два человека до вечера вбивали их в систему задним числом. Одну машину задержали на сорок минут, водитель без распечатанных документов уезжать отказался.
1: Ошибки при вводе задним числом были?
4 k8: Не знаю, и это меня беспокоит. Вбивали в спешке. Я хочу сверить все семнадцать отгрузок с бумажными накладными, сделаю до восьмого октября.
1 k9: Хорошо, сверяй. Теперь что меняем. Первое: очистка журналов.
2 k10: Очистку я уже переделал, сегодня утром: теперь ежедневно, и порог оповещения снизил до восьмидесяти процентов. Это сделано.
1 k11: Второе: оповещения. Критические не должны лежать в общем ящике. Предлагаю так: критический уровень уходит в мессенджер дежурному, и у нас появляется график дежурств, включая выходные.
3 k12: Дежурить в выходные втроём — это каждые третьи выходные. За это будут доплачивать?
1 k13: Вопрос справедливый, ответа пока нет: я должен обсудить доплату с кадрами. Но дежурства вводим в любом случае, с ближайших выходных. Алина, график на октябрь составь до пятницы.
3 k21: Составлю.
2 k14: Перенаправление критических оповещений в мессенджер настрою до пятого октября, там нужно завести бота и проверить на тестовом событии.
1 k15: Третье: у склада нет порядка действий на случай, когда система лежит. Работали по наитию.
4 k16: Да, бумажные накладные нашли только потому, что у меня в сейфе лежала старая пачка. Нужен регламент: где бланки, кто решает перейти на бумагу, как потом вводить.
3 k17: Регламент напишу я вместе с Дмитрием Олеговичем, срок — пятнадцатое октября.
1 k18: Договорились. С меня отчёт о сбое для директора, до шестого октября. И последнее: я хочу раз в квартал проводить учения — выключаем тестовый сервер и смотрим, как быстро восстановимся. Дату первых назначу позже.
2 k19: Учения — правильно. Только не в конец квартала, пожалуйста.
1 k20: Учту. Всем спасибо.
""",
    protocol="""
# Протокол совещания

**Название:** Разбор сбоя учётной системы 29 сентября
**Дата:** 01.10.2026 (из имени файла)
**Длительность:** {dur}
**Вёл:** Мельников Артём Вадимович

## 1. Кратко
- 29 сентября «Гроссбух» был недоступен с 9:12 до 11:47 — 2 часа 35 минут: закончилось место на диске журналов базы данных.
- Мониторинг «Филин» прислал семь оповещений начиная с субботы, но они ушли в общий ящик, который в выходные никто не читает.
- Склад отгрузил 17 машин по бумажным накладным и вводил их задним числом; правильность ввода не проверена.
- Очистка журналов уже переведена на ежедневную, порог оповещения снижен до 80 %.
- Вводятся дежурства, включая выходные; критические оповещения пойдут дежурному в мессенджер.
- Будет доплата за дежурства или нет — не решено.

## 2. Участники

| Участник | Роль на совещании |
|---|---|
| Мельников Артём Вадимович | вёл разбор, принимал решения |
| Крылов Степан Юрьевич | хронология и причина сбоя |
| Назарова Алина Тимуровна | первая линия, график дежурств |
| Кузнецов Дмитрий Олегович | последствия для склада |

Упоминали, но не выступали: бухгалтерия, директор, кадры.

## 3. Что докладывали

**Хронология и причина — Крылов** {k1}
- 29 сентября в 9:12 «Гроссбух» перестал принимать документы, в 11:47 работу восстановили; простой — 2 часа 35 минут.
- Причина: закончилось место на диске журналов базы данных.
- В пятницу вечером бухгалтерия запустила закрытие квартала; за выходные журналы выросли с 40 до 180 гигабайт при диске на 200. Автоочистка работала раз в неделю, по средам. {k2}

**Оповещения — Крылов, Назарова** {k4}
- «Филин» прислал первое оповещение в субботу, 26 сентября, в 14:00, при заполнении диска на 85 %; всего оповещений было семь.
- Все оповещения уходят в общий почтовый ящик отдела, в выходные его никто не читает.
- В понедельник в 8:30 в ящике было 140 писем от «Филина» за выходные; критические среди них не выделены. {k5}

**Склад — Кузнецов** {k7}
- С 9:12 склад отгружал по бумажным накладным: 17 отгрузок, потом два человека до вечера вводили их в систему задним числом.
- Одну машину задержали на 40 минут: водитель отказался уезжать без распечатанных документов.
- Бумажные бланки нашлись случайно — старая пачка в сейфе начальника склада. {k16}

**Что уже сделано — Крылов** {k10}
- Очистка журналов переведена на ежедневную, порог оповещения снижен до 80 %.

## 4. Решения

| Решение | В записи | Кто предложил | Кто согласился |
|---|---|---|---|
| Критические оповещения мониторинга уходят дежурному в мессенджер, а не в общий ящик | {k11} | Мельников | Крылов |
| Вводятся дежурства по графику, включая выходные, — с ближайших выходных | {k13} | Мельников | — |
| Раз в квартал проводятся учения по восстановлению; не в конце квартала | {k18} | Мельников | Крылов |

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Мельников Артём Вадимович** | | | |
| Подготовить отчёт о сбое для директора | до 6 октября | {k18} | — |
| Обсудить с кадрами доплату за дежурства в выходные | — | {k13} | — |
| Назначить дату первых учений по восстановлению | — | {k18} | — |
| **Назарова Алина Тимуровна** | | | |
| Составить график дежурств на октябрь, включая выходные | до пятницы | {k13} | — |
| Написать вместе с Кузнецовым регламент работы склада при недоступности системы | до 15 октября | {k17} | — |
| **Крылов Степан Юрьевич** | | | |
| Настроить отправку критических оповещений «Филина» дежурному в мессенджер | до 5 октября | {k14} | — |
| **Кузнецов Дмитрий Олегович** | | | |
| Сверить 17 отгрузок, введённых задним числом, с бумажными накладными | до 8 октября | {k8} | — |

Прочерк в колонке «Срок» — срок на совещании не назвали.

## 6. Открытые вопросы
- **Доплата за дежурства.** Назарова спросила, будут ли доплачивать за дежурства в выходные: втроём это каждые третьи выходные {k12}. Ответа нет, Мельников обсудит с кадрами {k13}.

## 7. Риски
- Отгрузки вводили задним числом в спешке; были ли ошибки, неизвестно — Кузнецов {k8}.
- У склада нет порядка действий на случай недоступности системы: в следующий раз бланков может не оказаться — Кузнецов {k16}.
""",
    marks=[
        ("2026-10-01 15:40", "a.melnikov", "отчёт о сбое", "due", "2026-10-06"),
        ("2026-10-01 15:41", "a.melnikov", "критических оповещений", "due", "2026-10-05"),
        ("2026-10-01 15:42", "a.melnikov", "Сверить 17 отгрузок", "due", "2026-10-08"),
        ("2026-10-02 16:50", "a.melnikov", "график дежурств", "done", "График на октябрь разослан"),
        ("2026-10-06 11:25", "a.melnikov", "критических оповещений", "done", "Бот работает с 06.10, проверен на тестовом событии"),
    ],
))

RECORDS.append(dict(
    file="Проекты/Склад/Обучение/2026-09-24 Лекция. Адресное хранение для кладовщиков.mp4", kind="lecture", minutes=52,
    title="Адресное хранение: что это и какие ошибки делают в первые месяцы", date="2026-09-24",
    why="один человек рассказывает, остальные задают вопросы",
    gist="Приглашённый консультант объясняет, как устроено адресное хранение, чем статическое отличается от "
         "динамического, какие операции выполняет кладовщик и на чём спотыкаются после запуска.",
    topics=["адресное хранение", "статическое и динамическое", "двойное сканирование", "этикетки ячеек",
            "запуск по зонам"],
    voices={
        "S1": ("Зотов Виктор Леонидович", "Виктор Леонидович", "лектор, консультант по складской логистике"),
        "S2": ("Кузнецов Дмитрий Олегович", "", "начальник склада, задаёт вопросы"),
        "S3": (None, "", "слушатель, кладовщик"),
    },
    uploaded=("e.safonova", "2026-09-24 17:10"), signed=("e.safonova", "2026-09-25 11:00"),
    talk="""
1 l0: Добрый день, коллеги. Меня зовут Виктор Леонидович Зотов, я консультант по складской логистике, двадцать лет занимаюсь складами, из них двенадцать — внедрением адресного хранения. Сегодня расскажу, что это такое, зачем оно вам и какие ошибки делают в первые месяцы. Вопросы можно задавать по ходу.
2 l1: Виктор Леонидович, добавлю для наших: у нас запуск первого декабря, так что слушаем внимательно, это про нас.
1 l2: Начну с определения. Адресное хранение — это когда у каждого места на складе есть адрес, как у квартиры: зона, ряд, секция, ярус, ячейка. И система в каждый момент знает, что лежит в каждой ячейке. Не кладовщик помнит, а система знает. Отсюда главное следствие: любой человек, даже новичок на второй день, находит товар за две-три минуты.
3 l3: А сейчас у нас новичок месяц ходит за старшим, пока запомнит.
1 l4: Именно. По моей практике, на складе без адресов новый кладовщик выходит на норму через шесть-восемь недель, на адресном — через пять-семь дней. Второе следствие — пересорт. Когда товар кладут куда свободно и ищут по памяти, пересорт три-четыре процента — обычное дело. На адресном складе нормальный показатель — три десятых процента и ниже.
2 l5: У нас сейчас три и две десятых.
1 l6: Вот, типичная цифра. Теперь о двух видах адресного хранения. Статическое — за каждым товаром закреплена своя ячейка, навсегда. Просто, понятно, но склад заполнен в среднем на шестьдесят-шестьдесят пять процентов, потому что ячейка ждёт свой товар, даже когда его нет. Динамическое — товар кладут в любую свободную подходящую ячейку, а система запоминает куда. Заполнение доходит до восьмидесяти пяти процентов, но без терминала вы не найдёте ничего.
3 l7: То есть если терминал сел или связь пропала, мы встали?
1 l8: Хороший вопрос, и ответ неприятный: да, при динамическом хранении без системы склад слепой. Поэтому я всегда советую смешанную схему. Ходовой товар, который отбирают каждый день, — в статических ячейках в нижних ярусах, поближе к воротам. Всё остальное — динамически. У большинства моих клиентов ходовых позиций пятнадцать-двадцать процентов, и они дают восемьдесят процентов отборов.
2 l9: Мы так и планируем, первая и вторая зоны под ходовой товар.
1 l10: Правильно. Теперь основные операции, их четыре. Приёмка: сканируете паллету, система печатает этикетку и говорит, в какую ячейку везти. Размещение: привезли, отсканировали ячейку, отсканировали паллету — система проверила, что вы не ошиблись. Отбор: система даёт задание и маршрут, вы сканируете ячейку и товар. Инвентаризация: система сама просит пересчитать ячейку, когда она опустела или когда в ней что-то не сошлось.
3 l11: Каждый раз сканировать и ячейку, и товар? Это же вдвое дольше.
1 l12: По замерам — на восемь-десять секунд дольше на операцию. Но вы перестаёте тратить двенадцать минут на поиск. Двойное сканирование — это и есть то, что убирает пересорт. Запомните правило: не отсканировал — не положил. Самая частая ошибка первых месяцев — кладовщик торопится, ставит паллету в соседнюю ячейку, потому что нужная занята, и думает: потом поправлю. Не поправит. Через неделю таких паллет сорок, и система врёт.
2 l13: А что делать, если нужная ячейка правда занята?
1 l14: Не ставить рядом, а сообщить системе: на терминале есть кнопка «ячейка занята». Система даст другую и поставит занятую на проверку. Это тридцать секунд. Вторая частая ошибка — этикетки ячеек. Их клеят на уровне глаз на всех ярусах, и на четвёртом ярусе сканер не добивает. Клейте этикетки верхних ярусов внизу стойки, столбиком, со стрелками.
2 l15: Это ценно, мы как раз заказываем этикетки. Запишу.
1 l16: Третья ошибка — справочник. Если у товара в системе неверные габариты, система отправит паллету в ячейку, куда она не влезет. Кладовщик поставит её куда-нибудь ещё — и дальше по кругу. Поэтому до запуска промеряйте всё, что сможете, а у чего габаритов нет — заведите отдельную зону напольного хранения, не пускайте такой товар в стеллажи.
3 l17: Сколько времени обычно уходит, пока люди привыкнут?
1 l18: Первые две недели производительность падает, обычно на двадцать-тридцать процентов, это нормально, к этому надо быть готовым и не паниковать. К концу первого месяца выходите на прежний уровень, к концу третьего — плюс пятнадцать-двадцать пять процентов к тому, что было. Но только если первые две недели на каждой смене есть человек, который отвечает на вопросы на месте.
2 l19: И последний вопрос от меня: запускаться лучше сразу всем складом или по зонам?
1 l20: По зонам, без вариантов. Начните с одной зоны, желательно не самой нагруженной, отработайте две недели и только потом подключайте остальные. Запуск всем складом в один день я видел четыре раза, и три раза из четырёх склад вставал на день-два. На этом у меня всё, спасибо за внимание.
2 l21: Спасибо, Виктор Леонидович. Коллеги, запись и конспект будут в общей папке проекта.
""",
    protocol="""
# Адресное хранение: что это и какие ошибки делают в первые месяцы

**Дата:** 24.09.2026 (из имени файла)
**Длительность:** {dur}
**Лекторы:** Зотов Виктор Леонидович — консультант по складской логистике; двадцать лет работает со складами, двенадцать из них внедряет адресное хранение.

## 1. О чём лекция
- Что такое адресное хранение и что оно даёт складу: быстрый поиск и низкий пересорт.
- Чем статическое хранение отличается от динамического и почему лектор советует смешанную схему.
- Четыре операции кладовщика и правило двойного сканирования.
- Три типичные ошибки первых месяцев и как их избежать.
- Главный совет: запускаться по зонам, а не всем складом сразу.

## 2. Структура
1. {l2} Что такое адресное хранение и что оно даёт — Зотов
2. {l6} Статическое и динамическое хранение, смешанная схема — Зотов
3. {l10} Четыре операции кладовщика — Зотов
4. {l12} Ошибки первых месяцев — Зотов
5. {l18} Как проходит привыкание и как запускаться — Зотов

## 3. Содержание

### 1. Что такое адресное хранение и что оно даёт
- У каждого места на складе есть адрес: зона, ряд, секция, ярус, ячейка; что лежит в ячейке, знает система, а не кладовщик. {l2}
- Любой сотрудник, включая новичка, находит товар за две-три минуты. {l2}
- По словам лектора, без адресов новый кладовщик выходит на норму через шесть-восемь недель, на адресном складе — через пять-семь дней. {l4}
- Пересорт без адресов — обычно 3–4 %, на адресном складе нормой лектор называет 0,3 % и ниже. {l4}

### 2. Статическое и динамическое хранение, смешанная схема
- Статическое: за товаром закреплена своя ячейка; просто, но склад заполнен в среднем на 60–65 %. {l6}
- Динамическое: товар кладут в любую подходящую свободную ячейку, система запоминает; заполнение до 85 %, но без терминала найти ничего нельзя. {l6}
- Смешанная схема: ходовой товар — в статических ячейках нижних ярусов ближе к воротам, остальное — динамически. {l8}
- По словам лектора, у большинства его клиентов ходовых позиций 15–20 %, и они дают 80 % отборов. {l8}

### 3. Четыре операции кладовщика
- Приёмка: сканируют паллету, система печатает этикетку и называет ячейку. {l10}
- Размещение: сканируют ячейку и паллету, система проверяет, что ошибки нет. {l10}
- Отбор: система даёт задание и маршрут, сканируют ячейку и товар. {l10}
- Инвентаризация: система сама просит пересчитать ячейку, когда она опустела или что-то не сошлось. {l10}
- Двойное сканирование добавляет 8–10 секунд на операцию, но убирает поиск и пересорт. {l12}

### 4. Ошибки первых месяцев
- Паллету ставят в соседнюю ячейку, потому что нужная занята, и не сообщают системе; через неделю таких паллет десятки. {l12}
- Если ячейка занята — нажать на терминале «ячейка занята»: система даст другую и поставит занятую на проверку, это около 30 секунд. {l14}
- Этикетки всех ярусов клеят на уровне глаз, и на четвёртом ярусе сканер не добивает. {l14}
- Неверные габариты в справочнике: система отправляет паллету в ячейку, куда она не помещается. {l16}

### 5. Как проходит привыкание и как запускаться
- Первые две недели производительность падает на 20–30 %; к концу первого месяца возвращается к прежней, к концу третьего — плюс 15–25 %. {l18}
- Запуск всем складом в один день лектор видел четыре раза, в трёх случаях склад вставал на день-два. {l20}

## 5. Советы и предостережения
- Правило «не отсканировал — не положил»: без сканирования паллету в ячейку не ставить. {l12}
- Этикетки верхних ярусов клеить внизу стойки, столбиком, со стрелками. {l14}
- До запуска промерить габариты всего, что возможно; товар без габаритов держать в отдельной зоне напольного хранения, не в стеллажах. {l16}
- Первые две недели на каждой смене должен быть человек, который отвечает на вопросы на месте. {l18}
- Запускаться по зонам: начать с одной, не самой нагруженной, отработать две недели и только потом подключать остальные. {l20}

## 6. Вопросы слушателей
- Что будет, если терминал сел или пропала связь? — S3 — При динамическом хранении без системы склад «слепой», поэтому ходовой товар лучше держать в статических ячейках. {l8}
- Сканировать и ячейку, и товар — не вдвое ли дольше? — S3 — На 8–10 секунд дольше на операцию, зато исчезают 12 минут поиска. {l12}
- Что делать, если нужная ячейка занята? — Кузнецов — Сообщить системе кнопкой «ячейка занята», а не ставить рядом. {l14}
- Сколько времени люди привыкают? — S3 — Две недели спада, месяц до прежнего уровня, три месяца до прироста. {l18}
- Запускаться всем складом или по зонам? — Кузнецов — Только по зонам. {l20}

## 7. Термины
**Адресное хранение** — у каждого места на складе есть адрес, и система знает, что в нём лежит.
**Статическое хранение** — за каждым товаром закреплена постоянная ячейка.
**Динамическое хранение** — товар размещают в любой свободной подходящей ячейке, место запоминает система.
**Пересорт** — расхождение между тем, что числится в ячейке, и тем, что в ней лежит на самом деле.

## Комментарии
- **Кто говорит** — S3 не подписан: по репликам это кладовщик из числа слушателей; возможно, под этой меткой говорили разные люди.
""",
    marks=[],
))

RECORDS.append(dict(
    file="ИТ/Обучение/2026-09-16 Вебинар. Пароли и фишинг.mp4", kind="lecture", minutes=31,
    title="Пароли и фишинг: что должен знать каждый сотрудник", date="2026-09-16",
    why="один человек рассказывает, остальные задают вопросы",
    gist="Вебинар для сотрудников: как распознать поддельное письмо, новые требования к паролям с 1 октября, "
         "менеджер паролей и что делать, если уже нажал на ссылку.",
    topics=["фишинговые письма", "признаки подделки", "новые требования к паролям", "менеджер паролей",
            "вход с подтверждением кодом"],
    voices={
        "S1": ("Крылов Степан Юрьевич", "Степан Юрьевич", "лектор, системный администратор"),
        "S2": ("Назарова Алина Тимуровна", "Алина", "ведущая, передаёт вопросы из чата"),
        "S3": (None, "", "слушательница из бухгалтерии"),
    },
    uploaded=("a.melnikov", "2026-09-16 16:40"), signed=("a.melnikov", "2026-09-17 09:15"),
    talk="""
2 m0: Коллеги, добрый день, начинаем вебинар по информационной безопасности. Меня зовут Алина Назарова, я из службы поддержки, а ведёт сегодня Степан Юрьевич Крылов, наш системный администратор. Вопросы пишите в чат или включайте микрофон.
1 m1: Здравствуйте. Тема — пароли и фишинг. Почему именно сейчас: за август наш почтовый фильтр остановил тысячу девятьсот сорок подозрительных писем, но одиннадцать дошли до адресатов, и по трём из них люди перешли по ссылке. Обошлось, но это три входа в компанию, которые мы открыли сами.
2 m2: Степан, а кто-то из этих троих ввёл пароль?
1 m3: Один ввёл. Мы заметили через сорок минут и пароль сбросили. Поэтому начну с главного правила: пароль от рабочей почты нельзя вводить нигде, кроме страницы входа, которую вы открыли сами, из закладки. Не из письма, не из мессенджера, а сами.
3 m4: А как понять, что письмо поддельное? Они же сейчас очень похожи на настоящие.
1 m5: Четыре признака. Первый — адрес отправителя: смотрите не на имя, а на то, что после собаки. Вместо нашего домена там будет похожий, с лишней буквой или цифрой. Второй — срочность: «ваш ящик будет заблокирован через два часа». Третий — ссылка: наведите мышь, не нажимая, и посмотрите, куда она ведёт на самом деле. Четвёртый — вложение с двойным расширением, когда файл называется как документ, а на самом деле это программа.
2 m6: В чате спрашивают: а если письмо от директора с просьбой срочно оплатить счёт?
1 m7: Это самый частый сценарий для бухгалтерии. Правило простое: любая просьба о деньгах, пришедшая письмом, подтверждается голосом — звонком по номеру из нашего справочника, не по номеру из письма. В прошлом году у одной знакомой мне фирмы так ушло четыреста тысяч. Директор был в самолёте, письмо было «от него».
3 m8: У нас в бухгалтерии так и было однажды, главный бухгалтер позвонила и выяснила, что директор ничего не просил.
1 m9: Вот это правильная реакция. Теперь пароли. С первого октября меняем требования: минимальная длина — четырнадцать знаков вместо восьми, зато обязательную смену раз в девяносто дней отменяем. Менять пароль нужно будет, только если есть подозрение, что он утёк.
2 m10: То есть длиннее, но реже?
1 m11: Да. Длинный пароль, который вы помните, лучше короткого, который вы меняете с единицы на двойку. Как придумать четырнадцать знаков: возьмите три-четыре несвязанных слова и число. Не дату рождения, не имя ребёнка, не название компании. И не используйте рабочий пароль на других сайтах: когда взломают какой-нибудь интернет-магазин, ваш рабочий пароль окажется в общей базе.
3 m12: А где хранить, если паролей двадцать штук? На бумажке под клавиатурой нельзя, я понимаю.
1 m13: Нельзя. С октября всем ставим менеджер паролей, он уже проверен на пяти машинах в нашем отделе. Вы помните один длинный пароль, остальные хранит он. Инструкцию разошлём, установка занимает пять минут, поддержка поможет.
2 m14: Заявки на установку принимаем через «Заявочник», я разошлю ссылку после вебинара.
1 m15: И второе новшество — вход в почту вне офиса только с подтверждением кодом из приложения на телефоне. Включим в ноябре, сначала для руководителей и бухгалтерии. Это добавляет десять секунд при входе и закрывает девять из десяти сценариев кражи пароля.
3 m16: Что делать, если я уже нажала на ссылку в подозрительном письме?
1 m17: Три шага, запомните порядок. Первое — ничего не вводить и закрыть страницу. Второе — сразу позвонить в поддержку, внутренний номер двести два, не писать письмо, а именно позвонить. Третье — не выключать компьютер и ничего не удалять, нам нужно увидеть, что произошло. И главное: за сообщение о своей ошибке никого не наказывают. Наказуемо — промолчать.
2 m18: Спасибо, Степан. Коллеги, запись будет в папке обучения, новые правила паролей придут письмом до конца сентября.
""",
    protocol="""
# Пароли и фишинг: что должен знать каждый сотрудник

**Дата:** 16.09.2026 (из имени файла)
**Длительность:** {dur}
**Лекторы:** Крылов Степан Юрьевич — системный администратор компании. Ведущая — Назарова Алина Тимуровна, служба поддержки.

## 1. О чём лекция
- Почему тема срочная: в августе 11 поддельных писем дошли до сотрудников, по трём перешли по ссылке, один человек ввёл пароль.
- Четыре признака поддельного письма и правило подтверждения денежных просьб голосом.
- Новые требования к паролям с 1 октября: длиннее, но без обязательной смены.
- Менеджер паролей с октября и вход с кодом из приложения с ноября.
- Что делать, если уже нажал на ссылку: три шага.

## 2. Структура
1. {m1} Зачем этот вебинар: цифры за август — Крылов
2. {m5} Как распознать поддельное письмо — Крылов
3. {m9} Новые требования к паролям — Крылов
4. {m13} Менеджер паролей и вход с кодом — Крылов
5. {m17} Что делать, если нажал на ссылку — Крылов

## 3. Содержание

### 1. Зачем этот вебинар: цифры за август
- По словам лектора, за август почтовый фильтр остановил 1 940 подозрительных писем; 11 дошли до адресатов, по трём перешли по ссылке. {m1}
- Один сотрудник ввёл пароль; это заметили через 40 минут и пароль сбросили. {m3}
- Главное правило: пароль от рабочей почты вводить только на странице входа, открытой самостоятельно из закладки, — не из письма и не из мессенджера. {m3}

### 2. Как распознать поддельное письмо
- Адрес отправителя: смотреть на часть после «собаки» — вместо домена компании там похожий, с лишней буквой или цифрой. {m5}
- Срочность: угроза блокировки «через два часа». {m5}
- Ссылка: навести мышь, не нажимая, и посмотреть, куда она ведёт. {m5}
- Вложение с двойным расширением: называется как документ, а на деле программа. {m5}
- Пример лектора: у знакомой ему фирмы по письму «от директора», который был в самолёте, ушло 400 тысяч рублей. {m7}

### 3. Новые требования к паролям
- С 1 октября минимальная длина пароля — 14 знаков вместо 8; обязательная смена раз в 90 дней отменяется. {m9}
- Менять пароль нужно только при подозрении, что он утёк. {m9}
- Как составить: три-четыре несвязанных слова и число; не дата рождения, не имя ребёнка, не название компании. {m11}

### 4. Менеджер паролей и вход с кодом
- С октября всем ставят менеджер паролей; он проверен на пяти машинах ИТ-отдела, установка занимает пять минут. {m13}
- Заявки на установку — через «Заявочник», ссылку разошлёт Назарова. {m14}
- В ноябре вход в почту вне офиса — только с кодом из приложения на телефоне, сначала для руководителей и бухгалтерии. {m15}
- По словам лектора, это добавляет 10 секунд при входе и закрывает девять из десяти сценариев кражи пароля. {m15}

### 5. Что делать, если нажал на ссылку
- Ничего не вводить и закрыть страницу. {m17}
- Сразу позвонить в поддержку по внутреннему номеру 202 — именно позвонить, не писать. {m17}
- Не выключать компьютер и ничего не удалять. {m17}

## 5. Советы и предостережения
- Любую просьбу о деньгах, пришедшую письмом, подтверждать звонком по номеру из справочника компании, а не из письма. {m7}
- Не использовать рабочий пароль на других сайтах: после взлома стороннего сайта он окажется в общей базе. {m11}
- Не хранить пароли на бумаге под клавиатурой. {m13}
- О своей ошибке сообщать сразу: за сообщение не наказывают, наказуемо молчание. {m17}

## 6. Вопросы слушателей
- Ввёл ли кто-то из троих пароль? — Назарова — Один ввёл, пароль сбросили через 40 минут. {m3}
- Как понять, что письмо поддельное? — S3 — По четырём признакам: адрес, срочность, ссылка, вложение. {m5}
- Что делать с письмом от директора с просьбой срочно оплатить счёт? — из чата — Подтверждать голосом, звонком по номеру из справочника. {m7}
- Где хранить двадцать паролей? — S3 — В менеджере паролей, который поставят с октября. {m13}
- Что делать, если уже нажала на ссылку? — S3 — Три шага: закрыть страницу, позвонить в поддержку, не выключать компьютер. {m17}

## 7. Термины
**Фишинг** — поддельное письмо или страница, которые выманивают пароль или деньги.
**Менеджер паролей** — программа, которая хранит пароли; человек помнит только один, главный.
**Вход с кодом** — кроме пароля нужен одноразовый код из приложения на телефоне.

## Комментарии
- **Кто говорит** — S3 не подписана; по репликам это сотрудница бухгалтерии.
""",
    marks=[],
))

CLIENT = f"{users.HOME_ROOT}/v.sokolova/Заказчики"

RECORDS.append(dict(
    file=f"{CLIENT}/2026-09-30 Созвон «Мятный берег». Подарочная коробка.m4a", kind="call", minutes=29,
    title="Созвон с «Мятным берегом»: подарочная коробка для чайного набора", date="2026-09-30",
    why="разговор двух сторон: одна заказывает и ставит условия, другая отвечает по срокам и цене",
    gist="Заказчику нужна подарочная коробка под три банки чая, 12 тысяч штук к 20 ноября; обсуждают конструкцию, "
         "тиснение, цену выше бюджета заказчика и условия оплаты.",
    topics=["конструкция коробки", "тиснение фольгой", "ложемент", "сроки образца", "цена", "условия оплаты"],
    voices={
        "S1": ("Соколова Вера Игоревна", "", "наша сторона, менеджер по продажам"),
        "S2": ("Латыпова Гульнара Ринатовна", "Гульнара Ринатовна", "заказчик, руководитель закупок"),
        "S3": ("Шубина Наталья Петровна", "Наталья Петровна", "наша сторона, технолог"),
        "S4": (None, "Кирилл", "заказчик, дизайнер"),
    },
    uploaded=("v.sokolova", "2026-09-30 16:05"), signed=("v.sokolova", "2026-09-30 17:20"),
    talk="""
1 n0: Гульнара Ринатовна, добрый день, спасибо, что нашли время. С нашей стороны я и Наталья Петровна, технолог. Давайте пройдёмся по вашему запросу на подарочные коробки.
2 n1: Добрый день. Да, со мной Кирилл, наш дизайнер. Задача такая: к новогоднему сезону нам нужна подарочная коробка под набор из трёх банок чая. Тираж — двенадцать тысяч штук, и они должны быть у нас на складе не позже двадцатого ноября, потому что с двадцать пятого мы начинаем отгрузку в сети.
1 n2: Двенадцать тысяч к двадцатому ноября, поняла. Какая конструкция?
4 n3: Коробка с откидной крышкой на магните и ложементом под три банки. Банки диаметром семьдесят пять миллиметров, высота сто десять. Снаружи полноцветная печать и тиснение логотипа золотой фольгой.
3 n4: По конструкции вопрос: магнит и ложемент — это уже не гофрокороб, это кашированный микрогофрокартон. Мы такое делаем, но тиснения фольгой у нас своего нет, это подрядчик, плюс пять-шесть рабочих дней.
2 n5: Тиснение обязательно, это фирменный элемент. Без него коробку не согласует наш директор.
3 n6: Тогда закладываем подрядчика. И второй момент — ложемент. Из картона или из вспененного материала?
4 n7: Мы хотели бы картонный, чтобы вся упаковка сдавалась в макулатуру, для нас это важно. Но если банки будут болтаться, тогда вспененный.
3 n8: Картонный сделаем, но мне нужны три настоящие банки, чтобы подогнать посадку. По чертежу я гарантировать не буду.
2 n9: Банки отправим курьером завтра, послезавтра будут у вас.
1 n10: Теперь сроки. Чтобы двенадцать тысяч были у вас двадцатого ноября, нам нужно утвердить образец не позже шестнадцатого октября. Образец делаем семь рабочих дней после получения банок и макета.
4 n11: Макет печати я пришлю до пятого октября. Тиснение дам отдельным слоем.
1 n12: Хорошо. По цене: предварительно, при тираже двенадцать тысяч, это от ста сорока до ста шестидесяти рублей за коробку, точнее скажу после образца. Оснастка для вырубки и клише для тиснения — отдельно, разово, около восьмидесяти пяти тысяч.
2 n13: Сто шестьдесят — это выше нашего бюджета. Мы закладывали сто тридцать пять за штуку. Что можно сделать, чтобы уложиться?
3 n14: Вариантов два. Заменить магнит на клапан с язычком — минус восемь-девять рублей. Или печатать не по всей поверхности, а только крышку — минус пять-шесть.
2 n15: Магнит я бы оставила, это ощущение дорогой вещи. Печать только на крышке — надо показать директору. Пока не готова ответить.
1 n16: Тогда я посчитаю оба варианта и пришлю вам к пятнице: полный и с печатью только на крышке. А вы решите по печати.
2 n17: Да, пришлите. И ещё вопрос по оплате: мы обычно работаем с отсрочкой тридцать дней после поставки.
1 n18: Для первого заказа у нас предоплата пятьдесят процентов, отсрочка возможна со второго. Это я могу обсудить с руководителем, но обещать не буду.
2 n19: Обсудите, пожалуйста, для нас это существенно. Пятьдесят процентов предоплаты в октябре нам тяжело.
1 n20: Обсужу и вернусь с ответом. Давайте созвонимся ещё раз седьмого октября, к этому времени будут банки, макет и расчёт.
2 n21: Договорились, седьмого в одиннадцать. Кирилл, хотелось бы ещё, чтобы внутри крышки была напечатана короткая история бренда, но это если не сильно удорожает.
4 n22: Да, пару строк текста, одним цветом.
3 n23: Одним цветом внутри — это копейки, рубль-полтора. Учтём в расчёте как вариант.
1 n24: Записала. Спасибо, до связи седьмого.
""",
    protocol="""
# Созвон с заказчиком: подарочная коробка для чайного набора

**Дата:** 30.09.2026 (из имени файла)
**Длительность:** {dur}

## 1. Кратко
- «Мятному берегу» нужна подарочная коробка под три банки чая: 12 000 штук на складе заказчика не позже 20 ноября.
- Главное требование — тиснение логотипа золотой фольгой; у нас это делает подрядчик, плюс пять-шесть рабочих дней.
- Образец нужно утвердить не позже 16 октября; для него нужны три банки и макет печати.
- Предварительная цена 140–160 рублей за штуку выше бюджета заказчика (135 рублей); считаем два варианта.
- Условия оплаты не согласованы: заказчик просит отсрочку 30 дней, у нас для первого заказа предоплата 50 %.

## 2. Участники

| Участник | Сторона | Роль в разговоре |
|---|---|---|
| Соколова Вера Игоревна | наша | вела созвон, сроки и цена |
| Шубина Наталья Петровна | наша | технолог: конструкция и материалы |
| Латыпова Гульнара Ринатовна | заказчик | ставила задачу, бюджет и оплата |
| Кирилл (S4) | заказчик | дизайнер: конструкция и макет |

## 3. Требования и пожелания заказчика
- Подарочная коробка под набор из трёх банок чая, тираж 12 000 штук, на складе заказчика не позже 20 ноября: с 25 ноября начинается отгрузка в сети. {n1}
- Конструкция: откидная крышка на магните, ложемент под три банки диаметром 75 мм и высотой 110 мм. {n3}
- Снаружи полноцветная печать и тиснение логотипа золотой фольгой; тиснение обязательно — без него коробку не согласует директор заказчика. {n5}
- Ложемент — картонный, чтобы вся упаковка сдавалась в макулатуру; вспененный — только если банки будут болтаться. {n7}
- Бюджет заказчика — 135 рублей за штуку. {n13}
- Магнит на крышке оставить. {n15}
- Отсрочка платежа 30 дней после поставки. {n17}
- Пожелание: внутри крышки короткая история бренда, пара строк одним цветом, если это не сильно удорожает. {n21}

## 4. Договорённости

| Договорённость | В записи | Кто предложил | Кто согласился |
|---|---|---|---|
| Тиснение фольгой делает подрядчик, срок на это закладывается в график | {n6} | Шубина | Латыпова |
| Образец утверждается не позже 16 октября; на его изготовление — семь рабочих дней после получения банок и макета | {n10} | Соколова | Кирилл (S4) |
| Следующий созвон — 7 октября в 11:00 | {n20} | Соколова | Латыпова |

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Соколова Вера Игоревна (наша сторона)** | | | |
| Посчитать и прислать два варианта цены: с полной печатью и с печатью только на крышке | к пятнице | {n16} | — |
| Обсудить с руководителем отсрочку платежа для первого заказа | — | {n20} | без обещаний |
| **Шубина Наталья Петровна (наша сторона)** | | | |
| Изготовить образец коробки с картонным ложементом | семь рабочих дней после получения банок и макета | {n10} | ждёт банки и макет |
| **Латыпова Гульнара Ринатовна (заказчик)** | | | |
| Отправить курьером три банки для подгонки ложемента | завтра | {n9} | — |
| Показать директору вариант с печатью только на крышке и дать ответ | — | {n15} | — |
| **S4 (Кирилл, дизайнер заказчика)** | | | |
| Прислать макет печати, тиснение — отдельным слоем | до 5 октября | {n11} | — |

Прочерк в колонке «Срок» — срок не назвали.
Следующий созвон — 7 октября в 11:00: банки, макет, расчёт.

## 6. Открытые вопросы
- **Цена.** Предварительно 140–160 рублей за штуку плюс разово около 85 тысяч за оснастку и клише {n12}; бюджет заказчика — 135 рублей {n13}. Варианты удешевления: клапан с язычком вместо магнита (минус 8–9 рублей) или печать только на крышке (минус 5–6 рублей) {n14}; заказчик не готов ответить {n15}.
- **Условия оплаты.** Заказчик просит отсрочку 30 дней {n17}; наша сторона для первого заказа требует предоплату 50 % и обещала только обсудить {n18}. Заказчику предоплата 50 % в октябре тяжела {n19}.

## 7. Риски
- Запаса по срокам нет: образец должен быть утверждён к 16 октября, а подрядчик по тиснению добавляет пять-шесть рабочих дней — Шубина {n4}.
- Посадку банок в картонный ложемент технолог по чертежу не гарантирует — нужны настоящие банки — Шубина {n8}.

## Комментарии
- **Кто говорит** — S4 в разговоре называют Кириллом, фамилия не звучала; человек не подписан.
""",
    marks=[
        ("2026-10-01 09:20", "v.sokolova", "Изготовить образец", "due", "2026-10-14"),
        ("2026-10-02 14:10", "v.sokolova", "три банки", "done", "Банки получены 02.10"),
        ("2026-10-02 17:35", "v.sokolova", "два варианта цены", "done", "Расчёт отправлен: 152 и 146 рублей за штуку"),
        ("2026-10-06 18:05", "v.sokolova", "макет печати", "done", "Макет пришёл 06.10, на день позже"),
        ("2026-10-07 15:00", "v.sokolova", "отсрочку платежа", "done", "Согласовали предоплату 30 %, отсрочку не дали"),
        ("2026-10-07 15:02", "v.sokolova", "Показать директору", "done", "Выбрали печать только на крышке"),
        ("2026-10-07 15:05", "v.sokolova", "Изготовить образец", "due", "2026-10-16"),
    ],
))

RECORDS.append(dict(
    file=f"{CLIENT}/2026-10-07 Созвон «Мятный берег». Образец и цена.m4a", kind="call", minutes=22,
    title="Созвон с «Мятным берегом»: образец, цена и оплата", date="2026-10-07",
    why="разговор двух сторон: согласуют образец, цену и условия оплаты",
    gist="Замечание к макету сдвигает образец на 16 октября; стороны сходятся на 15 тысячах штук по 144 рубля "
         "с хранением части тиража до января; предоплата 30 %, срок оплаты остатка не согласован.",
    topics=["замечание к макету", "срок образца", "цена и тираж", "хранение тиража", "предоплата", "оснастка"],
    voices={
        "S1": ("Соколова Вера Игоревна", "", "наша сторона, менеджер по продажам"),
        "S2": ("Латыпова Гульнара Ринатовна", "Гульнара Ринатовна", "заказчик, руководитель закупок"),
        "S3": ("Шубина Наталья Петровна", "Наталья Петровна", "наша сторона, технолог"),
    },
    uploaded=("v.sokolova", "2026-10-07 12:40"), signed=("v.sokolova", "2026-10-07 14:15"),
    talk="""
1 p0: Гульнара Ринатовна, добрый день. Сегодня Наталья Петровна со мной, Кирилла нет?
2 p1: Добрый день. Кирилл в отпуске до понедельника, я за него. Макет он вам отправил шестого, на день позже, извините.
3 p2: Макет получили, банки тоже, пришли второго. По макету одно замечание: логотип для тиснения слишком мелкий, линии тоньше трёх десятых миллиметра фольга не пропечатает, они сольются.
2 p3: Что нужно поменять?
3 p4: Либо увеличить логотип на пятнадцать процентов, либо утолщить линии в самом знаке. Иначе я не отвечаю за результат.
2 p5: Увеличить можно, я передам Кириллу, он поправит в понедельник.
1 p6: Понедельник — это двенадцатое. Тогда образец сдвигается: Наталья Петровна, сколько нужно после исправленного макета?
3 p7: Ложемент я уже подогнала, банки сидят плотно, из картона, вспененный не нужен. После макета — четыре рабочих дня на печать и тиснение у подрядчика. То есть образец будет шестнадцатого октября, если макет придёт двенадцатого.
2 p8: А утвердить образец мы должны были до шестнадцатого. Получается впритык.
1 p9: Да, запаса нет. Предлагаю так: шестнадцатого образец у вас курьером, утверждение — до девятнадцатого, и мы всё ещё успеваем к двадцатому ноября, но только если не будет второго круга правок.
2 p10: Хорошо, до девятнадцатого утвердим. Директор сейчас в городе, покажу сразу.
1 p11: Теперь цена. Я отправляла расчёт второго числа: полная печать — сто пятьдесят два рубля за штуку, печать только на крышке — сто сорок шесть. Текст внутри крышки одним цветом — плюс рубль двадцать.
2 p12: Директор посмотрел. Печать только на крышке его устраивает, текст внутри берём. Но сто сорок семь двадцать — это всё равно выше ста тридцати пяти. Он просит сто сорок за штуку, со всем.
1 p13: Сто сорок при двенадцати тысячах я не дам, это ниже нашей себестоимости с подрядчиком по тиснению. Могу предложить сто сорок четыре при условии, что тираж — пятнадцать тысяч, а не двенадцать.
2 p14: Пятнадцать тысяч нам много на этот сезон. Хотя, если три тысячи можно забрать в январе, под февральские праздники, это вариант.
1 p15: Забрать в январе можно, храним у себя бесплатно до тридцать первого января. Тогда фиксируем: пятнадцать тысяч, сто сорок четыре рубля, двенадцать тысяч к двадцатому ноября, три тысячи до конца января.
2 p16: Мне нужно подтверждение директора, но думаю, он согласится. Отвечу до пятницы.
1 p17: Жду. По оплате: руководитель согласовал вам тридцать процентов предоплаты вместо пятидесяти, остальное — в течение четырнадцати дней после поставки. Отсрочку тридцать дней на первый заказ не дали.
2 p18: Тридцать процентов — это лучше, чем пятьдесят. Четырнадцать дней вместо тридцати — передам в финансовый отдел, не уверена, что их устроит.
1 p19: Понимаю. Тогда этот вопрос остаётся за вами. Я пришлю проект договора с этими условиями до девятого октября, чтобы юристы уже смотрели.
2 p20: Да, присылайте. И оснастка — восемьдесят пять тысяч, она остаётся нашей?
3 p21: Оснастка хранится у нас и используется только под ваши заказы. При повторном тираже платить за неё не нужно.
2 p22: Отлично. Тогда до связи, жду договор.
""",
    protocol="""
# Созвон с заказчиком: образец, цена и оплата подарочной коробки

**Дата:** 07.10.2026 (из имени файла)
**Длительность:** {dur}

## 1. Кратко
- Логотип для тиснения в макете слишком мелкий; заказчик увеличит его на 15 %, исправленный макет — 12 октября.
- Образец сдвигается на 16 октября, утверждение — до 19 октября; запаса на второй круг правок нет.
- Заказчик выбрал печать только на крышке и текст внутри крышки; ложемент — картонный.
- Предварительно сошлись на 15 000 штук по 144 рубля: 12 000 к 20 ноября, 3 000 — до конца января; нужно подтверждение директора заказчика.
- Предоплата — 30 %; остаток за 14 дней после поставки заказчик ещё согласует с финансовым отделом.

## 2. Участники

| Участник | Сторона | Роль в разговоре |
|---|---|---|
| Соколова Вера Игоревна | наша | вела созвон, цена и оплата |
| Шубина Наталья Петровна | наша | технолог: макет, образец, оснастка |
| Латыпова Гульнара Ринатовна | заказчик | решения по макету, цене и срокам |

Упоминали, но не участвовали: Кирилл, дизайнер заказчика (в отпуске до понедельника); директор заказчика.

## 3. Требования и пожелания заказчика
- Цена 140 рублей за штуку «со всем» — просьба директора заказчика; бюджет был 135 рублей. {p12}
- Печать только на крышке, текст внутри крышки — берут. {p12}
- 15 000 штук на этот сезон много; приемлемо, если 3 000 можно забрать в январе, под февральские праздники. {p14}
- Вопрос: остаётся ли оснастка за 85 тысяч рублей за заказчиком. {p20}

## 4. Договорённости

| Договорённость | В записи | Кто предложил | Кто согласился |
|---|---|---|---|
| Логотип для тиснения увеличивается на 15 %: линии тоньше 0,3 мм фольга не пропечатает | {p4} | Шубина | Латыпова |
| Ложемент — картонный, вспененный не нужен: банки сидят плотно | {p7} | Шубина | — |
| Образец — у заказчика 16 октября курьером, утверждение — до 19 октября | {p9} | Соколова | Латыпова |
| 3 000 штук из тиража хранятся у нас бесплатно до 31 января | {p15} | Соколова | Латыпова |
| Предоплата — 30 % вместо 50 %; отсрочку 30 дней на первый заказ не дают | {p17} | Соколова | Латыпова |
| Оснастка хранится у нас, используется только под заказы «Мятного берега»; при повторном тираже не оплачивается | {p21} | Шубина | Латыпова |

## 5. Задачи

| Задача | Срок | В записи | Статус |
|---|---|---|---|
| **Латыпова Гульнара Ринатовна (заказчик)** | | | |
| Передать дизайнеру правку логотипа и прислать исправленный макет | в понедельник, 12 октября | {p5} | дизайнер в отпуске до понедельника |
| Утвердить образец | до 19 октября | {p10} | — |
| Получить подтверждение директора: 15 000 штук по 144 рубля | до пятницы | {p16} | — |
| Согласовать с финансовым отделом оплату остатка в течение 14 дней после поставки | — | {p18} | — |
| **Соколова Вера Игоревна (наша сторона)** | | | |
| Прислать проект договора: 15 000 штук по 144 рубля, предоплата 30 % | до 9 октября | {p19} | — |
| **Шубина Наталья Петровна (наша сторона)** | | | |
| Изготовить образец по исправленному макету и отправить заказчику курьером | 16 октября | {p7} | ложемент подогнан, ждёт макет |

Прочерк в колонке «Срок» — срок не назвали.

## 6. Открытые вопросы
- **Цена и тираж.** Заказчик просил 140 рублей за штуку {p12}; наша сторона при 12 000 штук на это не идёт и предлагает 144 рубля при тираже 15 000 {p13}. Латыпова ждёт подтверждения директора {p16}.
- **Срок оплаты остатка.** Мы даём 14 дней после поставки {p17}; Латыпова не уверена, что это устроит финансовый отдел заказчика {p18}.

## 7. Риски
- На второй круг правок образца времени нет: иначе тираж не успеет к 20 ноября — Соколова {p9}.
- Образец будет 16 октября, только если исправленный макет придёт 12 октября — Шубина {p7}.
""",
    marks=[
        ("2026-10-07 15:10", "v.sokolova", "проект договора", "due", "2026-10-09"),
    ],
))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
