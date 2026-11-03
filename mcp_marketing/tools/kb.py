"""Toolset `kb` (TASK-19, MCP_SPEC §4.9): knowledge base / vector DB per workspace."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from mcp_marketing.core.errors import Forbidden, NotFound
from mcp_marketing.models.dashboard import Project
from mcp_marketing.models.kb import KbCollection, KbDocument
from mcp_marketing.schemas.common import ReadInput, Table, ToolInput
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.db import entities as reg
from mcp_marketing.services.db import query as q
from mcp_marketing.services.db import write as w
from mcp_marketing.services.kb import store
from mcp_marketing.services.kb.embedder import embedder_for
from mcp_marketing.services.kb.identity import embed_identity
from mcp_marketing.services.kb.ingest import collection_name_for_project
from mcp_marketing.services.kb.search import search
from mcp_marketing.tools._jobs import enqueue_and_wait
from mcp_marketing.tools._scope import workspace_scope

COLLECTIONS = reg.entity("kb_collections")


async def visible_collection(ctx: ToolContext, collection_id: uuid.UUID) -> KbCollection:
    scope = await workspace_scope(ctx)
    async with ctx.services.db.session() as session:
        row = await q.get_one(session, COLLECTIONS, scope, collection_id, ["id"])
        coll = await session.get(KbCollection, collection_id) if row else None
    if coll is None:
        raise NotFound(f"Koleksi {collection_id} tidak ditemukan")
    return coll


async def project_in_scope(ctx: ToolContext, project_id: uuid.UUID) -> Project:
    scope = await workspace_scope(ctx)
    conds = [Project.id == project_id, Project.deleted_at.is_(None)]
    if scope.workspace_id is not None:
        conds.append(Project.workspace_id == scope.workspace_id)
    elif not scope.is_admin:
        raise Forbidden("Klien MCP belum dibatasi ke workspace Dashboard")
    async with ctx.services.db.session() as session:
        project = await session.scalar(select(Project).where(*conds))
    if project is None:
        raise NotFound(f"Project {project_id} tidak ditemukan")
    return project


async def project_collection_id(ctx: ToolContext, project_id: uuid.UUID) -> uuid.UUID | None:
    project = await project_in_scope(ctx, project_id)
    async with ctx.services.db.session() as session:
        return await session.scalar(
            select(KbCollection.id).where(
                KbCollection.workspace_id == project.workspace_id,
                KbCollection.name == collection_name_for_project(project_id),
            )
        )


# ------------------------------------------------------------------ koleksi
class CollectionsIn(ReadInput):
    pass


class CollectionsOut(BaseModel):
    summary: str
    items: Table


@tool(
    name="kb_collections",
    toolset="kb",
    summary="Daftar koleksi KB (dokumen, chunk, model embedding)",
    description="List knowledge-base collections visible to the caller with document/chunk "
    "counts and the locked embedding model.",
    input_model=CollectionsIn,
    output_model=CollectionsOut,
    cache_ttl_s=30,
)
async def kb_collections(inp: CollectionsIn, ctx: ToolContext) -> CollectionsOut:
    scope = await workspace_scope(ctx)
    async with ctx.services.db.session() as session:
        res = await q.run_query(
            session,
            COLLECTIONS,
            scope,
            fields=["id", "name", "embed_model"],
            limit=50,
            order_by="name",
        )
    ids = [uuid.UUID(r["id"]) for r in res.rows]
    counts = await store.collection_counts(ctx.services.db, ids)
    rows = [[r["id"], r["name"], *counts.get(r["id"], (0, 0)), r["embed_model"]] for r in res.rows]
    return CollectionsOut(
        summary=f"{len(rows)} koleksi",
        items=Table(cols=["id", "name", "documents", "chunks", "embed_model"], rows=rows),
    )


class CollectionCreateIn(ToolInput):
    name: str = Field(min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=2_000)
    project_id: uuid.UUID | None = None


class IdOut(BaseModel):
    id: str


@tool(
    name="kb_collection_create",
    toolset="kb",
    summary="Buat koleksi KB (model embedding dikunci)",
    description="Create a knowledge-base collection in the caller's workspace. The embedding "
    "model/dimension is locked at creation (EMBED_PROVIDER).",
    input_model=CollectionCreateIn,
    output_model=IdOut,
    writes=True,
)
async def kb_collection_create(inp: CollectionCreateIn, ctx: ToolContext) -> IdOut:
    if inp.project_id is not None:
        await project_in_scope(ctx, inp.project_id)
    scope = await workspace_scope(ctx)
    model, dim = embed_identity(ctx.settings)
    async with ctx.services.db.session() as session:
        out = await w.create(
            session,
            COLLECTIONS,
            scope,
            inp.model_dump(mode="json", exclude_none=True),
            defaults={"embed_model": model, "embed_dim": dim},
        )
    return IdOut(id=out["id"])


# ------------------------------------------------------------------ ingest & sync (job)
class KbJobOut(BaseModel):
    """Hasil job KB: ringkasan + hitungan dokumen/chunk/token embedding."""

    model_config = {"extra": "allow"}

    summary: str
    documents: int = 0
    chunks: int = 0
    tokens_embedded: int = 0


class IngestSource(BaseModel):
    type: Literal["text", "url", "handle", "report", "file_text"]
    value: str = Field(min_length=1, max_length=200_000)


class IngestIn(ToolInput):
    collection_id: uuid.UUID
    source: IngestSource
    metadata: dict[str, Any] | None = Field(default=None, max_length=20)
    chunk_size: int = Field(default=800, ge=200, le=2_000)
    overlap: int = Field(default=100, ge=0, le=400)
    wait_s: int = Field(default=15, ge=0, le=20)

    @model_validator(mode="after")
    def _check(self) -> IngestIn:
        if self.overlap >= self.chunk_size // 2:
            raise ValueError("overlap harus < chunk_size/2")
        if self.source.type == "url":
            from mcp_marketing.services.browser.ssrf import parse_http_url

            parse_http_url(self.source.value)
        return self


@tool(
    name="kb_ingest",
    toolset="kb",
    summary="Masukkan teks/URL/handle/laporan ke koleksi KB (job)",
    description="Ingest a source (text, url, handle of web_pages/social_posts, report id, "
    "file_text) into a collection: markdown-aware chunking + embedding. Idempotent: unchanged "
    "content is skipped.",
    input_model=IngestIn,
    output_model=KbJobOut,
    returns_job=True,
    writes=True,
)
async def kb_ingest(inp: IngestIn, ctx: ToolContext) -> Any:
    coll = await visible_collection(ctx, inp.collection_id)
    params = inp.model_dump(mode="json", exclude={"wait_s"}) | {"collection_id": str(coll.id)}
    return await enqueue_and_wait(ctx, "kb_ingest", params, wait_s=inp.wait_s, timeout_s=1_800)


class SyncIn(ToolInput):
    project_id: uuid.UUID
    full: bool = False
    wait_s: int = Field(default=15, ge=0, le=20)


@tool(
    name="kb_sync_dashboard",
    toolset="kb",
    summary="Sinkron Project Brief & task Dashboard ke KB (job)",
    description="Incrementally sync a Dashboard project (brief + tasks with their blocks) into "
    "collection dashboard:<project_id>. Unchanged documents are not re-embedded; full=true "
    "rebuilds.",
    input_model=SyncIn,
    output_model=KbJobOut,
    returns_job=True,
    writes=True,
)
async def kb_sync_dashboard(inp: SyncIn, ctx: ToolContext) -> Any:
    project = await project_in_scope(ctx, inp.project_id)
    params = {
        "project_id": str(project.id),
        "workspace_id": str(project.workspace_id),
        "full": inp.full,
    }
    return await enqueue_and_wait(
        ctx, "kb_sync_dashboard", params, wait_s=inp.wait_s, timeout_s=1_800
    )


# ------------------------------------------------------------------ search & delete
class SearchIn(ReadInput):
    query: str = Field(min_length=1, max_length=1_000)
    collection_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    top_k: int = Field(default=6, ge=1, le=20)
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid"
    filter: dict[str, Any] | None = Field(default=None, max_length=10)
    min_score: float | None = None

    @model_validator(mode="after")
    def _target(self) -> SearchIn:
        if (self.collection_id is None) == (self.project_id is None):
            raise ValueError("isi salah satu: collection_id atau project_id")
        return self


class SearchHit(BaseModel):
    chunk_id: str
    document_title: str | None
    source: str
    text: str
    score: float


class SearchOut(BaseModel):
    summary: str
    results: list[SearchHit]


async def run_search(
    ctx: ToolContext,
    coll: KbCollection,
    query: str,
    *,
    mode: str,
    top_k: int,
    filters: dict[str, Any] | None = None,
    min_score: float | None = None,
) -> list[SearchHit]:
    embedder = (
        None
        if mode == "keyword"
        else embedder_for(ctx.services, coll.embed_model, coll.embed_dim, ctx.budget)
    )
    hits = await search(
        ctx.services.db,
        [coll.id],
        query,
        mode=mode,  # type: ignore[arg-type]
        top_k=top_k,
        embedder=embedder,
        dim=coll.embed_dim,
        filters=filters,
        min_score=min_score,
    )
    return [
        SearchHit(
            chunk_id=h.chunk_id,
            document_title=h.document_title,
            source=h.source,
            text=h.text,
            score=round(h.score, 4),
        )
        for h in hits
    ]


@tool(
    name="kb_search",
    toolset="kb",
    summary="Cari potongan paling relevan di KB (hybrid/vector/keyword)",
    description="Semantic + keyword (RRF) search over one collection, or the dashboard "
    "collection of project_id. Returns top_k chunks (<=600 chars, adjacent chunks merged) "
    "with source and score.",
    input_model=SearchIn,
    output_model=SearchOut,
    cache_ttl_s=300,
)
async def kb_search(inp: SearchIn, ctx: ToolContext) -> SearchOut:
    if inp.collection_id is not None:
        coll = await visible_collection(ctx, inp.collection_id)
    else:
        assert inp.project_id is not None
        cid = await project_collection_id(ctx, inp.project_id)
        if cid is None:
            raise NotFound("Project belum disinkron ke KB; jalankan kb_sync_dashboard")
        coll = await visible_collection(ctx, cid)
    results = await run_search(
        ctx,
        coll,
        inp.query,
        mode=inp.mode,
        top_k=inp.top_k,
        filters=inp.filter,
        min_score=inp.min_score,
    )
    return SearchOut(summary=f"{len(results)} potongan dari '{coll.name}'", results=results)


class DeleteIn(ToolInput):
    document_id: uuid.UUID | None = None
    collection_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one(self) -> DeleteIn:
        if (self.document_id is None) == (self.collection_id is None):
            raise ValueError("isi salah satu: document_id atau collection_id")
        return self


class DeleteOut(BaseModel):
    deleted_chunks: int


@tool(
    name="kb_delete",
    toolset="kb",
    summary="Hapus dokumen atau seluruh koleksi KB",
    description="Delete one KB document (with its chunks) or a whole collection.",
    input_model=DeleteIn,
    output_model=DeleteOut,
    writes=True,
)
async def kb_delete(inp: DeleteIn, ctx: ToolContext) -> DeleteOut:
    if inp.collection_id is not None:
        coll = await visible_collection(ctx, inp.collection_id)
        return DeleteOut(deleted_chunks=await store.delete_collection(ctx.services.db, coll.id))
    async with ctx.services.db.session() as session:
        doc = await session.get(KbDocument, inp.document_id)
    if doc is None:
        raise NotFound(f"Dokumen {inp.document_id} tidak ditemukan")
    await visible_collection(ctx, doc.collection_id)
    return DeleteOut(deleted_chunks=await store.delete_document(ctx.services.db, doc.id))
