"""Route policy: `purpose` -> route Gateway termurah yang cukup (MCP_SPEC §6, T6)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from mcp_marketing.core.config import LlmSettings

Purpose = Literal[
    "classify",
    "extract",
    "estimate",
    "summarize_short",
    "sentiment_fallback",
    "reasoning",
    "strategy",
    "report",
    "agent_loop",
    "compact",
    "repair",
    "mission_plan",
    "task_breakdown",
]
Tier = Literal["fast", "smart"]


@dataclass(frozen=True)
class RouteConfig:
    tier: Tier
    temperature: float
    max_tokens: int
    json: bool
    # model reasoning (gpt-5): tanpa batas usaha, token output bisa habis → konten kosong
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None


POLICY: dict[str, RouteConfig] = {
    "classify": RouteConfig("fast", 0.0, 800, True),
    "extract": RouteConfig("fast", 0.0, 800, True),
    "estimate": RouteConfig("fast", 0.0, 800, True),
    "summarize_short": RouteConfig("fast", 0.0, 800, True),
    "sentiment_fallback": RouteConfig("fast", 0.0, 800, False),
    "reasoning": RouteConfig("smart", 0.3, 4_000, True),
    "strategy": RouteConfig("smart", 0.3, 4_000, True),
    "report": RouteConfig("smart", 0.3, 4_000, True),
    "agent_loop": RouteConfig("smart", 0.3, 2_000, False),
    "compact": RouteConfig("fast", 0.0, 400, False),
    "repair": RouteConfig("fast", 0.0, 800, True),
    # rencana misi ≤ 60 task + subtask (M3_TASK_013); model reasoning butuh ruang token lebih
    # minimal: rencana JSON terstruktur; reasoning panjang membuat gpt-5 melewati timeout 120 s
    "mission_plan": RouteConfig("smart", 0.3, 8_000, True, reasoning_effort="minimal"),
    "task_breakdown": RouteConfig("fast", 0.2, 2_000, True, reasoning_effort="low"),
}


def resolve(
    purpose: str, settings: LlmSettings, *, route_override: str | None = None
) -> tuple[str, RouteConfig]:
    """`(nama_route_gateway, config)`; `route_override` = `fast`/`smart` atau nama route."""
    config = POLICY.get(purpose)
    if config is None:
        raise ValueError(f"purpose LLM tidak dikenal: {purpose}")
    tiers = {"fast": settings.llm_route_fast, "smart": settings.llm_route_smart}
    if route_override:
        if route_override in tiers:
            config = replace(config, tier=route_override)  # type: ignore[arg-type]
        else:
            return route_override, config
    return tiers[config.tier], config
