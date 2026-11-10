"""Profil agent bawaan (MCP_SPEC §7) + akses profil dari DB."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_marketing.models import AgentProfile
from mcp_marketing.services.llm import prompts

PROMPT_VERSION = "v1"


@dataclass(frozen=True)
class ProfileSeed:
    agent_key: str
    display_name: str
    route: str
    toolsets: tuple[str, ...]
    run_token_budget: int
    max_steps: int = 25
    tool_denylist: tuple[str, ...] = field(default_factory=tuple)

    def system_prompt(self) -> str:
        role = prompts.load(f"agent_{self.agent_key}", PROMPT_VERSION)
        rules = prompts.load("agent_rules", PROMPT_VERSION)
        return f"{role}\n\n{rules}"


# researcher hanya boleh mi_keyword_expand dari toolset `mi` (MCP-V1-03): tool mi lain ditolak
RESEARCHER_MI_DENY = (
    "mi_product_analyze",
    "mi_product_analysis_review",
    "mi_kpis",
    "mi_industry_map",
    "mi_geo_summary",
    "mi_product_matrix",
    "mi_segment_compare",
)
# prospector: web terbatas (web_scrape/web_crawl/web_extract) tanpa tool profil browser
PROSPECTOR_WEB_DENY = ("browser_profile_list", "browser_profile_check")

DEFAULT_PROFILES: tuple[ProfileSeed, ...] = (
    ProfileSeed(
        agent_key="strategist",
        display_name="Agent Strategist",
        route="smart",
        # v1: + st, pb, kn; baca opp/mi (tool tulis opp ditolak)
        toolsets=(
            "pm_agent",
            "tpl",
            "task",
            "kb",
            "analysis",
            "data",
            "st",
            "pb",
            "kn",
            "opp",
            "mi",
        ),
        run_token_budget=120_000,
        tool_denylist=(
            "sentiment_analyze",
            "sentiment_summary",
            "market_analyze",
            "opp_score_run",
            "mi_product_analyze",
            "mi_product_analysis_review",
            "mi_keyword_expand",
        ),
    ),
    ProfileSeed(
        agent_key="researcher",
        display_name="Agent Researcher",
        route="smart",
        # v1: + mi_keyword_expand saja; tanpa enrichment perusahaan (tanpa ci)
        toolsets=("pm_agent", "web", "social", "trends", "kb", "analysis", "data", "job", "mi"),
        run_token_budget=150_000,
        tool_denylist=("campaign_analyze", "dm_strategy", *RESEARCHER_MI_DENY),
    ),
    ProfileSeed(
        agent_key="analyst",
        display_name="Agent Analyst",
        route="smart",
        # v1: + mi, opp, pb, kn
        toolsets=("pm_agent", "db", "data", "analysis", "kb", "job", "mi", "opp", "pb", "kn"),
        run_token_budget=100_000,
    ),
    ProfileSeed(
        agent_key="creative",
        display_name="Agent Creative",
        route="creative",
        # tanpa web, social, db, ci; mi hanya baca (tool tulis mi ditolak)
        toolsets=("pm_agent", "ct", "kn", "kb", "mi", "pb"),
        run_token_budget=80_000,
        tool_denylist=("mi_product_analyze", "mi_product_analysis_review", "mi_keyword_expand"),
    ),
    ProfileSeed(
        agent_key="curator",
        display_name="Agent Curator",
        route="smart",
        # tanpa web, social, task, ci; buku panjang -> budget besar
        toolsets=("kn", "kg", "kb"),
        run_token_budget=200_000,
    ),
    ProfileSeed(
        agent_key="prospector",
        display_name="Agent Prospector",
        route="extract",
        # tanpa social, trends, tpl, task; satu-satunya penulis companies/contacts (lihat
        # services.intel.capabilities.COMPANY_WRITERS)
        toolsets=("ci", "web", "job"),
        run_token_budget=60_000,
        tool_denylist=PROSPECTOR_WEB_DENY,
    ),
)
V1_AGENT_KEYS: tuple[str, ...] = tuple(p.agent_key for p in DEFAULT_PROFILES)


async def get_active_profile(session: AsyncSession, agent_key: str) -> AgentProfile | None:
    row = await session.scalar(
        select(AgentProfile).where(
            AgentProfile.agent_key == agent_key, AgentProfile.is_active.is_(True)
        )
    )
    return row
