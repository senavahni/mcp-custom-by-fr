"""Adapter X API v2 recent search (pay-per-use, 7 hari terakhir)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from mcp_marketing.core.errors import LoginRequired, RateLimited, UpstreamError, UpstreamTimeout
from mcp_marketing.core.logging import get_logger
from mcp_marketing.services.collectors.social import PostIn, hashtags_of, mentions_of

log = get_logger(__name__)

PAGE_SIZE = 100
MAX_RATE_WAITS = 5
TWEET_FIELDS = "created_at,public_metrics,lang,entities,author_id,conversation_id,referenced_tweets"


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_tweet(
    t: dict[str, Any], users: dict[str, dict[str, Any]], *, keep_raw: bool = False
) -> PostIn:
    metrics = t.get("public_metrics") or {}
    author = users.get(str(t.get("author_id")), {})
    refs = {r.get("type") for r in t.get("referenced_tweets") or []}
    entities = t.get("entities") or {}
    text = str(t.get("text") or "")
    handle = author.get("username")
    return PostIn(
        source="x",
        external_id=str(t["id"]),
        text=text,
        posted_at=datetime.fromisoformat(str(t["created_at"]).replace("Z", "+00:00")),
        author_handle=handle,
        author_id=str(t.get("author_id") or "") or None,
        author_followers=(author.get("public_metrics") or {}).get("followers_count"),
        url=f"https://x.com/{handle or 'i'}/status/{t['id']}",
        lang=t.get("lang"),
        is_reply="replied_to" in refs,
        is_repost="retweeted" in refs,
        likes=metrics.get("like_count"),
        replies=metrics.get("reply_count"),
        reposts=metrics.get("retweet_count"),
        quotes=metrics.get("quote_count"),
        views=metrics.get("impression_count"),
        hashtags=hashtags_of(text, [h.get("tag", "") for h in entities.get("hashtags") or []]),
        mentions=mentions_of(text),
        provider="x_api",
        raw=t if keep_raw else None,
    )


class XApiClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        *,
        base_url: str = "https://api.x.com/2",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.http = http
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._sleep = sleep
        self._clock = clock
        self._reset_at: float | None = None

    async def search(
        self,
        query: str,
        start: datetime,
        end: datetime,
        *,
        max_items: int,
        next_token: str | None = None,
        keep_raw: bool = False,
        on_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> AsyncIterator[tuple[list[PostIn], str | None]]:
        """Yield `(posts, next_token)` per halaman sampai `max_items` atau habis."""
        fetched = 0
        while fetched < max_items:
            params: dict[str, Any] = {
                "query": query,
                "start_time": _ts(start),
                "end_time": _ts(end),
                "max_results": max(10, min(PAGE_SIZE, max_items - fetched)),
                "tweet.fields": TWEET_FIELDS,
                "expansions": "author_id",
                "user.fields": "username,public_metrics",
            }
            if next_token:
                params["next_token"] = next_token
            body = await self._get("/tweets/search/recent", params, on_wait)
            users = {str(u["id"]): u for u in (body.get("includes") or {}).get("users", [])}
            posts = [parse_tweet(t, users, keep_raw=keep_raw) for t in body.get("data") or []]
            posts = posts[: max_items - fetched]
            fetched += len(posts)
            next_token = (body.get("meta") or {}).get("next_token")
            yield posts, next_token
            if not next_token or not posts:
                return

    async def _get(
        self, path: str, params: dict[str, Any], on_wait: Callable[[str], Awaitable[None]] | None
    ) -> dict[str, Any]:
        for _ in range(MAX_RATE_WAITS + 1):
            if self._reset_at and self._reset_at > self._clock():
                # kuota jendela habis pada respons sebelumnya: tunggu reset sebelum meminta lagi
                wait = min(900.0, self._reset_at - self._clock())
                if on_wait:
                    await on_wait(f"menunggu rate limit X ({int(wait)} s)")
                await self._sleep(wait)
            self._reset_at = None
            try:
                resp = await self.http.get(
                    f"{self.base_url}{path}",
                    params=params,
                    timeout=30,
                    headers={"Authorization": f"Bearer {self.token}"},
                )
            except httpx.TimeoutException:
                raise UpstreamTimeout("X API timeout") from None
            except httpx.HTTPError as exc:
                raise UpstreamError(f"X API tidak dapat dihubungi ({type(exc).__name__})") from None
            reset_header = resp.headers.get("x-rate-limit-reset")
            if resp.status_code == 429:
                reset = float(reset_header or self._clock() + 60)
                self._reset_at = max(self._clock() + 1.0, reset)
                log.info("x_api.rate_limited", wait_s=int(self._reset_at - self._clock()))
                continue
            if resp.headers.get("x-rate-limit-remaining") == "0" and reset_header:
                self._reset_at = float(reset_header)
            if resp.status_code in (401, 403):
                raise LoginRequired(
                    f"X API menolak token ({resp.status_code}); periksa X_BEARER_TOKEN/akses",
                    details={"status": resp.status_code},
                )
            if resp.status_code >= 500:
                raise UpstreamError(f"X API error {resp.status_code}", retryable=True)
            if resp.status_code >= 400:
                raise UpstreamError(
                    f"X API menolak query ({resp.status_code}): {resp.text[:200]}", retryable=False
                )
            return resp.json()  # type: ignore[no-any-return]
        raise RateLimited("Rate limit X API berulang; job dihentikan sementara")
