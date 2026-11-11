"""Toolset `kn` (MCP-V1-11): aset pengetahuan (buku/brand/artikel) & pencarian bersitasi.

Workspace: klien ber-workspace hanya melihat workspace-nya; klien tanpa workspace (Dashboard
multi-workspace, seperti toolset `intel`) WAJIB mengirim `workspace_id` (Dashboard yang RBAC).
Hak cipta (V1-K15): `kn_search` mengembalikan potongan <= 600 karakter + sitasi halaman; teks
chunk penuh hanya untuk klien admin.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import func, select, text, update

from mcp_marketing.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from mcp_marketing.models.kb import KbCollection, KbDocument
from mcp_marketing.models.knowledge import KnowledgeAsset, Upload
from mcp_marketing.schemas.common import ReadInput, ToolInput
from mcp_marketing.schemas.intel_v13 import PageIn, PageMeta
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.kb.embedder import embedder_for
from mcp_marketing.services.kb.search import search
from mcp_marketing.services.knowledge.ingest import (
    SOURCE_TYPE,
    books_collection_name,
    emit_status,
    source_ref,
)
from mcp_marketing.tools._jobs import enqueue_and_wait
from mcp_marketing.tools._scope import workspace_scope
from mcp_marketing.utils.time import now

TOOLSET = "kn"
SNIPPET_MAX_CHARS = 600
INGEST_TIMEOUT_S = 4 * 3_600
AssetKind = Literal["book", "brand", "article"]
AssetStatus = Literal["registered", "extracting", "embedding", "carding", "ready", "failed"]
Text300 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


async def resolve_workspace(ctx: ToolContext, requested: uuid.UUID | None) -> uuid.UUID:
    scope = await workspace_scope(ctx)
    if scope.workspace_id is not None:
        if requested is not None and requested != scope.workspace_id:
            raise Forbidden("workspace_id berbeda dengan workspace klien")
        return scope.workspace_id
    if requested is None:
        raise ValidationFailed("workspace_id wajib untuk klien tanpa workspace")
    return requested


async def asset_in_ws(ctx: ToolContext, ws: uuid.UUID, asset_id: uuid.UUID) -> KnowledgeAsset:
    async with ctx.services.db.session() as session:
        asset = await session.get(KnowledgeAsset, asset_id)
    if asset is None or asset.workspace_id != ws or asset.deleted_at is not None:
        raise NotFound(f"Aset {asset_id} tidak ditemukan")
    return asset


class AssetView(BaseModel):
    asset_id: str
    kind: AssetKind
    title: str
    authors: list[str]
    edition: str | None
    year: int | None
    isbn: str | None
    language: str
    license_note: str | None
    pages: int | None
    status: AssetStatus
    stats: dict[str, Any]
    error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


def asset_view(a: KnowledgeAsset) -> AssetView:
    return AssetView(
        asset_id=str(a.id),
        kind=a.kind,  # type: ignore[arg-type]
        title=a.title,
        authors=list(a.authors or []),
        edition=a.edition,
        year=a.year,
        isbn=a.isbn,
        language=a.language,
        license_note=a.license_note,
        pages=a.pages,
        status=a.status,  # type: ignore[arg-type]
        stats=a.stats or {},
        error=a.error,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


# ---------------------------------------------------------------------------- register & ingest


class AssetRegisterIn(ToolInput):
    workspace_id: uuid.UUID | None = None
    upload_id: uuid.UUID
    kind: AssetKind = "book"
    title: Text300
    authors: list[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default_factory=list, max_length=20
    )
    edition: Annotated[str, StringConstraints(max_length=50)] | None = None
    year: int | None = Field(default=None, ge=1800, le=2100)
    isbn: Annotated[str, StringConstraints(pattern=r"^[0-9Xx\-]{10,17}$")] | None = None
    language: Literal["id", "en"] = "id"
    license_note: Annotated[str, StringConstraints(max_length=1_000)] | None = None


@tool(
    name="kn_asset_register",
    toolset=TOOLSET,
    summary="Daftarkan PDF hasil POST /uploads sebagai aset buku/brand/artikel",
    description="Register an uploaded PDF (upload_id from POST /uploads) as a knowledge asset "
    "(book/brand/article) with bibliographic metadata. Same file twice in a workspace -> CONFLICT.",
    input_model=AssetRegisterIn,
    output_model=AssetView,
    machine_output=True,
    writes=True,
)
async def kn_asset_register(inp: AssetRegisterIn, ctx: ToolContext) -> AssetView:
    ws = await resolve_workspace(ctx, inp.workspace_id)
    async with ctx.services.db.session() as session:
        upload = await session.get(Upload, inp.upload_id)
        if upload is None or upload.workspace_id != ws:
            raise NotFound(f"Upload {inp.upload_id} tidak ditemukan")
        if upload.kind != "pdf":
            raise ValidationFailed("Aset pengetahuan harus PDF")
        dup = await session.scalar(
            select(KnowledgeAsset.id).where(
                KnowledgeAsset.workspace_id == ws,
                KnowledgeAsset.sha256 == upload.sha256,
                KnowledgeAsset.deleted_at.is_(None),
            )
        )
        if dup is not None:
            raise Conflict("File ini sudah terdaftar sebagai aset", details={"asset_id": str(dup)})
        asset = KnowledgeAsset(
            workspace_id=ws,
            upload_id=upload.id,
            kind=inp.kind,
            title=inp.title,
            authors=inp.authors,
            edition=inp.edition,
            year=inp.year,
            isbn=inp.isbn,
            language=inp.language,
            license_note=inp.license_note,
            sha256=upload.sha256,
            pages=upload.pages,
        )
        session.add(asset)
        await session.flush()
        await session.refresh(asset)
    await emit_status(ctx.services, asset, {"status": "registered", "pages_total": asset.pages})
    return asset_view(asset)


class AssetRefIn(ToolInput):
    workspace_id: uuid.UUID | None = None
    asset_id: uuid.UUID


class AssetIngestIn(AssetRefIn):
    wait_s: int = Field(default=0, ge=0, le=20)


class AssetIngestOut(BaseModel):
    """Hasil job ingest: ringkasan + statistik (halaman, chunk, token, OCR, struktur)."""

    model_config = {"extra": "allow"}

    summary: str
    asset_id: str
    pages: int = 0
    chunks: int = 0


@tool(
    name="kn_asset_ingest",
    toolset=TOOLSET,
    summary="Ekstrak, chunk & embed aset ke koleksi books:<ws> (job)",
    description="Ingest an asset: per-page PDF text, chapter structure, semantic chunks <= 800 "
    "tokens with page citations, embed-local (bulk) into books:<workspace>. Progress events "
    "kn.asset.status. Re-ingesting unchanged content makes 0 embedding calls.",
    input_model=AssetIngestIn,
    output_model=AssetIngestOut,
    returns_job=True,
    writes=True,
)
async def kn_asset_ingest(inp: AssetIngestIn, ctx: ToolContext) -> Any:
    ws = await resolve_workspace(ctx, inp.workspace_id)
    asset = await asset_in_ws(ctx, ws, inp.asset_id)
    if asset.status in ("extracting", "embedding", "carding"):
        raise Conflict(f"Aset sedang diproses ({asset.status})")
    params = {"asset_id": str(asset.id), "workspace_id": str(ws), "pages": asset.pages}
    return await enqueue_and_wait(
        ctx, "kn_asset_ingest", params, wait_s=inp.wait_s, timeout_s=INGEST_TIMEOUT_S
    )


# ---------------------------------------------------------------------------- baca & hapus


class AssetsListIn(PageIn):
    workspace_id: uuid.UUID | None = None
    kind: AssetKind | None = None
    status: AssetStatus | None = None


class AssetsListOut(PageMeta):
    assets: list[AssetView]


@tool(
    name="kn_assets_list",
    toolset=TOOLSET,
    summary="Daftar aset pengetahuan workspace (cursor)",
    description="List knowledge assets of the workspace, newest first, filter kind/status, "
    "cursor pagination (limit <= 200).",
    input_model=AssetsListIn,
    output_model=AssetsListOut,
    machine_output=True,
)
async def kn_assets_list(inp: AssetsListIn, ctx: ToolContext) -> AssetsListOut:
    ws = await resolve_workspace(ctx, inp.workspace_id)
    conds = [KnowledgeAsset.workspace_id == ws, KnowledgeAsset.deleted_at.is_(None)]
    if inp.kind:
        conds.append(KnowledgeAsset.kind == inp.kind)
    if inp.status:
        conds.append(KnowledgeAsset.status == inp.status)
    offset = _offset(inp.cursor)
    async with ctx.services.db.session() as session:
        total = await session.scalar(select(func.count()).select_from(KnowledgeAsset).where(*conds))
        rows = (
            await session.scalars(
                select(KnowledgeAsset)
                .where(*conds)
                .order_by(KnowledgeAsset.created_at.desc(), KnowledgeAsset.id)
                .offset(offset)
                .limit(inp.limit)
            )
        ).all()
    nxt = offset + len(rows)
    return AssetsListOut(
        assets=[asset_view(r) for r in rows],
        next_cursor=str(nxt) if nxt < (total or 0) else None,
        total_estimate=int(total or 0),
    )


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    if not cursor.isdigit():
        raise ValidationFailed("cursor tidak valid")
    return int(cursor)


@tool(
    name="kn_asset_get",
    toolset=TOOLSET,
    summary="Detail aset pengetahuan (status, statistik ingest)",
    description="Get one knowledge asset: metadata, ingest status and stats.",
    input_model=AssetRefIn,
    output_model=AssetView,
    machine_output=True,
)
async def kn_asset_get(inp: AssetRefIn, ctx: ToolContext) -> AssetView:
    ws = await resolve_workspace(ctx, inp.workspace_id)
    return asset_view(await asset_in_ws(ctx, ws, inp.asset_id))


class DeleteOut(BaseModel):
    deleted: bool
    chunks_removed: int


@tool(
    name="kn_asset_delete",
    toolset=TOOLSET,
    summary="Hapus aset + chunk KB-nya (file upload tetap untuk audit)",
    description="Soft-delete an asset and remove its chunks from books:<workspace>. Idempotent.",
    input_model=AssetRefIn,
    output_model=DeleteOut,
    machine_output=True,
    writes=True,
)
async def kn_asset_delete(inp: AssetRefIn, ctx: ToolContext) -> DeleteOut:
    ws = await resolve_workspace(ctx, inp.workspace_id)
    async with ctx.services.db.session() as session:
        asset = await session.get(KnowledgeAsset, inp.asset_id)
        if asset is None or asset.workspace_id != ws or asset.deleted_at is not None:
            return DeleteOut(deleted=False, chunks_removed=0)
        removed = 0
        doc = await session.scalar(
            select(KbDocument)
            .join(KbCollection, KbCollection.id == KbDocument.collection_id)
            .where(
                KbCollection.workspace_id == ws,
                KbCollection.name == books_collection_name(ws),
                KbDocument.source_type == SOURCE_TYPE,
                KbDocument.source_ref == source_ref(asset.id),
            )
        )
        if doc is not None:
            removed = int(
                await session.scalar(
                    text("SELECT count(*) FROM mcp.kb_chunks WHERE document_id = :d"), {"d": doc.id}
                )
                or 0
            )
            await session.delete(doc)
        await session.execute(
            update(KnowledgeAsset)
            .where(KnowledgeAsset.id == asset.id)
            .values(deleted_at=now(), updated_at=now())
        )
    return DeleteOut(deleted=True, chunks_removed=removed)


# ---------------------------------------------------------------------------- pencarian


class KnSearchIn(ReadInput):
    workspace_id: uuid.UUID | None = None
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)]
    top_k: int = Field(default=6, ge=1, le=20)
    asset_id: uuid.UUID | None = None
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid"
    full_text: bool = False


class Citation(BaseModel):
    asset_id: str | None
    title: str | None
    edition: str | None
    chapter: str | None
    section: str | None
    page_start: int | None
    page_end: int | None


class KnHit(BaseModel):
    chunk_id: str
    text: str
    score: float
    citation: Citation


class KnSearchOut(BaseModel):
    summary: str
    results: list[KnHit]


@tool(
    name="kn_search",
    toolset=TOOLSET,
    summary="Cari di buku/brand workspace: potongan <= 600 karakter + sitasi halaman",
    description="Hybrid search over books:<workspace> (embed-local). Each hit carries a citation "
    "(asset, title, edition, chapter, section, pages). Snippets are capped at 600 chars; "
    "full_text=true is admin-only.",
    input_model=KnSearchIn,
    output_model=KnSearchOut,
)
async def kn_search(inp: KnSearchIn, ctx: ToolContext) -> KnSearchOut:
    if inp.full_text and not ctx.client.is_admin:
        raise Forbidden("Teks penuh hanya untuk admin (hak cipta, V1-K15)")
    ws = await resolve_workspace(ctx, inp.workspace_id)
    async with ctx.services.db.session() as session:
        coll = await session.scalar(
            select(KbCollection).where(
                KbCollection.workspace_id == ws, KbCollection.name == books_collection_name(ws)
            )
        )
    if coll is None:
        return KnSearchOut(summary="Belum ada buku ter-ingest di workspace ini", results=[])
    embedder = (
        None
        if inp.mode == "keyword"
        else embedder_for(ctx.services, coll.embed_model, coll.embed_dim, ctx.budget)
    )
    hits = await search(
        ctx.services.db,
        [coll.id],
        inp.query,
        mode=inp.mode,
        top_k=inp.top_k,
        embedder=embedder,
        dim=coll.embed_dim,
        filters={"asset_id": str(inp.asset_id)} if inp.asset_id else None,
    )
    metas = await _chunk_metadata(ctx, [h.chunk_id for h in hits])
    results = [
        KnHit(
            chunk_id=h.chunk_id,
            text=h.text if inp.full_text else snippet(h.text),
            score=round(h.score, 4),
            citation=citation(metas.get(h.chunk_id, {})),
        )
        for h in hits
    ]
    return KnSearchOut(summary=f"{len(results)} potongan dari buku/brand", results=results)


def snippet(chunk_text: str) -> str:
    """Buang breadcrumb (baris pertama) lalu potong <= 600 karakter di batas kata."""
    body = chunk_text.split("\n\n", 1)[-1].strip()
    if len(body) <= SNIPPET_MAX_CHARS:
        return body
    cut = body[: SNIPPET_MAX_CHARS - 1].rsplit(" ", 1)[0]
    return cut + "…"


def citation(meta: dict[str, Any]) -> Citation:
    return Citation(
        asset_id=meta.get("asset_id"),
        title=meta.get("title"),
        edition=meta.get("edition"),
        chapter=meta.get("chapter"),
        section=meta.get("section"),
        page_start=meta.get("page_start"),
        page_end=meta.get("page_end"),
    )


async def _chunk_metadata(ctx: ToolContext, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not chunk_ids:
        return {}
    async with ctx.services.db.session() as session:
        rows = (
            await session.execute(
                text("SELECT id::text AS id, metadata FROM mcp.kb_chunks WHERE id = ANY(:ids)"),
                {"ids": [uuid.UUID(c) for c in chunk_ids]},
            )
        ).all()
    return {r.id: dict(r.metadata or {}) for r in rows}
