"""Toolset `pm_agent` — tool agent internal: baca konteks project Dashboard (kontrak §3.6)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from mcp_marketing.core.errors import Forbidden, ToolError, ValidationFailed
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import Run
from mcp_marketing.schemas.common import ToolInput
from mcp_marketing.server.registry import ToolContext, tool

log = get_logger(__name__)
RRF_K = 60


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
    summary="Cari konteks project/task di Dashboard (full-text + KB hybrid)",
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
    kb_hits = await _kb_hits(ctx, run, inp) if inp.query.strip() else []
    if kb_hits:
        hits = fuse(hits, kb_hits, inp.top_k)
    return ContextQueryOut(
        summary=f"{len(hits)} blok konteks untuk '{inp.query or 'project brief'}'"
        + (f" (+KB hybrid {len(kb_hits)})" if kb_hits else ""),
        results=hits,
    )


async def _kb_hits(ctx: ToolContext, run: Run, inp: ContextQueryIn) -> list[ContextHit]:
    """Hasil hybrid koleksi `dashboard:<project_id>` (TASK-19); gagal -> diabaikan."""
    from mcp_marketing.models.kb import KbCollection
    from mcp_marketing.services.kb.ingest import collection_name_for_project
    from mcp_marketing.tools.kb import run_search

    try:
        async with ctx.services.db.session() as session:
            coll = await session.scalar(
                select(KbCollection).where(
                    KbCollection.workspace_id == run.workspace_id,
                    KbCollection.name == collection_name_for_project(run.project_id),
                )
            )
        if coll is None:
            return []
        found = await run_search(ctx, coll, inp.query, mode="hybrid", top_k=inp.top_k)
    except ToolError as exc:
        log.warning("pm_context.kb_failed", code=exc.code)
        return []
    return [
        ContextHit(task_title=h.document_title, source=f"kb:{h.source}", text=h.text) for h in found
    ]


def _key(text: str) -> str:
    return " ".join(text.lower().split())[:80]


def fuse(primary: list[ContextHit], extra: list[ContextHit], top_k: int) -> list[ContextHit]:
    """RRF (k=60) dua daftar; teks yang sama (80 karakter awal) hanya muncul sekali."""
    scores: dict[str, float] = {}
    first: dict[str, ContextHit] = {}
    for hits in (primary, extra):
        for rank, hit in enumerate(hits, 1):
            key = _key(hit.text)
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
            first.setdefault(key, hit)
    ordered = sorted(scores, key=lambda k: -scores[k])[:top_k]
    return [
        first[k].model_copy(update={"text": first[k].text[:600], "rank": round(scores[k], 5)})
        for k in ordered
    ]
