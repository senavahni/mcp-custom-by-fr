"""Adapter Threads Keyword Search API resmi (`GET /keyword_search`, izin threads_keyword_search)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any

import httpx

from mcp_marketing.core.errors import (
    LoginRequired,
    UpstreamError,
    UpstreamTimeout,
    ValidationFailed,
)
from mcp_marketing.services.collectors.social import PostIn, hashtags_of, mentions_of

PAGE_LIMIT = 100
RETRY_BACKOFF_S = (2.0, 5.0, 10.0)
PAGE_PAUSE_S = 1.0
# Threads tidak menerima since sebelum peluncuran API publik
MIN_SINCE_UNIX = 1688540400
FIELDS = (
    "id,text,media_type,permalink,timestamp,username,has_replies,is_reply,is_quote_post,topic_tag,"
    "like_count,reply_count,repost_count,quote_count,views"
)
TOKEN_EXPIRED_CODE = 190


class ThreadsUnavailable(UpstreamError):
    """5xx berulang setelah retry: provider=auto boleh fallback ke browser."""


def parse_post(item: dict[str, Any], *, keep_raw: bool = False) -> PostIn:
    text = str(item.get("text") or "")
    tag = item.get("topic_tag")
    return PostIn(
        source="threads",
        external_id=str(item["id"]),
        text=text,
        posted_at=datetime.fromisoformat(
            str(item["timestamp"]).replace("Z", "+00:00").replace("+0000", "+00:00")
        ),
        author_handle=item.get("username"),
        url=item.get("permalink"),
        is_reply=bool(item.get("is_reply")),
        is_repost=False,
        likes=item.get("like_count"),
        replies=item.get("reply_count"),
        reposts=item.get("repost_count"),
        quotes=item.get("quote_count"),
        views=item.get("views"),
        media_type=item.get("media_type"),
        hashtags=hashtags_of(text, [tag] if tag else []),
        mentions=mentions_of(text),
        provider="threads_api",
        raw=item if keep_raw else None,
    )


class ThreadsApiClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        *,
        base_url: str = "https://graph.threads.net/v1.0",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.http = http
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._sleep = sleep

    async def search(
        self,
        keyword: str,
        since: datetime,
        until: datetime,
        *,
        search_type: str = "RECENT",
        search_mode: str = "KEYWORD",
        media_type: str | None = None,
        max_items: int = 300,
        after: str | None = None,
        keep_raw: bool = False,
    ) -> AsyncIterator[tuple[list[PostIn], str | None]]:
        since_unix = int(since.timestamp())
        if since_unix < MIN_SINCE_UNIX:
            raise ValidationFailed(
                "Threads API: since minimal 2023-07-05", details={"field": "since"}
            )
        fetched = 0
        while fetched < max_items:
            params: dict[str, Any] = {
                "q": keyword,
                "search_type": search_type,
                "search_mode": search_mode,
                "fields": FIELDS,
                "since": since_unix,
                "until": int(until.timestamp()),
                "limit": min(PAGE_LIMIT, max_items - fetched),
                "access_token": self.token,
            }
            if media_type:
                params["media_type"] = media_type
            if after:
                params["after"] = after
            body = await self._get("/keyword_search", params)
            posts = [parse_post(i, keep_raw=keep_raw) for i in body.get("data") or []][
                : max_items - fetched
            ]
            fetched += len(posts)
            after = ((body.get("paging") or {}).get("cursors") or {}).get("after")
            next_url = (body.get("paging") or {}).get("next")
            yield posts, after if next_url or after else None
            if not posts or not (after and next_url):
                return
            await self._sleep(PAGE_PAUSE_S)

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(len(RETRY_BACKOFF_S) + 1):
            try:
                resp = await self.http.get(f"{self.base_url}{path}", params=params, timeout=30)
            except httpx.TimeoutException:
                raise UpstreamTimeout("Threads API timeout") from None
            except httpx.HTTPError as exc:
                raise UpstreamError(
                    f"Threads API tidak dapat dihubungi ({type(exc).__name__})"
                ) from None
            if resp.status_code >= 500:
                if attempt < len(RETRY_BACKOFF_S):
                    await self._sleep(RETRY_BACKOFF_S[attempt])
                    continue
                raise ThreadsUnavailable(f"Threads API {resp.status_code} berulang", retryable=True)
            body = resp.json() if resp.content else {}
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                if error.get("code") == TOKEN_EXPIRED_CODE:
                    raise LoginRequired(
                        "Token Threads kedaluwarsa; jalankan manage.py threads-token-refresh",
                        details={"code": TOKEN_EXPIRED_CODE},
                    )
                raise UpstreamError(
                    f"Threads API menolak: {str(error.get('message'))[:200]}",
                    retryable=False,
                    details={"code": error.get("code")},
                )
            if resp.status_code >= 400:
                raise UpstreamError(f"Threads API error {resp.status_code}", retryable=False)
            return body  # type: ignore[no-any-return]
        raise ThreadsUnavailable("Threads API tidak tersedia", retryable=True)


async def refresh_long_lived_token(
    http: httpx.AsyncClient, token: str, base_url: str = "https://graph.threads.net"
) -> dict[str, Any]:
    """`GET /refresh_access_token?grant_type=th_refresh_token` -> {access_token, expires_in}."""
    resp = await http.get(
        f"{base_url.rstrip('/')}/refresh_access_token",
        params={"grant_type": "th_refresh_token", "access_token": token},
        timeout=30,
    )
    body = resp.json() if resp.content else {}
    if resp.status_code != 200 or "access_token" not in body:
        raise UpstreamError(f"Refresh token Threads gagal ({resp.status_code})", retryable=False)
    return body  # type: ignore[no-any-return]
