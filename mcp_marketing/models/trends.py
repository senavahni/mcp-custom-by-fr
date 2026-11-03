"""Google Trends — DATABASE §4.6 (dipakai penuh di TASK-17).

`trend_points` & `trend_related` tidak punya primary key di DDL; ORM memakai kolom gabungan
sebagai identitas pemetaan saja (tidak ada constraint di database).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import ForeignKey, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default, uuid_pk


class TrendQuery(Base):
    __tablename__ = "trend_queries"

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(16))
    keywords: Mapped[list[str]]
    geo: Mapped[str] = mapped_column(String(16), server_default="ID")
    start_date: Mapped[date]
    end_date: Mapped[date]
    category: Mapped[int] = mapped_column(server_default="0")
    gprop: Mapped[str] = mapped_column(String(16), server_default="web")
    resolution: Mapped[str | None] = mapped_column(String(8))
    provider: Mapped[str] = mapped_column(String(16))
    params_hash: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = ts_default()


class TrendPoint(Base):
    __tablename__ = "trend_points"

    query_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mcp.trend_queries.id", ondelete="CASCADE"), primary_key=True
    )
    keyword: Mapped[str] = mapped_column(Text, primary_key=True)
    bucket: Mapped[date | None] = mapped_column(primary_key=True)
    region: Mapped[str | None] = mapped_column(String(100), primary_key=True)
    value: Mapped[int] = mapped_column(SmallInteger)
    is_partial: Mapped[bool] = mapped_column(server_default="false")


class TrendRelated(Base):
    __tablename__ = "trend_related"

    query_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mcp.trend_queries.id", ondelete="CASCADE"), primary_key=True
    )
    keyword: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), primary_key=True)
    rank_type: Mapped[str] = mapped_column(String(8), primary_key=True)
    term: Mapped[str] = mapped_column(Text)
    value: Mapped[int | None]
    position: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
