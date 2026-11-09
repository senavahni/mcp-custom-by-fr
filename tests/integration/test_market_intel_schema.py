"""MCP-V1-02: migrasi 0005_market_intel (naik/turun, seed referensi, constraint unik & PDP)."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from alembic import command

from mcp_marketing.core.database import Database
from tests.integration.conftest import admin_test_url, alembic_config, owner_url

WS = uuid.UUID("00000000-0000-4000-8000-000000000001")


async def _conn() -> asyncpg.Connection:
    return await asyncpg.connect(admin_test_url())


async def _company(conn: asyncpg.Connection, name: str, domain: str | None) -> uuid.UUID:
    return await conn.fetchval(  # type: ignore[no-any-return]
        "INSERT INTO mcp.companies (workspace_id, name, primary_domain, origin, kbli_code) "
        "VALUES ($1, $2, $3, 'import', '85') RETURNING id",
        WS,
        name,
        domain,
    )


async def test_reference_seed_complete(db: Database) -> None:
    conn = await _conn()
    try:
        provinces = await conn.fetchval("SELECT count(*) FROM mcp.regions WHERE level='province'")
        regencies = await conn.fetchval(
            "SELECT count(*) FROM mcp.regions WHERE level='regency_city'"
        )
        subclasses = await conn.fetchval(
            "SELECT count(*) FROM mcp.industries WHERE level='subclass'"
        )
        unmapped = await conn.fetchval(
            "SELECT count(*) FROM mcp.industries i WHERE NOT EXISTS "
            "(SELECT 1 FROM mcp.industry_isic_map m WHERE m.kbli_code = i.kbli_code)"
        )
        alias_hit = await conn.fetchval(
            "SELECT code FROM mcp.regions WHERE 'Jakarta Selatan' = ANY(aliases)"
        )
    finally:
        await conn.close()
    assert (provinces, regencies, subclasses, unmapped) == (38, 514, 1790, 0)
    assert alias_hit == "31.74"


async def test_reference_load_is_idempotent(db: Database) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from mcp_marketing.services.market_intel.reference_data import load_reference_data

    engine = create_async_engine(owner_url())
    try:
        async with engine.begin() as conn:  # dijalankan ulang di atas seed migrasi
            counts = await conn.run_sync(load_reference_data)
    finally:
        await engine.dispose()
    conn2 = await _conn()
    try:
        total = await conn2.fetchval("SELECT count(*) FROM mcp.regions")
    finally:
        await conn2.close()
    assert counts.regions == total == 552


async def test_unique_domain_and_contact_constraints(clean_db: Database) -> None:
    conn = await _conn()
    try:
        first = await _company(conn, "SMAN 3 Bandung", "sman3-bdg.sch.id")
        with pytest.raises(asyncpg.UniqueViolationError):
            await _company(conn, "SMA Negeri 3 Bandung", "sman3-bdg.sch.id")
        # instansi tanpa website boleh lebih dari satu
        await _company(conn, "Dinas A", None)
        await _company(conn, "Dinas B", None)
        insert_contact = (
            "INSERT INTO mcp.contacts (company_id, workspace_id, kind, value_normalized, "
            "contact_type, review_status) VALUES ($1, $2, 'email', $3, $4, $5)"
        )
        await conn.execute(
            insert_contact, first, WS, "info@sman3.sch.id", "generic", "not_required"
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                insert_contact, first, WS, "info@sman3.sch.id", "generic", "not_required"
            )
        # kontak personal tidak boleh lolos tanpa review (V1-K6)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                insert_contact, first, WS, "budi@sman3.sch.id", "personal", "not_required"
            )
        await conn.execute(insert_contact, first, WS, "budi@sman3.sch.id", "personal", "pending")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO mcp.company_locations (company_id, workspace_id, region_code) "
                "VALUES ($1, $2, '99.99')",
                first,
                WS,
            )
    finally:
        await conn.close()


async def test_merged_company_releases_domain(clean_db: Database) -> None:
    conn = await _conn()
    try:
        keep = await _company(conn, "PT Contoh", None)
        dup = await _company(conn, "PT Contoh Tbk", "contoh.co.id")
        await conn.execute(
            "UPDATE mcp.companies SET status='merged', merged_into=$1 WHERE id=$2", keep, dup
        )
        await conn.execute(
            "UPDATE mcp.companies SET primary_domain='contoh.co.id' WHERE id=$1", keep
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE mcp.companies SET status='merged' WHERE id=$1", keep)
    finally:
        await conn.close()


async def test_downgrade_upgrade_0005_clean(db: Database) -> None:
    cfg = alembic_config(owner_url())
    await asyncio.to_thread(command.downgrade, cfg, "0004_llm_credentials")
    conn = await _conn()
    try:
        left = await conn.fetchval("SELECT to_regclass('mcp.companies')")
    finally:
        await conn.close()
    await asyncio.to_thread(command.upgrade, cfg, "head")
    conn = await _conn()
    try:
        regions = await conn.fetchval("SELECT count(*) FROM mcp.regions")
        emb_type = await conn.fetchval(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'mcp.industries'::regclass AND attname = 'embedding'"
        )
        has_vector = await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname='vector'")
    finally:
        await conn.close()
    assert left is None
    assert regions == 552
    assert emb_type == ("vector(1024)" if has_vector else "real[]")
