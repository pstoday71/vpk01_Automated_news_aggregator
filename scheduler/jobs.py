"""Фоновые задачи: периодический сбор и дайджест (APScheduler).

Основной режим (DIGEST_EVERY_N_COLLECTS > 0): дайджест отправляется сразу после
каждого N-го сбора — см. bot.cli.run_collect → _maybe_send_digest, но только
в «окне времени» DIGEST_FROM_HOUR–DIGEST_UNTIL_HOUR. Ночью дайджест откладывается
(флаг digest_deferred) и отправляется задачей job_digest_flush при открытии окна.
Резервный режим (DIGEST_EVERY_N_COLLECTS = 0): дайджест по DIGEST_HOUR:DIGEST_MINUTE.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot.cli import run_collect, run_digest
from config import config
from database import Repository
from database.db import KEY_DIGEST_DEFERRED

log = logging.getLogger(__name__)

_scheduler = None  # привязывается при старте (нужно для перенастройки из бота)
JOB_COLLECT = "collect"
JOB_DIGEST = "digest"
JOB_FLUSH = "digest_flush"


def job_collect() -> None:
    log.info("Запуск планового сбора данных")
    try:
        run_collect()  # внутри — регистрация сбора и, при необходимости, дайджест
    except Exception:
        log.exception("Плановый сбор завершился ошибкой")


def job_digest() -> None:
    log.info("Запуск плановой отправки дайджеста")
    try:
        run_digest()
    except Exception:
        log.exception("Плановая отправка дайджеста завершилась ошибкой")


def job_digest_flush() -> None:
    """Отправка отложенного (ночного) дайджеста после открытия окна времени."""
    try:
        repo = Repository()
        if repo.get_meta(KEY_DIGEST_DEFERRED) == "1":
            log.info("Окно отправки открыто — отправляю отложенный дайджест")
            run_digest(repo)
    except Exception:
        log.exception("Сбой отправки отложенного дайджеста")


def bind_scheduler(scheduler) -> None:
    """Сохраняет ссылку на планировщик — используется при настройках из бота."""
    global _scheduler
    _scheduler = scheduler


def reschedule_collect(minutes: int) -> None:
    if _scheduler is not None:
        _scheduler.reschedule_job(
            JOB_COLLECT, trigger=IntervalTrigger(minutes=max(1, minutes))
        )
        log.info("Интервал сбора изменён: %d мин", minutes)


def reschedule_digest_flush() -> None:
    if _scheduler is not None:
        _scheduler.reschedule_job(
            JOB_FLUSH, trigger=CronTrigger(hour=config.digest_from_hour, minute=0)
        )
        log.info("Окно дайджеста изменено: открытие %02d:00 (%s)",
                 config.digest_from_hour, config.timezone_name)


def _add_jobs(scheduler) -> None:
    scheduler.add_job(
        job_collect,
        IntervalTrigger(minutes=max(1, config.collect_interval_min)),
        id=JOB_COLLECT, max_instances=1, coalesce=True, replace_existing=True,
    )
    if config.digest_every_n_collects > 0:
        scheduler.add_job(
            job_digest_flush,
            CronTrigger(hour=config.digest_from_hour, minute=0),
            id=JOB_FLUSH, max_instances=1, coalesce=True, replace_existing=True,
        )
        log.info(
            "Сбор каждые %d мин; дайджест после каждого %d-го сбора "
            "(окно %02d:00–%02d:00 %s, топ %d–%d с источника, лимит %d; ночной — откладывается)",
            config.collect_interval_min,
            config.digest_every_n_collects,
            config.digest_from_hour,
            config.digest_until_hour,
            config.timezone_name,
            config.digest_min_per_source,
            config.digest_max_per_source,
            config.digest_max_items,
        )
    else:
        scheduler.add_job(
            job_digest,
            CronTrigger(hour=config.digest_hour, minute=config.digest_minute),
            id=JOB_DIGEST, max_instances=1, coalesce=True, replace_existing=True,
        )
        log.info(
            "Сбор каждые %d мин; дайджест по расписанию в %02d:%02d (резервный режим)",
            config.collect_interval_min, config.digest_hour, config.digest_minute,
        )


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=config.timezone_name)
    _add_jobs(scheduler)
    bind_scheduler(scheduler)
    return scheduler


def create_blocking_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=config.timezone_name)
    _add_jobs(scheduler)
    bind_scheduler(scheduler)
    return scheduler