"""Data master marketing (kompetitor, watchlist kata kunci) — DATABASE §4.7."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default, uuid_pk


class Competitor(Base):
    __tablename__ = "competitors"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID | None]
    name: Mapped[str] = mapped_column(String(150))
    website: Mapped[str | None] = mapped_column(Text)
    social: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    keywords: Mapped[list[str]] = mapped_column(server_default="{}")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts_default()
    updated_at: Mapped[datetime] = ts_default()
    deleted_at: Mapped[datetime | None]


class KeywordWatchlist(Base):
    __tablename__ = "keyword_watchlists"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID | None]
    name: Mapped[str] = mapped_column(String(150))
    competitor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mcp.competitors.id", ondelete="SET NULL")
    )
    keywords: Mapped[list[str]]
    sources: Mapped[list[str]] = mapped_column(server_default="{x,threads,trends}")
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mcp.schedules.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = ts_default()
    updated_at: Mapped[datetime] = ts_default()
    deleted_at: Mapped[datetime | None]
