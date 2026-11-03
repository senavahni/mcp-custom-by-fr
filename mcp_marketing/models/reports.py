"""Laporan analisis — DATABASE §4.7 (dipakai penuh di TASK-21..23)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default


class AnalysisReport(Base):
    __tablename__ = "analysis_reports"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict[str, Any]]
    input_handles: Mapped[list[str]] = mapped_column(server_default="{}")
    key_numbers: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    summary: Mapped[str] = mapped_column(Text)
    content: Mapped[dict[str, Any]]
    language: Mapped[str] = mapped_column(String(8), server_default="id")
    llm_tokens: Mapped[int] = mapped_column(server_default="0")
    workspace_id: Mapped[uuid.UUID | None]
    project_id: Mapped[uuid.UUID | None]
    task_id: Mapped[uuid.UUID | None]
    run_id: Mapped[uuid.UUID | None]
    client_id: Mapped[uuid.UUID | None]
    published_to: Mapped[list[Any]] = mapped_column(server_default="[]")
    created_at: Mapped[datetime] = ts_default()
