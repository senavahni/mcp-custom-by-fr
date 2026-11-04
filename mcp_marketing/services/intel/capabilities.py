"""Kapabilitas agent per tahap job `intel` — satu sumber aturan (M3_TASK_008).

collect = toolset `social` | `web` | `trends` (dan harus mencakup semua tipe sumber di spec);
analyze = toolset `analysis` tanpa `sentiment_analyze` di denylist; index = toolset `kb`.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_marketing.core.errors import AgentInactive, AgentNotCapable, UnknownAgent, ValidationFailed
from mcp_marketing.models import AgentProfile
from mcp_marketing.schemas.intel import Capability, JobSpec

TOOLSET_BY_SOURCE = {"x": "social", "threads": "social", "web": "web", "trends": "trends"}
COLLECT_TOOLSETS = frozenset(TOOLSET_BY_SOURCE.values())
ANALYZE_TOOL = "sentiment_analyze"
STAGE_LABEL = {"collect": "Kumpulkan", "analyze": "Analisis", "index": "Indeks"}


def capabilities(toolsets: Iterable[str], denylist: Iterable[str] = ()) -> list[Capability]:
    owned = set(toolsets)
    caps: list[Capability] = []
    if owned & COLLECT_TOOLSETS:
        caps.append("collect")
    if "analysis" in owned and ANALYZE_TOOL not in set(denylist):
        caps.append("analyze")
    if "kb" in owned:
        caps.append("index")
    return caps


def check_stage_agent(
    profile: AgentProfile | None, agent_key: str, stage: Capability, source_types: list[str]
) -> None:
    details = {"stage": stage, "agent_key": agent_key}
    if profile is None:
        raise UnknownAgent(f"Agent '{agent_key}' tidak dikenal MCP", details=details)
    if not profile.is_active:
        raise AgentInactive(f"Agent '{agent_key}' sedang nonaktif", details=details)
    if stage not in capabilities(profile.toolsets, profile.tool_denylist):
        raise AgentNotCapable(
            f"Agent '{agent_key}' tidak mampu mengerjakan tahap {STAGE_LABEL[stage]}",
            details=details,
        )
    if stage == "collect":
        missing = sorted({TOOLSET_BY_SOURCE[t] for t in source_types} - set(profile.toolsets))
        if missing:
            raise AgentNotCapable(
                f"Agent '{agent_key}' tidak punya toolset {', '.join(missing)} untuk sumber job",
                details={**details, "missing_toolsets": missing},
            )


def stage_agent_keys(spec: JobSpec) -> dict[str, str]:
    keys = {"collect": spec.stages.collect.agent_key, "analyze": spec.stages.analyze.agent_key}
    if spec.stages.index is not None and spec.stages.index.enabled:
        raise ValidationFailed(
            "Tahap index (Knowledge Base) belum didukung", details={"field": "stages.index"}
        )
    return keys


async def validate_stage_agents(session: AsyncSession, spec: JobSpec) -> dict[str, AgentProfile]:
    """Profil per tahap tervalidasi (UNKNOWN_AGENT / AGENT_INACTIVE / AGENT_NOT_CAPABLE)."""
    keys = stage_agent_keys(spec)
    rows = (
        await session.scalars(
            select(AgentProfile).where(AgentProfile.agent_key.in_(set(keys.values())))
        )
    ).all()
    by_key = {p.agent_key: p for p in rows}
    sources = spec.source_types()
    out: dict[str, AgentProfile] = {}
    for stage, key in keys.items():
        profile = by_key.get(key)
        check_stage_agent(profile, key, stage, sources)  # type: ignore[arg-type]
        assert profile is not None
        out[stage] = profile
    return out
