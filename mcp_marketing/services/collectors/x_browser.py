"""Adapter X mode browser: pencarian web per hari (`since:`/`until:`, tab Latest), profil login.

Strategi: hari diproses mundur; tiap hari scroll bertahap (jeda acak 1,5–4 s) sampai 3× tanpa post
baru atau kuota harian tercapai. Checkpoint per hari selesai; logout/challenge -> LOGIN_REQUIRED.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from mcp_marketing.core.errors import LoginRequired
from mcp_marketing.services.browser.driver_pool import PageSource
from mcp_marketing.services.collectors import x_selectors as sel
from mcp_marketing.services.collectors.social import PostIn, hashtags_of, mentions_of

SCROLL_PAUSE_S = (1.5, 4.0)
DAY_PAUSE_S = (10.0, 20.0)
MAX_IDLE_SCROLLS = 3
MAX_POSTS_PER_HOUR = 2_000
_NUM_RE = re.compile(r"(\d[\d.,]*)\s*(rb|ribu|jt|juta|k|m)?", re.I)


def parse_count(label: str | None) -> int | None:
    """'1,234 Likes' / '12 rb Suka' / '3.4K' -> int."""
    if not label:
        return None
    m = _NUM_RE.search(label)
    if not m:
        return None
    raw, unit = m.group(1), (m.group(2) or "").lower()
    if unit:
        value = float(raw.replace(",", "."))
        mult = 1_000 if unit in ("rb", "ribu", "k") else 1_000_000
        return int(value * mult)
    return int(raw.replace(",", "").replace(".", ""))


def _count(article: Node, css: str) -> int | None:
    node = article.css_first(css)
    return (
        parse_count(str(node.attributes.get("aria-label") or node.text() or "")) if node else None
    )


def parse_search_html(html: str) -> list[PostIn]:
    posts: list[PostIn] = []
    for article in HTMLParser(html).css(sel.ARTICLE):
        link = None
        for a in article.css(sel.STATUS_LINK):
            if a.css_first(sel.TIME) is not None:
                link = a
                break
        if link is None:
            continue
        href = str(link.attributes.get("href") or "")
        parts = href.strip("/").split("/")
        if len(parts) < 3 or parts[1] != "status":
            continue
        handle, tweet_id = parts[0], parts[2].split("?")[0]
        time_node = link.css_first(sel.TIME)
        if time_node is None:
            continue
        posted = datetime.fromisoformat(
            str(time_node.attributes.get("datetime")).replace("Z", "+00:00")
        )
        text_node = article.css_first(sel.TEXT)
        text_value = text_node.text(separator=" ").strip() if text_node else ""
        context = article.css_first(sel.SOCIAL_CONTEXT)
        context_text = context.text() if context else ""
        full_text = article.text()
        posts.append(
            PostIn(
                source="x",
                external_id=tweet_id,
                text=text_value,
                posted_at=posted,
                author_handle=handle,
                url=f"https://x.com/{handle}/status/{tweet_id}",
                lang=str(text_node.attributes.get("lang") or "") or None if text_node else None,
                is_reply=any(m in full_text for m in sel.REPLYING_TO_MARKERS),
                is_repost=any(m.lower() in context_text.lower() for m in sel.REPOST_MARKERS),
                replies=_count(article, sel.REPLY),
                reposts=_count(article, sel.REPOST),
                likes=_count(article, sel.LIKE),
                views=_count(article, sel.VIEWS),
                hashtags=hashtags_of(text_value),
                mentions=mentions_of(text_value),
                provider="browser",
            )
        )
    return posts


def search_url(query: str) -> str:
    return f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"


def days_desc(start: date, end: date) -> list[date]:
    return [end - timedelta(days=i) for i in range((end - start).days + 1)]


@dataclass
class DayResult:
    day: date
    posts: list[PostIn]
    scrolls: int


class XBrowserCollector:
    def __init__(
        self,
        page: PageSource,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.page = page
        self._sleep = sleep
        self._clock = clock
        self._window_start = clock()
        self._window_posts = 0

    async def _check_login(self) -> None:
        url = await self.page.current_url()
        if any(m in url for m in sel.LOGIN_URL_MARKERS):
            raise LoginRequired(
                "Profil browser keluar dari x.com; login ulang dengan manage.py profile-open",
                details={"url": url},
            )

    async def _throttle(self, new_posts: int) -> None:
        """Batas keras ≤ 2.000 post/jam per profil."""
        if self._clock() - self._window_start >= 3_600:
            self._window_start, self._window_posts = self._clock(), 0
        self._window_posts += new_posts
        if self._window_posts >= MAX_POSTS_PER_HOUR:
            await self._sleep(max(0.0, 3_600 - (self._clock() - self._window_start)))
            self._window_start, self._window_posts = self._clock(), 0

    async def collect_day(
        self, query: str, day: date, quota: int, *, max_scrolls: int = 60
    ) -> DayResult:
        await self.page.open(search_url(query), wait_for=None)
        await self._check_login()
        seen: dict[str, PostIn] = {}
        idle = 0
        scrolls = 0
        while len(seen) < quota and idle < MAX_IDLE_SCROLLS and scrolls < max_scrolls:
            before = len(seen)
            for post in parse_search_html(await self.page.html()):
                seen.setdefault(post.external_id, post)
            idle = idle + 1 if len(seen) == before else 0
            await self._throttle(len(seen) - before)
            if len(seen) >= quota or idle >= MAX_IDLE_SCROLLS:
                break
            await self.page.scroll(1, pause_s=0)
            scrolls += 1
            await self._sleep(random.uniform(*SCROLL_PAUSE_S))
            await self._check_login()
        return DayResult(day=day, posts=list(seen.values())[:quota], scrolls=scrolls)

    async def pause_between_days(self) -> None:
        await self._sleep(random.uniform(*DAY_PAUSE_S))


def per_day_quota(max_items: int, days: int) -> int:
    return max(1, math.ceil(max_items / max(1, days) * 1.5))
