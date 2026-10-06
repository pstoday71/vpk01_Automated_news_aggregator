"""Управление ботом из Telegram: кнопки, команды и настройки-диалоги.

Long polling (getUpdates) в фоновом потоке. Интерфейс:
  /start, /digest, /stats, /settings              — команды
  📥 Дайджест сейчас                              — принудительная отправка (буфер)
  📥 Дайджест: источник → <источник>              — сводка источника (буфер очищается)
  ⚙️ Настройки                                    — диалог: бот просит ввести значение,
                                                     показывает пример и лимиты,
                                                     проверяет ввод и сохраняет.
"""

from __future__ import annotations

import logging
import re
import threading

import httpx

from collectors import SOURCES
from config import config, set_env_var
from database import Repository
from database.db import KEY_DIGEST_DEFERRED
from scheduler.jobs import reschedule_collect, reschedule_digest_flush

from .cli import run_digest, run_source_digest

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
LB = "\n".join

_MAIN_KB = [
    [("📥 Дайджест сейчас", "act:digest")],
    [("📥 Дайджест: источник", "menu:src")],
    [("⚙️ Настройки", "menu:settings")],
    [("📊 Статус", "act:stats")],
]

# диалоги: chat_id -> вид настройки
_PENDING: dict[int, str] = {}

def _cancel_kb() -> dict:
    return _kb([[("🚫 Отмена", "set:cancel")]])

_LIMITS = {
    "interval": (5, 180, "целое число минут, например <code>15</code>"),
    "scans": (1, 12, "целое число сборов, например <code>2</code>"),
}


