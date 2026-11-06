"""Kelola profil agent dari Admin Console: daftar, buat, ubah (tanpa deploy).

`system_prompt` = prompt peran (diketik admin) + aturan umum `agent_rules` yang SELALU digabung
otomatis, sama seperti profil bawaan. Profil tidak dihapus (riwayat run merujuknya): nonaktifkan
dengan `is_active=false`. Runtime membaca profil dari DB setiap run, jadi perubahan langsung
berlaku untuk run berikutnya.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_marketing.core.errors import Conflict, NotFound, ValidationFailed
from mcp_marketing.models import AgentProfile
from mcp_marketing.services.agent.profiles import PROMPT_VERSION
from mcp_marketing.services.llm import prompts
from mcp_marketing.tools import TOOL_MODULES

# `pm` = toolset Dashboard; runtime agent tidak pernah memberikannya ke LLM
AGENT_TOOLSETS = tuple(t for t in TOOL_MODULES if t != "pm")
AGENT_KEY_PATTERN = r"^[a-z][a-z0-9_-]{1,49}$"
# `fast`/`smart` (tier MCP) atau nama route/alias model Gateway, mis. openrouter/gemma-4-31b
ROUTE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$"
MAX_ROLE_PROMPT_CHARS = 8_000


class _ProfileFields(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    role_prompt: str | None = Field(default=None, min_length=20, max_length=MAX_ROLE_PROMPT_CHARS)
    route: str | None = Field(default=None, pattern=ROUTE_PATTERN)
    toolsets: list[str] | None = Field(default=None, min_length=1, max_length=len(AGENT_TOOLSETS))
    tool_denylist: list[str] | None = Field(default=None, max_length=100)
    run_token_budget: int | None = Field(default=None, ge=1_000, le=2_000_000)
    max_steps: int | None = Field(default=None, ge=1, le=100)
    is_active: bool | None = None

    @field_validator("toolsets")
    @classmethod
    def _known_toolsets(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        unknown = sorted(set(v) - set(AGENT_TOOLSETS))
        if unknown:
            raise ValueError(f"toolset tidak dikenal: {', '.join(unknown)}")
        return list(dict.fromkeys(v))

    @field_validator("tool_denylist")
    @classmethod
    def _clean_denylist(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return list(dict.fromkeys(s.strip() for s in v if s.strip()))


class AgentCreate(_ProfileFields):
    agent_key: str = Field(pattern=AGENT_KEY_PATTERN)
    display_name: str = Field(min_length=1, max_length=100)
    role_prompt: str = Field(min_length=20, max_length=MAX_ROLE_PROMPT_CHARS)
    route: str = Field(default="smart", pattern=ROUTE_PATTERN)
    toolsets: list[str] = Field(min_length=1, max_length=len(AGENT_TOOLSETS))
    run_token_budget: int = Field(default=120_000, ge=1_000, le=2_000_000)
    max_steps: int = Field(default=25, ge=1, le=100)


class AgentUpdate(_ProfileFields):
    pass


def _rules() -> str:
    return prompts.load("agent_rules", PROMPT_VERSION)


def compose_prompt(role_prompt: str) -> str:
    return f"{role_prompt.strip()}\n\n{_rules()}"


def role_of(system_prompt: str) -> str:
    """Bagian prompt peran saja (aturan umum yang digabung otomatis tidak ditampilkan)."""
    suffix = f"\n\n{_rules()}"
    return system_prompt[: -len(suffix)] if system_prompt.endswith(suffix) else system_prompt


def profile_dto(p: AgentProfile) -> dict[str, Any]:
    return {
        "agent_key": p.agent_key,
        "display_name": p.display_name,
        "role_prompt": role_of(p.system_prompt),
        "route": p.route,
        "toolsets": list(p.toolsets or []),
        "tool_denylist": list(p.tool_denylist or []),
        "run_token_budget": p.run_token_budget,
        "max_steps": p.max_steps,
        "is_active": p.is_active,
        "version": p.version,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


async def list_profiles(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.scalars(select(AgentProfile).order_by(AgentProfile.agent_key))).all()
    return [profile_dto(r) for r in rows]


async def create_profile(session: AsyncSession, body: AgentCreate) -> dict[str, Any]:
    if await session.get(AgentProfile, body.agent_key) is not None:
        raise Conflict(f"Agent '{body.agent_key}' sudah ada")
    profile = AgentProfile(
        agent_key=body.agent_key,
        display_name=body.display_name,
        system_prompt=compose_prompt(body.role_prompt),
        route=body.route,
        toolsets=body.toolsets,
        tool_denylist=body.tool_denylist or [],
        run_token_budget=body.run_token_budget,
        max_steps=body.max_steps,
        is_active=True if body.is_active is None else body.is_active,
    )
    session.add(profile)
    await session.flush()
    await session.refresh(profile)
    return profile_dto(profile)


async def update_profile(
    session: AsyncSession, agent_key: str, body: AgentUpdate
) -> dict[str, Any]:
    profile = await session.get(AgentProfile, agent_key)
    if profile is None:
        raise NotFound(f"Agent '{agent_key}' tidak ditemukan")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        raise ValidationFailed("Tidak ada field yang diubah")
    role_prompt = changes.pop("role_prompt", None)
    if role_prompt is not None:
        profile.system_prompt = compose_prompt(role_prompt)
    for field_name, value in changes.items():
        setattr(profile, field_name, value)
    profile.version = profile.version + 1
    profile.updated_at = func.now()
    await session.flush()
    await session.refresh(profile)
    return profile_dto(profile)
