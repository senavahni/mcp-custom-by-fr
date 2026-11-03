"""CLI operasional MCP Marketing.

python scripts/manage.py migrate [--target head]
python scripts/manage.py seed [--force]
python scripts/manage.py db-check
python scripts/manage.py create-client --name claude-desktop --toolsets toolbox,data,job,trends
python scripts/manage.py list-clients
python scripts/manage.py revoke-client <id|name>
python scripts/manage.py rotate-client <id|name>
python scripts/manage.py job-retry <job_id>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402
from sqlalchemy import text  # noqa: E402

from mcp_marketing.core.config import get_settings  # noqa: E402
from mcp_marketing.core.database import Database, asyncpg_dsn  # noqa: E402


async def _vector_installed(url: str) -> bool:
    conn = await asyncpg.connect(asyncpg_dsn(url))
    try:
        return bool(await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    finally:
        await conn.close()


def cmd_migrate(args: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    url = get_settings().db.migration_database_url
    target = args.target
    if target == "head" and not asyncio.run(_vector_installed(url)):
        print("PERINGATAN: pgvector belum ada -> migrasi sampai 0001_init (lewati 0002_kb_vector)")
        target = "0001_init"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, target)
    print(f"migrate OK -> {target}")
    return 0


async def _with_db(fn):  # type: ignore[no-untyped-def]
    s = get_settings()
    db = Database(s.db.database_url, pool_size=2)
    try:
        async with db.session() as session:
            return await fn(session)
    finally:
        await db.dispose()


def cmd_seed(args: argparse.Namespace) -> int:
    from mcp_marketing.services.seed import seed

    counts = asyncio.run(_with_db(lambda s: seed(s, force=args.force)))
    print("seed OK (baris baru):", counts)
    return 0


async def _db_check() -> int:
    s = get_settings()
    db = Database(s.db.database_url, pool_size=1)
    try:
        async with db.engine.connect() as conn:
            version = (await conn.execute(text("SHOW server_version"))).scalar()
            vector = (
                await conn.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname='vector'")
                )
            ).scalar()
            revision = (
                await conn.execute(text("SELECT version_num FROM mcp.alembic_version"))
            ).scalar()
            checks = {
                "users.email (harus False)": (
                    "SELECT has_column_privilege('public.users','email','SELECT')"
                ),
                "users.display_name (harus True)": (
                    "SELECT has_column_privilege('public.users','display_name','SELECT')"
                ),
                "INSERT public.tasks (harus False)": (
                    "SELECT has_table_privilege('public.tasks','INSERT')"
                ),
                "INSERT mcp.jobs (harus True)": "SELECT has_table_privilege('mcp.jobs','INSERT')",
            }
            print(f"PostgreSQL : {version}")
            print(f"pgvector   : {vector or 'TIDAK TERPASANG (KB/vector belum aktif)'}")
            print(f"Alembic    : {revision}")
            for label, sql in checks.items():
                print(f"grant {label}: {(await conn.execute(text(sql))).scalar()}")
    finally:
        await db.dispose()
    return 0


def cmd_db_check(args: argparse.Namespace) -> int:
    return asyncio.run(_db_check())


def cmd_create_client(args: argparse.Namespace) -> int:
    from mcp_marketing.services.clients import client_summary, create_client

    s = get_settings()
    toolsets = [t.strip() for t in (args.toolsets or s.default_toolsets).split(",") if t.strip()]

    async def run(session):  # type: ignore[no-untyped-def]
        created = await create_client(
            session,
            pepper=s.mcp_key_pepper.get_secret_value(),
            name=args.name,
            toolsets=toolsets,
            kind=args.kind,
            is_admin=args.admin,
            workspace_id=uuid.UUID(args.workspace_id) if args.workspace_id else None,
            dashboard_agent_id=uuid.UUID(args.dashboard_agent_id)
            if args.dashboard_agent_id
            else None,
            rpm_limit=args.rpm,
            daily_token_budget=args.daily_token_budget,
            expires_at=datetime.fromisoformat(args.expires_at) if args.expires_at else None,
        )
        return created.key, client_summary(created.client)

    key, summary = asyncio.run(_with_db(run))
    print("Klien dibuat:", summary)
    print("API KEY (tampil SEKALI, simpan sekarang):")
    print(key)
    return 0


def cmd_list_clients(args: argparse.Namespace) -> int:
    from mcp_marketing.services.clients import client_summary, list_clients

    rows = asyncio.run(_with_db(lambda s: list_clients(s)))
    for c in rows:
        d = client_summary(c)
        print(
            f"{d['name']:<24} {d['kind']:<9} {d['status']:<8} prefix={d['key_prefix']} "
            f"toolsets={','.join(d['toolsets'])} admin={d['is_admin']}"
        )
    return 0


def cmd_revoke_client(args: argparse.Namespace) -> int:
    from mcp_marketing.services.clients import revoke_client

    c = asyncio.run(_with_db(lambda s: revoke_client(s, args.client)))
    print(f"Klien {c.name} dicabut (efektif <= 60 detik karena cache auth)")
    return 0


def cmd_rotate_client(args: argparse.Namespace) -> int:
    from mcp_marketing.services.clients import rotate_client

    pepper = get_settings().mcp_key_pepper.get_secret_value()
    created = asyncio.run(_with_db(lambda s: rotate_client(s, args.client, pepper=pepper)))
    print(f"Key klien {created.client.name} dirotasi. API KEY baru (tampil SEKALI):")
    print(created.key)
    return 0


def cmd_job_retry(args: argparse.Namespace) -> int:
    from mcp_marketing.worker.queue import retry_job

    job = asyncio.run(_with_db(lambda s: retry_job(s, args.job_id)))
    print(f"Job {job} dikembalikan ke antrean")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CLI MCP Marketing")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("migrate")
    p.add_argument("--target", default="head")
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("seed")
    p.add_argument("--force", action="store_true", help="timpa profil agent yang sudah ada")
    p.set_defaults(fn=cmd_seed)

    sub.add_parser("db-check").set_defaults(fn=cmd_db_check)

    p = sub.add_parser("create-client")
    p.add_argument("--name", required=True)
    p.add_argument("--toolsets", help="dipisah koma (default DEFAULT_TOOLSETS)")
    p.add_argument("--kind", default="external", choices=["external", "agent"])
    p.add_argument("--admin", action="store_true")
    p.add_argument("--workspace-id")
    p.add_argument("--dashboard-agent-id")
    p.add_argument("--rpm", type=int, default=120)
    p.add_argument("--daily-token-budget", type=int)
    p.add_argument("--expires-at", help="ISO-8601")
    p.set_defaults(fn=cmd_create_client)

    sub.add_parser("list-clients").set_defaults(fn=cmd_list_clients)
    for name, fn in (("revoke-client", cmd_revoke_client), ("rotate-client", cmd_rotate_client)):
        p = sub.add_parser(name)
        p.add_argument("client", help="id atau nama klien")
        p.set_defaults(fn=fn)

    p = sub.add_parser("job-retry")
    p.add_argument("job_id")
    p.set_defaults(fn=cmd_job_retry)

    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
