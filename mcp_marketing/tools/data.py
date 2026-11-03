"""Toolset `data`: baca, agregasi, dan export hasil besar lewat handle (MCP_SPEC §4.1, T2–T4)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from mcp_marketing.schemas.common import Envelope, ReadInput, ToolInput
from mcp_marketing.server.admin import sign_export
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.results.result_store import Owner

FilterOp = Literal[
    "eq",
    "ne",
    "lt",
    "lte",
    "gt",
    "gte",
    "in",
    "contains",
    "icontains",
    "starts_with",
    "is_null",
    "between",
]
HANDLE_PATTERN = r"^h_[0-9a-z]{10}$"


class FilterCond(BaseModel):
    op: FilterOp = "eq"
    value: Any = None


def _owner(ctx: ToolContext) -> Owner:
    return Owner(client_id=ctx.client.id, run_id=ctx.run_id, is_admin=ctx.client.is_admin)


def _filters(flt: dict[str, FilterCond] | None) -> dict[str, Any]:
    return {k: v.model_dump() for k, v in (flt or {}).items()}


class DataGetIn(ReadInput):
    handle: str = Field(pattern=HANDLE_PATTERN)
    fields: list[str] | None = Field(default=None, description="Proyeksi kolom")
    filter: dict[str, FilterCond] | None = Field(default=None, description="{kolom: {op, value}}")
    order_by: str | None = Field(default=None, description="kolom atau -kolom (menurun)")
    limit: int = Field(default=10, ge=1, le=20)
    cursor: str | None = None
    text_max_chars: int = Field(default=280, ge=20, le=2_000)


@tool(
    name="data_get",
    toolset="data",
    summary="Baca baris dari handle (filter, urut, paginasi)",
    description="Read rows from a result handle with optional field projection, filter "
    "({col:{op,value}}), order_by (col or -col), limit<=20 and cursor pagination.",
    input_model=DataGetIn,
    output_model=Envelope,
)
async def data_get(inp: DataGetIn, ctx: ToolContext) -> dict[str, Any]:
    return await ctx.services.results.page(
        inp.handle,
        _owner(ctx),
        fields=inp.fields,
        flt=_filters(inp.filter),
        order_by=inp.order_by,
        limit=inp.limit,
        cursor=inp.cursor,
        text_max_chars=inp.text_max_chars,
    )


class DataAggregateIn(ToolInput):
    handle: str = Field(pattern=HANDLE_PATTERN)
    group_by: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="kolom atau hour/day/week/month (waktu baris)",
    )
    metrics: list[str] = Field(
        default_factory=lambda: ["count"],
        min_length=1,
        max_length=10,
        description="count, sum:x, avg:x, min:x, max:x, p50:x, p90:x, top:x:n",
    )
    time_bucket: Literal["hour", "day", "week", "month"] | None = None
    filter: dict[str, FilterCond] | None = None
    limit: int = Field(default=50, ge=1, le=50)


class AggregateOut(BaseModel):
    summary: str
    cols: list[str]
    rows: list[list[Any]]


@tool(
    name="data_aggregate",
    toolset="data",
    summary="Agregasi server-side atas handle (count/sum/avg/p90/top)",
    description="Aggregate a result handle server-side: group_by<=3 (columns or hour/day/week/"
    "month), metrics count,sum:x,avg:x,min:x,max:x,p50:x,p90:x,top:x:n. Returns {cols,rows}.",
    input_model=DataAggregateIn,
    output_model=AggregateOut,
)
async def data_aggregate(inp: DataAggregateIn, ctx: ToolContext) -> dict[str, Any]:
    return await ctx.services.results.aggregate(
        inp.handle,
        _owner(ctx),
        group_by=inp.group_by,
        metrics=inp.metrics,
        time_bucket=inp.time_bucket,
        limit=inp.limit,
        flt=_filters(inp.filter),
    )


class DataExportIn(ToolInput):
    handle: str = Field(pattern=HANDLE_PATTERN)
    format: Literal["csv", "jsonl", "xlsx"] = "csv"


class DataExportOut(BaseModel):
    summary: str
    download_url: str
    rows: int


@tool(
    name="data_export",
    toolset="data",
    summary="Export handle ke CSV/JSONL/XLSX (link 1 jam)",
    description="Export all rows of a handle to csv, jsonl or xlsx; returns a signed download URL "
    "valid for 1 hour.",
    input_model=DataExportIn,
    output_model=DataExportOut,
    timeout_s=60,
)
async def data_export(inp: DataExportIn, ctx: ToolContext) -> DataExportOut:
    path, rows = await ctx.services.results.export(inp.handle, _owner(ctx), inp.format)
    return DataExportOut(
        summary=f"{rows} baris {inp.handle} diekspor ke {inp.format}",
        download_url=sign_export(ctx.services, path.name),
        rows=rows,
    )
