"""Orkestrasi Google Trends: cache DB 24 jam, retry 429 + fallback provider, anchor, output."""

from __future__ import annotations

import asyncio
import contextlib
import random
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, TypeVar

from sqlalchemy import insert, select

from mcp_marketing.core.errors import ToolError, UpstreamError, ValidationFailed
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models.trends import TrendPoint, TrendQuery, TrendRelated
from mcp_marketing.services.analysis import trend_stats
from mcp_marketing.services.browser.driver_pool import launcher_for
from mcp_marketing.services.collectors.trends_base import (
    InterestResult,
    RegionValue,
    RelatedItem,
    RelatedResult,
    TrendsRateLimited,
)
from mcp_marketing.services.collectors.trends_google import (
    BrowserTransport,
    GoogleTrendsClient,
    HttpxTransport,
    OfficialTrendsProvider,
)
from mcp_marketing.services.results.cache import canonical_json
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.utils.crypto import sha256_hex
from mcp_marketing.utils.time import now

log = get_logger(__name__)
T = TypeVar("T")

BACKOFF_S = (30.0, 60.0, 120.0)
JITTER_S = 10.0
FALLBACKS = {
    "unofficial": ["unofficial", "browser"],
    "browser": ["browser", "unofficial"],
    "official": ["official", "unofficial", "browser"],
}
SERIES_COLS = ["date", "keyword", "value", "is_partial"]


def params_hash(query_kind: str, /, **params: Any) -> str:
    return sha256_hex(f"{query_kind}|{canonical_json(params)}")


@dataclass
class TrendsRecord:
    query_id: str
    kind: str
    provider: str
    cache_hit: bool
    interest: InterestResult | None = None
    regions: list[RegionValue] | None = None
    related: RelatedResult | None = None


class TrendsStore:
    def __init__(self, services: Any) -> None:
        self.db = services.db
        self.ttl = timedelta(seconds=services.settings.trends.trends_cache_ttl_s)

    async def _fresh(self, phash: str) -> TrendQuery | None:
        async with self.db.session() as session:
            return await session.scalar(
                select(TrendQuery)
                .where(TrendQuery.params_hash == phash, TrendQuery.fetched_at > now() - self.ttl)
                .order_by(TrendQuery.fetched_at.desc())
                .limit(1)
            )

    async def load(self, kind: str, phash: str) -> TrendsRecord | None:
        query = await self._fresh(phash)
        if query is None:
            return None
        async with self.db.session() as session:
            if kind == "related":
                rows = (
                    await session.execute(
                        select(
                            TrendRelated.term,
                            TrendRelated.value,
                            TrendRelated.position,
                            TrendRelated.rank_type,
                        )
                        .where(TrendRelated.query_id == query.id)
                        .order_by(TrendRelated.position)
                    )
                ).all()
                related = RelatedResult(
                    top=[
                        RelatedItem(r.term, r.value or 0, r.position)
                        for r in rows
                        if r.rank_type == "top"
                    ],
                    rising=[
                        RelatedItem(r.term, r.value or 0, r.position)
                        for r in rows
                        if r.rank_type == "rising"
                    ],
                )
                return TrendsRecord(str(query.id), kind, query.provider, True, related=related)
            points = (
                await session.execute(
                    select(
                        TrendPoint.keyword,
                        TrendPoint.bucket,
                        TrendPoint.region,
                        TrendPoint.value,
                        TrendPoint.is_partial,
                    ).where(TrendPoint.query_id == query.id)
                )
            ).all()
        if kind == "region":
            regions = sorted(
                (RegionValue(p.region or "", p.value) for p in points),
                key=lambda r: (-r.value, r.region),
            )
            return TrendsRecord(str(query.id), kind, query.provider, True, regions=regions)
        by_date: dict[date, dict[str, int]] = {}
        partial: dict[date, bool] = {}
        for p in points:
            if p.bucket is None:
                continue
            by_date.setdefault(p.bucket, {})[p.keyword] = p.value
            partial[p.bucket] = partial.get(p.bucket, False) or p.is_partial
        interest = InterestResult(
            keywords=list(query.keywords),
            points=[(d, by_date[d], partial[d]) for d in sorted(by_date)],
            resolution=query.resolution,
            provider=query.provider,
        )
        return TrendsRecord(str(query.id), kind, query.provider, True, interest=interest)

    async def save(
        self,
        kind: str,
        phash: str,
        *,
        keywords: list[str],
        geo: str,
        start: date,
        end: date,
        category: int,
        gprop: str,
        provider: str,
        interest: InterestResult | None = None,
        regions: list[RegionValue] | None = None,
        related: RelatedResult | None = None,
    ) -> str:
        query_id = uuid.uuid4()
        async with self.db.session() as session:
            session.add(
                TrendQuery(
                    id=query_id,
                    kind=kind.split("_")[0],
                    keywords=keywords,
                    geo=geo,
                    start_date=start,
                    end_date=end,
                    category=category,
                    gprop=gprop,
                    resolution=interest.resolution if interest else None,
                    provider=provider,
                    params_hash=phash,
                )
            )
            await session.flush()
            # tabel tanpa primary key asli (kolom nullable) -> insert Core, bukan identitas ORM
            points = [
                {
                    "query_id": query_id,
                    "keyword": kw,
                    "bucket": d,
                    "region": None,
                    "value": max(0, min(100, v)),
                    "is_partial": p,
                }
                for d, values, p in (interest.points if interest else [])
                for kw, v in values.items()
            ]
            points += [
                {
                    "query_id": query_id,
                    "keyword": keywords[0],
                    "bucket": None,
                    "region": r.region[:100],
                    "value": max(0, min(100, r.value)),
                    "is_partial": False,
                }
                for r in regions or []
            ]
            if points:
                await session.execute(insert(TrendPoint), points)
            if related is not None:
                rel_kind = "query" if kind == "related_queries" else "topic"
                rel_rows = [
                    {
                        "query_id": query_id,
                        "keyword": keywords[0],
                        "kind": rel_kind,
                        "rank_type": rank_type,
                        "term": i.term[:500],
                        "value": i.value,
                        "position": i.position,
                    }
                    for rank_type, items in (("top", related.top), ("rising", related.rising))
                    for i in items
                ]
                if rel_rows:
                    await session.execute(insert(TrendRelated), rel_rows)
        return str(query_id)


