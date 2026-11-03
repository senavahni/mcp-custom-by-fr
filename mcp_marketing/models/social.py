"""Post sosial media — DATABASE §4.6 (dipakai penuh di TASK-15/16)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Computed, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default, uuid_pk


class SocialPost(Base):
    __tablename__ = "social_posts"

    id: Mapped[uuid.UUID] = uuid_pk()
    source: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(Text)
    author_handle: Mapped[str | None] = mapped_column(String(100))
    author_id: Mapped[str | None] = mapped_column(String(64))
    author_followers: Mapped[int | None]
    text: Mapped[str] = mapped_column(Text)
    lang: Mapped[str | None] = mapped_column(String(8))
    posted_at: Mapped[datetime]
    is_reply: Mapped[bool] = mapped_column(server_default="false")
    is_repost: Mapped[bool] = mapped_column(server_default="false")
    likes: Mapped[int] = mapped_column(server_default="0")
    replies: Mapped[int] = mapped_column(server_default="0")
    reposts: Mapped[int] = mapped_column(server_default="0")
    quotes: Mapped[int] = mapped_column(server_default="0")
    views: Mapped[int | None] = mapped_column(BigInteger)
    hashtags: Mapped[list[str]] = mapped_column(server_default="{}")
    mentions: Mapped[list[str]] = mapped_column(server_default="{}")
    media_type: Mapped[str | None] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(16))
    raw: Mapped[dict[str, Any] | None]
    first_seen_job: Mapped[str | None] = mapped_column(String(16))
    collected_at: Mapped[datetime] = ts_default()
    text_tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True), deferred=True
    )


class JobPost(Base):
    __tablename__ = "job_posts"

    job_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("mcp.jobs.id", ondelete="CASCADE"), primary_key=True
    )
    post_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mcp.social_posts.id", ondelete="CASCADE"), primary_key=True
    )
    matched_keywords: Mapped[list[str]] = mapped_column(server_default="{}")
