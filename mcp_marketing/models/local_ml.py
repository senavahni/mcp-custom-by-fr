"""Classifier lokal berversi (MCP-V1-04)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default, uuid_pk


class LocalClassifier(Base):
    __tablename__ = "local_classifiers"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(48))
    version: Mapped[int]
    embed_model: Mapped[str] = mapped_column(String(64))
    embed_dim: Mapped[int] = mapped_column(SmallInteger)
    labels: Mapped[list[str]]
    params: Mapped[dict[str, Any]]
    metrics: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    dataset: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    status: Mapped[str] = mapped_column(String(16), server_default="draft")
    created_at: Mapped[datetime] = ts_default()
    activated_at: Mapped[datetime | None]
