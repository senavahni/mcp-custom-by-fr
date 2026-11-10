"""Kapabilitas agent per tahap job `intel` — satu sumber aturan (M3_TASK_008).

collect = toolset `social` | `web` | `trends` (dan harus mencakup semua tipe sumber di spec);
analyze = toolset `analysis` tanpa `sentiment_analyze` di denylist; index = toolset `kb`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_marketing.core.errors import (
    AgentInactive,
    AgentNotCapable,
    UnknownAgent,
    ValidationFailed,
)
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


def index_enabled(spec: JobSpec) -> bool:
    return spec.stages.index is not None and spec.stages.index.enabled


def stage_agent_keys(spec: JobSpec) -> dict[str, str]:
    """Agent per tahap aktif, urut eksekusi (index hanya bila `stages.index.enabled`)."""
    keys = {"collect": spec.stages.collect.agent_key, "analyze": spec.stages.analyze.agent_key}
    if index_enabled(spec):
        assert spec.stages.index is not None
        keys["index"] = spec.stages.index.agent_key
    return keys


# ============================================================================ v1 (MCP-V1-03)
# Kapabilitas tahap job kind kontrak 1.3. Fungsi 1.2 di atas TIDAK berubah (AgentInfo 1.2 hanya
# mengenal collect/analyze/index).

CapabilityV1 = Literal[
    "collect",
    "analyze",
    "index",
    "expand_keywords",
    "analyze_product",
    "discover",
    "enrich",
    "import_map",
    "find_contacts",
    "extract_knowledge",
    "strategize",
    "create_content",
]
# kapabilitas -> (toolset yang wajib dimiliki semua, tool yang tidak boleh ada di denylist)
V1_RULES: dict[str, tuple[frozenset[str], str | None]] = {
    "expand_keywords": (frozenset({"mi"}), "mi_keyword_expand"),
    "analyze_product": (frozenset({"mi"}), "mi_product_analyze"),
    "discover": (frozenset({"ci", "web"}), None),
    "enrich": (frozenset({"ci", "web"}), None),
    "import_map": (frozenset({"ci"}), None),
    "find_contacts": (frozenset({"ci", "web"}), None),
    "extract_knowledge": (frozenset({"kn", "kg"}), None),
    "strategize": (frozenset({"st"}), None),
    "create_content": (frozenset({"ct"}), None),
}
# tahap -> kapabilitas per job kind; None = tahap sistem/engine (tanpa agent)
KIND_STAGE_CAPABILITY: dict[str, dict[str, str | None]] = {
    "company_discovery": {
        "product_analysis": "analyze_product",
        "discovery": "discover",
        "enrichment": "enrich",
        "index": None,
        "fit": None,
    },
    "company_import": {"profile": None, "mapping": "import_map", "normalize": None, "insert": None},
    "contact_finder": {
        "lookup": "find_contacts",
        "verify": "find_contacts",
        "enrichment": "enrich",
    },
    "social_listening": {
        "keywords": "expand_keywords",
        "collect": "collect",
        "analyze": None,  # sentimen/tema/sinyal lokal (MCP-V1-04)
        "index": None,
        "report": "analyze",
    },
    "book_ingest": {
        "extract": None,
        "chunk": None,
        "embed": None,
        "cards": "extract_knowledge",
        "graph": "extract_knowledge",
    },
    "opportunity_scoring": {"fit": None, "score": None},
    "strategy": {"insight": "analyze", "strategy": "strategize"},
    "content_bank": {"ideate": "create_content", "evaluate": None, "originality": None},
}
DEFAULT_KIND_AGENTS: dict[str, dict[str, str]] = {
    "company_discovery": {
        "product_analysis": "analyst",
        "discovery": "prospector",
        "enrichment": "prospector",
    },
    "company_import": {"mapping": "prospector"},
    "contact_finder": {"lookup": "prospector", "verify": "prospector", "enrichment": "prospector"},
    "social_listening": {"keywords": "researcher", "collect": "researcher", "report": "analyst"},
    "book_ingest": {"cards": "curator", "graph": "curator"},
    "opportunity_scoring": {},
    "strategy": {"insight": "analyst", "strategy": "strategist"},
    "content_bank": {"ideate": "creative"},
}
# satu-satunya agent yang boleh menulis mcp.companies / mcp.contacts (dipakai tool ci_*)
COMPANY_WRITERS = frozenset({"prospector"})
V1_STAGE_LABEL = {
    "product_analysis": "Analisis produk",
    "discovery": "Discovery",
    "enrichment": "Enrichment",
    "mapping": "Mapping kolom",
    "lookup": "Pencarian kontak",
    "verify": "Verifikasi domain",
    "keywords": "Kata kunci",
    "collect": "Kumpulkan",
    "report": "Laporan",
    "cards": "Knowledge card",
    "graph": "Knowledge graph",
    "insight": "Insight",
    "strategy": "Strategi",
    "ideate": "Ide konten",
}


def capabilities_v1(toolsets: Iterable[str], denylist: Iterable[str] = ()) -> list[CapabilityV1]:
    """Kapabilitas 1.2 + v1 dari toolset & denylist profil (urutan stabil)."""
    owned, denied = set(toolsets), set(denylist)
    caps: list[CapabilityV1] = list(capabilities(owned, denied))
    for cap, (needed, tool) in V1_RULES.items():
        if needed <= owned and (tool is None or tool not in denied):
            caps.append(cap)  # type: ignore[arg-type]
    return caps


def agent_stages(kind: str) -> dict[str, str]:
    """Tahap yang dikerjakan agent untuk `kind` -> kapabilitas yang dibutuhkan."""
    mapping = KIND_STAGE_CAPABILITY.get(kind)
    if mapping is None:
        raise ValueError(f"job kind tidak dikenal: {kind}")
    return {stage: cap for stage, cap in mapping.items() if cap is not None}


def check_kind_stage_agent(
    profile: AgentProfile | None, agent_key: str, stage: str, cap: str
) -> None:
    details = {"stage": stage, "agent_key": agent_key, "capability": cap}
    if profile is None:
        raise UnknownAgent(f"Agent '{agent_key}' tidak dikenal MCP", details=details)
    if not profile.is_active:
        raise AgentInactive(f"Agent '{agent_key}' sedang nonaktif", details=details)
    if cap not in capabilities_v1(profile.toolsets, profile.tool_denylist):
        label = V1_STAGE_LABEL.get(stage, stage)
        raise AgentNotCapable(
            f"Agent '{agent_key}' tidak mampu mengerjakan tahap {label}", details=details
        )


async def validate_kind_stage_agents(
    session: AsyncSession, kind: str, overrides: dict[str, str] | None = None
) -> dict[str, AgentProfile]:
    """Agent per tahap agentic job v1 (default + override Dashboard), tervalidasi.

    Override untuk tahap sistem atau tahap tak dikenal ditolak (VALIDATION_ERROR)."""
    stages = agent_stages(kind)
    overrides = overrides or {}
    unknown = sorted(set(overrides) - set(stages))
    if unknown:
        raise ValidationFailed(
            f"Tahap tanpa agent untuk {kind}: {', '.join(unknown)}", details={"stages": unknown}
        )
    keys = {stage: overrides.get(stage) or DEFAULT_KIND_AGENTS[kind][stage] for stage in stages}
    rows = (
        await session.scalars(
            select(AgentProfile).where(AgentProfile.agent_key.in_(set(keys.values())))
        )
    ).all()
    by_key = {p.agent_key: p for p in rows}
    out: dict[str, AgentProfile] = {}
    for stage, key in keys.items():
        profile = by_key.get(key)
        check_kind_stage_agent(profile, key, stage, stages[stage])
        assert profile is not None
        out[stage] = profile
    return out


def require_company_writer(agent_key: str | None) -> None:
    """Tulis companies/contacts hanya oleh `prospector` (atau sistem/Dashboard tanpa agent)."""
    if agent_key is not None and agent_key not in COMPANY_WRITERS:
        raise AgentNotCapable(
            f"Agent '{agent_key}' tidak boleh menulis data perusahaan/kontak",
            details={"agent_key": agent_key, "allowed": sorted(COMPANY_WRITERS)},
        )


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
