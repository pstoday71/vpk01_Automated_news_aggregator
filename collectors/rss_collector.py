"""Сбор данных из RSS-лент через httpx + feedparser."""

from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timezone
from html import unescape
from time import struct_time

import feedparser
import httpx

from config import config
from database.repository import Item

from .base import BaseCollector
from .sources import Source

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str, max_len: int = 300) -> str:
    """Очищает HTML-разметку и ограничивает длину описания."""
    if not text:
        return ""
    plain = unescape(_TAG_RE.sub(" ", text))
    plain = _WS_RE.sub(" ", plain).strip()
    if len(plain) > max_len:
        plain = plain[: max_len - 1].rstrip() + "…"
    return plain


def _to_iso(published: struct_time | None) -> str:
    if published is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    ts = calendar.timegm(published)
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


class RssCollector(BaseCollector):
    def __init__(self, source: Source, client: httpx.Client | None = None) -> None:
        self.source = source
        self.name = source.name
        self._client = client

    def collect(self) -> list[Item]:
        raw = self._download()
        if not raw:
            return []
        parsed = feedparser.parse(raw)
        if parsed.bozo and not parsed.entries:
            log.error("[%s] RSS не разобран: %s", self.name, parsed.bozo_exception)
            return []
        items: list[Item] = []
        for entry in parsed.entries:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            description = strip_html(
                entry.get("summary") or entry.get("description") or ""
            )
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            items.append(
                Item(
                    title=title,
                    url=link,
                    source=self.name,
                    description=description,
                    created_at=_to_iso(published),
                )
            )
        log.info("[%s] получено элементов из RSS: %d", self.name, len(items))
        return items

    def _download(self) -> bytes | str | None:
        try:
            client = self._client or httpx.Client(
                timeout=config.http_timeout_sec,
                headers={"User-Agent": config.user_agent},
                follow_redirects=True,
            )
            owns_client = self._client is None
            try:
                response = client.get(self.source.url)
                response.raise_for_status()
                return response.content
            finally:
                if owns_client:
                    client.close()
        except httpx.HTTPError as exc:
            log.error("[%s] ошибка загрузки %s: %s", self.name, self.source.url, exc)
            return None
