"""Kepatuhan scraping: robots.txt (protego, cache 1 jam) dan rate limit per domain + jeda acak."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from protego import Protego

from mcp_marketing.core.logging import get_logger

log = get_logger(__name__)

USER_AGENT = "MCPMarketingBot/0.1 (+internal)"
ROBOTS_TTL_S = 3_600
ROBOTS_TIMEOUT_S = 10.0
JITTER_S = (1.0, 3.0)


def domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class RobotsCache:
    """robots.txt per origin. Gagal diambil (bukan 4xx) -> dianggap boleh, sesuai praktik umum."""

    def __init__(self, http: httpx.AsyncClient, *, user_agent: str = USER_AGENT) -> None:
        self.http = http
        self.user_agent = user_agent
        self._cache: dict[str, tuple[float, Protego | None]] = {}

    async def _load(self, origin: str) -> Protego | None:
        cached = self._cache.get(origin)
        if cached and time.monotonic() - cached[0] < ROBOTS_TTL_S:
            return cached[1]
        parser: Protego | None = None
        try:
            resp = await self.http.get(
                f"{origin}/robots.txt", timeout=ROBOTS_TIMEOUT_S, follow_redirects=True
            )
            if resp.status_code == 200:
                parser = Protego.parse(resp.text)
        except httpx.HTTPError as exc:
            log.info("robots.fetch_failed", origin=origin, error=type(exc).__name__)
        self._cache[origin] = (time.monotonic(), parser)
        return parser

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        parser = await self._load(f"{parts.scheme}://{parts.netloc}")
        return True if parser is None else bool(parser.can_fetch(url, self.user_agent))


class DomainRateLimiter:
    """Maks `rpm` permintaan per domain per menit + jeda acak 1–3 s antar halaman."""

    def __init__(
        self,
        rpm: int,
        *,
        jitter: tuple[float, float] = JITTER_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval = 60.0 / max(1, rpm)
        self.jitter = jitter
        self._sleep = sleep
        self._clock = clock
        self._next: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait(self, url: str) -> float:
        domain = domain_of(url)
        async with self._lock:
            now = self._clock()
            start = max(now, self._next.get(domain, now))
            self._next[domain] = start + self.interval
        delay = (start - now) + random.uniform(*self.jitter)
        if delay > 0:
            await self._sleep(delay)
        return delay
