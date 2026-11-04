"""Key LLM Gateway per agent (M3_TASK_009) — disimpan terenkripsi AES-256-GCM (`MCP_SECRET_KEY`)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default, uuid_pk


class LlmCredential(Base):
    __tablename__ = "llm_credentials"

    id: Mapped[uuid.UUID] = uuid_pk()
    # nama unik = aad enkripsi: "agent:<agent_key>" atau "system"
    name: Mapped[str] = mapped_column(String(110), unique=True)
    agent_key: Mapped[str | None] = mapped_column(String(100))
    gateway_key_id: Mapped[uuid.UUID | None]
    key_prefix: Mapped[str] = mapped_column(String(16))
    key_enc: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    last_error: Mapped[str | None] = mapped_column(String(300))
    last_checked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = ts_default()
    rotated_at: Mapped[datetime | None]
