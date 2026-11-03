"""CLI operasional MCP Marketing.

python scripts/manage.py migrate [--target head]
python scripts/manage.py seed [--force]
python scripts/manage.py db-check
python scripts/manage.py create-client --name claude-desktop --toolsets toolbox,data,job,trends
python scripts/manage.py list-clients
python scripts/manage.py revoke-client <id|name>
python scripts/manage.py rotate-client <id|name>
python scripts/manage.py job-retry <job_id>
python scripts/manage.py profile-add riset-x --sites x.com,threads.net
python scripts/manage.py profile-open riset-x --site x.com      # login manual
python scripts/manage.py profile-check riset-x                  # butuh worker berjalan
python scripts/manage.py profile-list [--all]
python scripts/manage.py profile-disable <name>
python scripts/manage.py threads-token-refresh                  # tulis token baru ke .env
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


# ------------------------------------------------------------------ profil browser (TASK-14)
CLI_LOCK_ID = "cli-profile"
PROFILE_CHECK_WAIT_S = 180


def _profile_store(db: Database):  # type: ignore[no-untyped-def]
    from mcp_marketing.services.browser.profiles import ProfileStore

    return ProfileStore(db, get_settings().browser.browser_profiles_dir)


async def _with_profiles(fn):  # type: ignore[no-untyped-def]
    db = Database(get_settings().db.database_url, pool_size=2)
    try:
        return await fn(_profile_store(db))
    finally:
        await db.dispose()


def cmd_profile_add(args: argparse.Namespace) -> int:
    sites = [s.strip() for s in (args.sites or "").split(",") if s.strip()]
    profile = asyncio.run(
        _with_profiles(
            lambda st: st.add(
                name=args.name,
                purpose=args.purpose,
                sites=sites,
                headless=args.headless,
                user_agent=args.user_agent,
                proxy_env=args.proxy_env,
            )
        )
    )
    print(f"Profil {profile.name} dibuat: {profile.user_data_dir}")
    print(f"Langkah berikut: python scripts/manage.py profile-open {profile.name} --site x.com")
    return 0


def cmd_profile_list(args: argparse.Namespace) -> int:
    from mcp_marketing.services.browser.profiles import profile_view

    rows = asyncio.run(_with_profiles(lambda st: st.list(include_inactive=args.all)))
    for p in rows:
        v = profile_view(p)
        print(
            f"{v['name']:<20} aktif={v['is_active']} headless={v['headless']} "
            f"situs={','.join(v['allowed_sites']) or '-'} "
            f"login={','.join(v['logged_in_sites']) or '-'} terkunci={v['locked']}"
        )
    return 0


def cmd_profile_disable(args: argparse.Namespace) -> int:
    asyncio.run(_with_profiles(lambda st: st.disable(args.name)))
    print(f"Profil {args.name} dinonaktifkan (folder profil tidak dihapus)")
    return 0


async def _profile_open(name: str, urls: list[str]) -> None:
    from mcp_marketing.services.browser.driver_pool import (
        create_driver,
        kill_driver,
        require_compatible,
    )

    settings = get_settings()
    require_compatible(settings)
    db = Database(settings.db.database_url, pool_size=2)
    store = _profile_store(db)
    try:
        profile = await store.acquire(name, CLI_LOCK_ID, wait_s=0)
        profile.headless = False  # login manual selalu dengan jendela terlihat
        driver = await asyncio.to_thread(create_driver, profile, settings)
        try:
            for i, url in enumerate(urls):
                if i:
                    driver.switch_to.new_window("tab")
                driver.get(url)
            await asyncio.to_thread(
                input, "Login manual di jendela Chrome, lalu tekan Enter di sini untuk menutup... "
            )
        finally:
            await asyncio.to_thread(kill_driver, driver)
    finally:
        await store.release(name, CLI_LOCK_ID)
        await db.dispose()


def cmd_profile_open(args: argparse.Namespace) -> int:
    from mcp_marketing.worker.handlers.web import SITE_ALIASES, SITE_CHECKS

    sites = [SITE_ALIASES.get(s, s) for s in args.site or []]
    unknown = [s for s in sites if s not in SITE_CHECKS]
    if unknown:
        print(f"Situs tidak dikenal: {unknown}. Pilihan: {sorted(SITE_CHECKS)}", file=sys.stderr)
        return 2
    urls = [SITE_CHECKS[s]["url"] for s in sites] or [args.url or "about:blank"]
    asyncio.run(_profile_open(args.name, urls))
    print(f"Sesi ditutup. Verifikasi: python scripts/manage.py profile-check {args.name}")
    return 0


async def _profile_check(name: str, sites: list[str]) -> int:
    from mcp_marketing.models import Job
    from mcp_marketing.worker.queue import JobQueue

    db = Database(get_settings().db.database_url, pool_size=2)
    try:
        queue = JobQueue(db)
        params: dict[str, object] = {"profile": name}
        if sites:
            params["sites"] = sites
        accepted = await queue.enqueue(
            "browser_profile_check",
            params,
            client_id=None,
            dedupe_key=f"cli-check-{uuid.uuid4()}",  # selalu cek ulang, jangan pakai job lama
        )
        job_id = accepted.job_id
        print(f"Job {job_id} diantrekan; menunggu worker (maks {PROFILE_CHECK_WAIT_S} s)...")
        for _ in range(PROFILE_CHECK_WAIT_S):
            async with db.session() as session:
                job = await session.get(Job, job_id)
            if job is not None and job.status in ("succeeded", "failed", "cancelled"):
                if job.status != "succeeded":
                    print(f"Job {job.status}: {job.error}")
                    return 1
                for c in (job.result or {}).get("checks", []):
                    print(f"{c['site']:<20} login={c['logged_in']}  ({c['detail']})")
                return 0
            await asyncio.sleep(1)
        print("Worker belum memproses job; pastikan scripts\\start-worker.bat berjalan.")
        return 1
    finally:
        await db.dispose()


def cmd_profile_check(args: argparse.Namespace) -> int:
    return asyncio.run(_profile_check(args.name, args.site or []))


# ------------------------------------------------------------------ token Threads (TASK-16)
def _replace_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out = [ln for ln in lines if not ln.startswith(f"{key}=")]
    out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def cmd_threads_token_refresh(args: argparse.Namespace) -> int:
    import httpx

    from mcp_marketing.services.collectors.threads_api import refresh_long_lived_token

    token = get_settings().social.threads_access_token
    if token is None:
        print("THREADS_ACCESS_TOKEN belum diisi di .env", file=sys.stderr)
        return 2

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient() as http:
            return await refresh_long_lived_token(http, token.get_secret_value())

    body = asyncio.run(run())
    _replace_env_value(ROOT / ".env", "THREADS_ACCESS_TOKEN", str(body["access_token"]))
    days = int(body.get("expires_in") or 0) // 86_400  # type: ignore[call-overload]
    print(f"Token Threads diperbarui di .env (berlaku ~{days} hari). Restart server & worker.")
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

    p = sub.add_parser("profile-add", help="daftarkan profil Chrome baru")
    p.add_argument("name")
    p.add_argument("--purpose")
    p.add_argument("--sites", help="dipisah koma, mis. x.com,threads.net")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--user-agent")
    p.add_argument("--proxy-env", help="NAMA env var berisi URL proxy")
    p.set_defaults(fn=cmd_profile_add)

    p = sub.add_parser("profile-list")
    p.add_argument("--all", action="store_true", help="termasuk profil nonaktif")
    p.set_defaults(fn=cmd_profile_list)

    p = sub.add_parser("profile-disable")
    p.add_argument("name")
    p.set_defaults(fn=cmd_profile_disable)

    p = sub.add_parser("profile-open", help="buka Chrome dengan profil untuk login manual")
    p.add_argument("name")
    p.add_argument("--site", action="append", help="x.com / threads.net / trends (boleh berulang)")
    p.add_argument("--url", help="URL lain bila --site tidak dipakai")
    p.set_defaults(fn=cmd_profile_open)

    p = sub.add_parser("profile-check", help="cek status login lewat worker")
    p.add_argument("name")
    p.add_argument("--site", action="append")
    p.set_defaults(fn=cmd_profile_check)

    sub.add_parser("threads-token-refresh").set_defaults(fn=cmd_threads_token_refresh)

    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
