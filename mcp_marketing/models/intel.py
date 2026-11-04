"""Job riset `intel` (kontrak 1.2, Modul 3 Dashboard) — satu baris per job induk `mcp.jobs`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default


class IntelJob(Base):
    __tablename__ = "intel_jobs"

    id: Mapped[str] = mapped_column(
        String(16), ForeignKey("mcp.jobs.id", ondelete="CASCADE"), primary_key=True
    )
    dashboard_job_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    workspace_id: Mapped[uuid.UUID]
    client_id: Mapped[uuid.UUID | None]
    requested_by: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    spec: Mapped[dict[str, Any]]
    stage_agents: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    stages: Mapped[list[Any]] = mapped_column(server_default="[]")
    event_sequence: Mapped[int] = mapped_column(server_default="0")
    estimate: Mapped[dict[str, Any] | None]
    items_handle: Mapped[str | None] = mapped_column(String(16))
    result: Mapped[dict[str, Any] | None]
    cost: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    error: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = ts_default()
    updated_at: Mapped[datetime] = ts_default()
    finished_at: Mapped[datetime | None]
