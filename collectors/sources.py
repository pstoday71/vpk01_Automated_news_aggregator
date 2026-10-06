"""Реестр подключённых источников.

Добавление нового источника = одна запись в SOURCES.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    kind: str = "rss"


SOURCES: list[Source] = [
    Source(
        name="Habr",
        url="https://habr.com/ru/rss/hubs/all/articles/?fl=ru",
    ),
    Source(
        name="Lenta.ru",
        url="https://lenta.ru/rss/news",
    ),
    # Пример расширения (достаточно раскомментировать):
    # Source(name="РБК", url="https://www.rbc.ru/v1_5/rss/"),
]
