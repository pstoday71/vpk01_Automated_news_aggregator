"""Точка входа сервиса агрегатора новостей.

    python main.py             — API (FastAPI) + планировщик в одном процессе
    python main.py --no-api    — только фоновый планировщик (сбор + дайджест)
"""

from __future__ import annotations

import argparse
import logging
import threading

import uvicorn

from config import setup_logging
from scheduler.jobs import create_blocking_scheduler

log = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Агрегатор новостей")
    parser.add_argument("--no-api", action="store_true",
                        help="Запускать только планировщик без FastAPI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    setup_logging()
    if args.no_api:
        log.info("Запуск планировщика без API")
        from bot.bot import start_poller
        from bot.cli import run_collect_if_empty

        run_collect_if_empty()
        scheduler = create_blocking_scheduler()
        stop = threading.Event()
        start_poller(stop)
        try:
            scheduler.start()  # блокирует выполнение до Ctrl+C
        except (KeyboardInterrupt, SystemExit):
            stop.set()
            log.info("Остановка по запросу пользователя")
    else:
        # Планировщик поднимается в lifespan приложения (api/main.py)
        log.info("Запуск FastAPI + планировщика: http://%s:%d", args.host, args.port)
        uvicorn.run("api.main:app", host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
