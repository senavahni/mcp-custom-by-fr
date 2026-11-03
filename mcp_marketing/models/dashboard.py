"""Model READ-ONLY untuk tabel `public` milik Dashboard (Prisma) — hanya kolom yang di-grant.

Tidak dikelola Alembic (schema `public`). Flush yang menyentuh model ini -> RuntimeError (D4:
penulisan data inti lewat webhook/Service API, bukan SQL langsung).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import String, Text, event
from sqlalchemy.orm import Mapped, Session, mapped_column

from mcp_marketing.models.base import Base

READONLY = {"schema": "public", "info": {"readonly": True}}


class DashboardUser(Base):
    """Tanpa email/password_hash (tidak di-grant ke mcp_app)."""

    __tablename__ = "users"
    __table_args__ = READONLY

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(String(100))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool]


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = READONLY

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None]


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = READONLY

    workspace_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(16))


class DashboardAgent(Base):
    __tablename__ = "agents"
    __table_args__ = READONLY

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(String(100))
    handle: Mapped[str] = mapped_column(Text)
    mcp_agent_key: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool]
    deleted_at: Mapped[datetime | None]


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = READONLY

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = READONLY

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    project_id: Mapped[uuid.UUID]
    parent_task_id: Mapped[uuid.UUID | None]
    depth: Mapped[int]
    title: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16))
    priority: Mapped[str] = mapped_column(String(16))
    assignee_type: Mapped[str | None] = mapped_column(String(16))
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]
    is_milestone: Mapped[bool]
    tags: Mapped[list[str]]
    agent_status: Mapped[str] = mapped_column(String(16))
    completed_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]


class DocumentBlock(Base):
    __tablename__ = "document_blocks"
    __table_args__ = READONLY

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    project_id: Mapped[uuid.UUID]
    task_id: Mapped[uuid.UUID | None]
    parent_block_id: Mapped[uuid.UUID | None]
    block_type: Mapped[str] = mapped_column(String(32))
    content_json: Mapped[dict[str, Any]]
    plain_text: Mapped[str] = mapped_column(Text)
    order_index: Mapped[int]
    version: Mapped[int]
    updated_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = READONLY

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    project_id: Mapped[uuid.UUID]
    entity_type: Mapped[str] = mapped_column(String(16))
    entity_id: Mapped[uuid.UUID]
    parent_comment_id: Mapped[uuid.UUID | None]
    author_type: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]


class ActivityLog(Base):
    __tablename__ = "activity_logs"
    __table_args__ = READONLY

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    project_id: Mapped[uuid.UUID | None]
    entity_type: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))
    actor_type: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime]


class DashboardAgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = READONLY

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    project_id: Mapped[uuid.UUID]
    task_id: Mapped[uuid.UUID | None]
    agent_id: Mapped[uuid.UUID]
    trigger: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    mcp_run_id: Mapped[str | None] = mapped_column(String(200))


def _is_readonly(obj: object) -> bool:
    table = getattr(obj, "__table__", None)
    return bool(table is not None and table.info.get("readonly"))


@event.listens_for(Session, "before_flush")
def _block_readonly_writes(session: Session, flush_context: Any, instances: Any) -> None:
    for obj in (*session.new, *session.dirty, *session.deleted):
        if _is_readonly(obj):
            raise RuntimeError(
                f"Model {type(obj).__name__} read-only (schema public milik Dashboard); "
                "tulis lewat webhook/Service API"
            )
