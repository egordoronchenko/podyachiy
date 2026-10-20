"""Открытие базы при запуске сервиса и разовый перенос прежних файлов состояния в базу.

До базы состояние лежало отдельными файлами: в папке состояния — users.json, keys.json,
dictionaries.json, settings.json, requests.json, journal.jsonl, brand/brand.json; в папке
результатов — _state/processed.json и файлы отметок поручений <запись>.tasks.json. Первый
запуск версии с базой переносит всё это в базу. Только здесь остаётся знание о прежних видах
этих файлов (права списками, ключи внутри учётных записей, суммы без валюты, расходы «до
журнала»): остальной код читает только базу.

Перенос идёт так, чтобы половины не осталось:
  1. прежние файлы читаются целиком; нечитаемый файл — отказ, а не «начну с пустого»: за
     пустым журналом обработки стоит повторная оплата всех записей;
  2. база собирается во временной папке рядом, одной транзакцией;
  3. сверка: из новой базы теми же классами, которыми сервис будет работать, читается всё
     обратно и сравнивается с прочитанным из файлов — пользователи, права, ключи, словари,
     настройки, заявки, журнал обработки, отметки, каждое событие журнала и отчёт о расходах;
  4. готовый файл базы одним переименованием встаёт на место;
  5. прежние файлы переезжают в STATE/до-базы-<дата>/ — копия на случай отката; сервис их
     больше не читает.
Сбой до шага 4 — базы нет, прежние файлы на месте, сервис не запускается и называет причину;
следующий запуск начнёт перенос заново. Сбой на шаге 5 доделывает следующий запуск: что и куда
переносить, записано в самой базе.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import time
from pathlib import Path

from . import about, brand, db, dictionary, journal, keys, ledger, marks, pipeline, settings, signup, usage, users

WORK = ".перенос-в-базу"                # временная папка в папке состояния: в ней собирается база
KEEP = "до-базы-"                       # папка с прежними файлами: до-базы-ГГГГ-ММ-ДД
NOTE = "ПРОЧТИ.txt"
TASKS_SUFFIX = ".tasks.json"            # прежний файл отметок поручений рядом с протоколом
STATE_FILES = ("users.json", "keys.json", "dictionaries.json", "settings.json", "requests.json", "journal.jsonl",
               "brand/brand.json")
OUTBOX_FILES = ("_state/processed.json",)
USER_FIELDS = {"name", "email", "password", "home", "admin", "read", "upload", "manage", "home_upload",
               "notify_ready", "source", "checked", "link"}
KEY_FIELDS = ("id", "hash", "prefix", "owner", "name", "kind", "created", "expires", "used")
CLAIM_FIELDS = {"id", "at", "name", "email", "login", "comment", "status", "decided_at", "decided_by"}
MARK_FIELDS = {*marks.COLUMNS, "who", "task"}


def human(size: int) -> str:
    return f"{size / 2 ** 20:.1f} МБ" if size >= 2 ** 20 else f"{max(size, 0) / 1024:.0f} КБ"


def storage_warnings(cfg, storage: dict) -> list[str]:
    """О чём администратору надо знать про папку состояния — для журнала работы и страницы
    «Обработка». Без путей на диске сервера: откуда папка подключена, сказано в журнале работы,
    в строке «база: …»."""
    if storage["kind"] != db.NETWORK:
        return []
    return [f"папка состояния лежит на сетевой папке ({storage['type']}): SQLite на сетевых "
            "папках ненадёжен — блокировки файлов там работают не всегда, и при обрыве связи база может "
            "повредиться. Перенесите папку состояния (HOST_STATE в .env) на локальный диск сервера; входная "
            "папка и папка результатов на сетевом диске допустимы"]


def open_base(cfg, log, kept: bool) -> db.Database:
    """База сервиса, проверенная и готовая к работе. Нельзя — db.Refusal с причиной."""
    state = Path(cfg.state_dir)
    if not kept:
        raise db.Refusal(
            f"папка состояния {state} не подключена с хоста: в ней база сервиса — пользователи, расходы и журнал "
            "обработки. Внутри контейнера она пропала бы при его пересоздании, и все записи пошли бы в "
            "обработку заново, за деньги. Верните в compose.yml строку с HOST_STATE (том /data/state)")
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise db.Refusal(f"папка состояния {state} недоступна: {e}") from e
    db.probe(state)
    if (state / WORK).exists():                             # след переноса, оборванного в прошлый раз
        shutil.rmtree(state / WORK)
    brought = False
    if not (state / db.FILE).exists():
        if _present(cfg):
            bring(cfg, log)
            brought = True
        elif sorted(state.glob(KEEP + "*")):
            raise db.Refusal(
                f"файла базы {state / db.FILE} нет, хотя раньше он был: рядом лежит папка «{sorted(state.glob(KEEP + '*'))[0].name}» "
                "с файлами, перенесёнными в базу. С пустой базой сервис счёл бы все записи новыми и обработал "
                "их заново, за деньги. Восстановите базу из резервной копии (install/README.md, раздел 12)")
    base = db.Database(state, log)
    try:
        base.check()
        _finish(base, cfg, log)
    except BaseException:
        base.close()
        raise
    if base.created:
        base.note("created", f"{journal.now_iso()} · {about.version() or 'без номера версии'}")
    elif not brought and _present(cfg):
        log("ВНИМАНИЕ: на прежних местах лежат файлы состояния прежнего вида (" + ", ".join(found_names(cfg)) + "), "
            "но база уже есть — сервис их не читает. Если это копия, уберите её оттуда")
    place = db.filesystem(state)
    log(f"база: {base.path}, схема {base.version}, {human(base.size())}; файловая система папки состояния — "
        + (place["type"] or "не определена") + (f" ({place['source']})" if place["kind"] != db.LOCAL else "")
        + {db.DESKTOP: ", папка компьютера в Docker Desktop: годится, если это его локальный диск",
           db.NETWORK: ", СЕТЕВАЯ ПАПКА", db.LOCAL: ""}[place["kind"]])
    return base


# ---------- что лежит от прежней версии ----------

def _present(cfg) -> list[tuple[str, str]]:
    """Прежние файлы состояния, которые есть на диске: (где — state или outbox, путь внутри)."""
    found = [("state", name) for name in STATE_FILES if (Path(cfg.state_dir) / name).is_file()]
    return found + [("outbox", name) for name in OUTBOX_FILES if (Path(cfg.outbox) / name).is_file()]


def found_names(cfg) -> list[str]:
    return [name for _, name in _present(cfg)]


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise db.Refusal(f"файл {path} не читается ({e}). Перенос в базу остановлен, ничего не изменено: "
                         "исправьте файл или верните его из резервной копии. Если его содержимое не нужно, "
                         "уберите файл из папки — и запустите сервис снова") from e


def _dict(value, path: Path, what: str) -> dict:
    if not isinstance(value, dict):
        raise db.Refusal(f"файл {path}: {what} — не того вида, что пишет сервис. Перенос в базу остановлен, "
                         "ничего не изменено")
    return value


def _money(run: dict) -> dict:
    """Прогон в том виде, в каком он лежит в базе: суммы числами с точкой, недостающие поля — пустые."""
    run = journal._with_money(run)
    out = {"at": run["at"], "kind": "run", "by": run.get("by") or "", "file": run["file"],
           "uploaded_by": run.get("uploaded_by") or "", "reason": run.get("reason"), "status": run.get("status"),
           "started_at": run.get("started_at"),
           "audio_sec": None if run.get("audio_sec") is None else float(run["audio_sec"]),
           "cost": {cur: float(v) for cur, v in sorted(run["cost"].items())},
           "stages": [{k: (float(s[k]) if k == "cost" else s[k]) for k in journal.STAGE_FIELDS} for s in run["stages"]],
           "providers": [{k: (float(s[k]) if k == "cost" else s[k]) for k in journal.PROVIDER_FIELDS}
                         for s in run["providers"]]}
    if run.get("runs") is not None:
        out["runs"] = int(run["runs"])
    return {**out, **{k: v for k, v in run.items() if k not in journal.RUN_FIELDS}}


def read_legacy(cfg) -> dict:
    """Всё прежнее состояние из файлов — уже в нынешнем виде. Ничего не меняет на диске."""
    state, outbox = Path(cfg.state_dir), Path(cfg.outbox)
    old: dict = {"skipped": {}, "tasks_files": []}

    # ----- пользователи; ключи раньше лежали в учётных записях -----
    raw = _json(state / "users.json")
    people = _dict(raw, state / "users.json", "содержимое").get("users", {}) if raw is not None else {}
    kept = _json(state / "keys.json")
    vault = list(_dict(kept, state / "keys.json", "содержимое").get("keys", [])) if kept is not None else []
    old["users"] = {}
    for login, saved in sorted(_dict(people, state / "users.json", "users").items()):
        user = dict(_dict(saved, state / "users.json", f"учётная запись {login}"))
        # файл прежнего вида: права хранились списками и всегда действовали с подпапками
        if "read" not in user:
            user["read"] = {f: users.TREE for f in user.pop("folders", [])}
            user["manage"] = {f: users.TREE for f in user.pop("manages", [])}
        user.setdefault("email", "")            # до заявок на доступ почты у пользователя не было
        user.setdefault("notify_ready", True)   # и писем о готовности записи
        user.setdefault("source", users.LOCAL)  # до входа через домен все записи были локальными
        # до раздельных прав загрузка шла вместе с чтением и с личной папкой; в файле прежнего
        # вида её нет ни у кого: чтение — только чтение
        user.setdefault("upload", {})
        user.setdefault("manage", {})
        user.setdefault("home_upload", False)
        user.setdefault("admin", False)
        user.setdefault("password", "")
        user.setdefault("home", f"{users.HOME_ROOT}/{login}")
        user.setdefault("name", login)
        known = {k["hash"] for k in vault}
        vault += [{**k, "owner": login, "kind": keys.PLAIN} for k in user.pop("keys", []) if k["hash"] not in known]
        if user["source"] == users.DOMAIN:
            user["checked"] = int(user.get("checked") or 0)
        else:
            user.pop("checked", None)
        if not user.get("link"):
            user.pop("link", None)
        unknown = set(user) - USER_FIELDS
        if unknown:
            raise db.Refusal(f"файл {state / 'users.json'}: у учётной записи {login} поля, которых сервис не знает "
                             f"({', '.join(sorted(unknown))}). Перенос в базу остановлен, ничего не изменено")
        old["users"][login] = {**{k: user[k] for k in ("name", "email", "password", "home")},
                               "admin": bool(user["admin"]),
                               **{kind: dict(sorted(user[kind].items())) for kind in users.KINDS},
                               "home_upload": bool(user["home_upload"]), "notify_ready": bool(user["notify_ready"]),
                               "source": user["source"],
                               **({"checked": user["checked"]} if "checked" in user else {}),
                               **({"link": dict(user["link"])} if "link" in user else {})}
    old["keys"] = [{field: key.get(field, keys.PLAIN if field == "kind" else None) for field in KEY_FIELDS}
                   for key in vault]

    # ----- словари -----
    raw = _json(state / "dictionaries.json")
    raw = _dict(raw, state / "dictionaries.json", "содержимое") if raw is not None else {}
    old["dictionaries"] = {}
    for scope in (dictionary.FOLDERS, dictionary.RECORDS):
        for path, saved in sorted((raw.get(scope) if isinstance(raw.get(scope), dict) else {}).items()):
            if saved and (saved.get("people") or saved.get("terms")):
                old["dictionaries"][scope, path] = {
                    "people": saved.get("people") or [], "terms": saved.get("terms") or [],
                    "by": saved.get("by", ""), "at": saved.get("at", ""), "changed": float(saved.get("changed", 0))}

    # ----- настройки со страницы и оформление -----
    raw = _json(state / "settings.json")
    raw = dict(_dict(raw, state / "settings.json", "содержимое")) if raw is not None else {}
    chosen, notes = raw.pop("speech_model", None), raw.pop("templates", None)
    old["speech_model"] = ({"value": chosen["value"], "by": chosen.get("by", ""), "at": chosen.get("at", "")}
                           if isinstance(chosen, dict) and chosen.get("value") else None)
    old["template_notes"] = ({item: {"by": note.get("by", ""), "at": note.get("at", ""), "digest": note["digest"]}
                              for item, note in sorted(notes.items()) if isinstance(note, dict) and note.get("digest")}
                             if isinstance(notes, dict) else {})
    old["settings_rest"] = raw                  # чего сервис в этом файле не знает — в базу как есть
    raw = _json(state / "brand" / "brand.json")
    old["brand"] = dict(_dict(raw, state / "brand" / "brand.json", "содержимое")) if raw is not None else None

    # ----- заявки -----
    raw = _json(state / "requests.json")
    claims = _dict(raw, state / "requests.json", "содержимое").get("requests", []) if raw is not None else []
    old["requests"] = []
    for claim in claims:
        unknown = set(_dict(claim, state / "requests.json", "заявка")) - CLAIM_FIELDS
        if unknown:
            raise db.Refusal(f"файл {state / 'requests.json'}: у заявки поля, которых сервис не знает "
                             f"({', '.join(sorted(unknown))}). Перенос в базу остановлен, ничего не изменено")
        fresh = {k: claim.get(k, "") for k in ("id", "at", "name", "email", "login", "comment")}
        fresh["status"] = claim.get("status", signup.PENDING)
        if fresh["status"] != signup.PENDING:
            fresh.update(decided_at=claim.get("decided_at"), decided_by=claim.get("decided_by"))
        old["requests"].append(fresh)

    # ----- журнал обработки -----
    raw = _json(outbox / "_state" / "processed.json")
    files = _dict(raw, outbox / "_state" / "processed.json", "содержимое").get("files", []) if raw is not None else []
    old["records"] = {}
    for rec in files:
        if isinstance(rec, dict) and rec.get("file"):
            old["records"][rec["file"]] = rec
        else:
            old["skipped"]["строк журнала обработки без пути записи"] = old["skipped"].get(
                "строк журнала обработки без пути записи", 0) + 1

    # ----- отметки поручений: файл рядом с протоколом записи -----
    old["marks"], old["marks_at"] = {}, {}
    for file, rec in sorted(old["records"].items()):
        stem = pipeline.stem_of(cfg.inbox, file, rec)
        path = outbox / Path(file).parent / stem / f"{stem}{TASKS_SUFFIX}"
        saved = _json(path)
        if saved is None:
            continue
        for k, mark in _dict(saved, path, "содержимое").items():
            unknown = set(_dict(mark, path, "отметка")) - MARK_FIELDS
            if unknown or not isinstance(mark.get("task"), str):
                raise db.Refusal(f"файл {path}: отметка не того вида, что пишет сервис"
                                 + (f" (поля {', '.join(sorted(unknown))})" if unknown else "")
                                 + ". Перенос в базу остановлен, ничего не изменено")
        old["tasks_files"].append(path.relative_to(outbox).as_posix())
        old["marks_at"][file] = path.stat().st_mtime
        if saved:
            old["marks"][file] = {k: {**{f: mark[f] for f in marks.COLUMNS if mark.get(f) is not None},
                                      "who": mark.get("who"), "task": mark["task"]}
                                  for k, mark in sorted(saved.items())}

    # ----- журнал событий -----
    path = state / "journal.jsonl"
    old["events"], torn = [], 0
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError) as e:
            raise db.Refusal(f"файл {path} не читается ({e}). Перенос в базу остановлен, ничего не изменено") from e
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            # оборванная строка — след падения посреди записи; остальное цело
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                torn += 1
                continue
            if not isinstance(event, dict) or not isinstance(event.get("at"), str) or not isinstance(event.get("kind"), str) \
                    or (event["kind"] == "run" and not isinstance(event.get("file"), str)):
                raise db.Refusal(f"файл {path}, строка {number}: событие не того вида, что пишет сервис. Перенос в "
                                 "базу остановлен, ничего не изменено")
            old["events"].append(_money(event) if event["kind"] == "run"
                                 else {"at": event["at"], "kind": event["kind"], "by": event.get("by") or "",
                                       **{k: v for k, v in event.items() if k not in ("at", "kind", "by")}})
    else:
        # Журнала событий ещё не было: расходы, набежавшие до него, — по строке на запись, датой
        # последнего прогона. Когда и сколько стоил каждый прежний прогон, уже не узнать.
        for rec in sorted(old["records"].values(), key=lambda r: r.get("finished_at") or ""):
            cost = usage.amounts(rec.get("cost_total") or rec.get("cost"))
            old["events"].append(_money({
                "at": rec.get("finished_at") or journal.now_iso(), "kind": "run", "by": "",
                "file": rec["file"], "uploaded_by": "", "reason": journal.EARLIER,
                "status": rec.get("status"), "started_at": rec.get("started_at"),
                "audio_sec": rec.get("audio_duration_sec"), "cost": cost, "runs": int(rec.get("runs") or 1),
                # прогоны до журнала шли через единственного тогда поставщика
                "stages": [{"stage": journal.EARLIER, "model": "", "provider": usage.LEGACY_PROVIDER,
                            "currency": cur, "cost": v} for cur, v in cost.items()],
                "providers": [{"provider": usage.LEGACY_PROVIDER, "currency": cur, "cost": v}
                              for cur, v in cost.items()]}))
        old["adopted"] = len(old["events"])
    if torn:
        old["skipped"]["оборванных строк журнала событий"] = torn
    return old


def spent(events: list[dict]) -> dict[str, float]:
    """Итог расходов по валютам — по прогонам журнала событий."""
    total: dict[str, float] = {}
    for event in events:
        if event["kind"] == "run":
            total = usage.plus(total, usage.amounts(event.get("cost")))
    return total


def counts(old: dict) -> dict:
    """Числа для сверки и для журнала работы — по прочитанному из файлов."""
    return {"пользователей": len(old["users"]),
            "прав на папки": sum(len(u[kind]) for u in old["users"].values() for kind in users.KINDS),
            "ключей доступа": len(old["keys"]),
            "словарей": len(old["dictionaries"]),
            "заявок": len(old["requests"]),
            "записей в журнале обработки": len(old["records"]),
            "отметок и сроков поручений": sum(len(m) for m in old["marks"].values()),
            "событий": len(old["events"]),
            "из них прогонов": sum(1 for e in old["events"] if e["kind"] == "run"),
            "расходы": spent(old["events"])}


def counted(base: db.Database) -> dict:
    """Те же числа — запросами к базе."""
    one = base.value
    money: dict[str, float] = {}            # тем же сложением, что и отчёт: по порядку прогонов
    for row in base.rows("SELECT currency, cost FROM run_money ORDER BY id, currency"):
        money = usage.plus(money, {row["currency"]: row["cost"]})
    return {"пользователей": one("SELECT count(*) FROM users"),
            "прав на папки": one("SELECT count(*) FROM grants"),
            "ключей доступа": one("SELECT count(*) FROM keys"),
            "словарей": one("SELECT count(*) FROM dictionaries"),
            "заявок": one("SELECT count(*) FROM requests"),
            "записей в журнале обработки": one("SELECT count(*) FROM records"),
            "отметок и сроков поручений": one("SELECT count(*) FROM marks"),
            "событий": one("SELECT count(*) FROM events"),
            "из них прогонов": one("SELECT count(*) FROM runs"),
            "расходы": money}


def _fill(base: db.Database, old: dict) -> None:
    """Всё прежнее состояние — в базу, одной транзакцией."""
    people, trail, book = users.Users(base), journal.Journal(base), ledger.Ledger(base)
    with base.write() as con:
        for login, user in old["users"].items():
            people._store(con, login, user)
            con.executemany("INSERT INTO grants (login, kind, folder, scope) VALUES (?, ?, ?, ?)",
                            [(login, kind, folder, scope) for kind in users.KINDS for folder, scope in user[kind].items()])
        con.executemany(f"INSERT INTO keys ({', '.join(KEY_FIELDS)}) VALUES ({', '.join('?' * len(KEY_FIELDS))})",
                        [tuple(key[f] for f in KEY_FIELDS) for key in old["keys"]])
        con.executemany(
            "INSERT INTO dictionaries (scope, path, people, terms, author, at, changed) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(scope, path, json.dumps(d["people"], ensure_ascii=False), json.dumps(d["terms"], ensure_ascii=False),
              d["by"], d["at"], d["changed"]) for (scope, path), d in old["dictionaries"].items()])
        rows = [(key, json.dumps(value, ensure_ascii=False), "", "") for key, value in old["settings_rest"].items()]
        if old["speech_model"]:
            chosen = old["speech_model"]
            rows.append((settings.SPEECH_MODEL, json.dumps(chosen["value"], ensure_ascii=False), chosen["by"], chosen["at"]))
        if old["brand"] is not None:
            look = old["brand"]
            rows.append((brand.KEY, json.dumps({k: v for k, v in look.items() if k not in ("by", "at")},
                                               ensure_ascii=False), look.get("by") or "", look.get("at") or ""))
        con.executemany("INSERT INTO settings (key, value, author, at) VALUES (?, ?, ?, ?)", rows)
        con.executemany("INSERT INTO template_notes (item, author, at, digest) VALUES (?, ?, ?, ?)",
                        [(item, note["by"], note["at"], note["digest"]) for item, note in old["template_notes"].items()])
        con.executemany(
            "INSERT INTO requests (id, at, name, email, login, comment, status, decided_at, decided_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(c["id"], c["at"], c["name"], c["email"], c["login"], c["comment"], c["status"], c.get("decided_at"),
              c.get("decided_by")) for c in old["requests"]])
        for record in old["records"].values():
            book._store(con, record)
        noted = marks.Store(base)
        for file, saved in old["marks"].items():
            noted._write(con, file, saved)
        # время правки отметок — время прежнего файла: и у тех записей, где все отметки сняты
        con.executemany("UPDATE records SET marks_at = ? WHERE file = ?",
                        [(at, file) for file, at in old["marks_at"].items()])
        for event in old["events"]:
            trail._insert(con, event)


def _verify(base: db.Database, old: dict) -> dict:
    """Сверка: всё, что легло в базу, читается из неё обратно таким же, каким пришло из файлов.
    Расхождение — db.Refusal. Возвращает числа для журнала работы."""
    def same(what: str, was, now) -> None:
        if was != now:
            raise db.Refusal(f"сверка после переноса не сошлась: {what}. Перенос в базу отменён, прежние файлы "
                             "на месте — сообщите автору сервиса")

    people, trail = users.Users(base), journal.Journal(base)
    same("пользователи и права", old["users"], people.users)
    same("ключи доступа", old["keys"],
         [{f: row[f] for f in KEY_FIELDS} for row in base.rows("SELECT * FROM keys ORDER BY n")])
    words = dictionary.Store(base)
    same("словари", old["dictionaries"],
         {(row["scope"], row["path"]): words.get(row["scope"], row["path"])
          for row in base.rows("SELECT scope, path FROM dictionaries ORDER BY scope, path")})
    options = settings.Store(base)
    same("модель распознавания, выбранная на странице",
         old["speech_model"] and (old["speech_model"] if old["speech_model"]["value"] in settings.speech.ENGINES else None),
         options.speech_model())
    same("отметки о шаблонах", old["template_notes"], options.template_notes())
    if old["brand"] is not None:
        row = base.row("SELECT value, author, at FROM settings WHERE key = ?", brand.KEY)
        same("оформление", {k: v for k, v in old["brand"].items() if k not in ("by", "at")}, json.loads(row["value"]))
    same("заявки", old["requests"], signup.Requests(base).all())
    same("журнал обработки", old["records"], ledger.Ledger(base).records)
    same("отметки и сроки поручений", old["marks"], marks.Store(base).everything())
    same("события журнала", old["events"], trail.read())
    # отчёт о расходах: пути прогонов, которые база вела при записи событий, против тех же
    # событий, проигранных по порядку
    for bucket in ("month", "day"):
        same(f"отчёт о расходах ({bucket})", journal.report(old["events"], bucket=bucket), trail.report(bucket=bucket))
    same("авторы загрузок", _uploaded(old["events"]), trail.uploaded)
    before, after = counts(old), counted(base)
    same(f"итоги ({before} и {after})", before, after)
    # и независимо от порядка сложения: сумма по валюте одним запросом — та же до копейки
    for row in base.rows("SELECT currency, sum(cost) AS total FROM run_money GROUP BY currency"):
        if abs(row["total"] - before["расходы"].get(row["currency"], 0.0)) >= 0.005:
            same(f"расходы в {row['currency']}: {before['расходы'].get(row['currency'])} и {row['total']}", 0, 1)
    return after


def _uploaded(events: list[dict]) -> dict[str, str]:
    """Кто загрузил запись, лежащую сейчас по этому пути, — проигрыванием событий по порядку."""
    found: dict[str, str] = {}
    for event in events:
        kind = event["kind"]
        if kind == "upload":
            found[event["file"]] = event.get("by", "")
        elif kind == "folder_rename":
            found = {journal._moved(f, event["folder"], event["to"]): by for f, by in found.items()}
        elif kind in journal.RECORD_MOVES and event["file"] in found:
            found[event["to"]] = found.pop(event["file"])
        elif kind == "record_delete":
            found.pop(event["file"], None)
    return found


def bring(cfg, log) -> None:
    """Перенос прежних файлов состояния в новую базу. Сбой — db.Refusal; базы тогда нет, файлы на месте."""
    state = Path(cfg.state_dir)
    started = time.perf_counter()
    sources = _present(cfg)
    log("первый запуск версии с базой: переношу состояние из файлов (" + ", ".join(name for _, name in sources)
        + f") в {state / db.FILE}")
    old = read_legacy(cfg)
    sources += [("outbox", name) for name in old["tasks_files"]]
    work = state / WORK
    base = None
    try:
        work.mkdir()
        base = db.Database(work, log)
        _fill(base, old)
        found = _verify(base, old)
        base.check()
        today = datetime.date.today().isoformat()
        folder = next(name for name in (f"{KEEP}{today}" + (f"-{n}" if n > 1 else "") for n in range(1, 1000))
                      if not (state / name).exists())
        base.note("created", f"{journal.now_iso()} · {about.version() or 'без номера версии'}")
        base.note("legacy", json.dumps({"at": journal.now_iso(), "folder": folder, "moved": False, "files": sources,
                                        "counts": found, "skipped": old["skipped"],
                                        "adopted": old.get("adopted", 0)}, ensure_ascii=False))
        # одним файлом, без спутников журнала: на место он встаёт одним переименованием
        base.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        base.con.execute("PRAGMA journal_mode=DELETE")
        base.close()
        base = None
        os.replace(work / db.FILE, state / db.FILE)
    except db.Refusal:
        raise
    except Exception as e:      # что бы ни случилось — базы нет, прежние файлы на месте
        raise db.Refusal(f"перенос состояния в базу не удался: {e!r}. База не создана, прежние файлы на месте — "
                         "сервис не запущен. Устраните причину и запустите снова") from e
    finally:
        if base is not None:
            base.close()
        if work.exists():
            shutil.rmtree(work)
    money = usage.text(found["расходы"]) or "0"
    log("перенос в базу завершён за " + f"{time.perf_counter() - started:.1f} с, сверка сошлась: "
        + ", ".join(f"{name} — {value}" for name, value in found.items() if name != "расходы")
        + f"; расходы по журналу — {money} (до и после переноса совпадают)"
        + "".join(f"; пропущено {name} — {n}" for name, n in old["skipped"].items())
        + (f"; журнала расходов не было — прежние расходы перенесены по строке на запись ({old['adopted']})"
           if old.get("adopted") else ""))


def _finish(base: db.Database, cfg, log) -> None:
    """Прежние файлы — в папку «до-базы-<дата>». Доделывает и перенос, оборванный на этом шаге."""
    noted = base.noted("legacy")
    plan = json.loads(noted) if noted else None
    if not plan or plan["moved"]:
        return
    roots = {"state": Path(cfg.state_dir), "outbox": Path(cfg.outbox)}
    keep = roots["state"] / plan["folder"]
    try:
        keep.mkdir(parents=True, exist_ok=True)
        for where, name in plan["files"]:
            source = roots[where] / name
            target = keep / name if where == "state" else keep / "outbox" / name
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
        (keep / NOTE).write_text(
            f"Файлы состояния сервиса, какими они были до переноса в базу ({plan['at']}).\n"
            f"Сервис их больше не читает: всё это теперь лежит в базе {db.FILE} в папке состояния.\n"
            "Папка нужна только на случай отката на прежнюю версию сервиса (install/README.md, раздел 13).\n"
            "В ней хеши паролей и отпечатки ключей доступа: храните её так же закрыто, как саму базу,\n"
            "а когда убедитесь, что новая версия работает, — удалите.\n"
            "В подпапке outbox — файлы, лежавшие в папке результатов: журнал обработки и отметки поручений.\n",
            encoding="utf-8")
        for leftover in (roots["outbox"] / "_state",):
            try:
                leftover.rmdir()                # пустая — больше не нужна
            except OSError:
                pass
    except OSError as e:
        raise db.Refusal(f"прежние файлы состояния не удалось убрать в {keep}: {e}. База уже собрана и цела; "
                         "устраните причину и запустите сервис снова — он доделает перенос файлов") from e
    base.note("legacy", json.dumps({**plan, "moved": True}, ensure_ascii=False))
    log(f"прежние файлы состояния убраны в {keep} — копия на случай отката, сервис их больше не читает")
