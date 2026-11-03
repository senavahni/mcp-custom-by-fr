"""Job `x_search` & `threads_search` (TASK-15/16); provider diputuskan tool sebelum enqueue."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from mcp_marketing.core.errors import LoginRequired, ToolError, UpstreamError
from mcp_marketing.core.logging import get_logger
from mcp_marketing.schemas.common import JobEstimate
from mcp_marketing.services.browser.driver_pool import launcher_for, profile_store
from mcp_marketing.services.collectors import social
from mcp_marketing.services.collectors.threads_api import ThreadsApiClient, ThreadsUnavailable
from mcp_marketing.services.collectors.threads_browser import ThreadsBrowserCollector
from mcp_marketing.services.collectors.x_api import XApiClient
from mcp_marketing.services.collectors.x_browser import XBrowserCollector, days_desc, per_day_quota
from mcp_marketing.services.collectors.x_query import build_query, estimate_cost, web_query
from mcp_marketing.utils.time import now, parse_period
from mcp_marketing.worker.handlers.base import JobCtx, JobResult

log = get_logger(__name__)


def _sleep(job: JobCtx) -> Any:
    return job.services.extras.get("sleep") or asyncio.sleep


def _period(job: JobCtx) -> tuple[datetime, datetime]:
    return parse_period(job.params["since"], job.params["until"], job.services.settings.timezone)


def _filters(job: JobCtx, start: datetime, end: datetime) -> dict[str, Any]:
    p = job.params
    return {
        "start": start,
        "end": end,
        "lang": p.get("lang"),
        "min_likes": p.get("min_likes"),
        "exclude_retweets": bool(p.get("exclude_retweets", True)),
        "exclude_replies": bool(p.get("exclude_replies", False)),
    }


async def x_reads_today(services: Any) -> int:
    """Post X yang dibaca via API hari ini (zona TIMEZONE) — dasar kuota X_MAX_READS_PER_DAY."""
    tz = ZoneInfo(services.settings.timezone)
    day_start = datetime.combine(now().astimezone(tz).date(), datetime.min.time(), tzinfo=tz)
    sql = text(
        "SELECT COALESCE(sum(COALESCE((result->'cost'->>'external_units')::int, "
        "(checkpoint->>'units')::int, 0)), 0) FROM mcp.jobs "
        "WHERE kind = 'x_search' AND params->>'provider' = 'x_api' AND created_at >= :start"
    )
    async with services.db.session() as session:
        return int((await session.execute(sql, {"start": day_start})).scalar() or 0)


class XSearchHandler:
    kind = "x_search"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        items = int(params.get("max_items") or 500)
        if params.get("provider") == "x_api":
            return JobEstimate(
                seconds=max(10, items // 50),
                external_units=items,
                usd=estimate_cost(items, float(params.get("price_per_read") or 0.005)),
            )
        days = max(1, int(params.get("days") or 1))
        return JobEstimate(seconds=days * 60 + items // 10)

    async def run(self, job: JobCtx) -> JobResult:
        start, end = _period(job)
        provider = job.params["provider"]
        if provider == "x_api":
            return await self._api(job, start, end)
        return await self._browser(job, start, end)

    async def _api(self, job: JobCtx, start: datetime, end: datetime) -> JobResult:
        p, s = job.params, job.services.settings.social
        if s.x_bearer_token is None:
            raise UpstreamError("X_BEARER_TOKEN tidak dikonfigurasi di worker", retryable=False)
        keywords = list(p["keywords"])
        ck = job.checkpoint
        units = int(ck.get("units", 0))
        used_before = await x_reads_today(job.services) - units
        budget_left = max(0, s.x_max_reads_per_day - used_before)
        max_items = min(int(p.get("max_items") or 500), budget_left)
        client = XApiClient(
            job.services.http,
            s.x_bearer_token.get_secret_value(),
            base_url=s.x_api_base_url,
            sleep=_sleep(job),
        )
        query = build_query(
            keywords,
            match=p.get("match", "any"),
            lang=p.get("lang"),
            exclude_retweets=p.get("exclude_retweets", True),
            exclude_replies=p.get("exclude_replies", False),
        )

        async def on_wait(message: str) -> None:
            await job.progress(units, max_items, message)

        filters = _filters(job, start, end)
        if units < max_items and not ck.get("done"):
            async for posts, token in client.search(
                query,
                start,
                end,
                max_items=max_items - units,
                next_token=ck.get("next_token"),
                on_wait=on_wait,
                keep_raw=job.services.settings.app_env == "dev",
            ):
                await job.raise_if_cancelled()
                units += len(
                    posts
                )  # biaya dihitung per post yang dikembalikan, sebelum filter lokal
                kept = [x for x in posts if social.passes_filters(x, **filters)]
                await social.store_posts(
                    job, kept, keywords, keep_raw=job.services.settings.app_env == "dev"
                )
                await job.save_checkpoint(
                    {"next_token": token, "units": units, "done": token is None}
                )
                await job.progress(units, max_items, f"{units} post dibaca dari X API")
        usd = estimate_cost(units, s.x_price_per_read_usd)
        notes = (
            ["kuota harian membatasi max_items"]
            if max_items < int(p.get("max_items") or 500)
            else None
        )
        return await social.build_result(
            job,
            source="x",
            provider="x_api",
            keywords=keywords,
            period=(p["since"], p["until"]),
            external_units=units,
            usd=usd,
            notes=notes,
        )

    async def _browser(self, job: JobCtx, start: datetime, end: datetime) -> JobResult:
        p = job.params
        keywords = list(p["keywords"])
        tz = ZoneInfo(job.services.settings.timezone)
        days = days_desc(start.astimezone(tz).date(), end.astimezone(tz).date())
        quota = per_day_quota(int(p.get("max_items") or 500), len(days))
        done: list[str] = list(job.checkpoint.get("days_done", []))
        filters = _filters(job, start, end)
        partial = False
        profile = p.get("profile") or "default"
        try:
            async with launcher_for(job.services).session(profile, job.job_id) as page:
                collector = XBrowserCollector(page, sleep=_sleep(job))
                for i, day in enumerate(days):
                    if day.isoformat() in done:
                        continue
                    await job.raise_if_cancelled()
                    if await social.job_post_count(job) >= int(p.get("max_items") or 500):
                        break
                    q = web_query(
                        keywords,
                        match=p.get("match", "any"),
                        lang=p.get("lang"),
                        day_start=day.isoformat(),
                        day_end=(day + timedelta(days=1)).isoformat(),
                        exclude_retweets=p.get("exclude_retweets", True),
                        exclude_replies=p.get("exclude_replies", False),
                    )
                    result = await collector.collect_day(q, day, quota)
                    kept = [x for x in result.posts if social.passes_filters(x, **filters)]
                    await social.store_posts(job, kept, keywords)
                    done.append(day.isoformat())
                    await job.save_checkpoint({"days_done": done})
                    await job.progress(
                        len(done), len(days), f"hari {day.isoformat()}: {len(kept)} post"
                    )
                    if i < len(days) - 1:
                        await collector.pause_between_days()
        except LoginRequired:
            if not done:
                raise
            partial = True  # hasil parsial tetap dikembalikan
        return await social.build_result(
            job,
            source="x",
            provider="browser",
            keywords=keywords,
            period=(p["since"], p["until"]),
            partial=partial,
            notes=["profil logout di tengah job"] if partial else None,
        )


class ThreadsSearchHandler:
    kind = "threads_search"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        items = int(params.get("max_items") or 300)
        per_kw = 5 if params.get("provider") == "threads_api" else 60
        return JobEstimate(seconds=len(params.get("keywords") or [1]) * per_kw + items // 20)

    async def run(self, job: JobCtx) -> JobResult:
        p = job.params
        start, end = _period(job)
        keywords = list(p["keywords"])
        provider = p["provider"]
        notes: list[str] = list(job.checkpoint.get("notes", []))
        if provider == "threads_api":
            try:
                await self._api(job, start, end, keywords, notes)
            except ThreadsUnavailable:
                if not p.get("fallback_browser"):
                    raise
                provider = "browser"
                notes.append("threads_api_5xx_fallback_browser")
                await job.save_checkpoint({**job.checkpoint, "notes": notes, "fallback": True})
        if provider == "browser":
            await self._browser(job, start, end, keywords)
        stats_provider = provider if provider == p["provider"] else f"{p['provider']}->{provider}"
        return await social.build_result(
            job,
            source="threads",
            provider=stats_provider,
            keywords=keywords,
            period=(p["since"], p["until"]),
            notes=notes or None,
        )

    async def _api(
        self, job: JobCtx, start: datetime, end: datetime, keywords: list[str], notes: list[str]
    ) -> None:
        p, s = job.params, job.services.settings.social
        if s.threads_access_token is None:
            raise UpstreamError("THREADS_ACCESS_TOKEN tidak dikonfigurasi", retryable=False)
        client = ThreadsApiClient(
            job.services.http,
            s.threads_access_token.get_secret_value(),
            base_url=s.threads_api_base_url,
            sleep=_sleep(job),
        )
        per_kw = max(1, int(p.get("max_items") or 300) // len(keywords))
        ck = dict(job.checkpoint)
        for idx, kw in enumerate(keywords):
            if kw in ck.get("keywords_done", []):
                continue
            got = 0
            async for posts, after in client.search(
                kw,
                start,
                end,
                search_type=p.get("search_type", "RECENT"),
                search_mode=p.get("search_mode", "KEYWORD"),
                media_type=p.get("media_type"),
                max_items=per_kw,
                after=ck.get("after") if ck.get("keyword") == kw else None,
            ):
                await job.raise_if_cancelled()
                got += len(posts)
                await social.store_posts(job, posts, keywords)
                ck.update(keyword=kw, after=after)
                await job.save_checkpoint({**ck, "notes": notes})
            if got == 0:
                notes.append(f"keyword_returned_empty_possibly_filtered:{kw}")
            ck["keywords_done"] = [*ck.get("keywords_done", []), kw]
            ck.update(keyword=None, after=None)
            await job.save_checkpoint({**ck, "notes": notes})
            await job.progress(idx + 1, len(keywords), f"kata kunci {kw}: {got} post")

    async def _browser(
        self, job: JobCtx, start: datetime, end: datetime, keywords: list[str]
    ) -> None:
        p = job.params
        profile = p.get("profile")
        if not profile:
            found = await profile_store(job.services).for_site("threads.net")
            if found is None:
                raise ToolError("Tidak ada profil browser login threads.net", code="LOGIN_REQUIRED")
            profile = found.name
        per_kw = max(1, int(p.get("max_items") or 300) // len(keywords))
        done = list(job.checkpoint.get("browser_done", []))
        async with launcher_for(job.services).session(profile, job.job_id) as page:
            collector = ThreadsBrowserCollector(page, sleep=_sleep(job))
            for kw in keywords:
                if kw in done:
                    continue
                await job.raise_if_cancelled()
                posts = await collector.collect(
                    kw, start, end, per_kw, search_mode=p.get("search_mode", "KEYWORD")
                )
                await social.store_posts(job, posts, keywords)
                done.append(kw)
                await job.save_checkpoint({**job.checkpoint, "browser_done": done})
