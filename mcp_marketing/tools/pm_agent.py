"""Toolset `pm_agent` — tool agent internal: baca konteks project Dashboard (kontrak §3.6)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from mcp_marketing.core.errors import Forbidden, ValidationFailed
from mcp_marketing.models import Run
from mcp_marketing.schemas.common import ToolInput
from mcp_marketing.server.registry import ToolContext, tool


class ContextQueryIn(ToolInput):
    query: str = Field(
        default="", max_length=500, description="Kata kunci; kosong = blok Project Brief"
    )
    scope: Literal["project", "task"] = "project"
    top_k: int = Field(default=6, ge=1, le=20)


class ContextHit(BaseModel):
    block_id: str | None = None
    task_title: str | None = None
    source: str | None = None
    text: str
    rank: float | None = None


class ContextQueryOut(BaseModel):
    summary: str
    results: list[ContextHit]


@tool(
    name="pm_query_project_context",
    toolset="pm_agent",
    summary="Cari konteks project/task di Dashboard (full-text)",
    description="Search the Dashboard project (or current task subtree) for relevant document "
    "blocks. "
    "Empty query returns the Project Brief. Text is truncated to 600 chars per result.",
    input_model=ContextQueryIn,
    output_model=ContextQueryOut,
    agent_only=True,
)
async def pm_query_project_context(inp: ContextQueryIn, ctx: ToolContext) -> ContextQueryOut:
    if not ctx.run_id:
        raise Forbidden("pm_query_project_context hanya tersedia di dalam agent run")
    if ctx.services.agent_api is None:
        raise ValidationFailed("Agent API Dashboard tidak dikonfigurasi")
    async with ctx.services.db.session() as session:
        run = await session.get(Run, uuid.UUID(ctx.run_id))
    if run is None:
        raise Forbidden("Run tidak dikenal")
    results = await ctx.services.agent_api.query_context(
        ctx.run_id,
        inp.query,
        scope=inp.scope,
        task_id=str(run.task_id) if inp.scope == "task" and run.task_id else None,
        top_k=inp.top_k,
        base_url=(run.callback or {}).get("agent_api_base_url"),
    )
    hits = [ContextHit(**r) for r in results]
    return ContextQueryOut(
        summary=f"{len(hits)} blok konteks untuk '{inp.query or 'project brief'}'", results=hits
    )
