"""Klien MCP (API key) — DATABASE §4.1."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default, uuid_pk


class ApiClient(Base):
    __tablename__ = "api_clients"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(16), server_default="external")
    key_prefix: Mapped[str] = mapped_column(String(8), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    toolsets: Mapped[list[str]] = mapped_column(server_default="{}")
    is_admin: Mapped[bool] = mapped_column(server_default="false")
    dashboard_agent_id: Mapped[uuid.UUID | None]
    workspace_id: Mapped[uuid.UUID | None]
    daily_token_budget: Mapped[int | None]
    rpm_limit: Mapped[int] = mapped_column(server_default="120")
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = ts_default()
    updated_at: Mapped[datetime] = ts_default()