class TrendsService:
    def __init__(
        self,
        services: Any,
        *,
        job_id: str = "trends",
        sleep: Callable[[float], Awaitable[None]] | None = None,
        providers: dict[str, Any] | None = None,
    ) -> None:
        self.services = services
        self.settings = services.settings.trends
        self.job_id = job_id
        self.store = TrendsStore(services)
        self._sleep = sleep or services.extras.get("sleep") or asyncio.sleep
        self._providers = providers or {}
        self._stack = contextlib.AsyncExitStack()
        self.requests = 0

    async def __aenter__(self) -> TrendsService:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.aclose()

    async def _provider(self, name: str) -> Any:
        if name in self._providers:
            return self._providers[name]
        interval = self.settings.trends_min_interval_s
        if name == "unofficial":
            provider: Any = GoogleTrendsClient(
                HttpxTransport(self.services.http, self.settings.trends_base_url),
                name="unofficial",
                min_interval_s=interval,
                sleep=self._sleep,
            )
        elif name == "browser":
            page = await self._stack.enter_async_context(
                launcher_for(self.services).session("default", self.job_id)
            )
            provider = GoogleTrendsClient(
                BrowserTransport(page, self.settings.trends_base_url),
                name="browser",
                min_interval_s=interval,
                sleep=self._sleep,
            )
        else:
            key = self.settings.trends_official_api_key
            provider = OfficialTrendsProvider(key.get_secret_value() if key else None)
        self._providers[name] = provider
        return provider

    async def _call(self, fn: Callable[[Any], Awaitable[T]]) -> tuple[T, str]:
        errors: list[str] = []
        for name in FALLBACKS[self.settings.trends_provider]:
            try:
                provider = await self._provider(name)
            except ToolError as err:
                errors.append(f"{name}: {err.message}")
                continue
            for attempt in range(len(BACKOFF_S) + 1):
                try:
                    self.requests += 1
                    return await fn(provider), name
                except TrendsRateLimited:
                    if attempt >= len(BACKOFF_S):
                        errors.append(f"{name}: 429 berulang")
                        break
                    delay = BACKOFF_S[attempt] + random.uniform(0, JITTER_S)
                    log.info("trends.backoff", provider=name, delay_s=round(delay))
                    await self._sleep(delay)
                except UpstreamError as err:
                    errors.append(f"{name}: {err.message}")
                    break
        raise UpstreamError(
            "Semua provider Google Trends gagal", retryable=True, details={"errors": errors[-5:]}
        )

    async def _interest(
        self, keywords: list[str], start: date, end: date, geo: str, category: int, gprop: str
    ) -> tuple[InterestResult, str]:
        return await self._call(
            lambda p: p.interest_over_time(keywords, start, end, geo, category, gprop)
        )

    async def interest(
        self,
        keywords: list[str],
        start: date,
        end: date,
        geo: str,
        category: int,
        gprop: str,
        *,
        anchor: str | None = None,
    ) -> TrendsRecord:
        phash = params_hash(
            "interest",
            keywords=keywords,
            start=start,
            end=end,
            geo=geo,
            category=category,
            gprop=gprop,
            anchor=anchor,
        )
        cached = await self.store.load("interest", phash)
        if cached:
            return cached
        if len(keywords) > 5:
            if not anchor:
                raise ValidationFailed(
                    "Maksimal 5 kata kunci per permintaan; pecah atau isi `anchor`"
                )
            others = [k for k in keywords if k != anchor]
            groups = [[anchor, *others[i : i + 4]] for i in range(0, len(others), 4)]
            parts = []
            provider = ""
            for group in groups:
                res, provider = await self._interest(group, start, end, geo, category, gprop)
                parts.append(res)
            result = trend_stats.scale_with_anchor(parts, anchor)
        else:
            result, provider = await self._interest(keywords, start, end, geo, category, gprop)
        result.provider = result.provider or provider
        qid = await self.store.save(
            "interest",
            phash,
            keywords=result.keywords,
            geo=geo,
            start=start,
            end=end,
            category=category,
            gprop=gprop,
            provider=result.provider,
            interest=result,
        )
        return TrendsRecord(qid, "interest", result.provider, False, interest=result)

    async def region(
        self, keyword: str, start: date, end: date, geo: str, resolution: str
    ) -> TrendsRecord:
        phash = params_hash(
            "region", keyword=keyword, start=start, end=end, geo=geo, resolution=resolution
        )
        cached = await self.store.load("region", phash)
        if cached:
            return cached
        regions, provider = await self._call(
            lambda p: p.interest_by_region(keyword, start, end, geo, resolution)
        )
        qid = await self.store.save(
            "region",
            phash,
            keywords=[keyword],
            geo=geo,
            start=start,
            end=end,
            category=0,
            gprop="web",
            provider=provider,
            regions=regions,
        )
        return TrendsRecord(qid, "region", provider, False, regions=regions)

    async def related(
        self, keyword: str, start: date, end: date, geo: str, kind: str
    ) -> TrendsRecord:
        phash = params_hash("related", keyword=keyword, start=start, end=end, geo=geo, kind=kind)
        cached = await self.store.load("related", phash)
        if cached:
            return cached
        related, provider = await self._call(lambda p: p.related(keyword, start, end, geo, kind))
        qid = await self.store.save(
            f"related_{kind}",
            phash,
            keywords=[keyword],
            geo=geo,
            start=start,
            end=end,
            category=0,
            gprop="web",
            provider=provider,
            related=related,
        )
        return TrendsRecord(qid, "related", provider, False, related=related)


