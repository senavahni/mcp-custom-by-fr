"""Test integrasi PostgreSQL.

Butuh `TEST_ADMIN_DATABASE_URL` (superuser, mis. postgresql://postgres:pw@127.0.0.1:5432/postgres).
Sekali per sesi: database `marketing_dashboard_test` dibuat ulang, skema Prisma Dashboard diterapkan
(dari repo `marketingdashboardjtp` bila ada, selain itu fixture minimal), lalu `bootstrap.sql`
dengan role khusus test (`mcp_owner_test`/`mcp_app_test`) dan migrasi Alembic.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import asyncpg
import httpx
import httpx2
import pytest
import pytest_asyncio
import uvicorn
from alembic import command
from alembic.config import Config
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_marketing.core.config import PROJECT_ROOT, get_settings
from mcp_marketing.core.database import Database
from mcp_marketing.server.app import build_asgi_app
from mcp_marketing.server.auth import Authenticator, ClientIdentity
from mcp_marketing.server.pipeline import ToolPipeline
from mcp_marketing.server.services import Services
from mcp_marketing.services.agent.budget import BudgetManager
from mcp_marketing.services.clients import create_client
from mcp_marketing.services.results.cache import ResultCache
from mcp_marketing.services.results.result_store import ResultStore

TEST_DB = "marketing_dashboard_test"
OWNER, OWNER_PW = "mcp_owner_test", "mcp_owner_test_pw"
APP, APP_PW = "mcp_app_test", "mcp_app_test_pw"
DASHBOARD_MIGRATIONS = (
    PROJECT_ROOT.parent / "marketingdashboardjtp" / "backend" / "prisma" / "migrations"
)
PSQL_DEFAULT = Path(r"D:\Data.Fahrul\Documents\PortableDev\postgres\pgsql\bin\psql.exe")

ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
            if not ADMIN_URL:
                item.add_marker(pytest.mark.skip(reason="TEST_ADMIN_DATABASE_URL tidak diset"))


def _with(
    url: str,
    *,
    user: str | None = None,
    password: str | None = None,
    db: str | None = None,
    driver: str = "postgresql",
) -> str:
    parts = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 5432
    u = user or parts.username or "postgres"
    p = password if password is not None else (parts.password or "")
    netloc = f"{u}:{p}@{host}:{port}"
    path = f"/{db}" if db else parts.path
    return urlunsplit((driver, netloc, path, "", ""))


def owner_url() -> str:
    return _with(
        ADMIN_URL or "", user=OWNER, password=OWNER_PW, db=TEST_DB, driver="postgresql+asyncpg"
    )


def app_url() -> str:
    return _with(
        ADMIN_URL or "", user=APP, password=APP_PW, db=TEST_DB, driver="postgresql+asyncpg"
    )


def admin_test_url() -> str:
    return _with(ADMIN_URL or "", db=TEST_DB)


MINIMAL_PUBLIC_SCHEMA = """
CREATE TABLE users (id uuid PRIMARY KEY, email text, username text, display_name varchar(100),
  password_hash text, avatar_url text, is_active boolean DEFAULT true);
CREATE TABLE refresh_tokens (id uuid PRIMARY KEY, token_hash text);
CREATE TABLE workspaces (id uuid PRIMARY KEY, name varchar(100), timezone varchar(64), deleted_at timestamptz);
CREATE TABLE workspace_members (workspace_id uuid, user_id uuid, role text, PRIMARY KEY (workspace_id, user_id));
CREATE TABLE agents (id uuid PRIMARY KEY, workspace_id uuid, name varchar(100), handle text,
  mcp_agent_key varchar(100), is_active boolean, deleted_at timestamptz);
CREATE TABLE projects (id uuid PRIMARY KEY, workspace_id uuid, title varchar(200), description text,
  created_at timestamptz DEFAULT now(), deleted_at timestamptz);
CREATE TABLE tasks (id uuid PRIMARY KEY, project_id uuid, parent_task_id uuid, depth smallint, title varchar(500),
  status text, priority text, assignee_type text, start_date date, end_date date, is_milestone boolean,
  tags text[], agent_status text, completed_at timestamptz, deleted_at timestamptz);
CREATE TABLE document_blocks (id uuid PRIMARY KEY, project_id uuid, task_id uuid, parent_block_id uuid,
  block_type varchar(32), content_json jsonb, plain_text text, order_index int, version int,
  updated_at timestamptz, deleted_at timestamptz);
CREATE TABLE comments (id uuid PRIMARY KEY, project_id uuid, entity_type text, entity_id uuid,
  parent_comment_id uuid, author_type text, content text, created_at timestamptz, deleted_at timestamptz);
CREATE TABLE activity_logs (id uuid PRIMARY KEY, workspace_id uuid, project_id uuid, entity_type text,
  action varchar(64), actor_type text, created_at timestamptz);
CREATE TABLE agent_runs (id uuid PRIMARY KEY, workspace_id uuid, project_id uuid, task_id uuid, agent_id uuid,
  trigger text, status text, mcp_run_id varchar(200));
