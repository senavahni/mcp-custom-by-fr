"""Seed data referensi (idempoten): profil agent, klien sistem `dashboard`, jadwal bawaan.

Template seed menyusul di TASK-12. Profil yang sudah ada tidak ditimpa kecuali `force=True`
(agar penyetelan prompt/budget oleh admin tidak hilang).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_marketing.core.config import get_settings
from mcp_marketing.models import AgentProfile, ApiClient, Schedule
from mcp_marketing.models.browser import BrowserProfile
from mcp_marketing.services.agent.profiles import DEFAULT_PROFILES
from mcp_marketing.services.clients import SYSTEM_DASHBOARD, SYSTEM_KEY_HASH, SYSTEM_KEY_PREFIX
from mcp_marketing.services.templates.library import seed_templates

DEFAULT_SCHEDULES: tuple[dict[str, Any], ...] = (
    {"name": "cache_cleanup", "cron": "0 * * * *", "job_kind": "cache_cleanup"},
    {"name": "retention", "cron": "15 2 * * *", "job_kind": "retention"},
    {"name": "usage_rollup", "cron": "*/10 * * * *", "job_kind": "usage_rollup"},
)


async def upgrade_profiles(session: AsyncSession) -> int:
    """MCP-V1-03: tambahkan toolset & denylist bawaan v1 ke profil lama SECARA ADITIF (prompt,
    route, budget & toolset tambahan admin tidak disentuh). Idempoten."""
    changed = 0
    for p in DEFAULT_PROFILES:
        row = await session.get(AgentProfile, p.agent_key)
        if row is None:
            continue
        toolsets = list(dict.fromkeys([*(row.toolsets or []), *p.toolsets]))
        denylist = list(dict.fromkeys([*(row.tool_denylist or []), *p.tool_denylist]))
        if toolsets != list(row.toolsets or []) or denylist != list(row.tool_denylist or []):
            row.toolsets, row.tool_denylist = toolsets, denylist
            row.version = row.version + 1
            row.updated_at = func.now()
            changed += 1
    return changed


async def seed(
    session: AsyncSession, *, force: bool = False, upgrade: bool = False
) -> dict[str, int]:
    counts = {
        "agent_profiles": 0,
        "agent_profiles_upgraded": 0,
        "clients": 0,
        "schedules": 0,
        "templates": 0,
        "browser_profiles": 0,
    }
    for p in DEFAULT_PROFILES:
        values = {
            "agent_key": p.agent_key,
            "display_name": p.display_name,
            "system_prompt": p.system_prompt(),
            "route": p.route,
            "toolsets": list(p.toolsets),
            "tool_denylist": list(p.tool_denylist),
            "run_token_budget": p.run_token_budget,
            "max_steps": p.max_steps,
        }
        stmt = insert(AgentProfile).values(**values)
        if force:
            update_cols = {k: v for k, v in values.items() if k != "agent_key"}
            stmt = stmt.on_conflict_do_update(
                index_elements=["agent_key"],
                set_={**update_cols, "version": AgentProfile.version + 1, "updated_at": func.now()},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=["agent_key"])
        counts["agent_profiles"] += (await session.execute(stmt)).rowcount or 0  # type: ignore[attr-defined]
    if upgrade and not force:
        counts["agent_profiles_upgraded"] = await upgrade_profiles(session)

    client_stmt = (
        insert(ApiClient)
        .values(
            name=SYSTEM_DASHBOARD,
            kind="system",
            key_prefix=SYSTEM_KEY_PREFIX,
            key_hash=SYSTEM_KEY_HASH,
            toolsets=["pm"],
            rpm_limit=600,
        )
        .on_conflict_do_nothing(index_elements=["name"])
    )
    counts["clients"] += (await session.execute(client_stmt)).rowcount or 0  # type: ignore[attr-defined]

    for s in DEFAULT_SCHEDULES:
        sched = insert(Schedule).values(**s).on_conflict_do_nothing(index_elements=["name"])
        counts["schedules"] += (await session.execute(sched)).rowcount or 0  # type: ignore[attr-defined]
    counts["templates"] = await seed_templates(session)
    # profil Chrome "default" (tanpa login, headless) untuk scraping umum (TASK-14)
    profiles_dir = get_settings().browser.browser_profiles_dir
    profile = (
        insert(BrowserProfile)
        .values(
            name="default",
            purpose="Scraping umum tanpa login",
            user_data_dir=str(profiles_dir / "default"),
            headless=True,
        )
        .on_conflict_do_nothing(index_elements=["name"])
    )
    counts["browser_profiles"] += (await session.execute(profile)).rowcount or 0  # type: ignore[attr-defined]
    return counts
