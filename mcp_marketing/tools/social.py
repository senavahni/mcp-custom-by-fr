"""Toolset `social`: `x_search`, `threads_search`, `social_stats` (MCP_SPEC §4.6, TASK-15/16)."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.models.results import ResultItem
from mcp_marketing.schemas.common import Envelope, ToolInput
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.browser.driver_pool import profile_store
from mcp_marketing.services.collectors.x_query import choose_provider, validate_period
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.tools._jobs import enqueue_and_wait
from mcp_marketing.utils.time import now, parse_period
from mcp_marketing.worker.handlers.social import x_reads_today

PROFILE_PATTERN = r"^[a-z0-9][a-z0-9_-]{1,62}$"


def _keywords(v: list[str]) -> list[str]:
    cleaned = [k.strip() for k in v if k.strip()]
    if not cleaned:
        raise ValueError("keywords minimal 1")
    if any(len(k) > 100 for k in cleaned):
        raise ValueError("tiap keyword maksimal 100 karakter")
    return cleaned


class _KeywordsInput(ToolInput):
    @field_validator("keywords", check_fields=False)
    @classmethod
    def _kw(cls, v: list[str]) -> list[str]:
        return _keywords(v)


class XSearchIn(_KeywordsInput):
    keywords: list[str] = Field(min_length=1, max_length=10)
    match: Literal["any", "all"] = "any"
    since: str = Field(description="YYYY-MM-DD atau ISO-8601 (zona TIMEZONE bila tanpa offset)")
    until: str
    lang: str | None = Field(default="id", max_length=8)
    exclude_retweets: bool = True
    exclude_replies: bool = False
    min_likes: int | None = Field(default=None, ge=0)
    max_items: int = Field(default=500, ge=1, le=5_000)
    provider: Literal["auto", "x_api", "browser"] = "auto"
    profile: str | None = Field(default=None, pattern=PROFILE_PATTERN)
    wait_s: int = Field(default=0, ge=0, le=20)


@tool(
    name="x_search",
    toolset="social",
    summary="Crawl post X per kata kunci & periode -> handle + statistik",
    description=(
        "Collect X/Twitter posts for keywords and a period. provider=auto uses the X API (<=7 "
        "days, within daily read quota) else a logged-in browser profile. Returns a job; "
        "result = handle + stats."
    ),
    input_model=XSearchIn,
    output_model=Envelope,
    returns_job=True,
)
async def x_search(inp: XSearchIn, ctx: ToolContext) -> Any:
    s = ctx.settings
    start, end = parse_period(inp.since, inp.until, s.timezone)
    validate_period(start, end)
    store = profile_store(ctx.services)
    profile = inp.profile
    browser_ready = False
    if profile:
        p = await store.get(profile)
        browser_ready = bool(((p.login_status or {}).get("x.com") or {}).get("ok"))
    else:
        found = await store.for_site("x.com")
        browser_ready, profile = found is not None, found.name if found else None
    remaining = max(0, s.social.x_max_reads_per_day - await x_reads_today(ctx.services))
    decision = choose_provider(
        requested=inp.provider,
        has_token=s.social.x_bearer_token is not None,
        start=start,
        now=now(),
        estimate_units=inp.max_items,
        remaining_quota=remaining,
        browser_ready=browser_ready,
    )
    tz = ZoneInfo(s.timezone)
    params = inp.model_dump(exclude={"wait_s"}) | {
        "provider": decision.provider,
        "profile": profile,
        "days": (end.astimezone(tz).date() - start.astimezone(tz).date()).days + 1,
        "price_per_read": s.social.x_price_per_read_usd,
    }
    timeout = 3_600 if decision.provider == "x_api" else 4 * 3_600
    return await enqueue_and_wait(ctx, "x_search", params, wait_s=inp.wait_s, timeout_s=timeout)


class ThreadsSearchIn(_KeywordsInput):
    keywords: list[str] = Field(min_length=1, max_length=10)
    since: str
    until: str
    search_type: Literal["TOP", "RECENT"] = "RECENT"
    search_mode: Literal["KEYWORD", "TAG"] = "KEYWORD"
    media_type: Literal["TEXT", "IMAGE", "VIDEO"] | None = None
    max_items: int = Field(default=300, ge=1, le=2_000)
    provider: Literal["auto", "threads_api", "browser"] = "auto"
    profile: str | None = Field(default=None, pattern=PROFILE_PATTERN)
    wait_s: int = Field(default=0, ge=0, le=20)


@tool(
    name="threads_search",
    toolset="social",
    summary="Crawl post Threads per kata kunci & periode -> handle + statistik",
    description=(
        "Collect public Threads posts via the official keyword_search API (fallback to a "
        "logged-in browser profile on repeated 5xx when provider=auto). Same output format as "
        "x_search."
    ),
    input_model=ThreadsSearchIn,
    output_model=Envelope,
    returns_job=True,
)
async def threads_search(inp: ThreadsSearchIn, ctx: ToolContext) -> Any:
    s = ctx.settings
    start, end = parse_period(inp.since, inp.until, s.timezone)
    validate_period(start, end)
    store = profile_store(ctx.services)
    browser = None
    if inp.profile:
        browser = await store.get(inp.profile)
    else:
        browser = await store.for_site("threads.net")
    has_token = s.social.threads_access_token is not None
    if inp.provider == "threads_api" and not has_token:
        raise ValidationFailed("THREADS_ACCESS_TOKEN belum dikonfigurasi; pakai provider=browser")
    if inp.provider == "browser" and browser is None:
        raise ValidationFailed("Tidak ada profil browser login threads.net (browser_profile_check)")
    provider = inp.provider
    if provider == "auto":
        if has_token:
            provider = "threads_api"
        elif browser is not None:
            provider = "browser"
        else:
            raise ValidationFailed(
                "Isi THREADS_ACCESS_TOKEN atau siapkan profil browser login threads.net"
            )
    params = inp.model_dump(exclude={"wait_s"}) | {
        "provider": provider,
        "profile": browser.name if browser else None,
        "fallback_browser": inp.provider == "auto" and browser is not None,
    }
    return await enqueue_and_wait(
        ctx, "threads_search", params, wait_s=inp.wait_s, timeout_s=3 * 3_600
    )


Metric = Literal["posts", "likes", "replies", "reposts", "views", "avg_likes"]
DEFAULT_METRICS: tuple[Metric, ...] = ("posts", "likes")


class SocialStatsIn(ToolInput):
    handle: str = Field(pattern=r"^h_[0-9a-z]{10}$")
    group_by: Literal["day", "keyword", "author", "source"] = "day"
    metrics: list[Metric] = Field(
        default_factory=lambda: list(DEFAULT_METRICS), min_length=1, max_length=6
    )
    limit: int = Field(default=20, ge=1, le=50)


class SocialStatsOut(BaseModel):
    summary: str
    cols: list[str]
    rows: list[list[Any]]
    share_of_voice: dict[str, float] | None = None


@tool(
    name="social_stats",
    toolset="social",
    summary="Statistik handle post sosial (per hari/keyword/author/source), tanpa LLM",
    description=(
        "Server-side stats for a social_posts handle (X and/or Threads) grouped by day, "
        "keyword, author or source; includes share of voice for keyword/source. No LLM tokens "
        "used."
    ),
    input_model=SocialStatsIn,
    output_model=SocialStatsOut,
)
async def social_stats(inp: SocialStatsIn, ctx: ToolContext) -> SocialStatsOut:
    owner = Owner(client_id=ctx.client.id, run_id=ctx.run_id, is_admin=ctx.client.is_admin)
    async with ctx.services.db.session() as session:
        rs = await ctx.services.results.get_set(session, inp.handle, owner)
        if rs.kind != "social_posts":
            raise ValidationFailed("handle bukan hasil x_search/threads_search")
        rows = (
            await session.scalars(select(ResultItem.data).where(ResultItem.handle == rs.handle))
        ).all()
    tz = ZoneInfo(ctx.settings.timezone)
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if inp.group_by == "day":
            keys = [
                datetime.fromisoformat(str(r["posted_at"]).replace("Z", "+00:00"))
                .astimezone(tz)
                .date()
                .isoformat()
            ]
        elif inp.group_by == "keyword":
            keys = list(r.get("keywords") or ["-"])
        elif inp.group_by == "author":
            keys = [r.get("author") or "-"]
        else:
            keys = [r.get("source") or "-"]
        for k in keys:
            groups.setdefault(k, []).append(r)

    def metric(items: list[dict[str, Any]], name: str) -> Any:
        if name == "posts":
            return len(items)
        if name == "avg_likes":
            return round(statistics.fmean(int(i.get("likes") or 0) for i in items), 2)
        return sum(int(i.get(name) or 0) for i in items)

    ordered = (
        sorted(groups) if inp.group_by == "day" else sorted(groups, key=lambda k: -len(groups[k]))
    )
    table = [[k, *(metric(groups[k], m) for m in inp.metrics)] for k in ordered[: inp.limit]]
    share = None
    if inp.group_by in ("keyword", "source"):
        counts = Counter({k: len(v) for k, v in groups.items()})
        total = sum(counts.values()) or 1
        share = {k: round(c / total * 100, 1) for k, c in counts.most_common(inp.limit)}
    return SocialStatsOut(
        summary=f"{len(rows)} post, {len(groups)} grup per {inp.group_by}",
        cols=[inp.group_by, *inp.metrics],
        rows=table,
        share_of_voice=share,
    )
