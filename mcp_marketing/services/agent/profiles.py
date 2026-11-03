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


DEFAULT_PROFILES: tuple[ProfileSeed, ...] = (
    ProfileSeed(
        agent_key="strategist",
        display_name="Agent Strategist",
        route="smart",
        toolsets=("pm_agent", "tpl", "task", "kb", "analysis", "data"),
        run_token_budget=120_000,
        tool_denylist=("sentiment_analyze", "sentiment_summary", "market_analyze"),
    ),
    ProfileSeed(
        agent_key="researcher",
        display_name="Agent Researcher",
        route="smart",
        toolsets=("pm_agent", "web", "social", "trends", "kb", "analysis", "data", "job"),
        run_token_budget=150_000,
        tool_denylist=("campaign_analyze", "dm_strategy"),
    ),
    ProfileSeed(
        agent_key="analyst",
        display_name="Agent Analyst",
        route="smart",
        toolsets=("pm_agent", "db", "data", "analysis", "kb", "job"),
        run_token_budget=100_000,
    ),
)


async def get_active_profile(session: AsyncSession, agent_key: str) -> AgentProfile | None:
    row = await session.scalar(
        select(AgentProfile).where(
            AgentProfile.agent_key == agent_key, AgentProfile.is_active.is_(True)
        )
    )
    return row
