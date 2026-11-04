"""Profil agent, run Dashboard, langkah run, webhook outbox — DATABASE §4.3."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default


class AgentProfile(Base):
    __tablename__ = "agent_profiles"

    agent_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100))
    system_prompt: Mapped[str] = mapped_column(Text)
    route: Mapped[str] = mapped_column(String(64), server_default="smart")
    toolsets: Mapped[list[str]]
    tool_denylist: Mapped[list[str]] = mapped_column(server_default="{}")
    run_token_budget: Mapped[int] = mapped_column(server_default="120000")
    max_steps: Mapped[int] = mapped_column(SmallInteger, server_default="25")
    is_active: Mapped[bool] = mapped_column(server_default="true")
    version: Mapped[int] = mapped_column(server_default="1")
    updated_at: Mapped[datetime] = ts_default()


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    mcp_run_id: Mapped[str] = mapped_column(String(32), unique=True)
    trigger: Mapped[str] = mapped_column(String(16))
    agent_key: Mapped[str] = mapped_column(String(100), ForeignKey("mcp.agent_profiles.agent_key"))
    agent_id: Mapped[uuid.UUID]
    workspace_id: Mapped[uuid.UUID | None]
    project_id: Mapped[uuid.UUID]
    task_id: Mapped[uuid.UUID | None]
    status: Mapped[str] = mapped_column(String(24), server_default="accepted")
    input: Mapped[dict[str, Any]]
    callback: Mapped[dict[str, Any]]
    tokens_used: Mapped[int] = mapped_column(server_default="0")
    token_budget: Mapped[int]
    steps: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    cancel_requested: Mapped[bool] = mapped_column(server_default="false")
    cancel_reason: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[dict[str, Any] | None]
    summary: Mapped[str | None] = mapped_column(Text)
    subtask_refs: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    last_event_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = ts_default()


class RunStep(Base):
    __tablename__ = "run_steps"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mcp.runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    step: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    tool: Mapped[str | None] = mapped_column(String(64))
    content: Mapped[dict[str, Any]]
    tokens: Mapped[int] = mapped_column(server_default="0")
    created_at: Mapped[datetime] = ts_default()


class WebhookOutbox(Base):
    __tablename__ = "webhook_outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    # tepat satu dari run_id (event agent.* kontrak 1.0) atau job_id (event intel.job.* 1.2)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mcp.runs.run_id"))
    job_id: Mapped[str | None] = mapped_column(
        String(16), ForeignKey("mcp.intel_jobs.id", ondelete="CASCADE")
    )
    seq: Mapped[int]
    event_type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    next_attempt_at: Mapped[datetime] = ts_default()
    first_attempt_at: Mapped[datetime | None]
    last_http_status: Mapped[int | None] = mapped_column(SmallInteger)
    last_error: Mapped[str | None] = mapped_column(Text)
    response: Mapped[dict[str, Any] | None]
    delivered_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = ts_default()
