"""Handle hasil (result store) — DATABASE §4.2."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default


class ResultSet(Base):
    __tablename__ = "result_sets"

    handle: Mapped[str] = mapped_column(String(16), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    client_id: Mapped[uuid.UUID | None]
    run_id: Mapped[uuid.UUID | None]
    job_id: Mapped[str | None] = mapped_column(String(16))
    columns: Mapped[list[str]]
    total: Mapped[int] = mapped_column(server_default="0")
    stats: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    source_ref: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = ts_default()


class ResultItem(Base):
    __tablename__ = "result_items"

    handle: Mapped[str] = mapped_column(
        String(16), ForeignKey("mcp.result_sets.handle", ondelete="CASCADE"), primary_key=True
    )
    idx: Mapped[int] = mapped_column(primary_key=True)
    ref_id: Mapped[uuid.UUID | None]
    data: Mapped[dict[str, Any]]
    sort_ts: Mapped[datetime | None]
    sort_num: Mapped[float | None] = mapped_column(Float)