def previous_period(start: date, end: date) -> tuple[date, date]:
    length = (end - start).days + 1
    return start - timedelta(days=length), start - timedelta(days=1)


async def interest_output(
    services: Any,
    owner: Owner,
    record: TrendsRecord,
    prev: TrendsRecord | None,
    *,
    extra_requests: int = 0,
) -> tuple[dict[str, Any], str]:
    assert record.interest is not None
    interest = record.interest
    stats = trend_stats.summarize(interest, prev.interest if prev else None)
    table, granularity = trend_stats.resample_table(interest)
    rows = [
        {"date": d.isoformat(), "keyword": kw, "value": v, "is_partial": p}
        for d, values, p in interest.points
        for kw, v in values.items()
    ]
    rs = await services.results.create(
        kind="trend_points",
        columns=SERIES_COLS,
        rows=rows,
        owner=owner,
        sort_ts="date",
        sort_num="value",
        stats=stats,
        source_ref={"table": "trend_points", "query_id": record.query_id},
    )
    best = (
        max(stats["per_keyword"].items(), key=lambda kv: kv[1]["avg"])
        if stats["per_keyword"]
        else None
    )
    names = ", ".join(interest.keywords)[:80]
    summary = f"Google Trends {names}: {len(interest.points)} titik ({interest.resolution})"
    if best:
        summary += f"; tertinggi rata-rata {best[0]} ({best[1]['avg']})"
    stats.update(
        provider=record.provider, table_granularity=granularity, extra_requests=extra_requests
    )
    output = {
        "summary": summary[:300],
        "handle": rs.handle,
        "total": rs.total,
        "stats": stats,
        "items": table,
        "cost": {"llm_tokens": 0, "external_units": 0, "cache_hit": record.cache_hit},
    }
    return output, rs.handle


def region_output(record: TrendsRecord, keyword: str, top_n: int) -> dict[str, Any]:
    regions = (record.regions or [])[:top_n]
    return {
        "summary": f"{len(regions)} wilayah teratas untuk '{keyword}'"
        + (f" (puncak {regions[0].region})" if regions else ""),
        "cols": ["region", "value"],
        "rows": [[r.region, r.value] for r in regions],
        "provider": record.provider,
        "cache_hit": record.cache_hit,
    }


def related_output(record: TrendsRecord, keyword: str, kind: str) -> dict[str, Any]:
    related = record.related or RelatedResult()

    def table(items: list[RelatedItem]) -> dict[str, Any]:
        return {"cols": ["term", "value"], "rows": [[i.term, i.value] for i in items[:10]]}

    return {
        "summary": f"Related {kind} '{keyword}': {len(related.top)} top, "
        f"{len(related.rising)} rising",
        "top": table(related.top),
        "rising": table(related.rising),
        "provider": record.provider,
        "cache_hit": record.cache_hit,
    }


@contextlib.asynccontextmanager
async def trends_service(services: Any, job_id: str) -> AsyncIterator[TrendsService]:
    service = TrendsService(services, job_id=job_id)
    async with service:
        yield service
