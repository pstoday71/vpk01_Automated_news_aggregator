"""Репозиторий: сборы, сохранение с дедупликацией, окно выборки для дайджеста."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .db import KEY_LAST_DIGEST_RUN, get_connection, init_db

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Item:
    title: str
    url: str
    source: str
    description: str = ""
    created_at: str = ""

    def normalized(self) -> "Item":
        created = self.created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        return Item(
            title=self.title.strip(),
            url=self.url.strip(),
            source=self.source.strip(),
            description=(self.description or "").strip(),
            created_at=created,
        )


class Repository:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path
        init_db(db_path)

    # ------------------------------------------------------------------ сборы
    def start_run(self) -> int:
        """Регистрирует начало нового сбора, возвращает его id."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with get_connection(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO collections (started_at) VALUES (?)", (now,)
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, saved: int, skipped: int) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE collections SET finished_at = ?, new_items = ?, skipped = ? WHERE id = ?",
                (now, saved, skipped, run_id),
            )

    def max_run_id(self) -> int:
        with get_connection(self.db_path) as conn:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM collections").fetchone()
        return int(row["m"])

    def get_last_digest_run_id(self) -> int:
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (KEY_LAST_DIGEST_RUN,)
            ).fetchone()
        return int(row["value"]) if row else 0

    def set_last_digest_run_id(self, run_id: int) -> None:
        with get_connection(self.db_path) as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (KEY_LAST_DIGEST_RUN, str(run_id)),
            )

    def runs_since_last_digest(self) -> int:
        """Сколько сборов произошло с момента последнего дайджеста."""
        return max(0, self.max_run_id() - self.get_last_digest_run_id())

    # ------------------------------------------------------------- meta-флаги
    def get_meta(self, key: str, default: str = "0") -> str:
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with get_connection(self.db_path) as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ------------------------------------------------------------- сохранение
    def insert_items(self, items: Iterable[Item], run_id: int = 0) -> tuple[int, int]:
        """Сохраняет элементы, пропуская дубликаты.

        Два уровня защиты:
          1. UNIQUE(url) и UNIQUE(title, source) на уровне SQLite;
          2. предварительная проверка существующих записей.
        Дубликат, повторно увиденный в новом сборе, остаётся привязанным к
        первому сбору — в дайджесте он появится максимум один раз.
        Возвращает (сохранено, пропущено_дубликатов).
        """
        saved = 0
        skipped = 0
        with get_connection(self.db_path) as conn:
            for raw in items:
                item = raw.normalized()
                if not item.title or not item.url:
                    skipped += 1
                    continue
                if self._exists(conn, item):
                    skipped += 1
                    continue
                try:
                    conn.execute(
                        """
                        INSERT INTO items (title, url, source, description, created_at, run_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (item.title, item.url, item.source, item.description,
                         item.created_at, run_id),
                    )
                    saved += 1
                except sqlite3.IntegrityError:
                    skipped += 1
        if saved or skipped:
            log.info("Сохранено новых: %d, пропущено дубликатов: %d", saved, skipped)
        return saved, skipped

    @staticmethod
    def _exists(conn: sqlite3.Connection, item: Item) -> bool:
        row = conn.execute(
            "SELECT 1 FROM items WHERE url = ? OR (title = ? AND source = ?) LIMIT 1",
            (item.url, item.title, item.source),
        ).fetchone()
        return row is not None

    # -------------------------------------------------------------- дайджест
    def get_digest_window(self) -> list[sqlite3.Row]:
        """Новые материалы окна: всё, что собрано после последнего дайджеста.

        При настройке «дайджест каждые 2 сбора» окно = ровно два последних сбора.
        """
        last = self.get_last_digest_run_id()
        with get_connection(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM items
                WHERE run_id > ? AND is_read = 0
                ORDER BY created_at DESC, id DESC
                """,
                (last,),
            ).fetchall()
        return list(rows)

    def mark_read(self, ids: Sequence[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with get_connection(self.db_path) as conn:
            conn.execute(
                f"UPDATE items SET is_read = 1 WHERE id IN ({placeholders})", list(ids)
            )

    def count_unsent(self) -> int:
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM items WHERE is_read = 0 AND run_id > 0"
            ).fetchone()
        return int(row["c"])

    # --------------------------------------------------------------- прочее
    def get_items(self, source: str | None = None, new_only: bool = False,
                  limit: int = 50) -> list[sqlite3.Row]:
        query = "SELECT * FROM items WHERE 1=1"
        params: list[object] = []
        if source:
            query += " AND source = ?"
            params.append(source)
        if new_only:
            query += " AND is_read = 0 AND run_id > 0"
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with get_connection(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return list(rows)

    def stats(self) -> dict[str, int]:
        with get_connection(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
            runs = conn.execute("SELECT COUNT(*) AS c FROM collections").fetchone()["c"]
            by_source = conn.execute(
                "SELECT source, COUNT(*) AS c FROM items GROUP BY source ORDER BY c DESC"
            ).fetchall()
        return {
            "total": int(total),
            "unsent": self.count_unsent(),
            "runs": int(runs),
            "last_digest_run": self.get_last_digest_run_id(),
            **{row["source"]: int(row["c"]) for row in by_source},
        }
