"""Job queue & jadwal — DATABASE §4.2."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default, uuid_pk


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    priority: Mapped[int] = mapped_column(SmallInteger, server_default="5")
    params: Mapped[dict[str, Any]]
    client_id: Mapped[uuid.UUID | None]
    run_id: Mapped[uuid.UUID | None]
    parent_job_id: Mapped[str | None] = mapped_column(String(16), ForeignKey("mcp.jobs.id"))
    browser_profile_id: Mapped[uuid.UUID | None]
    progress_done: Mapped[int] = mapped_column(server_default="0")
    progress_total: Mapped[int | None]
    checkpoint: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    result_handle: Mapped[str | None] = mapped_column(String(16))
    result: Mapped[dict[str, Any] | None]
    error: Mapped[dict[str, Any] | None]
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    max_attempts: Mapped[int] = mapped_column(SmallInteger, server_default="3")
    run_after: Mapped[datetime] = ts_default()
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]
    timeout_s: Mapped[int] = mapped_column(server_default="1800")
    cancel_requested: Mapped[bool] = mapped_column(server_default="false")
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = ts_default()
    dedupe_key: Mapped[str | None] = mapped_column(String(64))
    estimate: Mapped[dict[str, Any] | None]
    message: Mapped[str | None] = mapped_column(String(300))


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(100), unique=True)
    cron: Mapped[str] = mapped_column(String(64))
    job_kind: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    is_enabled: Mapped[bool] = mapped_column(server_default="true")
    last_run_at: Mapped[datetime | None]
    next_run_at: Mapped[datetime | None]
