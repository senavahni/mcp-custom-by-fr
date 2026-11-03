"""Toolset `analysis` — sentimen (TASK-20). Pasar/campaign/strategi ditambahkan di TASK-21..23."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.models.results import ResultItem
from mcp_marketing.schemas.common import Cost, ReadInput, ToolInput
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.nlp import aggregate
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.tools._jobs import enqueue_and_wait
from mcp_marketing.utils.tokens import count_tokens

MAX_INLINE_TEXTS = 300
TOKENS_PER_ITEM_LLM = 70  # perkiraan: teks <=280 karakter + baris jawaban
HANDLE_PATTERN = r"^h_[0-9a-z]{10}$"


class SentimentSource(BaseModel):
    handle: str | None = Field(default=None, pattern=HANDLE_PATTERN)
    texts: list[str] | None = Field(default=None, max_length=MAX_INLINE_TEXTS)

    @model_validator(mode="after")
    def _one(self) -> SentimentSource:
        if bool(self.handle) == bool(self.texts):
            raise ValueError("isi salah satu: handle atau texts (<=300)")
        if self.texts and any(len(t) > 5_000 for t in self.texts):
            raise ValueError("tiap teks maksimal 5.000 karakter")
        return self


class SentimentIn(ToolInput):
    source: SentimentSource
    aspects: list[str] | None = Field(default=None, max_length=10)
    method: Literal["auto", "model", "llm"] = "auto"
    language: Literal["auto", "id", "en"] = "auto"
    group_by: Literal["day", "keyword", "source", "author"] | None = None
    confirm_cost: bool = Field(
        default=False, description="wajib true untuk method=llm setelah melihat estimasi"
    )
    wait_s: int = Field(default=20, ge=0, le=20)


class SentimentOut(BaseModel):
    model_config = {"extra": "allow"}

    summary: str
    handle: str | None = None
    distribution: dict[str, float] | None = None
    net_sentiment: float | None = None
    estimate: dict[str, Any] | None = None
    cost: Cost | dict[str, Any] | None = None


async def _item_count(ctx: ToolContext, source: SentimentSource) -> int:
    if source.texts:
        return len(source.texts)
    owner = Owner(client_id=ctx.client.id, run_id=ctx.run_id, is_admin=ctx.client.is_admin)
    async with ctx.services.db.session() as session:
        rs = await ctx.services.results.get_set(session, str(source.handle), owner)
    return int(rs.total)


@tool(
    name="sentiment_analyze",
    toolset="analysis",
    summary="Sentimen ID/EN (model lokal + LLM untuk kasus ragu) + agregasi",
    description=(
        "Sentiment for a social_posts handle or <=300 texts: local model first, LLM fast only "
        "for low-confidence/sarcastic items; optional aspects. Returns distribution, net, "
        "groups, examples, per-item handle, cost. method=llm needs confirm_cost."
    ),
    input_model=SentimentIn,
    output_model=SentimentOut,
    returns_job=True,
)
async def sentiment_analyze(inp: SentimentIn, ctx: ToolContext) -> Any:
    n = await _item_count(ctx, inp.source)
    if inp.method == "llm" and not inp.confirm_cost:
        sample = inp.source.texts or []
        per_item = (
            count_tokens([t[:280] for t in sample]) / max(1, len(sample)) + 10
            if sample
            else TOKENS_PER_ITEM_LLM
        )
        estimate = {"items": n, "llm_tokens": int(per_item * n + 150 * (n // 50 + 1))}
        return SentimentOut(
            summary=f"method=llm untuk {n} teks perlu konfirmasi biaya",
            estimate=estimate,
            hint="panggil ulang dengan confirm_cost=true, atau pakai method=auto (lebih hemat)",
        )
    if inp.aspects and any(not a.strip() or len(a) > 40 for a in inp.aspects):
        raise ValidationFailed("aspek 1–40 karakter", details={"field": "aspects"})
    params = inp.model_dump(mode="json", exclude={"wait_s", "confirm_cost"})
    wait = inp.wait_s if n <= MAX_INLINE_TEXTS else 0
    return await enqueue_and_wait(ctx, "sentiment_analyze", params, wait_s=wait, timeout_s=3_600)


class SummaryIn(ReadInput):
    handle: str = Field(pattern=HANDLE_PATTERN)
    group_by: Literal["day", "keyword", "source", "author"] = "day"
    compare_handle: str | None = Field(default=None, pattern=HANDLE_PATTERN)


class SummaryOut(BaseModel):
    summary: str
    distribution: dict[str, float]
    net_sentiment: float
    total: int
    by_group: dict[str, Any]
    aspects: dict[str, Any] | None = None
    compare: dict[str, Any] | None = None


async def _sentiment_rows(ctx: ToolContext, handle: str) -> list[dict[str, Any]]:
    owner = Owner(client_id=ctx.client.id, run_id=ctx.run_id, is_admin=ctx.client.is_admin)
    async with ctx.services.db.session() as session:
        rs = await ctx.services.results.get_set(session, handle, owner)
        if rs.kind != "sentiment":
            raise ValidationFailed(f"handle {handle} bukan hasil sentiment_analyze")
        data = (
            await session.scalars(
                select(ResultItem.data)
                .where(ResultItem.handle == rs.handle)
                .order_by(ResultItem.idx)
            )
        ).all()
    return [dict(d) for d in data]


@tool(
    name="sentiment_summary",
    toolset="analysis",
    summary="Ringkasan sentimen dari handle (per grup, banding periode), tanpa LLM",
    description="Aggregate a sentiment_analyze handle by day/keyword/source/author and "
    "optionally compare with another sentiment handle (deltas). No LLM tokens.",
    input_model=SummaryIn,
    output_model=SummaryOut,
    cache_ttl_s=300,
)
async def sentiment_summary(inp: SummaryIn, ctx: ToolContext) -> SummaryOut:
    rows = await _sentiment_rows(ctx, inp.handle)
    dist = aggregate.distribution(rows)
    compare = None
    if inp.compare_handle:
        other = aggregate.distribution(await _sentiment_rows(ctx, inp.compare_handle))
        compare = aggregate.compare(dist, other)
    return SummaryOut(
        summary=f"{dist['total']} teks, net {dist['net_sentiment']:+.2f}",
        distribution=dist["pct"],
        net_sentiment=dist["net_sentiment"],
        total=dist["total"],
        by_group=aggregate.by_group(rows, inp.group_by, ctx.settings.timezone),
        aspects=aggregate.aspects_table(rows),
        compare=compare,
    )
