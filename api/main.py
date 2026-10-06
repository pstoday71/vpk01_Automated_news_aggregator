"""FastAPI-приложение: просмотр собранных данных и ручной запуск операций.

Запуск:  uvicorn api.main:app  (или python main.py — API + планировщик вместе)
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query

from bot.cli import run_collect, run_collect_if_empty, run_digest
from bot.digest import build_digest, select_top_items
from collectors import SOURCES
from config import config, setup_logging
from database import Repository
from scheduler.jobs import create_scheduler

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    from bot.bot import start_poller
    from scheduler.jobs import create_scheduler

    run_collect_if_empty()
    stop_poller = threading.Event()
    start_poller(stop_poller)
    scheduler = create_scheduler()
    scheduler.start()
    log.info("Планировщик и Telegram-поллер запущены (lifespan)")
    try:
        yield
    finally:
        stop_poller.set()
        scheduler.shutdown(wait=False)
        log.info("Планировщик остановлен")


app = FastAPI(
    title="News Aggregator",
    description="Агрегатор новостей: сбор из RSS, SQLite, дайджест в Telegram",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/sources")
def sources() -> list[dict]:
    return [{"name": s.name, "url": s.url, "kind": s.kind} for s in SOURCES]


@app.get("/items")
def items(
    source: str | None = Query(default=None),
    new_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    rows = Repository().get_items(source=source, new_only=new_only, limit=limit)
    return [dict(row) for row in rows]


@app.get("/stats")
def stats() -> dict:
    return Repository().stats()


@app.post("/items/collect")
def collect() -> dict:
    saved, skipped = run_collect()
    return {"saved": saved, "skipped_duplicates": skipped}


@app.post("/digest")
def digest() -> dict:
    """Формирует дайджест и отправляет его (Telegram или консоль)."""
    text, delivered = run_digest()
    if text is None:
        return {"sent": False, "reason": "no_new_items"}
    channel = "telegram" if delivered else "console"
    return {"sent": True, "channel": channel, "preview": text[:500]}


@app.get("/digest/preview")
def digest_preview() -> dict:
    """Предпросмотр дайджеста без отправки и без пометки прочитанными."""
    repo = Repository()
    window = repo.get_digest_window()
    kwargs = dict(
        min_per_source=config.digest_min_per_source,
        max_per_source=config.digest_max_per_source,
        max_total=config.digest_max_items,
    )
    selected = select_top_items(window, **kwargs)
    text = build_digest(window, **kwargs)
    return {
        "window_items": len(window),
        "selected": len(selected),
        "runs_since_digest": repo.runs_since_last_digest(),
        "text": text,
    }
