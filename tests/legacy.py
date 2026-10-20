"""Общее для тестов переноса: установка прежнего вида (состояние файлами) и её первый запуск с базой."""
import json
import types

from app import db, migrate


def setup(tmp_path):
    """Папки установки: состояние, записи, результаты."""
    cfg = types.SimpleNamespace(state_dir=tmp_path / "state", inbox=tmp_path / "inbox", outbox=tmp_path / "outbox")
    for folder in (cfg.state_dir, cfg.inbox, cfg.outbox):
        folder.mkdir(parents=True, exist_ok=True)
    return cfg


def put(path, data) -> None:
    """Файл прежнего вида: JSON или, для журнала событий, по строке JSON на событие."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in data) if path.suffix == ".jsonl"
            else json.dumps(data, ensure_ascii=False))
    path.write_text(text, encoding="utf-8")


def start(cfg, lines=None, monkeypatch=None):
    """Первый (или очередной) запуск версии с базой: как делает сервис. Проверку блокировок —
    она запускает второй процесс — здесь не гоняем: у неё свой тест."""
    real = db.probe
    db.probe = lambda path: None
    try:
        return migrate.open_base(cfg, (lines if lines is not None else []).append, True)
    finally:
        db.probe = real
