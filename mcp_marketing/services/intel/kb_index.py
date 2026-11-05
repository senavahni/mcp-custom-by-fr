"""Tahap Indeks job `intel` (M3_TASK_010): hasil riset -> koleksi KB `intel:<workspace_id>`.

Satu dokumen per item (post/halaman) agar metadata per chunk tetap spesifik
(`job_id, source, sentiment, created_at, author, url`) dan bisa difilter di `kb_search`, plus satu
dokumen ringkasan per job. Dedupe per `sha256(text)` dalam koleksi: teks yang sudah ada (dari job
lain di workspace yang sama) tidak di-embed ulang dan tetap milik job pertamanya. Embedding dibuat
per batch (bukan per dokumen) karena item sosial pendek dan jumlahnya bisa ribuan.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select, text, tuple_

from mcp_marketing.models.kb import KbChunk, KbCollection, KbDocument
from mcp_marketing.services.kb.chunker import DEFAULT_CHUNK_TOKENS, DEFAULT_OVERLAP, chunk_markdown
from mcp_marketing.services.kb.embedder import Embedder
from mcp_marketing.services.kb.identity import embed_identity
from mcp_marketing.services.kb.store import DocInput, content_hash, insert_chunks
from mcp_marketing.services.kb.vector import as_float_list
from mcp_marketing.utils.crypto import sha256_hex

COLLECTION_PREFIX = "intel:"
SOURCE_ITEM = "intel_item"
SOURCE_SUMMARY = "intel_summary"
ITEM_MAX_CHARS = 4_000
EMBED_BATCH = 64
SAMPLE_TOKENS_MAX = 20
LABEL_MAX_CHARS = 60
PROJECTION_SOURCES = ("x", "threads", "web", "trends")
_MARKUP = re.compile(r"<[^>]*>|https?://\S+|www\.\S+")
_WORD = re.compile(r"[^\W\d_]{3,24}")
_BREADCRUMB = re.compile(r"^\[[^\]]*\]\s*")

# progres: (chunk ter-embed, total chunk, kata terbaru untuk animasi stream M3_TASK_011)
Progress = Callable[[int, int, list[str]], Awaitable[None]]
Checkpoint = Callable[[], Awaitable[None]]


def sample_tokens(texts: list[str]) -> list[str]:
    """≤ 20 kata unik terbaru, huruf saja (markup, URL, angka, simbol dibuang)."""
    latest: dict[str, None] = {}
    for body in texts:
        for word in _WORD.findall(_MARKUP.sub(" ", body).lower()):
            latest.pop(word, None)
            latest[word] = None
    return list(latest)[-SAMPLE_TOKENS_MAX:]


def projection_label(chunk_text: str) -> str:
    """Teks chunk tanpa breadcrumb `[judul > bagian]`, spasi dirapikan, ≤ 60 karakter."""
    return " ".join(_BREADCRUMB.sub("", chunk_text.strip()).split())[:LABEL_MAX_CHARS]


@dataclass
class IndexStats:
    documents: int = 0
    created: int = 0
    skipped: int = 0
    chunks: int = 0
    tokens: int = 0

    def view(self, collection: str) -> dict[str, Any]:
        return {
            "collection": collection,
            "documents": self.documents,
            "created": self.created,
            "skipped": self.skipped,
            "chunks": self.chunks,
        }


def collection_name(workspace_id: uuid.UUID | str) -> str:
    return f"{COLLECTION_PREFIX}{workspace_id}"


async def intel_collection(services: Any, workspace_id: uuid.UUID) -> KbCollection:
    name = collection_name(workspace_id)
    async with services.db.session() as session:
        coll = await session.scalar(
            select(KbCollection).where(
                KbCollection.workspace_id == workspace_id, KbCollection.name == name
            )
        )
        if coll is None:
            model, dim = embed_identity(services.settings)
            coll = KbCollection(
                name=name,
                description="Hasil job Riset & Insight (post, halaman, ringkasan)",
                workspace_id=workspace_id,
                embed_model=model,
                embed_dim=dim,
            )
            session.add(coll)
            await session.flush()
    return coll


def _summary_doc(job_id: str, title: str, result: dict[str, Any]) -> DocInput | None:
    summary = (result.get("summary_md") or "").strip()
    recs = [str(r) for r in result.get("recommendations") or [] if str(r).strip()]
    if not summary and not recs:
        return None
    parts = [f"# Ringkasan riset: {title}", summary]
    if recs:
        parts.append("## Rekomendasi\n" + "\n".join(f"- {r}" for r in recs))
    body = "\n\n".join(p for p in parts if p)
    meta = {"job_id": job_id, "source": "summary"}
    return DocInput(SOURCE_SUMMARY, job_id, f"Ringkasan riset: {title}"[:150], body, meta)


def build_docs(
    job_id: str, title: str, rows: list[dict[str, Any]], result: dict[str, Any]
) -> list[DocInput]:
    """Dokumen ringkasan (bila ada) + satu dokumen per item unik (sha256 teks)."""
    docs: list[DocInput] = []
    summary = _summary_doc(job_id, title, result)
    if summary is not None:
        docs.append(summary)
    seen: set[str] = set()
    for row in rows:
        body = str(row.get("text") or "").strip()[:ITEM_MAX_CHARS]
        if not body:
            continue
        digest = sha256_hex(body)
        if digest in seen:
            continue
        seen.add(digest)
        meta = {
            "job_id": job_id,
            "source": row.get("source"),
            "sentiment": row.get("sentiment"),
            "created_at": row.get("posted_at"),
            "author": row.get("author"),
            "url": row.get("url"),
        }
        author = f" @{row['author']}" if row.get("author") else ""
        docs.append(DocInput(SOURCE_ITEM, digest, f"{row.get('source')}{author}"[:150], body, meta))
    return docs


async def _existing_refs(db: Any, collection_id: uuid.UUID, docs: list[DocInput]) -> set[tuple]:
    keys = [(d.source_type, d.source_ref) for d in docs]
    if not keys:
        return set()
    async with db.session() as session:
        rows = await session.execute(
            select(KbDocument.source_type, KbDocument.source_ref).where(
                KbDocument.collection_id == collection_id,
                tuple_(KbDocument.source_type, KbDocument.source_ref).in_(keys),
            )
        )
    return {(r[0], r[1]) for r in rows}


async def index_documents(
    db: Any,
    collection: KbCollection,
    docs: list[DocInput],
    embedder: Embedder,
    *,
    progress: Progress,
    checkpoint: Checkpoint,
) -> IndexStats:
    stats = IndexStats(documents=len(docs))
    existing = await _existing_refs(db, collection.id, docs)
    fresh = [d for d in docs if (d.source_type, d.source_ref) not in existing]
    stats.skipped = len(docs) - len(fresh)
    planned = [
        (d, chunk_markdown(d.content, title=d.title, chunk_size=DEFAULT_CHUNK_TOKENS,
                           overlap=DEFAULT_OVERLAP))
        for d in fresh
    ]  # fmt: skip
    total = sum(len(chunks) for _, chunks in planned)
    await progress(0, total, [])
    batch: list[tuple[DocInput, list[Any]]] = []
    batch_chunks = 0
    for doc, chunks in planned:
        batch.append((doc, chunks))
        batch_chunks += len(chunks)
        if batch_chunks >= EMBED_BATCH:
            await _write_batch(db, collection, batch, embedder, stats)
            await progress(stats.chunks, total, sample_tokens([d.content for d, _ in batch]))
            await checkpoint()
            batch, batch_chunks = [], 0
    if batch:
        await _write_batch(db, collection, batch, embedder, stats)
        await progress(stats.chunks, total, sample_tokens([d.content for d, _ in batch]))
    return stats


async def _write_batch(
    db: Any,
    collection: KbCollection,
    batch: list[tuple[DocInput, list[Any]]],
    embedder: Embedder,
    stats: IndexStats,
) -> None:
    texts = [c.text for _, chunks in batch for c in chunks]
    vectors = await embedder.embed(texts, "passage")
    pos = 0
    async with db.session() as session:
        for doc, chunks in batch:
            doc_id = uuid.uuid4()
            session.add(
                KbDocument(
                    id=doc_id,
                    collection_id=collection.id,
                    source_type=doc.source_type,
                    source_ref=doc.source_ref,
                    title=doc.title,
                    metadata_=doc.metadata,
                    content_hash=content_hash(doc.content, DEFAULT_CHUNK_TOKENS, DEFAULT_OVERLAP),
                )
            )
            await session.flush()
            await insert_chunks(
                session, collection, doc_id, doc, chunks, vectors[pos : pos + len(chunks)]
            )
            pos += len(chunks)
            stats.created += 1
            stats.chunks += len(chunks)
            stats.tokens += sum(c.tokens for c in chunks)


_PROJECTION_ROWS = text(
    "SELECT d.id, d.metadata, c.text, c.embedding::real[] AS embedding "
    "FROM mcp.kb_documents d JOIN mcp.kb_chunks c ON c.document_id = d.id "
    "WHERE d.collection_id = :coll AND d.source_type = :src "
    "AND d.metadata->>'job_id' = :job AND c.chunk_index = 0 "
    "ORDER BY d.created_at, d.id LIMIT :limit"
)


async def projection_rows(
    services: Any, workspace_id: uuid.UUID, job_id: str, limit: int
) -> list[dict[str, Any]]:
    """Satu baris per item job (chunk pertama): id, source, sentiment, label, vektor."""
    async with services.db.session() as session:
        coll_id = await session.scalar(
            select(KbCollection.id).where(
                KbCollection.workspace_id == workspace_id,
                KbCollection.name == collection_name(workspace_id),
            )
        )
        if coll_id is None:
            return []
        rows = await session.execute(
            _PROJECTION_ROWS,
            {"coll": coll_id, "src": SOURCE_ITEM, "job": job_id, "limit": limit},
        )
    out = []
    for doc_id, meta, chunk_text, embedding in rows:
        source = (meta or {}).get("source")
        out.append(
            {
                "id": str(doc_id),
                "source": source if source in PROJECTION_SOURCES else "web",
                "sentiment": (meta or {}).get("sentiment"),
                "label": projection_label(chunk_text),
                "vector": as_float_list(embedding),
            }
        )
    return out


async def purge_job(services: Any, workspace_id: uuid.UUID, job_id: str) -> tuple[int, int]:
    """Hapus dokumen milik job dari koleksi workspace -> (dokumen, chunk) terhapus."""
    async with services.db.session() as session:
        coll_id = await session.scalar(
            select(KbCollection.id).where(
                KbCollection.workspace_id == workspace_id,
                KbCollection.name == collection_name(workspace_id),
            )
        )
        if coll_id is None:
            return 0, 0
        ids = list(
            (
                await session.scalars(
                    select(KbDocument.id).where(
                        KbDocument.collection_id == coll_id,
                        KbDocument.metadata_["job_id"].astext == job_id,
                    )
                )
            ).all()
        )
        if not ids:
            return 0, 0
        chunks = await session.scalar(
            select(func.count()).select_from(KbChunk).where(KbChunk.document_id.in_(ids))
        )
        # chunk ikut terhapus (FK ON DELETE CASCADE)
        await session.execute(delete(KbDocument).where(KbDocument.id.in_(ids)))
    return len(ids), int(chunks or 0)
