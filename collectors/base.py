"""Интерфейс коллектора источника."""

from __future__ import annotations

from abc import ABC, abstractmethod

from database.repository import Item


class BaseCollector(ABC):
    """Каждый источник реализует collect() и возвращает нормализованные элементы."""

    name: str = "base"

    @abstractmethod
    def collect(self) -> list[Item]:
        """Загружает данные источника. Ошибки источника не должны ронять сервис."""
        raise NotImplementedError
