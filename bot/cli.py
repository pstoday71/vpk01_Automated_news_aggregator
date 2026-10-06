"""CLI-команды: ручной сбор, дайджест, статистика, получение chat_id.

Примеры:
    python -m bot.cli collect
    python -m bot.cli digest
    python -m bot.cli stats
    python -m bot.cli chat-id
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading

import httpx

from collectors import SOURCES, RssCollector
from config import config, setup_logging
from database import Repository
from database.db import KEY_DIGEST_DEFERRED

from .digest import build_digest, is_digest_time_allowed, select_top_items
from .telegram import send_message

log = logging.getLogger(__name__)

# Сбор и дайджест сериализуются, чтобы исключить гонки при ручном/кнопочном запуске
_LOCK = threading.Lock()


def run_collect(repo: Repository | None = None) -> tuple[int, int]:
    """Один «сбор»: обходит все источники, сохраняет данные, регистрирует запуск.

    После N-го сбора с прошлого дайджеста автоматически отправляется дайджест
    (окно = эти N сборов). Ошибка одного источника не блокирует остальные.
    """
    repo = repo or Repository()
    with _LOCK:
        run_id = repo.start_run()
        total_saved = 0
        total_skipped = 0
        for source in SOURCES:
            try:
                items = RssCollector(source).collect()
            except Exception:
                log.exception("Сбой коллектора %s", source.name)
                continue
            saved, skipped = repo.insert_items(items, run_id=run_id)
            total_saved += saved
            total_skipped += skipped
        repo.finish_run(run_id, total_saved, total_skipped)
    log.info("Сбор №%d завершён: новых %d, дубликатов %d",
             run_id, total_saved, total_skipped)
    _maybe_send_digest(repo)
    return total_saved, total_skipped


def _maybe_send_digest(repo: Repository) -> None:
    """Авто-дайджест после N сборов, с учётом «ночного окна».

    Если окно отправки закрыто (например, ночь) — ставим флаг отложенного
    дайджеста: его отправит scheduler.jobs.job_digest_flush при открытии окна.
    """
    n = config.digest_every_n_collects
    if n <= 0:
        return  # резервный режим по времени (DIGEST_HOUR) — см. scheduler/jobs.py
    if repo.runs_since_last_digest() < n:
        return
    if is_digest_time_allowed():
        run_digest(repo)
    else:
        repo.set_meta(KEY_DIGEST_DEFERRED, "1")
        log.info(
            "Накоплено %d сбор(ов), но окно дайджеста закрыто (%02d:00–%02d:00, %s) — отложено",
            n, config.digest_from_hour, config.digest_until_hour, config.timezone_name,
        )


def run_collect_if_empty(repo: Repository | None = None) -> tuple[int, int]:
    """Первичный сбор при пустой базе — чтобы сервис сразу имел данные."""
    repo = repo or Repository()
    if repo.stats()["total"] == 0:
        log.info("База пуста — выполняется первичный сбор")
        return run_collect(repo)
    return 0, 0


def run_digest(repo: Repository | None = None,
               source: str | None = None) -> tuple[str | None, bool]:
    """Формирует дайджест из окна последних сборов и отправляет его.

    source — если задан, берётся только окно этого источника.
    Выборка: топ 1–3 новости с каждого источника, общий лимит DIGEST_MAX_ITEMS.
    Всё выбранное окно помечается обработанным (отправленное + отсечённое
    лимитами), маркер окна сдвигается — материал не повторится в следующих
    дайджестах. Возвращает (текст | None, доставлено_в_telegram).
    """
    repo = repo or Repository()
    with _LOCK:
        window = repo.get_digest_window()
        if source:
            window = [row for row in window if row["source"] == source]
        current_run = repo.max_run_id()
        if not window:
            log.info("Окно пусто (новых материалов с последнего дайджеста нет)")
            repo.set_last_digest_run_id(current_run)
            repo.set_meta(KEY_DIGEST_DEFERRED, "0")
            return None, False

        text = build_digest(
            window,
            min_per_source=config.digest_min_per_source,
            max_per_source=config.digest_max_per_source,
            max_total=config.digest_max_items,
        )
        if text is None:
            repo.mark_read([row["id"] for row in window])
            repo.set_last_digest_run_id(max(current_run, max(row["run_id"] for row in window)))
            repo.set_meta(KEY_DIGEST_DEFERRED, "0")
            return None, False

        delivered = send_message(text)
        repo.mark_read([row["id"] for row in window])
        last_run = max(current_run, max(row["run_id"] for row in window))
        repo.set_last_digest_run_id(last_run)
        repo.set_meta(KEY_DIGEST_DEFERRED, "0")
    log.info("Дайджест отправлен: окно %d материалов, сборы до №%d, канал=%s%s",
             len(window), last_run, "telegram" if delivered else "console",
             f", источник={source}" if source else "")
    return text, delivered


def run_source_digest(name: str, limit: int = 12) -> str | None:
    """Дайджест конкретного источника (кнопка в боте) — по последним материалам.

    Отправляет сводку и помечает показанные материалы обработанными
    (убирает из буфера), чтобы повторный вызов не дублировал то же самое.
    Возвращает отправленный текст или None.
    """
    name = next((s.name for s in SOURCES if s.name.lower() == name.strip().lower()),
                name)
    repo = Repository()
    rows = repo.get_items(source=name, limit=limit)
    kwargs = dict(
        min_per_source=1,
        max_per_source=min(config.digest_max_per_source, limit),
        max_total=min(config.digest_max_items, limit),
    )
    selected = select_top_items(rows, **kwargs)
    if not selected:
        log.info("По источнику %s нет материалов", name)
        return None
    text = build_digest(selected, **kwargs)
    with _LOCK:
        delivered = send_message(text)
        repo.mark_read([row["id"] for row in selected])
    log.info("Дайджест источника %s отправлен (%d шт., канал=%s, буфер очищен)",
             name, len(selected), "telegram" if delivered else "console")
    return text


def run_chat_id() -> int:
    """Определяет chat_id из входящих сообщений бота (Telegram getUpdates)."""
    if not config.telegram_bot_token:
        print("Не задан TELEGRAM_BOT_TOKEN в .env — сначала получите токен у @BotFather")
        return 1
    print("1) Убедитесь, что боту уже написали любое сообщение (или он добавлен в чат)")
    print("2) Запрашиваю chat_id через getUpdates...\n")
    try:
        response = httpx.get(
            f"https://api.telegram.org/bot{config.telegram_bot_token}/getUpdates",
            timeout=config.http_timeout_sec,
        )
        data = response.json()
    except httpx.HTTPError as exc:
        print(f"Ошибка запроса к Telegram API: {exc}")
        return 1
    if not data.get("ok"):
        print(f"Telegram API вернул ошибку: {data.get('description')}")
        return 1
    results = data.get("result") or []
    if not results:
        print("Нет входящих сообщений. Напишите боту /start и повторите команду.")
        return 1
    found = False
    chat_id: int | str | None = None
    for update in results:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat")
        if not chat:
            continue
        found = True
        chat_id = chat["id"]
        kind = {"private": "личный чат", "group": "группа", "supergroup": "супергруппа",
                "channel": "канал"}.get(chat.get("type"), chat.get("type"))
        print(f"chat_id: {chat_id}   ({kind}: {chat.get('title') or chat.get('username') or ''})")
    if not found:
        print("Сообщения не найдены. Напишите боту /start и повторите команду.")
        return 1
    print(f"\nДобавьте в .env: TELEGRAM_CHAT_ID={chat_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(prog="python -m bot.cli", description="Агрегатор новостей")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="Собрать данные из всех источников сейчас")
    sub.add_parser("digest", help="Сформировать и отправить дайджест")
    sub.add_parser("stats", help="Показать статистику базы")
    sub.add_parser("chat-id", help="Определить TELEGRAM_CHAT_ID для .env")
    sub.add_parser("bot", help="Запустить только Telegram-поллер (кнопки/команды)")
    args = parser.parse_args(argv)

    repo = Repository()
    if args.command == "collect":
        run_collect(repo)
    elif args.command == "digest":
        run_digest(repo)
    elif args.command == "stats":
        for key, value in repo.stats().items():
            print(f"{key}: {value}")
    elif args.command == "chat-id":
        return run_chat_id()
    elif args.command == "bot":
        from bot.bot import poller_loop

        poller_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
