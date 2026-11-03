"""Profil browser & halaman web — DATABASE §4.5 (dipakai penuh di TASK-14)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default, uuid_pk


class BrowserProfile(Base):
    __tablename__ = "browser_profiles"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str | None] = mapped_column(String(200))
    user_data_dir: Mapped[str] = mapped_column(Text)
    profile_directory: Mapped[str] = mapped_column(String(64), server_default="Default")
    user_agent: Mapped[str | None] = mapped_column(Text)
    window_size: Mapped[str] = mapped_column(String(16), server_default="1366,900")
    proxy_url_env: Mapped[str | None] = mapped_column(String(64))
    headless: Mapped[bool] = mapped_column(server_default="false")
    allowed_sites: Mapped[list[str]] = mapped_column(server_default="{}")
    login_status: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    locked_by_job: Mapped[str | None] = mapped_column(String(16))
    locked_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    is_active: Mapped[bool] = mapped_column(server_default="true")
    created_at: Mapped[datetime] = ts_default()


class WebPage(Base):
    __tablename__ = "web_pages"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[str | None] = mapped_column(
        String(16), ForeignKey("mcp.jobs.id", ondelete="SET NULL")
    )
    url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(24))
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    title: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None]
    content_md: Mapped[str | None] = mapped_column(Text)
    extracted: Mapped[dict[str, Any] | None]
    content_hash: Mapped[str | None] = mapped_column(String(64))
    chars: Mapped[int] = mapped_column(server_default="0")
    fetched_at: Mapped[datetime] = ts_default()
