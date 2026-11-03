"""Toolset `trends`: Google Trends dengan statistik siap pakai (MCP_SPEC §4.7, TASK-17).

Cache hit (≤ TRENDS_CACHE_TTL_S) dijawab sinkron tanpa memanggil Google; selain itu job worker
dengan `wait_s` default 15.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from mcp_marketing.schemas.common import Envelope, Table, ToolInput
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.collectors.trends_service import (
    TrendsStore,
    interest_output,
    params_hash,
    previous_period,
    region_output,
    related_output,
)
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.tools._jobs import enqueue_and_wait
from mcp_marketing.utils.time import today_in

MAX_PERIOD_YEARS = 20


class _Period(ToolInput):
    start: date
    end: date
    geo: str = Field(default="ID", max_length=16, pattern=r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$|^$")
    wait_s: int = Field(default=15, ge=0, le=20)

    @model_validator(mode="after")
    def _range(self) -> _Period:
        if self.end <= self.start:
            raise ValueError("end harus > start")
        if (self.end - self.start).days > 366 * MAX_PERIOD_YEARS:
            raise ValueError("periode terlalu panjang")
        return self


class InterestIn(_Period):
    keywords: list[str] = Field(min_length=1, max_length=20)
    category: int = Field(default=0, ge=0)
    gprop: Literal["web", "news", "images", "youtube", "froogle"] = "web"
    anchor: str | None = Field(
        default=None,
        max_length=100,
        description=("Wajib bila keywords > 5: tiap grup 4 + anchor lalu diskalakan"),
    )
    compare_previous: bool = Field(
        default=True, description="Hitung perubahan vs periode sebelumnya (+1 permintaan)"
    )

    @field_validator("keywords")
    @classmethod
    def _kw(cls, v: list[str]) -> list[str]:
        cleaned = [k.strip() for k in v if k.strip()]
        if any(len(k) > 100 for k in cleaned) or not cleaned:
            raise ValueError("keywords 1–100 karakter")
        return cleaned

    @model_validator(mode="after")
    def _anchor(self) -> InterestIn:
        if len(self.keywords) > 5 and not self.anchor:
            raise ValueError("Maksimal 5 kata kunci per permintaan; pecah atau isi anchor")
        return self


async def _cached(ctx: ToolContext, kind: str, phash: str) -> Any:
    return await TrendsStore(ctx.services).load(kind, phash)


def _clamp_end(end: date, ctx: ToolContext) -> date:
    return min(end, today_in(ctx.settings.timezone))


@tool(
    name="trends_interest",
    toolset="trends",
    summary="Minat pencarian Google Trends + statistik (avg, puncak, slope, share)",
    description=(
        "Google Trends interest over time for <=5 keywords (or more with an anchor). Returns a "
        "handle of the series, per-keyword stats (avg, peak, slope/week, change vs previous "
        "period) and a weekly/monthly table."
    ),
    input_model=InterestIn,
    output_model=Envelope,
    returns_job=True,
)
async def trends_interest(inp: InterestIn, ctx: ToolContext) -> Any:
    end = _clamp_end(inp.end, ctx)
    phash = params_hash(
        "interest",
        keywords=inp.keywords,
        start=inp.start,
        end=end,
        geo=inp.geo,
        category=inp.category,
        gprop=inp.gprop,
        anchor=inp.anchor,
    )
    record = await _cached(ctx, "interest", phash)
    if record is not None:
        prev = None
        if inp.compare_previous and len(inp.keywords) <= 5:
            ps, pe = previous_period(inp.start, end)
            prev = await _cached(
                ctx,
                "interest",
                params_hash(
                    "interest",
                    keywords=inp.keywords,
                    start=ps,
                    end=pe,
                    geo=inp.geo,
                    category=inp.category,
                    gprop=inp.gprop,
                    anchor=None,
                ),
            )
        if prev is not None or not inp.compare_previous or len(inp.keywords) > 5:
            owner = Owner(client_id=ctx.client.id, run_id=ctx.run_id)
            output, _ = await interest_output(ctx.services, owner, record, prev)
            return output
    params = inp.model_dump(mode="json", exclude={"wait_s"}) | {"end": end.isoformat()}
    return await enqueue_and_wait(
        ctx, "trends_interest", params, wait_s=inp.wait_s, timeout_s=1_800
    )


class RegionIn(_Period):
    keyword: str = Field(min_length=1, max_length=100)
    resolution: Literal["REGION", "CITY"] = "REGION"
    top_n: int = Field(default=10, ge=1, le=20)


class RegionOut(BaseModel):
    summary: str
    cols: list[str]
    rows: list[list[Any]]
    provider: str | None = None
    cache_hit: bool = False


@tool(
    name="trends_by_region",
    toolset="trends",
    summary="Minat Google Trends per provinsi/kota (top_n ≤ 20)",
    description=(
        "Google Trends interest by region (REGION=province or CITY) for one keyword; top_n <= "
        "20 rows."
    ),
    input_model=RegionIn,
    output_model=RegionOut,
    returns_job=True,
)
async def trends_by_region(inp: RegionIn, ctx: ToolContext) -> Any:
    end = _clamp_end(inp.end, ctx)
    phash = params_hash(
        "region",
        keyword=inp.keyword,
        start=inp.start,
        end=end,
        geo=inp.geo,
        resolution=inp.resolution,
    )
    record = await _cached(ctx, "region", phash)
    if record is not None:
        return region_output(record, inp.keyword, inp.top_n)
    params = inp.model_dump(mode="json", exclude={"wait_s"}) | {"end": end.isoformat()}
    return await enqueue_and_wait(ctx, "trends_region", params, wait_s=inp.wait_s, timeout_s=900)


class RelatedIn(_Period):
    keyword: str = Field(min_length=1, max_length=100)
    kind: Literal["queries", "topics"] = "queries"


class RelatedOut(BaseModel):
    summary: str
    top: Table
    rising: Table
    provider: str | None = None
    cache_hit: bool = False


@tool(
    name="trends_related",
    toolset="trends",
    summary="Related queries/topics Google Trends (top & rising ≤ 10)",
    description=(
        "Related queries or topics for a keyword: top and rising lists (<=10 each; Breakout=5000)."
    ),
    input_model=RelatedIn,
    output_model=RelatedOut,
    returns_job=True,
)
async def trends_related(inp: RelatedIn, ctx: ToolContext) -> Any:
    end = _clamp_end(inp.end, ctx)
    phash = params_hash(
        "related", keyword=inp.keyword, start=inp.start, end=end, geo=inp.geo, kind=inp.kind
    )
    record = await _cached(ctx, "related", phash)
    if record is not None:
        return related_output(record, inp.keyword, inp.kind)
    params = inp.model_dump(mode="json", exclude={"wait_s"}) | {"end": end.isoformat()}
    return await enqueue_and_wait(ctx, "trends_related", params, wait_s=inp.wait_s, timeout_s=900)
