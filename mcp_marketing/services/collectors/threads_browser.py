"""Adapter Threads mode browser (cadangan API): pencarian tab Recent, filter periode lokal.

UI tidak menyediakan rentang tanggal; scroll berhenti bila 20 post berturut-turut lebih tua dari
`since`, kuota tercapai, atau 3× scroll tanpa post baru.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from mcp_marketing.core.errors import LoginRequired
from mcp_marketing.services.browser.driver_pool import PageSource
from mcp_marketing.services.collectors import threads_selectors as sel
from mcp_marketing.services.collectors.social import PostIn, hashtags_of, mentions_of
from mcp_marketing.services.collectors.x_browser import parse_count

OLD_STREAK_STOP = 20
MAX_IDLE_SCROLLS = 3
SCROLL_PAUSE_S = (1.5, 3.5)


def _metric(container: Node, labels: tuple[str, ...]) -> int | None:
    for svg in container.css("svg[aria-label]"):
        label = str(svg.attributes.get("aria-label") or "")
        if label in labels:
            holder = svg.parent
            for _ in range(4):
                if holder is None:
                    break
                value = parse_count(holder.text(strip=True))
                if value is not None:
                    return value
                holder = holder.parent
    return 0


def parse_search_html(html: str) -> list[PostIn]:
    posts: list[PostIn] = []
    seen: set[str] = set()
    for box in HTMLParser(html).css(sel.POST_CONTAINER):
        link = box.css_first(sel.PERMALINK)
        time_node = box.css_first(sel.TIME)
        if link is None or time_node is None:
            continue
        href = str(link.attributes.get("href") or "")
        parts = [p for p in href.split("?")[0].split("/") if p]
        if len(parts) < 3 or parts[1] != "post" or not parts[0].startswith("@"):
            continue
        username, code = parts[0][1:], parts[2]
        if code in seen:
            continue
        seen.add(code)
        texts = [s.text(strip=True) for s in box.css(sel.TEXT_SPANS)]
        text_value = max(texts, key=len) if texts else ""
        posts.append(
            PostIn(
                source="threads",
                external_id=code,
                text=text_value,
                posted_at=datetime.fromisoformat(
                    str(time_node.attributes.get("datetime")).replace("Z", "+00:00")
                ),
                author_handle=username,
                url=f"https://www.threads.net/@{username}/post/{code}",
                likes=_metric(box, sel.LIKE_LABELS),
                replies=_metric(box, sel.REPLY_LABELS),
                reposts=_metric(box, sel.REPOST_LABELS),
                hashtags=hashtags_of(text_value),
                mentions=mentions_of(text_value),
                provider="browser",
            )
        )
    return posts


def search_url(keyword: str, search_mode: str = "KEYWORD") -> str:
    q = keyword.lstrip("#") if search_mode == "TAG" else keyword
    serp = "tags" if search_mode == "TAG" else "default"
    return f"https://www.threads.net/search?q={quote(q)}&serp_type={serp}&filter=recent"


class ThreadsBrowserCollector:
    def __init__(
        self, page: PageSource, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    ) -> None:
        self.page = page
        self._sleep = sleep

    async def collect(
        self,
        keyword: str,
        since: datetime,
        until: datetime,
        quota: int,
        *,
        search_mode: str = "KEYWORD",
        max_scrolls: int = 80,
    ) -> list[PostIn]:
        await self.page.open(search_url(keyword, search_mode))
        current = await self.page.current_url()
        if any(m in current for m in sel.LOGIN_URL_MARKERS):
            raise LoginRequired(
                "Profil browser tidak login di threads.net; login ulang (manage.py profile-open)"
            )
        kept: dict[str, PostIn] = {}
        seen: set[str] = set()
        old_streak = idle = scrolls = 0
        while len(kept) < quota and idle < MAX_IDLE_SCROLLS and scrolls < max_scrolls:
            new = 0
            for post in parse_search_html(await self.page.html()):
                if post.external_id in seen:
                    continue
                seen.add(post.external_id)
                new += 1
                if post.posted_at < since:
                    old_streak += 1
                    continue
                old_streak = 0
                if post.posted_at <= until:
                    kept[post.external_id] = post
            if old_streak >= OLD_STREAK_STOP:
                break
            idle = idle + 1 if new == 0 else 0
            await self.page.scroll(1, pause_s=0)
            scrolls += 1
            await self._sleep(random.uniform(*SCROLL_PAUSE_S))
        return list(kept.values())[:quota]
