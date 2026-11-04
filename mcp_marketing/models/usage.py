"""Audit tool/LLM, rollup pemakaian, cache — DATABASE §4.1."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from mcp_marketing.models.base import Base, ts_default, uuid_pk


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = uuid_pk()
    request_id: Mapped[str] = mapped_column(String(64))
    client_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mcp.api_clients.id"))
    run_id: Mapped[uuid.UUID | None]
    tool: Mapped[str] = mapped_column(String(64))
    args_hash: Mapped[str] = mapped_column(String(64))
    args_preview: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(48))
    duration_ms: Mapped[int]
    output_tokens: Mapped[int] = mapped_column(server_default="0")
    baseline_tokens: Mapped[int | None]
    llm_tokens: Mapped[int] = mapped_column(server_default="0")
    job_id: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = ts_default()


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = uuid_pk()
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mcp.tool_calls.id", ondelete="SET NULL")
    )
    run_id: Mapped[uuid.UUID | None]
    client_id: Mapped[uuid.UUID | None]
    purpose: Mapped[str] = mapped_column(String(32))
    route: Mapped[str] = mapped_column(String(64))
    model_resolved: Mapped[str | None] = mapped_column(String(128))
    gateway_request_id: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(server_default="0")
    completion_tokens: Mapped[int] = mapped_column(server_default="0")
    cached: Mapped[bool] = mapped_column(server_default="false")
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int]
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(48))
    agent_key: Mapped[str | None] = mapped_column(String(100))
    job_id: Mapped[str | None] = mapped_column(String(16))
    stage: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = ts_default()


class UsageDaily(Base):
    __tablename__ = "usage_daily"

    day: Mapped[date] = mapped_column(primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tool: Mapped[str] = mapped_column(String(64), primary_key=True)
    calls: Mapped[int] = mapped_column(server_default="0")
    errors: Mapped[int] = mapped_column(server_default="0")
    cache_hits: Mapped[int] = mapped_column(server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0")
    baseline_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0")
    llm_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), server_default="0")
    external_units: Mapped[int] = mapped_column(BigInteger, server_default="0")


class CacheEntry(Base):
    __tablename__ = "cache_entries"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(32))
    value: Mapped[dict[str, Any]]
    size_bytes: Mapped[int]
    hits: Mapped[int] = mapped_column(server_default="0")
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = ts_default()
