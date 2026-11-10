"""Dokumen & chunk KB: upsert idempoten per `(koleksi, source_type, source_ref)` + `content_hash`.

Konten sama -> dilewati tanpa memanggil embedding. Berubah -> chunk lama dihapus, chunk baru
di-embed (embedding dihitung SEBELUM transaksi tulis agar transaksi DB tetap pendek).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

import orjson
from sqlalchemy import delete, func, select, text, update

from mcp_marketing.core.errors import NotFound, ValidationFailed
from mcp_marketing.models.kb import KbCollection, KbDocument
from mcp_marketing.services.kb.chunker import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_OVERLAP,
    chunk_markdown,
)
from mcp_marketing.services.kb.embedder import Embedder
from mcp_marketing.services.kb.identity import max_chunk_tokens
from mcp_marketing.utils.crypto import sha256_hex
from mcp_marketing.utils.time import now

Status = Literal["created", "updated", "skipped"]
MAX_DOC_CHARS = 400_000

_INSERT_CHUNK = text(
    "INSERT INTO mcp.kb_chunks "
    "(document_id, collection_id, chunk_index, text, tokens, embed_dim, embedding, metadata) "
    "VALUES (:d, :c, :i, :t, :n, :dim, CAST(:e AS real[]), CAST(:m AS jsonb))"
)


@dataclass
class DocInput:
    source_type: str
    source_ref: str
    title: str | None
    content: str
    metadata: dict[str, Any]
    source_version: int | None = None


@dataclass
class UpsertResult:
    document_id: str
    status: Status
    chunks: int
    tokens_embedded: int


def content_hash(content: str, chunk_size: int, overlap: int) -> str:
    return sha256_hex(f"{chunk_size}|{overlap}|{content}")


async def get_collection(db: Any, collection_id: uuid.UUID | str) -> KbCollection:
    async with db.session() as session:
        coll = await session.get(KbCollection, uuid.UUID(str(collection_id)))
    if coll is None:
        raise NotFound(f"Koleksi {collection_id} tidak ditemukan")
    return coll


async def upsert_document(
    db: Any,
    collection: KbCollection,
    doc: DocInput,
    embedder: Embedder,
    *,
    chunk_size: int = DEFAULT_CHUNK_TOKENS,
    overlap: int = DEFAULT_OVERLAP,
) -> UpsertResult:
    if embedder.model != collection.embed_model or embedder.dim != collection.embed_dim:
        raise ValidationFailed(
            "Model/dimensi embedding berbeda dengan koleksi; buat koleksi baru atau kb-reembed",
            details={"collection": collection.embed_model, "embedder": embedder.model},
        )
    body = doc.content[:MAX_DOC_CHARS]
    capped = max_chunk_tokens(collection.embed_model, chunk_size)
    if capped != chunk_size:  # hanya koleksi embed-local; koleksi lama tidak berubah hash-nya
        chunk_size, overlap = capped, min(overlap, capped // 4)
    digest = content_hash(body, chunk_size, overlap)
    ref = doc.source_ref[:2_000]
    async with db.session() as session:
        existing = await session.scalar(
            select(KbDocument).where(
                KbDocument.collection_id == collection.id,
                KbDocument.source_type == doc.source_type,
                KbDocument.source_ref == ref,
            )
        )
    if existing is not None and existing.content_hash.strip() == digest:
        if doc.source_version is not None and existing.source_version != doc.source_version:
            async with db.session() as session:
                await session.execute(
                    update(KbDocument)
                    .where(KbDocument.id == existing.id)
                    .values(source_version=doc.source_version)
                )
        return UpsertResult(str(existing.id), "skipped", 0, 0)

    chunks = chunk_markdown(body, title=doc.title, chunk_size=chunk_size, overlap=overlap)
    vectors = await embedder.embed([c.text for c in chunks], "passage")
    if any(len(v) != collection.embed_dim for v in vectors):
        raise ValidationFailed("Dimensi embedding tidak sama dengan koleksi (campur dimensi)")

    async with db.session() as session:
        if existing is None:
            doc_id = uuid.uuid4()
            session.add(
                KbDocument(
                    id=doc_id,
                    collection_id=collection.id,
                    source_type=doc.source_type,
                    source_ref=ref,
                    source_version=doc.source_version,
                    title=(doc.title or "")[:500] or None,
                    metadata_=doc.metadata,
                    content_hash=digest,
                )
            )
            await session.flush()
            status: Status = "created"
        else:
            doc_id = existing.id
            await session.execute(
                text("DELETE FROM mcp.kb_chunks WHERE document_id = :d"), {"d": doc_id}
            )
            await session.execute(
                update(KbDocument)
                .where(KbDocument.id == doc_id)
                .values(
                    content_hash=digest,
                    title=(doc.title or "")[:500] or None,
                    metadata_=doc.metadata,
                    source_version=doc.source_version,
                    updated_at=now(),
                )
            )
            status = "updated"
        await insert_chunks(session, collection, doc_id, doc, chunks, vectors)
    return UpsertResult(str(doc_id), status, len(chunks), sum(c.tokens for c in chunks))


async def insert_chunks(
    session: Any,
    collection: KbCollection,
    doc_id: uuid.UUID,
    doc: DocInput,
    chunks: list[Any],
    vectors: list[list[float]],
) -> None:
    """Tulis chunk + vektor satu dokumen (metadata dokumen ikut di tiap chunk)."""
    meta = orjson.dumps({**doc.metadata, "source_type": doc.source_type}).decode()
    for chunk, vec in zip(chunks, vectors, strict=True):
        await session.execute(
            _INSERT_CHUNK,
            {
                "d": doc_id,
                "c": collection.id,
                "i": chunk.index,
                "t": chunk.text,
                "n": chunk.tokens,
                "dim": collection.embed_dim,
                "e": [float(x) for x in vec],
                "m": meta,
            },
        )


async def delete_document(db: Any, document_id: uuid.UUID) -> int:
    async with db.session() as session:
        n = await session.scalar(
            text("SELECT count(*) FROM mcp.kb_chunks WHERE document_id = :d"), {"d": document_id}
        )
        result = await session.execute(delete(KbDocument).where(KbDocument.id == document_id))
    if not result.rowcount:  # type: ignore[attr-defined]
        raise NotFound(f"Dokumen {document_id} tidak ditemukan")
    return int(n or 0)


async def delete_collection(db: Any, collection_id: uuid.UUID) -> int:
    async with db.session() as session:
        n = await session.scalar(
            text("SELECT count(*) FROM mcp.kb_chunks WHERE collection_id = :c"),
            {"c": collection_id},
        )
        await session.execute(
            text("DELETE FROM mcp.kb_chunks WHERE collection_id = :c"), {"c": collection_id}
        )
        result = await session.execute(delete(KbCollection).where(KbCollection.id == collection_id))
    if not result.rowcount:  # type: ignore[attr-defined]
        raise NotFound(f"Koleksi {collection_id} tidak ditemukan")
    return int(n or 0)


async def reembed_collection(
    db: Any, source: KbCollection, target: KbCollection, embedder: Embedder, *, batch: int = 64
) -> int:
    """Salin dokumen & teks chunk `source` ke `target` (model baru) lalu embed ulang."""
    async with db.session() as session:
        docs = (
            await session.scalars(select(KbDocument).where(KbDocument.collection_id == source.id))
        ).all()
    total = 0
    for doc in docs:
        async with db.session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT chunk_index, text, tokens, metadata FROM mcp.kb_chunks "
                        "WHERE document_id = :d ORDER BY chunk_index"
                    ),
                    {"d": doc.id},
                )
            ).all()
        vectors: list[list[float]] = []
        for start in range(0, len(rows), batch):
            vectors += await embedder.embed(
                [r.text for r in rows[start : start + batch]], "passage"
            )
        new_id = uuid.uuid4()
        async with db.session() as session:
            session.add(
                KbDocument(
                    id=new_id,
                    collection_id=target.id,
                    source_type=doc.source_type,
                    source_ref=doc.source_ref,
                    source_version=doc.source_version,
                    title=doc.title,
                    metadata_=doc.metadata_,
                    content_hash=doc.content_hash,
                )
            )
            await session.flush()
            for row, vec in zip(rows, vectors, strict=True):
                await session.execute(
                    _INSERT_CHUNK,
                    {
                        "d": new_id,
                        "c": target.id,
                        "i": row.chunk_index,
                        "t": row.text,
                        "n": row.tokens,
                        "dim": target.embed_dim,
                        "e": [float(x) for x in vec],
                        "m": orjson.dumps(row.metadata).decode(),
                    },
                )
        total += len(rows)
    return total


async def collection_counts(db: Any, collection_ids: list[uuid.UUID]) -> dict[str, tuple[int, int]]:
    if not collection_ids:
        return {}
    async with db.session() as session:
        docs = dict(
            (
                await session.execute(
                    select(KbDocument.collection_id, func.count())
                    .where(KbDocument.collection_id.in_(collection_ids))
                    .group_by(KbDocument.collection_id)
                )
            ).all()
        )
        chunks = dict(
            (
                await session.execute(
                    text(
                        "SELECT collection_id, count(*) FROM mcp.kb_chunks "
                        "WHERE collection_id = ANY(:c) GROUP BY collection_id"
                    ),
                    {"c": collection_ids},
                )
            ).all()
        )
    return {str(c): (int(docs.get(c, 0)), int(chunks.get(c, 0))) for c in collection_ids}
