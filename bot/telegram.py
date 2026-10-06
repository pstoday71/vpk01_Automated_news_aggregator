"""Отправка сообщений через Telegram Bot API (с fallback в консоль)."""

from __future__ import annotations

import logging

import httpx

from config import config

from .digest import split_message

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(text: str, chat_id: str | None = None, parse_mode: str = "HTML") -> bool:
    """Отправляет дайджест в Telegram.

    Если токен/чат не настроены — печатает в консоль (fallback для разработки).
    Возвращает True при успешной отправке.
    """
    target_chat = chat_id or config.telegram_chat_id
    if not config.telegram_bot_token or not target_chat:
        log.warning("Telegram не настроен — дайджест выведен в консоль")
        print("\n" + "=" * 40 + " DIGEST " + "=" * 40)
        print(text)
        print("=" * 88 + "\n")
        return False

    url = TELEGRAM_API.format(token=config.telegram_bot_token)
    delivered = True
    for chunk in split_message(text):
        try:
            response = httpx.post(
                url,
                json={"chat_id": target_chat, "text": chunk, "parse_mode": parse_mode,
                      "disable_web_page_preview": False},
                timeout=config.http_timeout_sec,
            )
            data = response.json()
            if not data.get("ok"):
                log.error("Telegram API ошибка: %s", data.get("description"))
                delivered = False
        except httpx.HTTPError as exc:
            log.error("Не удалось отправить сообщение в Telegram: %s", exc)
            delivered = False
    if delivered:
        log.info("Дайджест отправлен в Telegram (чат %s)", target_chat)
    return delivered
