"""Template Notion-like — DATABASE §4.4 (dipakai penuh di TASK-12)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default


class Template(Base):
    __tablename__ = "templates"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    category: Mapped[str] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(16), server_default="seed")
    current_version: Mapped[int] = mapped_column(server_default="1")
    workspace_id: Mapped[uuid.UUID | None]
    is_active: Mapped[bool] = mapped_column(server_default="true")
    created_by_client: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = ts_default()
    updated_at: Mapped[datetime] = ts_default()


class TemplateVersion(Base):
    __tablename__ = "template_versions"

    template_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("mcp.templates.key", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(primary_key=True)
    variables: Mapped[list[Any]] = mapped_column(server_default="[]")
    blocks: Mapped[list[Any]]
    subtasks: Mapped[list[Any]] = mapped_column(server_default="[]")
    outline: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = ts_default()
