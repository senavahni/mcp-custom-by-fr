"""MCP-V1-03: 6 profil agent, kapabilitas tahap job v1, injeksi doktrin."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_marketing.core.errors import AgentInactive, AgentNotCapable, ToolError, UnknownAgent
from mcp_marketing.schemas.intel_v13 import JOB_KINDS, KIND_STAGES, TOOLSETS_V13
from mcp_marketing.services.agent import doctrine as doc
from mcp_marketing.services.agent.profile_admin import AGENT_TOOLSETS, AgentCreate
from mcp_marketing.services.agent.profiles import DEFAULT_PROFILES, V1_AGENT_KEYS
from mcp_marketing.services.intel import capabilities as caps
from mcp_marketing.services.llm import prompts
from mcp_marketing.utils.tokens import count_tokens

PROFILES = {p.agent_key: p for p in DEFAULT_PROFILES}


def _profile(key: str, *, active: bool = True) -> SimpleNamespace:
    p = PROFILES[key]
    return SimpleNamespace(
        agent_key=key,
        is_active=active,
        toolsets=list(p.toolsets),
        tool_denylist=list(p.tool_denylist),
    )


# ---------------------------------------------------------------------------- profil


def test_six_default_agents_with_spec_routes_and_budgets() -> None:
    assert set(V1_AGENT_KEYS) == {
        "researcher",
        "analyst",
        "strategist",
        "creative",
        "curator",
        "prospector",
    }
    assert (PROFILES["creative"].route, PROFILES["creative"].run_token_budget) == (
        "creative",
        80_000,
    )
    assert (PROFILES["curator"].route, PROFILES["curator"].run_token_budget) == ("smart", 200_000)
    assert (PROFILES["prospector"].route, PROFILES["prospector"].run_token_budget) == (
        "extract",
        60_000,
    )


@pytest.mark.parametrize(
    ("key", "forbidden"),
    [
        ("creative", {"web", "social", "db", "ci"}),
        ("curator", {"web", "social", "task", "ci"}),
        ("prospector", {"social", "trends", "tpl", "task"}),
        ("researcher", {"ci"}),
    ],
)
def test_toolset_restrictions(key: str, forbidden: set[str]) -> None:
    assert not forbidden & set(PROFILES[key].toolsets)


def test_old_profiles_extended() -> None:
    assert {"mi", "opp", "pb", "kn"} <= set(PROFILES["analyst"].toolsets)
    assert {"st", "pb", "kn"} <= set(PROFILES["strategist"].toolsets)
    assert "mi" in PROFILES["researcher"].toolsets
    assert "mi_product_analyze" in PROFILES["researcher"].tool_denylist
    assert "mi_keyword_expand" not in PROFILES["researcher"].tool_denylist


@pytest.mark.parametrize("key", ["creative", "curator", "prospector"])
def test_new_prompts_within_600_tokens(key: str) -> None:
    role = prompts.load(f"agent_{key}", "v1")
    assert 50 < count_tokens(role) <= 600
    assert "agent_rules" not in role
    assert PROFILES[key].system_prompt().endswith(prompts.load("agent_rules", "v1").strip())


def test_admin_accepts_v1_toolsets() -> None:
    assert set(TOOLSETS_V13) <= set(AGENT_TOOLSETS)
    body = AgentCreate(
        agent_key="creative_b",
        display_name="Creative B",
        role_prompt="Penulis konten kreatif untuk kampanye kedua.",
        toolsets=["ct", "kn"],
    )
    assert body.toolsets == ["ct", "kn"]


def test_v12_capabilities_unchanged_for_old_agents() -> None:
    expected = {
        "researcher": ["collect", "analyze", "index"],
        "analyst": ["analyze", "index"],
        "strategist": ["index"],
    }
    for key, want in expected.items():
        p = PROFILES[key]
        assert caps.capabilities(p.toolsets, p.tool_denylist) == want


# ---------------------------------------------------------------------------- kapabilitas


def test_capabilities_v1_per_agent() -> None:
    def cv1(key: str) -> set[str]:
        p = PROFILES[key]
        return set(caps.capabilities_v1(p.toolsets, p.tool_denylist))

    assert {"discover", "enrich", "import_map", "find_contacts"} <= cv1("prospector")
    assert "extract_knowledge" in cv1("curator")
    assert "create_content" in cv1("creative")
    assert "strategize" in cv1("strategist")
    assert "analyze_product" in cv1("analyst")
    assert "expand_keywords" in cv1("researcher")
    assert "analyze_product" not in cv1("researcher")  # mi_product_analyze ditolak
    assert "discover" not in cv1("researcher")  # tanpa enrichment perusahaan
    assert "create_content" not in cv1("strategist")


def test_kind_stage_map_covers_contract_stages() -> None:
    assert set(caps.KIND_STAGE_CAPABILITY) == set(JOB_KINDS)
    for kind in JOB_KINDS:
        assert tuple(caps.KIND_STAGE_CAPABILITY[kind]) == KIND_STAGES[kind]
        assert set(caps.DEFAULT_KIND_AGENTS[kind]) == set(caps.agent_stages(kind))


@pytest.mark.parametrize("kind", JOB_KINDS)
def test_default_agents_are_capable(kind: str) -> None:
    for stage, cap in caps.agent_stages(kind).items():
        key = caps.DEFAULT_KIND_AGENTS[kind][stage]
        caps.check_kind_stage_agent(_profile(key), key, stage, cap)  # type: ignore[arg-type]


def test_incapable_inactive_unknown_agents_rejected() -> None:
    with pytest.raises(AgentNotCapable) as exc:
        caps.check_kind_stage_agent(
            _profile("creative"),
            "creative",
            "discovery",
            "discover",  # type: ignore[arg-type]
        )
    assert exc.value.details and exc.value.details["capability"] == "discover"
    with pytest.raises(AgentInactive):
        caps.check_kind_stage_agent(
            _profile("prospector", active=False),
            "prospector",
            "discovery",
            "discover",  # type: ignore[arg-type]
        )
    with pytest.raises(UnknownAgent):
        caps.check_kind_stage_agent(None, "ghost", "discovery", "discover")


class _Session:
    def __init__(self, profiles: list[Any]) -> None:
        self.profiles = profiles

    async def scalars(self, _stmt: Any) -> Any:
        return SimpleNamespace(all=lambda: self.profiles)


async def test_validate_kind_stage_agents_defaults_and_override() -> None:
    session = _Session([_profile("analyst"), _profile("prospector"), _profile("creative")])
    out = await caps.validate_kind_stage_agents(session, "company_discovery")  # type: ignore[arg-type]
    assert {s: p.agent_key for s, p in out.items()} == {
        "product_analysis": "analyst",
        "discovery": "prospector",
        "enrichment": "prospector",
    }
    with pytest.raises(AgentNotCapable):
        await caps.validate_kind_stage_agents(
            session,
            "company_discovery",
            {"discovery": "creative"},  # type: ignore[arg-type]
        )
    with pytest.raises(ToolError) as exc:
        await caps.validate_kind_stage_agents(session, "company_discovery", {"index": "analyst"})  # type: ignore[arg-type]
    assert exc.value.code == "VALIDATION_ERROR"


def test_only_prospector_writes_companies() -> None:
    caps.require_company_writer("prospector")
    caps.require_company_writer(None)  # sistem/Dashboard
    for key in ("analyst", "creative", "researcher"):
        with pytest.raises(AgentNotCapable):
            caps.require_company_writer(key)


# ---------------------------------------------------------------------------- doktrin

RULES = prompts.load("agent_rules", "v1").strip()


def _doctrine(content: str = "Gunakan STP sebelum menyusun pesan.") -> doc.ActiveDoctrine:
    return doc.ActiveDoctrine(uuid.uuid4(), "strategist", 3, content, count_tokens(content))


def test_doctrine_inserted_after_role_before_rules() -> None:
    system = PROFILES["strategist"].system_prompt()
    out = doc.compose_with_doctrine(system, _doctrine())
    role_end = out.index(prompts.load("agent_strategist", "v1").strip()[-30:])
    d_pos, r_pos = out.index('<doctrine role="strategist" version="3">'), out.index(RULES)
    assert role_end < d_pos < r_pos
    assert out.endswith(RULES)


def test_no_doctrine_keeps_prompt_identical() -> None:
    system = PROFILES["analyst"].system_prompt()
    assert doc.compose_with_doctrine(system, None) == system


def test_custom_prompt_without_rules_appends_doctrine() -> None:
    out = doc.compose_with_doctrine("Prompt kustom admin.", _doctrine())
    assert out.startswith("Prompt kustom admin.") and "</doctrine>" in out


def test_compose_is_deterministic_for_prompt_caching() -> None:
    system, d = PROFILES["creative"].system_prompt(), _doctrine()
    assert doc.compose_with_doctrine(system, d) == doc.compose_with_doctrine(system, d)


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    async def scalars(self, _stmt: Any) -> Any:
        return SimpleNamespace(all=lambda: self.rows)


def _row(ws: uuid.UUID | None, version: int, content: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), workspace_id=ws, role="analyst", version=version, content=content
    )


async def test_load_prefers_workspace_doctrine_and_caps_tokens() -> None:
    ws = uuid.uuid4()
    long_text = "prinsip segmentasi pasar " * 800
    session = _Rows([_row(None, 1, "global"), _row(ws, 4, long_text)])
    d = await doc.load_active_doctrine(session, ws, "analyst")  # type: ignore[arg-type]
    assert d is not None and d.version == 4
    assert d.tokens <= doc.DOCTRINE_MAX_TOKENS
    assert d.run_record()["version"] == 4
    assert await doc.load_active_doctrine(_Rows([]), ws, "analyst") is None  # type: ignore[arg-type]