def _kb(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": c} for t, c in row] for row in rows]}


def _main_kb() -> dict:
    return _kb(_MAIN_KB)


def _src_kb() -> dict:
    rows: list[list[tuple[str, str]]] = [[(s.name, f"digest:src:{s.name}")] for s in SOURCES]
    rows.append([("↩️ Назад", "menu:main")])
    return _kb(rows)


def _settings_kb() -> dict:
    rows = [
        [("🔁 Интервал сбора новостей", "set:interval")],
        [("🗓 Дайджест после сборов", "set:scans")],
        [("⏰ Диапазон времени отсылки", "set:window")],
        [("⟨ Назад", "menu:main")],
    ]
    return _kb(rows)


def _fmt_window() -> str:
    if config.digest_from_hour == 0 and config.digest_until_hour == 0:
        return "круглосуточно"
    return (f"{config.digest_from_hour:02d}:00–{config.digest_until_hour:02d}:00 "
            f"({config.timezone_name})")


def _settings_text() -> str:
    return LB([
        "<b>⚙️ Текущие настройки</b>",
        f"🔁 Интервал сбора: <code>{config.collect_interval_min}</code> мин",
        f"🗓 Дайджест после: <code>{config.digest_every_n_collects}</code> сбор(ов)",
        f"⏰ Диапазон отсылки: <code>{_fmt_window()}</code>",
        "",
        "Выберите настройку — я подскажу пример и лимиты, затем введите значение.",
    ])


def _instructions(kind: str) -> str:
    if kind == "interval":
        return LB([
            "<b>🔁 Интервал сбора новостей</b>",
            f"Сейчас: <code>{config.collect_interval_min}</code> мин",
            "",
            "Введите целое число минут.",
            f"Пример: <code>15</code>  ·  Лимиты: от <b>{_LIMITS['interval'][0]}</b> "
            f"до <b>{_LIMITS['interval'][1]}</b>",
        ])
    if kind == "scans":
        return LB([
            "<b>🗓 Дайджест после сборов</b>",
            f"Сейчас: после <code>{config.digest_every_n_collects}</code> сборов",
            "",
            "Введите целое число сборов, после которых присылать дайджест.",
            f"Пример: <code>2</code>  ·  Лимиты: от <b>{_LIMITS['scans'][0]}</b> "
            f"до <b>{_LIMITS['scans'][1]}</b>",
        ])
    return LB([
        "<b>⏰ Диапазон времени отсылки</b>",
        f"Сейчас: <code>{_fmt_window()}</code>",
        "",
        "Введите диапазон в формате ЧЧ-ЧЧ (24-часовой).",
        "Пример: <code>9-21</code> — отправка с 09:00 до 21:00, ночью не беспокою.",
        "Если <code>0-0</code> — присылать круглосуточно.",
        "Лимиты: часы от <b>0</b> до <b>23</b>, начало и конец не должны совпадать.",
    ])


# ---------------------------------------------------------------------- ввод
def _check_int(raw: str, lo: int, hi: int) -> int | None:
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if lo <= value <= hi else None


def _check_window(raw: str) -> str | None:
    text = raw.strip().lower().replace(" ", "")
    if any(w in text for w in ("всегда", "круглосуточно")):
        return "0-0"
    match = re.fullmatch(r"(\d{1,2})(?::?00)?\s*[-:–]\s*(\d{1,2})(?::?00)?", text)
    if not match:
        return None
    frm, unt = int(match.group(1)), int(match.group(2))
    if not (0 <= frm <= 23 and 0 <= unt <= 23):
        return None
    if frm == unt and frm != 0:
        return None
    return f"{frm}-{unt}"


def _begin_dialog(chat_id: int, kind: str) -> None:
    _PENDING[chat_id] = kind
    _send(chat_id, _instructions(kind), _cancel_kb())


def _cancel_dialog(chat_id: int) -> None:
    if _PENDING.pop(chat_id, None):
        _send(chat_id, "Отменено. Настроек не менял.", _settings_kb())
    else:
        _send(chat_id, "Нет активного ввода.", _main_kb())


def _send_err(chat_id: int, kind: str, raw: str) -> None:
    _send(chat_id, f"❌ Не удалось распознать значение «{raw.strip()}».\n\n{_instructions(kind)}",
          _cancel_kb())


def _finish_setting(chat_id: int, label: str, display: str) -> None:
    _PENDING.pop(chat_id, None)
    _send(chat_id, f"✅ {label} сохранено: <b>{display}</b>\n\n{_settings_text()}", _settings_kb())


def _handle_input(chat_id: int, raw: str) -> None:
    kind = _PENDING[chat_id]
    if kind == "window":
        value = _check_window(raw)
        if value is None:
            return _send_err(chat_id, kind, raw)
        frm, unt = value.split("-")
        set_env_var("DIGEST_FROM_HOUR", frm)
        set_env_var("DIGEST_UNTIL_HOUR", unt)
        config.reload()
        reschedule_digest_flush()
        _finish_setting(chat_id, "⏰ Диапазон времени отсылки", _fmt_window())
        return
    lo, hi, _ = _LIMITS[kind]
    value = _check_int(raw, lo, hi)
    if value is None:
        return _send_err(chat_id, kind, raw)
    if kind == "interval":
        set_env_var("COLLECT_INTERVAL_MIN", str(value))
        config.reload()
        reschedule_collect(value)
        _finish_setting(chat_id, "🔁 Интервал сбора новостей", f"{value} мин")
    else:
        set_env_var("DIGEST_EVERY_N_COLLECTS", str(value))
        config.reload()
        _finish_setting(chat_id, "🗓 Дайджест после сборов", f"{value} сбор(ов)")


# ------------------------------------------------------------------- действия
def _stats_text() -> str:
    repo = Repository()
    st = repo.stats()
    deferred = repo.get_meta(KEY_DIGEST_DEFERRED) == "1"
    lines = [
        "<b>📊 Статус агрегатора</b>",
        f"Сборов: {st['runs']} · последний обработан дайджестом №{st['last_digest_run']}",
        f"Материалов в базе: {st['total']} · в буфере (не отправлено): {st['unsent']}",
        f"Источники: {', '.join(f'{k}={v}' for k, v in st.items() if k not in ('total', 'unsent', 'runs', 'last_digest_run'))}",
        "",
        f"🔁 Интервал сканирования: {config.collect_interval_min} мин",
        f"🗓 Дайджест: после {config.digest_every_n_collects} сбор(ов)",
        f"⏰ Окно отправки: {_fmt_window()}",
        f"🌙 Отложенный дайджест: {'есть (уйдёт в окне)' if deferred else 'нет'}",
    ]
    return LB(lines)


def dispatch(chat_id: int, data: str) -> None:
    if data == "menu:main":
        _send(chat_id, "Главное меню:", _main_kb())
    elif data == "menu:src":
        _send(chat_id, "Сводку какого источника прислать?", _src_kb())
    elif data == "menu:settings":
        _send(chat_id, _settings_text(), _settings_kb())
    elif data == "set:interval":
        _begin_dialog(chat_id, "interval")
    elif data == "set:scans":
        _begin_dialog(chat_id, "scans")
    elif data == "set:window":
        _begin_dialog(chat_id, "window")
    elif data == "set:cancel":
        _cancel_dialog(chat_id)
    elif data == "act:digest":
        text, delivered = run_digest()
        if text is None:
            st = Repository().stats()
            _send(chat_id,
                  f"📭 В буфере новых материалов нет (всего в базе: {st['total']}).\n"
                  "Дайджест приходит автоматически после накопления сборов.", _main_kb())
        else:
            _send(chat_id,
                  f"Дайджест отправлен ✅ (канал: {'Telegram' if delivered else 'консоль'})",
                  _main_kb())
    elif data == "act:stats":
        _send(chat_id, _stats_text(), _main_kb())
    elif data.startswith("digest:src:"):
        name = data.split(":", 2)[2]
        text = run_source_digest(name)
        if text is None:
            _send(chat_id, f"По источнику «{name}» новых материалов нет 🙂", _main_kb())
        else:
            _send(chat_id, f"Сводка «{name}» отправлена ✅ (показанное убрано из буфера)",
                  _main_kb())
    else:
        _send(chat_id, "Не понял команду.", _main_kb())


def handle_update(update: dict) -> None:
    if "callback_query" in update:
        cb = update["callback_query"]
        user_id = (cb.get("from") or {}).get("id")
        if not _authorized(user_id):
            log.warning("Отклонён callback от неавторизованного пользователя %s", user_id)
            return
        _answer(cb.get("id", ""))
        chat_id = (cb.get("message") or {}).get("chat", {}).get("id") or user_id
        dispatch(chat_id, cb.get("data", ""))
        return
    if "message" not in update:
        return
    msg = update["message"]
    user_id = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    if not _authorized(user_id):
        log.warning("Отклонено сообщение от неавторизованного %s", user_id)
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if _PENDING.get(chat_id) and not text.startswith("/"):
        _handle_input(chat_id, text)
        return
    if text in {"/cancel", "🚫 Отмена"}:
        _cancel_dialog(chat_id)
    elif text in {"/start", "Меню", "menu"}:
        _PENDING.pop(chat_id, None)
        _send(chat_id, "Привет! Я агрегатор новостей.", _main_kb())
    elif text in {"/digest", "📥 Дайджест сейчас"}:
        dispatch(chat_id, "act:digest")
    elif text == "/stats":
        dispatch(chat_id, "act:stats")
    elif text in {"/settings", "⚙️ Настройки"}:
        _PENDING.pop(chat_id, None)
        dispatch(chat_id, "menu:settings")
    elif text == "📥 Дайджест: источник":
        _PENDING.pop(chat_id, None)
        dispatch(chat_id, "menu:src")
    elif any(s.name.lower() == text.lower() for s in SOURCES):
        dispatch(chat_id, "digest:src:" + text)
    else:
        _send(chat_id,
              "Команды: /start, /digest, /stats, /settings\n"
              "Либо сообщите название источника.", _main_kb())


# --------------------------------------------------------------------- API
def _api(method: str, payload: dict) -> dict | None:
    try:
        data = httpx.post(
            API.format(token=config.telegram_bot_token, method=method),
            json=payload, timeout=config.http_timeout_sec,
        ).json()
        if not data.get("ok"):
            log.error("Telegram %s: %s", method, data.get("description"))
            return None
        return data.get("result")
    except httpx.HTTPError as exc:
        log.error("Telegram %s: %s", method, exc)
        return None


def _send(chat_id: int | str, text: str, kb: dict | None = None) -> None:
    text = text or "Готово ✅"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if kb is not None:
        payload["reply_markup"] = kb
    _api("sendMessage", payload)


def _answer(callback_query_id: str) -> None:
    _api("answerCallbackQuery", {"callback_query_id": callback_query_id})


def _authorized(user_id) -> bool:
    if not config.telegram_chat_id:
        return True  # dev-режим: без chat_id доступ открыт
    return str(user_id) == str(config.telegram_chat_id)


# ------------------------------------------------------------------- поллинг
def _get_updates(offset: int, timeout: int) -> list:
    try:
        data = httpx.get(
            API.format(token=config.telegram_bot_token, method="getUpdates"),
            params={"offset": offset, "timeout": timeout,
                    "allowed_updates": '["message", "callback_query"]'},
            timeout=timeout + 10,
        ).json()
        if not data.get("ok"):
            log.error("Telegram getUpdates: %s", data.get("description"))
            return []
        return data.get("result") or []
    except httpx.ReadTimeout:
        return []
    except httpx.HTTPError as exc:
        log.error("Telegram getUpdates: %s", exc)
        return []


def poller_loop(stop: threading.Event | None = None) -> None:
    """Бесконечный long-polling. Вызывается в фоновом потоке."""
    log.info("Telegram-поллер запущен")
    offset = 0
    while stop is None or not stop.is_set():
        updates = _get_updates(offset, 30)
        for update in updates:
            try:
                handle_update(update)
            except Exception:
                log.exception("Ошибка обработки обновления %s", update.get("update_id"))
            offset = max(offset, update["update_id"] + 1)
    log.info("Telegram-поллер остановлен")


def start_poller(stop: threading.Event | None = None) -> threading.Thread | None:
    """Запускает поллер в фоне. None, если токен не настроен."""
    if not config.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN не задан — управление ботом недоступно")
        return None
    thread = threading.Thread(target=poller_loop, args=(stop,),
                              name="tg-poller", daemon=True)
    thread.start()
    return thread