CREATE TABLE notifications (id uuid PRIMARY KEY);
"""


async def _apply_public_schema(conn: asyncpg.Connection) -> None:
    files = (
        sorted(DASHBOARD_MIGRATIONS.glob("*/migration.sql"))
        if DASHBOARD_MIGRATIONS.exists()
        else []
    )
    if not files:
        await conn.execute(MINIMAL_PUBLIC_SCHEMA)
        return
    for path in files:
        await conn.execute(path.read_text(encoding="utf-8"))


def _psql() -> str:
    found = shutil.which("psql")
    return found or str(PSQL_DEFAULT)


def _run_bootstrap() -> None:
    parts = urlsplit(admin_test_url())
    env = {**os.environ, "PGPASSWORD": unquote(parts.password or "")}
    cmd = [
        _psql(),
        "-h",
        parts.hostname or "127.0.0.1",
        "-p",
        str(parts.port or 5432),
        "-U",
        parts.username or "postgres",
        "-d",
        TEST_DB,
        "-q",
        "-v",
        f"owner_role={OWNER}",
        "-v",
        f"app_role={APP}",
        "-v",
        f"owner_pw={OWNER_PW}",
        "-v",
        f"app_pw={APP_PW}",
        "-f",
        str(PROJECT_ROOT / "scripts" / "sql" / "bootstrap.sql"),
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"bootstrap.sql gagal: {proc.stderr.strip()}")


def alembic_config(url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    return cfg


async def migrate_target() -> str:
    # 0002 berjalan juga tanpa pgvector (mode cadangan real[])
    return "head"


@pytest_asyncio.fixture(scope="session")
async def prepared_db() -> AsyncIterator[None]:
    admin = await asyncpg.connect(_with(ADMIN_URL or "", db="postgres"))
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1", TEST_DB
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        await admin.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await admin.close()
    conn = await asyncpg.connect(admin_test_url())
    try:
        await _apply_public_schema(conn)
    finally:
        await conn.close()
    await asyncio.to_thread(_run_bootstrap)
    target = await migrate_target()
    await asyncio.to_thread(command.upgrade, alembic_config(owner_url()), target)
    yield


@pytest_asyncio.fixture(scope="session")
async def db(prepared_db: None) -> AsyncIterator[Database]:
    database = Database(app_url(), pool_size=5)
    yield database
    await database.dispose()


@pytest_asyncio.fixture
async def services(clean_db: Database) -> AsyncIterator[Services]:
    settings = get_settings()
    http = httpx.AsyncClient()
    svc = Services(
        settings=settings,
        db=clean_db,
        http=http,
        results=ResultStore(clean_db, export_dir=settings.storage_dir / "exports"),
        cache=ResultCache(clean_db),
        budgets=BudgetManager(),
    )
    yield svc
    pipeline = svc.extras.get("pipeline")
    if pipeline is not None:
        await pipeline.drain()
    await http.aclose()


async def make_client(
    services: Services, *, name: str = "tester", toolsets: list[str] | None = None, **kw: Any
) -> tuple[str, ClientIdentity]:
    async with services.db.session() as session:
        created = await create_client(
            session,
            pepper=services.settings.mcp_key_pepper.get_secret_value(),
            name=name,
            toolsets=toolsets or ["toolbox", "data", "job"],
            **kw,
        )
        identity = ClientIdentity.from_row(created.client)
    return created.key, identity


@dataclass
class LiveServer:
    url: str
    pipeline: ToolPipeline
    authenticator: Authenticator


@pytest_asyncio.fixture
async def live_server(services: Services) -> AsyncIterator[LiveServer]:
    app, pipeline, authenticator = build_asgi_app(services)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    yield LiveServer(f"http://127.0.0.1:{port}", pipeline, authenticator)
    server.should_exit = True
    await task


@contextlib.asynccontextmanager
async def mcp_session(
    base_url: str, key: str, *, path: str = "/mcp", mode: str = "legacy"
) -> AsyncIterator[Client]:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=30)
    async with Client(
        streamable_http_client(base_url + path, http_client=http), mode=mode
    ) as client:  # type: ignore[arg-type]
        yield client
    await http.aclose()


# data referensi yang di-seed migrasi 0005 (wilayah, KBLI, ISIC) tidak boleh dikosongkan
REFERENCE_TABLES = ("alembic_version", "regions", "industries", "industry_isic_map")


@pytest_asyncio.fixture
async def clean_db(db: Database) -> Database:
    """Kosongkan tabel mcp (kecuali seed referensi) sebelum test."""
    conn = await asyncpg.connect(_with(ADMIN_URL or "", user=OWNER, password=OWNER_PW, db=TEST_DB))
    try:
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'mcp' "
            "AND tablename <> ALL($1::text[])",
            list(REFERENCE_TABLES),
        )
        names = ", ".join(f"mcp.{r['tablename']}" for r in tables)
        await conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    finally:
        await conn.close()
    return db
