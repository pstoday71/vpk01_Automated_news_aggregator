"""Конфигурация сервиса: переменные окружения с значениями по умолчанию.

Настройки можно менять на лету из Telegram-бота: значение записывается в .env
(set_env_var) и подхватывается через config.reload().
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)


def _get(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    telegram_bot_token: str = field(default_factory=lambda: _get("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _get("TELEGRAM_CHAT_ID", ""))
    collect_interval_min: int = field(default_factory=lambda: _get_int("COLLECT_INTERVAL_MIN", 30))
    # Резервный режим дайджеста по времени (когда DIGEST_EVERY_N_COLLECTS = 0)
    digest_hour: int = field(default_factory=lambda: _get_int("DIGEST_HOUR", 9))
    digest_minute: int = field(default_factory=lambda: _get_int("DIGEST_MINUTE", 0))
    # Дайджест автоматически после каждого N-го сбора (0 — выключено)
    digest_every_n_collects: int = field(default_factory=lambda: _get_int("DIGEST_EVERY_N_COLLECTS", 2))
    digest_min_per_source: int = field(default_factory=lambda: _get_int("DIGEST_MIN_PER_SOURCE", 1))
    digest_max_per_source: int = field(default_factory=lambda: _get_int("DIGEST_MAX_PER_SOURCE", 3))
    digest_max_items: int = field(default_factory=lambda: _get_int("DIGEST_MAX_ITEMS", 30))
    # Окно времени, когда разрешена отправка дайджеста (не присылать ночью)
    digest_from_hour: int = field(default_factory=lambda: _get_int("DIGEST_FROM_HOUR", 8))
    digest_until_hour: int = field(default_factory=lambda: _get_int("DIGEST_UNTIL_HOUR", 23))
    timezone_name: str = field(default_factory=lambda: _get("TIMEZONE", "UTC"))
    db_path: Path = field(default_factory=lambda: BASE_DIR / _get("DB_PATH", "news.db"))
    log_file: Path = field(default_factory=lambda: BASE_DIR / _get("LOG_FILE", "collector.log"))
    http_timeout_sec: float = field(
        default_factory=lambda: float(_get("HTTP_TIMEOUT_SEC", "15"))
    )
    user_agent: str = field(
        default_factory=lambda: _get(
            "USER_AGENT", "news-aggregator/1.0 (+https://example.com)"
        )
    )

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def reload(self) -> None:
        """Перечитывает .env и обновляет значения в этом же объекте."""
        load_dotenv(ENV_FILE, override=True)
        fresh = Config()
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(fresh, name))


config = Config()


def set_env_var(key: str, value: str) -> None:
    """Записывает/обновляет переменную в .env и в os.environ."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    line = f"{key}={value}"
    idx = next((i for i, l in enumerate(lines) if l.startswith(key + "=")), None)
    if idx is not None:
        lines[idx] = line
    else:
        lines.append(line)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


def setup_logging(log_file: Path | None = None) -> None:
    """Консоль + ротируемый файл-лог (ТЗ: лог запуска сборщика)."""
    target = log_file or config.log_file
    # Windows-консоль может быть в cp1251/cp866 — принудительно UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        file_handler = RotatingFileHandler(
            target, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        logging.warning("Не удалось открыть лог-файл %s, работает только консоль", target)