"""Vector DB (pgvector) — DATABASE §4.8. Tabel ber-vector dibuat di revisi 0002_kb_vector.

Kolom `vector` tanpa dimensi tetap; dimensi divalidasi di service (= kb_collections.embed_dim).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, ForeignKey, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default, uuid_pk


class KbCollection(Base):
    __tablename__ = "kb_collections"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(150))
    description: Mapped[str | None] = mapped_column(Text)
    workspace_id: Mapped[uuid.UUID | None]
    project_id: Mapped[uuid.UUID | None]
    embed_model: Mapped[str] = mapped_column(String(64))
    embed_dim: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = ts_default()


class KbDocument(Base):
    __tablename__ = "kb_documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mcp.kb_collections.id", ondelete="CASCADE")
    )
    source_type: Mapped[str] = mapped_column(String(24))
    source_ref: Mapped[str] = mapped_column(Text)
    source_version: Mapped[int | None]
    title: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", server_default="{}")
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = ts_default()
    updated_at: Mapped[datetime] = ts_default()


class KbChunk(Base):
    __tablename__ = "kb_chunks"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mcp.kb_documents.id", ondelete="CASCADE")
    )
    collection_id: Mapped[uuid.UUID]
    chunk_index: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    tokens: Mapped[int]
    embed_dim: Mapped[int] = mapped_column(SmallInteger)
    embedding: Mapped[Any] = mapped_column(Vector())
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", server_default="{}")
    text_tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True), deferred=True
    )


class EmbeddingCache(Base):
    __tablename__ = "embedding_cache"

    text_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(64), primary_key=True)
    dim: Mapped[int] = mapped_column(SmallInteger)
    embedding: Mapped[Any] = mapped_column(Vector())
    created_at: Mapped[datetime] = ts_default()
