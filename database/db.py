"""Подключение к SQLite и создание схемы."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from config import config

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    source      TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    is_read     INTEGER NOT NULL DEFAULT 0,
    run_id      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (url)
);

-- Каждый запуск сборщика — отдельный «сбор»; окно дайджеста = последние N сборов
CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    new_items   INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0
);

-- Ключ-значение: маркеры состояния (например, последний сбор в дайджесте)
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items (source);
CREATE INDEX IF NOT EXISTS idx_items_read ON items (is_read);
"""

# Правило дедупликации «title + source» (помимо UNIQUE(url))
DEDUP_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_title_source
ON items (title, source);
"""

KEY_LAST_DIGEST_RUN = "last_digest_run_id"
KEY_DIGEST_DEFERRED = "digest_deferred"


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or config.db_path)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Миграция старых баз: появилась колонка run_id и таблицы collections/meta."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if "run_id" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN run_id INTEGER NOT NULL DEFAULT 0")
        # Записи, собранные до появления учёта сборов, обрабатываются сразу:
        # они уже были показаны/устарели — помечаем прочитанными, чтобы не попадали в окно
        cur = conn.execute("UPDATE items SET is_read = 1 WHERE run_id = 0")
        if cur.rowcount:
            log.info(
                "Миграция: помечено прочитанными %d старых записей без run_id", cur.rowcount
            )


def init_db(db_path: Path | None = None) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)
        # Индекс вводится отдельно: если старая база уже содержит дубли,
        # ошибка гасится и дедуп продолжает работать на уровне приложения.
        try:
            conn.execute(DEDUP_INDEX)
        except sqlite3.IntegrityError:
            pass
        _migrate(conn)
        # Только теперь колонка run_id гарантированно существует
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_run ON items (run_id)")
