"""Ingest aset buku/brand ke KB `books:<ws>` (MCP-V1-11, F4).

registered -> extracting (teks per halaman + struktur) -> embedding (embed-local, antrean bulk)
-> ready. Progres tiap tahap dikirim sebagai event `kn.asset.status` (kontrak 1.3). Ingest ulang
dengan isi sama = 0 panggilan embedding (digest dokumen sama -> dilewati). `embed-local` mati ->
`LOCAL_MODEL_UNAVAILABLE` (worker menjeda job; status tetap `embedding`).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update

from mcp_marketing.core.errors import LocalModelUnavailable, NotFound, ToolError
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models.kb import KbCollection
from mcp_marketing.models.knowledge import KnowledgeAsset, KnowledgePage, Upload
from mcp_marketing.services.kb import store
from mcp_marketing.services.kb.embedder import Embedder, embedder_for
from mcp_marketing.services.kb.identity import EMBED_LOCAL_DIM, EMBED_LOCAL_MODEL
from mcp_marketing.services.knowledge.chunking import CHUNK_TOKENS, OVERLAP_TOKENS, chunk_book
from mcp_marketing.services.knowledge.pdf import PdfContent, extract_pdf
from mcp_marketing.utils.crypto import sha256_hex
from mcp_marketing.utils.time import now

log = get_logger(__name__)

BOOKS_PREFIX = "books:"
SOURCE_TYPE = "book"
EMBED_BATCH = 32
Progress = Callable[[int, int | None, str], Awaitable[None]]


def books_collection_name(workspace_id: uuid.UUID) -> str:
    return f"{BOOKS_PREFIX}{workspace_id}"


def source_ref(asset_id: uuid.UUID) -> str:
    return f"asset:{asset_id}"


async def books_collection(db: Any, workspace_id: uuid.UUID) -> KbCollection:
    """Koleksi `books:<ws>` (dibuat bila belum ada), selalu embed-local 1024 (V1-K8)."""
    name = books_collection_name(workspace_id)
    async with db.session() as session:
        coll = await session.scalar(
            select(KbCollection).where(
                KbCollection.workspace_id == workspace_id, KbCollection.name == name
            )
        )
        if coll is None:
            coll = KbCollection(
                name=name,
                description="Buku & dokumen brand (Knowledge Engine v1)",
                workspace_id=workspace_id,
                embed_model=EMBED_LOCAL_MODEL,
                embed_dim=EMBED_LOCAL_DIM,
            )
            session.add(coll)
            await session.flush()
        return coll


async def emit_status(services: Any, asset: KnowledgeAsset, data: dict[str, Any]) -> None:
    """`kn.asset.status` ke Dashboard; kegagalan kirim tidak menggagalkan ingest (dicatat)."""
    outbox = getattr(services, "outbox", None)
    if outbox is None:
        return
    try:
        await outbox.emit_entity(
            f"asset:{asset.id}",
            asset.workspace_id,
            "kn.asset.status",
            {"asset_id": str(asset.id), **data},
        )
    except Exception as exc:  # noqa: BLE001 - event bersifat informatif
        log.warning("knowledge.event_failed", asset_id=str(asset.id), error=type(exc).__name__)


async def set_status(
    services: Any,
    asset: KnowledgeAsset,
    status: str,
    *,
    event: dict[str, Any] | None = None,
    **values: Any,
) -> None:
    async with services.db.session() as session:
        await session.execute(
            update(KnowledgeAsset)
            .where(KnowledgeAsset.id == asset.id)
            .values(status=status, updated_at=now(), **values)
        )
    asset.status = status
    await emit_status(services, asset, {"status": status, **(event or {})})


async def load_asset(db: Any, asset_id: uuid.UUID) -> tuple[KnowledgeAsset, Upload]:
    async with db.session() as session:
        asset = await session.get(KnowledgeAsset, asset_id)
        upload = await session.get(Upload, asset.upload_id) if asset is not None else None
    if asset is None or upload is None or asset.deleted_at is not None:
        raise NotFound(f"Aset {asset_id} tidak ditemukan")
    return asset, upload


async def save_pages(db: Any, asset_id: uuid.UUID, content: PdfContent) -> None:
    async with db.session() as session:
        await session.execute(delete(KnowledgePage).where(KnowledgePage.asset_id == asset_id))
        session.add_all(
            KnowledgePage(
                asset_id=asset_id,
                page_no=p.page_no,
                text=p.text,
                needs_ocr=p.needs_ocr,
                chapter=p.chapter,
                section=p.section,
            )
            for p in content.pages
        )


def base_metadata(asset: KnowledgeAsset) -> dict[str, Any]:
    return {
        "asset_id": str(asset.id),
        "kind": asset.kind,
        "title": asset.title,
        "edition": asset.edition,
    }


async def ingest_asset(
    services: Any,
    asset_id: uuid.UUID,
    *,
    progress: Progress | None = None,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    asset, upload = await load_asset(services.db, asset_id)
    settings = services.settings.upload
    try:
        await set_status(services, asset, "extracting", error=None)
        content = await asyncio.to_thread(
            extract_pdf,
            Path(upload.path),
            max_pages=settings.pdf_max_pages,
            timeout_s=settings.pdf_extract_timeout_s,
        )
        await save_pages(services.db, asset.id, content)
        total = len(content.pages)
        ocr = sum(1 for p in content.pages if p.needs_ocr)
        chunks = chunk_book(content.pages, title=asset.title, base_metadata=base_metadata(asset))
        coll = await books_collection(services.db, asset.workspace_id)
        digest = sha256_hex(
            f"{CHUNK_TOKENS}|{OVERLAP_TOKENS}|" + "\x1e".join(c.text for c in chunks)
        )
        stats: dict[str, Any] = {
            "pages": total,
            "pages_needs_ocr": ocr,
            "chunks": len(chunks),
            "tokens": sum(c.tokens for c in chunks),
            "structure": content.structure,
            "chapters": len({c.chapter for c in chunks if c.chapter}),
        }
        existing = await store.find_document(
            services.db, coll.id, SOURCE_TYPE, source_ref(asset.id)
        )
        if existing is not None and existing.content_hash.strip() == digest:
            stats |= {"embedded": 0, "skipped": True}
            done = {"pages_done": total, "pages_total": total, "message": "isi sama, tanpa embed"}
            await set_status(
                services, asset, "ready", pages=total, stats=stats, collection_id=coll.id,
                event=done,
            )  # fmt: skip
            return stats
        await set_status(
            services, asset, "embedding", pages=total, collection_id=coll.id,
            event={"pages_done": 0, "pages_total": total, "message": f"{len(chunks)} chunk"},
        )  # fmt: skip
        emb = embedder or embedder_for(services, coll.embed_model, coll.embed_dim)
        vectors: list[list[float]] = []
        for start in range(0, len(chunks), EMBED_BATCH):
            batch = chunks[start : start + EMBED_BATCH]
            vectors.extend(await emb.embed([c.text for c in batch], "passage"))
            done_page = batch[-1].page_end
            if progress is not None:
                await progress(len(vectors), len(chunks), f"{len(vectors)}/{len(chunks)} chunk")
            await emit_status(
                services,
                asset,
                {"status": "embedding", "pages_done": done_page, "pages_total": total},
            )
        await store.replace_document_chunks(
            services.db,
            coll,
            source_type=SOURCE_TYPE,
            source_ref=source_ref(asset.id),
            title=asset.title,
            metadata=base_metadata(asset),
            digest=digest,
            chunks=chunks,  # type: ignore[arg-type]
            vectors=vectors,
        )
        stats |= {"embedded": len(chunks), "skipped": False}
        await set_status(
            services, asset, "ready", stats=stats,
            event={"pages_done": total, "pages_total": total},
        )  # fmt: skip
        return stats
    except LocalModelUnavailable:
        await emit_status(
            services,
            asset,
            {"status": "embedding", "message": "embed-local tidak tersedia; ingest dijeda"},
        )
        raise
    except ToolError as exc:
        await set_status(
            services, asset, "failed", error=exc.to_structured(),
            event={"message": exc.message[:300]},
        )  # fmt: skip
        raise
    except Exception:
        await set_status(
            services, asset, "failed",
            error={"code": "INTERNAL", "message": "Ingest gagal", "retryable": True},
            event={"message": "Ingest gagal (kesalahan internal)"},
        )  # fmt: skip
        raise
