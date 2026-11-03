"""Toolset `db` / `db_admin` (TASK-18, MCP_SPEC §4.8): akses data yang aman & hemat token."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.exc import DBAPIError

from mcp_marketing.core.errors import Forbidden, ToolError, UpstreamTimeout, ValidationFailed
from mcp_marketing.schemas.common import Envelope, ReadInput, Table, ToolInput
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.db import entities as reg
from mcp_marketing.services.db import query as q
from mcp_marketing.services.db import write as w
from mcp_marketing.services.db.sql_readonly import execute_readonly
from mcp_marketing.services.kb.identity import embed_identity
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.tools._scope import workspace_scope

INLINE_ROWS = 20
SAMPLE_ROWS = 5


def _db_error(exc: DBAPIError) -> ToolError:
    """Pesan DB disederhanakan (tanpa SQL/parameter) agar tidak membocorkan detail."""
    msg = str(exc.orig).splitlines()[0] if exc.orig else type(exc).__name__
    if "statement timeout" in msg:
        return UpstreamTimeout("Query melebihi batas 5 detik; persempit filter")
    if "read-only transaction" in msg or "permission denied" in msg:
        return Forbidden("Query membutuhkan hak yang tidak dimiliki (read-only)")
    return ValidationFailed(f"Query gagal: {msg[:200]}")


# ------------------------------------------------------------------ db_entities
class EntitiesIn(ToolInput):
    pass


class EntitiesOut(BaseModel):
    entities: list[dict[str, Any]]


@tool(
    name="db_entities",
    toolset="db",
    summary="Whitelist entitas DB (field:tipe, akses r/rw)",
    description=(
        "List entities usable with db_query/db_get/db_create/db_update/db_delete: schema, "
        "access (r, rw, rd=read+delete) and fields as name:type. Core Dashboard entities are "
        "read-only."
    ),
    input_model=EntitiesIn,
    output_model=EntitiesOut,
    cache_ttl_s=3_600,
)
async def db_entities(inp: EntitiesIn, ctx: ToolContext) -> EntitiesOut:
    return EntitiesOut(entities=reg.describe())


# ------------------------------------------------------------------ db_query / db_get
class QueryIn(ReadInput):
    entity: str = Field(max_length=40)
    filter: dict[str, Any] | None = Field(
        default=None, description='{field: value} atau {field: {"op": "gte", "value": x}}'
    )
    fields: list[str] | None = Field(default=None, max_length=30)
    search: str | None = Field(default=None, max_length=200, description="teks bebas (ilike)")
    order_by: str | None = Field(default=None, max_length=60, description="field / -field")
    limit: int = Field(default=20, ge=1, le=q.MAX_LIMIT)
    cursor: str | None = Field(default=None, max_length=500)


@tool(
    name="db_query",
    toolset="db",
    summary="Query entitas DB terstruktur (filter/fields/order, cursor)",
    description=(
        "Structured, workspace-scoped query on a whitelisted entity. Filter ops: eq, ne, lt, "
        "lte, gt, gte, in, contains, icontains, starts_with, is_null, between. >20 rows -> "
        "handle + 5 samples."
    ),
    input_model=QueryIn,
    output_model=Envelope,
    cache_ttl_s=30,
)
async def db_query(inp: QueryIn, ctx: ToolContext) -> Envelope:
    spec = reg.entity(inp.entity)
    scope = await workspace_scope(ctx)
    try:
        async with ctx.services.db.session() as session:
            res = await q.run_query(
                session,
                spec,
                scope,
                filters=inp.filter,
                fields=inp.fields,
                search=inp.search,
                order_by=inp.order_by,
                limit=inp.limit,
                cursor=inp.cursor,
            )
    except DBAPIError as exc:
        raise _db_error(exc) from None
    stats = {"entity": spec.name, "matched": res.total, "matched_capped": res.total_capped}
    rows = [[r[c] for c in res.cols] for r in res.rows]
    summary = f"{len(rows)} baris {spec.name} dari {res.total}{'+' if res.total_capped else ''}"
    if len(rows) <= INLINE_ROWS:
        return Envelope(
            summary=summary,
            total=res.total,
            stats=stats,
            items=Table(cols=res.cols, rows=rows),
            next_cursor=res.next_cursor,
        )
    rs = await ctx.services.results.create(
        kind=f"db:{spec.name}",
        columns=res.cols,
        rows=res.rows,
        owner=Owner(client_id=ctx.client.id, run_id=ctx.run_id),
        stats=stats,
        source_ref={"tool": "db_query", "entity": spec.name},
    )
    return Envelope(
        summary=summary,
        handle=rs.handle,
        total=res.total,
        stats=stats,
        items=Table(cols=res.cols, rows=rows[:SAMPLE_ROWS]),
        next_cursor=res.next_cursor,
        hint="baca sisa baris dengan data_get(handle)",
    )


class GetIn(ReadInput):
    entity: str = Field(max_length=40)
    id: str = Field(max_length=100)
    fields: list[str] | None = Field(default=None, max_length=30)


class GetOut(BaseModel):
    entity: str
    data: dict[str, Any]


@tool(
    name="db_get",
    toolset="db",
    summary="Ambil satu baris entitas berdasarkan id",
    description="Get one row of a whitelisted entity by id (workspace-scoped). fields? limits "
    "the projection; default = all readable fields.",
    input_model=GetIn,
    output_model=GetOut,
    cache_ttl_s=30,
)
async def db_get(inp: GetIn, ctx: ToolContext) -> GetOut:
    spec = reg.entity(inp.entity)
    scope = await workspace_scope(ctx)
    async with ctx.services.db.session() as session:
        row = await q.get_one(session, spec, scope, inp.id, inp.fields)
    if row is None:
        from mcp_marketing.core.errors import NotFound

        raise NotFound(f"{spec.name} {inp.id} tidak ditemukan")
    return GetOut(entity=spec.name, data=row)


# ------------------------------------------------------------------ tulis
class CreateIn(ToolInput):
    entity: str = Field(max_length=40)
    data: dict[str, Any]


class CreateOut(BaseModel):
    id: str


def _defaults(spec: reg.EntitySpec, ctx: ToolContext) -> dict[str, Any]:
    if spec.name == "kb_collections":
        model, dim = embed_identity(ctx.settings)
        return {"embed_model": model, "embed_dim": dim}
    return {}


@tool(
    name="db_create",
    toolset="db",
    summary="Buat baris entitas rw (kompetitor, watchlist, koleksi KB)",
    description="Create a row in a read-write MCP entity (competitors, watchlists, "
    "kb_collections). Validated per entity; workspace is set from the client/run.",
    input_model=CreateIn,
    output_model=CreateOut,
    writes=True,
)
async def db_create(inp: CreateIn, ctx: ToolContext) -> CreateOut:
    spec = reg.entity(inp.entity)
    scope = await workspace_scope(ctx)
    async with ctx.services.db.session() as session:
        out = await w.create(session, spec, scope, inp.data, defaults=_defaults(spec, ctx))
    return CreateOut(id=out["id"])


class UpdateIn(ToolInput):
    entity: str = Field(max_length=40)
    id: str = Field(max_length=100)
    patch: dict[str, Any]
    expected_updated_at: datetime | None = Field(
        default=None, description="optimistic locking: updated_at terakhir yang dibaca"
    )


class UpdateOut(BaseModel):
    id: str
    updated_fields: list[str]
    updated_at: str | None = None


@tool(
    name="db_update",
    toolset="db",
    summary="Ubah baris entitas rw (optimistic locking)",
    description="Patch a read-write MCP entity row. Pass expected_updated_at to fail with "
    "CONFLICT (and the current updated_at) if someone else changed it first.",
    input_model=UpdateIn,
    output_model=UpdateOut,
    writes=True,
)
async def db_update(inp: UpdateIn, ctx: ToolContext) -> UpdateOut:
    spec = reg.entity(inp.entity)
    scope = await workspace_scope(ctx)
    async with ctx.services.db.session() as session:
        out = await w.update_row(
            session, spec, scope, inp.id, inp.patch, expected_updated_at=inp.expected_updated_at
        )
    return UpdateOut(**out)


class DeleteIn(ToolInput):
    entity: str = Field(max_length=40)
    id: str = Field(max_length=100)
    hard: bool = False


class DeleteOut(BaseModel):
    deleted: bool
    mode: str


@tool(
    name="db_delete",
    toolset="db",
    summary="Hapus baris entitas rw (default soft delete)",
    description="Delete a row of a read-write MCP entity (or a report). Soft delete by default; "
    "hard=true only for admin clients.",
    input_model=DeleteIn,
    output_model=DeleteOut,
    writes=True,
)
async def db_delete(inp: DeleteIn, ctx: ToolContext) -> DeleteOut:
    spec = reg.entity(inp.entity)
    scope = await workspace_scope(ctx)
    async with ctx.services.db.session() as session:
        out = await w.delete_row(session, spec, scope, inp.id, hard=inp.hard)
    return DeleteOut(**out)


# ------------------------------------------------------------------ db_sql_readonly
class SqlIn(ToolInput):
    sql: str = Field(max_length=10_000)
    params: dict[str, Any] | None = Field(default=None, description="bind :nama -> nilai skalar")
    limit: int = Field(default=100, ge=1, le=1_000)


@tool(
    name="db_sql_readonly",
    toolset="db_admin",
    summary="SQL SELECT bebas (admin, read-only, timeout 5 s)",
    description="Admin-only single SELECT/WITH query over schemas mcp/public in a READ ONLY "
    "transaction with a 5 s timeout; LIMIT forced <= 1000; >20 rows -> handle.",
    input_model=SqlIn,
    output_model=Envelope,
    admin_only=True,
)
async def db_sql_readonly(inp: SqlIn, ctx: ToolContext) -> Envelope:
    if not ctx.client.is_admin:
        raise Forbidden("db_sql_readonly hanya untuk klien admin")
    try:
        async with ctx.services.db.session() as session:
            res = await execute_readonly(session, inp.sql, inp.params, inp.limit)
    except DBAPIError as exc:
        raise _db_error(exc) from None
    summary = f"{len(res.rows)} baris{' (dipotong LIMIT)' if res.truncated else ''}"
    if len(res.rows) <= INLINE_ROWS:
        return Envelope(
            summary=summary,
            total=len(res.rows),
            truncated=res.truncated,
            items=Table(cols=res.cols, rows=res.rows),
        )
    rs = await ctx.services.results.create(
        kind="db:sql",
        columns=res.cols,
        rows=(dict(zip(res.cols, r, strict=False)) for r in res.rows),
        owner=Owner(client_id=ctx.client.id, run_id=ctx.run_id),
        source_ref={"tool": "db_sql_readonly"},
    )
    return Envelope(
        summary=summary,
        handle=rs.handle,
        total=len(res.rows),
        truncated=res.truncated,
        items=Table(cols=res.cols, rows=res.rows[:SAMPLE_ROWS]),
    )
