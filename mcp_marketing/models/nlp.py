"""Hasil sentimen — DATABASE §4.7 (dipakai penuh di TASK-20)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default, uuid_pk


class SentimentResult(Base):
    __tablename__ = "sentiment_results"

    id: Mapped[uuid.UUID] = uuid_pk()
    target_type: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[uuid.UUID | None]
    text_sha256: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(100))
    label: Mapped[str] = mapped_column(String(8))
    score: Mapped[float] = mapped_column(Float(precision=24))
    aspects: Mapped[list[Any]] = mapped_column(server_default="[]")
    lang: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = ts_default()
