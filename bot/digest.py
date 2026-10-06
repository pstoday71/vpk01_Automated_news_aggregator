"""Формирование дайджеста: топ-N из каждого источника с общим лимитом."""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

from config import config

TELEGRAM_MESSAGE_LIMIT = 4096

DIGEST_HEADER = "📰 Дайджест новостей за {date}"


def is_digest_time_allowed(now: datetime | None = None) -> bool:
    """Разрешена ли отправка дайджеста в текущий час (окно DIGEST_FROM/UNTIL_HOUR).

    Поддерживает и «ночное» окно вида 22–8 (from >= until): такое окно
    проходит с from до полуночи и с полуночи до until.
    """
    try:
        tz = ZoneInfo(config.timezone_name)
    except Exception:
        tz = ZoneInfo("UTC")
    now = now or datetime.now(tz)
    hour = now.hour
    frm, until = config.digest_from_hour, config.digest_until_hour
    if frm == until:  # 0–0 или одинаковые — всегда разрешено
        return True
    if frm < until:
        return frm <= hour < until
    return hour >= frm or hour < until


def dedup_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Убирает повторы внутри одного дайджеста (по URL)."""
    seen: set[str] = set()
    unique: list[sqlite3.Row] = []
    for row in rows:
        key = (row["url"] or "").strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def select_top_items(
    rows: list[sqlite3.Row],
    min_per_source: int = 1,
    max_per_source: int = 3,
    max_total: int = 30,
) -> list[sqlite3.Row]:
    """Отбирает топовые новости: по 1–3 с каждого источника, общий лимит.

    rows ожидаются отсортированными от свежих к старым.
    При обрезке по общему лимиту сначала режутся новости сверх минимума,
    минимум (1 на источник) сохраняется, пока хватает лимита.
    """
    unique = dedup_rows(rows)
    by_source: "OrderedDict[str, list[sqlite3.Row]]" = OrderedDict()
    for row in unique:
        by_source.setdefault(row["source"], []).append(row)

    selected: dict[int, sqlite3.Row] = {}
    for items in by_source.values():
        for row in items[:max_per_source]:
            selected[row["id"]] = row

    if len(selected) > max_total:
        protected: set[int] = set()
        for items in by_source.values():
            for row in items[: min(max(min_per_source, 0), len(items))]:
                protected.add(row["id"])
        rest = sorted(
            (row for rid, row in selected.items() if rid not in protected),
            key=_sort_key,
            reverse=True,
        )
        keep = protected | {row["id"] for row in rest[: max(max_total - len(protected), 0)]}
        if len(keep) > max_total:
            # Защищённых больше, чем помещается в лимит — оставляем самые свежие
            protected_rows = sorted(
                (selected[rid] for rid in protected), key=_sort_key, reverse=True
            )
            keep = {row["id"] for row in protected_rows[:max_total]}
        selected = {rid: row for rid, row in selected.items() if rid in keep}

    return sorted(selected.values(), key=_sort_key, reverse=True)


def _sort_key(row: sqlite3.Row) -> tuple[str, int]:
    return (row["created_at"] or "", row["id"])


def _row_line(row: sqlite3.Row) -> str:
    title = row["title"]
    url = row["url"]
    date = (row["created_at"] or "")[:10]
    line = f"• <a href=\"{url}\">{title}</a>"
    if date:
        line += f" ({date})"
    return line


def build_digest(
    rows: list[sqlite3.Row],
    min_per_source: int = 1,
    max_per_source: int = 3,
    max_total: int = 30,
) -> str | None:
    """Собирает текст дайджеста. None — если нет материалов в окне."""
    if not rows:
        return None
    selected = select_top_items(
        rows,
        min_per_source=min_per_source,
        max_per_source=max_per_source,
        max_total=max_total,
    )
    if not selected:
        return None

    grouped: "OrderedDict[str, list[sqlite3.Row]]" = OrderedDict()
    for row in selected:
        grouped.setdefault(row["source"], []).append(row)

    window_runs = sorted({row["run_id"] for row in rows if row["run_id"]})
    run_label = (
        f"сборы №{window_runs[0]}–№{window_runs[-1]}"
        if len(window_runs) > 1
        else (f"сбор №{window_runs[0]}" if window_runs else "ручной запуск")
    )
    times = sorted((row["created_at"] or "") for row in rows if row["created_at"])
    period = f"{times[0][:16].replace('T', ' ')} — {times[-1][:16].replace('T', ' ')}"

    parts: list[str] = [
        DIGEST_HEADER.format(date=datetime.now().strftime("%d.%m.%Y")),
        f"Окно: {run_label} · период {period}",
        "",
    ]
    for source, items in grouped.items():
        parts.append(f"<b>{source}</b> — {len(items)} шт.")
        for row in items:
            parts.append(_row_line(row))
        parts.append("")
    parts.append(
        f"В дайджесте: {len(selected)} из {len(rows)} новых "
        f"(источников: {len(grouped)})"
    )
    return "\n".join(parts)


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Разбивает длинный текст на части по лимиту Telegram (4096 символов)."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current))
    return chunks
