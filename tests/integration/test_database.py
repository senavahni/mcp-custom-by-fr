"""TASK-03: migrasi, grant least-privilege, model read-only, seed idempoten."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from alembic import command
from sqlalchemy import func, select

from mcp_marketing.core.database import Database
from mcp_marketing.models import AgentProfile, ApiClient, Schedule
from mcp_marketing.models.dashboard import Task
from mcp_marketing.services.seed import seed
from tests.integration.conftest import (
    admin_test_url,
    alembic_config,
    app_url,
    migrate_target,
    owner_url,
)

PUBLIC_CHECKSUM_SQL = """
SELECT md5(string_agg(table_name || '.' || column_name || ':' || data_type, ',' ORDER BY table_name, column_name))
FROM information_schema.columns WHERE table_schema = 'public'
"""


async def _admin() -> asyncpg.Connection:
    return await asyncpg.connect(admin_test_url())


async def test_upgrade_downgrade_upgrade_keeps_public_untouched(db: Database) -> None:
    conn = await _admin()
    try:
        before = await conn.fetchval(PUBLIC_CHECKSUM_SQL)
        cfg = alembic_config(owner_url())
        target = await migrate_target()
        await asyncio.to_thread(command.downgrade, cfg, "base")
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM pg_tables WHERE schemaname='mcp' AND tablename <> 'alembic_version'"
            )
            == 0
        )
        await asyncio.to_thread(command.upgrade, cfg, target)
        # downgrade satu revisi (setara `downgrade -1`)
        previous = "base" if target == "0001_init" else "0001_init"
        await asyncio.to_thread(command.downgrade, cfg, previous)
        await asyncio.to_thread(command.upgrade, cfg, target)
        after = await conn.fetchval(PUBLIC_CHECKSUM_SQL)
        tables = await conn.fetchval("SELECT count(*) FROM pg_tables WHERE schemaname='mcp'")
    finally:
        await conn.close()
    assert before == after
    assert tables >= 28


async def _app_conn() -> asyncpg.Connection:
    return await asyncpg.connect(app_url().replace("postgresql+asyncpg://", "postgresql://"))


async def test_grants_least_privilege(db: Database) -> None:
    conn = await _app_conn()
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.fetch("SELECT email FROM public.users")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.fetch("SELECT password_hash FROM public.users")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM public.refresh_tokens")
        await conn.fetch("SELECT display_name FROM public.users")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("INSERT INTO public.tasks (id) VALUES (gen_random_uuid())")
        await conn.execute(
            "INSERT INTO mcp.schedules (name, cron, job_kind) VALUES ('grant-test', '* * * * *', 'x')"
        )
        await conn.execute("DELETE FROM mcp.schedules WHERE name = 'grant-test'")
    finally:
        await conn.close()


async def test_readonly_model_flush_rejected(db: Database) -> None:
    with pytest.raises(RuntimeError, match="read-only"):
        async with db.session() as session:
            session.add(
                Task(
                    id=uuid.uuid4(),
                    project_id=uuid.uuid4(),
                    depth=1,
                    title="x",
                    status="todo",
                    priority="none",
                    is_milestone=False,
                    tags=[],
                    agent_status="idle",
                )
            )
            await session.flush()


async def test_seed_idempotent(clean_db: Database) -> None:
    async with clean_db.session() as s:
        first = await seed(s)
    async with clean_db.session() as s:
        second = await seed(s)
    assert first == {
        "agent_profiles": 3,
        "browser_profiles": 1,
        "clients": 1,
        "schedules": 3,
        "templates": 8,
    }
    assert second == {
        "agent_profiles": 0,
        "browser_profiles": 0,
        "clients": 0,
        "schedules": 0,
        "templates": 0,
    }
    async with clean_db.session() as s:
        assert await s.scalar(select(func.count()).select_from(AgentProfile)) == 3
        assert await s.scalar(select(func.count()).select_from(Schedule)) == 3
        dash = await s.scalar(select(ApiClient).where(ApiClient.name == "dashboard"))
        assert dash is not None and dash.kind == "system" and dash.toolsets == ["pm"]


async def test_seed_force_updates_profiles(clean_db: Database) -> None:
    async with clean_db.session() as s:
        await seed(s)
    async with clean_db.session() as s:
        await seed(s, force=True)
    async with clean_db.session() as s:
        prof = await s.scalar(select(AgentProfile).where(AgentProfile.agent_key == "strategist"))
    assert prof is not None and prof.version == 2
    assert "finish" in prof.system_prompt


async def test_health_reports_vector_flag(db: Database) -> None:
    health = await db.health()
    assert health["db"] is True
    assert isinstance(health["vector"], bool)
