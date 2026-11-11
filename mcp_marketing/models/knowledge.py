"""Upload file & aset pengetahuan (buku/brand/artikel) — MCP-V1-11."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default, uuid_pk


class Upload(Base):
    """File yang di-stream Dashboard ke `POST /uploads`; disimpan `<ws>/<sha256>.<ext>`."""

    __tablename__ = "uploads"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID]
    sha256: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(8))  # pdf | xlsx | csv
    purpose: Mapped[str] = mapped_column(String(16))  # book | brand | article | import
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    path: Mapped[str] = mapped_column(Text)
    original_name: Mapped[str | None] = mapped_column(String(255))  # metadata, bukan path
    pages: Mapped[int | None]
    uploaded_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = ts_default()


class KnowledgeAsset(Base):
    __tablename__ = "knowledge_assets"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID]
    upload_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mcp.uploads.id"))
    kind: Mapped[str] = mapped_column(String(16))  # book | brand | article
    title: Mapped[str] = mapped_column(String(300))
    authors: Mapped[list[str]] = mapped_column(server_default="{}")
    edition: Mapped[str | None] = mapped_column(String(50))
    year: Mapped[int | None] = mapped_column(SmallInteger)
    isbn: Mapped[str | None] = mapped_column(String(20))
    language: Mapped[str] = mapped_column(String(8), server_default="id")
    license_note: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    pages: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(16), server_default="registered")
    stats: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    collection_id: Mapped[uuid.UUID | None]
    error: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = ts_default()
    updated_at: Mapped[datetime] = ts_default()
    deleted_at: Mapped[datetime | None]


class KnowledgePage(Base):
    __tablename__ = "knowledge_pages"

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mcp.knowledge_assets.id", ondelete="CASCADE"), primary_key=True
    )
    page_no: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    needs_ocr: Mapped[bool] = mapped_column(server_default="false")
    chapter: Mapped[str | None] = mapped_column(String(300))
    section: Mapped[str | None] = mapped_column(String(300))
