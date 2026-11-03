"""Registry entitas tool `db_*` (whitelist DATABASE §5).

Hanya field yang terdaftar di sini bisa dibaca, difilter, atau diurutkan; kolom lain (mis.
`users.email`) memang tidak di-grant. Entitas inti Dashboard read-only (D4); tulis diarahkan ke
toolset `task`/`tpl`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import FromClause, select

from mcp_marketing.models.dashboard import (
    ActivityLog,
    Comment,
    DashboardAgent,
    DashboardUser,
    DocumentBlock,
    Project,
    Task,
    Workspace,
    WorkspaceMember,
)
from mcp_marketing.models.kb import KbCollection
from mcp_marketing.models.master import Competitor, KeywordWatchlist
from mcp_marketing.models.reports import AnalysisReport
from mcp_marketing.models.social import SocialPost
from mcp_marketing.models.templates import Template

Access = Literal["r", "rw", "rd"]  # rd = baca + hapus (laporan)
# Cara membatasi baris ke workspace klien/run
Scope = Literal["self", "column", "project", "none"]
TASK_STATUS = ("todo", "in_progress", "in_review", "done")
PRIORITY = ("none", "low", "medium", "high", "urgent")
MAX_KEYWORDS = 50
SOCIAL_SOURCES = frozenset({"x", "threads", "trends"})


@dataclass(frozen=True)
class EntitySpec:
    name: str
    source: Callable[[], FromClause]
    access: Access
    fields: dict[
        str, str
    ]  # nama -> tipe: uuid|str|int|float|bool|date|datetime|str[]|json|enum:a,b
    default_fields: tuple[str, ...]
    scope: Scope = "none"
    schema: str = "public"
    soft_delete: bool = False
    updated_col: str | None = None
    redirect_write: str | None = None
    write_model: type[BaseModel] | None = None
    id_field: str = "id"
    searchable: tuple[str, ...] = ()
    hidden_filters: dict[str, Any] = field(default_factory=dict)

    def field_type(self, name: str) -> str:
        return self.fields[name]


def _table(model: Any) -> Callable[[], FromClause]:
    return lambda: model.__table__


def _members() -> FromClause:
    return (
        select(
            WorkspaceMember.workspace_id,
            WorkspaceMember.user_id,
            DashboardUser.display_name,
            WorkspaceMember.role,
        )
        .join(DashboardUser, DashboardUser.id == WorkspaceMember.user_id)
        .subquery("members")
    )


# ------------------------------------------------------------------ model tulis (entitas rw)
def _clean_keywords(v: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(k.strip() for k in v if k.strip()))
    if len(cleaned) > MAX_KEYWORDS:
        raise ValueError(f"keywords maksimal {MAX_KEYWORDS}")
    if any(len(k) > 100 for k in cleaned):
        raise ValueError("tiap keyword maksimal 100 karakter")
    return cleaned


class CompetitorWrite(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=150)
    website: str | None = Field(default=None, max_length=500)
    social: dict[str, str] = Field(default_factory=dict, max_length=20)
    keywords: list[str] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=5_000)

    @field_validator("website")
    @classmethod
    def _website(cls, v: str | None) -> str | None:
        if v and not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("website harus URL http/https")
        return v

    @field_validator("keywords")
    @classmethod
    def _kw(cls, v: list[str]) -> list[str]:
        return _clean_keywords(v)


class WatchlistWrite(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=150)
    competitor_id: uuid.UUID | None = None
    keywords: list[str] = Field(min_length=1)
    sources: list[str] = Field(default_factory=lambda: ["x", "threads", "trends"])
    schedule_id: uuid.UUID | None = None

    @field_validator("keywords")
    @classmethod
    def _kw(cls, v: list[str]) -> list[str]:
        cleaned = _clean_keywords(v)
        if not cleaned:
            raise ValueError("keywords minimal 1")
        return cleaned

    @field_validator("sources")
    @classmethod
    def _sources(cls, v: list[str]) -> list[str]:
        if not v or not set(v) <= SOCIAL_SOURCES:
            raise ValueError(f"sources harus subset {sorted(SOCIAL_SOURCES)}")
        return sorted(set(v))


class KbCollectionWrite(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=2_000)
    project_id: uuid.UUID | None = None


# ------------------------------------------------------------------ registry
_TASK_FIELDS = {
    "id": "uuid",
    "project_id": "uuid",
    "parent_task_id": "uuid",
    "depth": "int",
    "title": "str",
    "status": "enum:" + ",".join(TASK_STATUS),
    "priority": "enum:" + ",".join(PRIORITY),
    "assignee_type": "str",
    "start_date": "date",
    "end_date": "date",
    "is_milestone": "bool",
    "tags": "str[]",
    "agent_status": "str",
    "completed_at": "datetime",
}

ENTITIES: dict[str, EntitySpec] = {
    spec.name: spec
    for spec in (
        EntitySpec(
            "workspaces",
            _table(Workspace),
            "r",
            {"id": "uuid", "name": "str", "timezone": "str"},
            ("id", "name", "timezone"),
            scope="self",
            soft_delete=True,
        ),
        EntitySpec(
            "members",
            _members,
            "r",
            {"workspace_id": "uuid", "user_id": "uuid", "display_name": "str", "role": "str"},
            ("user_id", "display_name", "role"),
            scope="column",
            id_field="user_id",
            searchable=("display_name",),
        ),
        EntitySpec(
            "agents",
            _table(DashboardAgent),
            "r",
            {
                "id": "uuid",
                "name": "str",
                "handle": "str",
                "mcp_agent_key": "str",
                "is_active": "bool",
            },
            ("id", "name", "handle", "mcp_agent_key"),
            scope="column",
            soft_delete=True,
        ),
        EntitySpec(
            "projects",
            _table(Project),
            "r",
            {
                "id": "uuid",
                "workspace_id": "uuid",
                "title": "str",
                "description": "str",
                "created_at": "datetime",
            },
            ("id", "title", "created_at"),
            scope="column",
            soft_delete=True,
            searchable=("title",),
            redirect_write="tpl_render (Project Brief) / Dashboard",
        ),
        EntitySpec(
            "tasks",
            _table(Task),
            "r",
            _TASK_FIELDS,
            ("id", "title", "status", "end_date"),
            scope="project",
            soft_delete=True,
            searchable=("title",),
            redirect_write="task_create / task_update",
        ),
        EntitySpec(
            "blocks",
            _table(DocumentBlock),
            "r",
            {
                "id": "uuid",
                "project_id": "uuid",
                "task_id": "uuid",
                "block_type": "str",
                "plain_text": "str",
                "version": "int",
                "updated_at": "datetime",
            },
            ("id", "block_type", "plain_text"),
            scope="project",
            soft_delete=True,
            searchable=("plain_text",),
            redirect_write="tpl_render / write_blocks",
        ),
        EntitySpec(
            "comments",
            _table(Comment),
            "r",
            {
                "id": "uuid",
                "project_id": "uuid",
                "entity_type": "str",
                "entity_id": "uuid",
                "author_type": "str",
                "content": "str",
                "created_at": "datetime",
            },
            ("id", "author_type", "content", "created_at"),
            scope="project",
            soft_delete=True,
            searchable=("content",),
            redirect_write="task_comment",
        ),
        EntitySpec(
            "activity",
            _table(ActivityLog),
            "r",
            {
                "id": "uuid",
                "project_id": "uuid",
                "entity_type": "str",
                "action": "str",
                "actor_type": "str",
                "created_at": "datetime",
            },
            ("id", "entity_type", "action", "created_at"),
            scope="column",
        ),
        EntitySpec(
            "competitors",
            _table(Competitor),
            "rw",
            {
                "id": "uuid",
                "workspace_id": "uuid",
                "name": "str",
                "website": "str",
                "social": "json",
                "keywords": "str[]",
                "notes": "str",
                "created_at": "datetime",
                "updated_at": "datetime",
            },
            ("id", "name", "website", "keywords"),
            scope="column",
            schema="mcp",
            soft_delete=True,
            updated_col="updated_at",
            write_model=CompetitorWrite,
            searchable=("name",),
        ),
        EntitySpec(
            "watchlists",
            _table(KeywordWatchlist),
            "rw",
            {
                "id": "uuid",
                "workspace_id": "uuid",
                "name": "str",
                "competitor_id": "uuid",
                "keywords": "str[]",
                "sources": "str[]",
                "schedule_id": "uuid",
                "created_at": "datetime",
                "updated_at": "datetime",
            },
            ("id", "name", "keywords", "sources"),
            scope="column",
            schema="mcp",
            soft_delete=True,
            updated_col="updated_at",
            write_model=WatchlistWrite,
            searchable=("name",),
        ),
        EntitySpec(
            "reports",
            _table(AnalysisReport),
            "rd",
            {
                "id": "str",
                "kind": "enum:sentiment,market,campaign_plan,campaign_eval,digital_strategy",
                "title": "str",
                "summary": "str",
                "key_numbers": "json",
                "project_id": "uuid",
                "created_at": "datetime",
            },
            ("id", "kind", "title", "created_at"),
            scope="column",
            schema="mcp",
            searchable=("title",),
        ),
        EntitySpec(
            "templates",
            _table(Template),
            "r",
            {"key": "str", "name": "str", "category": "str", "origin": "str"},
            ("key", "name", "category", "origin"),
            scope="none",
            schema="mcp",
            id_field="key",
            searchable=("name",),
            redirect_write="tpl_save",
            hidden_filters={"is_active": True},
        ),
        EntitySpec(
            "social_posts",
            _table(SocialPost),
            "r",
            {
                "id": "uuid",
                "source": "enum:x,threads",
                "posted_at": "datetime",
                "author_handle": "str",
                "text": "str",
                "likes": "int",
                "replies": "int",
                "reposts": "int",
            },
            ("source", "posted_at", "author_handle", "text", "likes"),
            scope="none",  # post publik, tidak terikat workspace
            schema="mcp",
            searchable=("text",),
        ),
        EntitySpec(
            "kb_collections",
            _table(KbCollection),
            "rw",
            {
                "id": "uuid",
                "workspace_id": "uuid",
                "project_id": "uuid",
                "name": "str",
                "description": "str",
                "embed_model": "str",
                "embed_dim": "int",
                "created_at": "datetime",
            },
            ("id", "name", "embed_model", "embed_dim"),
            scope="column",
            schema="mcp",
            write_model=KbCollectionWrite,
            searchable=("name",),
        ),
    )
}


def entity(name: str) -> EntitySpec:
    from mcp_marketing.core.errors import ValidationFailed

    spec = ENTITIES.get(name)
    if spec is None:
        raise ValidationFailed(
            f"Entitas '{name}' tidak dikenal", details={"allowed": sorted(ENTITIES)}
        )
    return spec


def describe() -> list[dict[str, Any]]:
    """Ringkasan whitelist untuk `db_entities` (hemat token: `nama:tipe`)."""
    return [
        {
            "name": s.name,
            "schema": s.schema,
            "access": s.access,
            "fields": [f"{k}:{v}" for k, v in s.fields.items()],
            **({"write_via": s.redirect_write} if s.redirect_write else {}),
        }
        for s in ENTITIES.values()
    ]
