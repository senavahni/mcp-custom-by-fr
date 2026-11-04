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
python scripts/manage.py download-models                        # model ONNX lokal (embedding)
python scripts/manage.py kb-reembed <collection_id>             # koleksi baru, model saat ini
python scripts/manage.py gateway-keys gen-secret                # nilai MCP_SECRET_KEY baru
python scripts/manage.py gateway-keys provision [--daily-usd 5]  # project + key per agent
python scripts/manage.py gateway-keys list | import --agent X | disable --agent X
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
    if not asyncio.run(_vector_installed(url)):
        print(
            "CATATAN: pgvector belum ada -> KB memakai mode cadangan (embedding real[], tanpa HNSW)"
        )
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, target)
    if asyncio.run(_promote_vector(url)):
        print("KB dipromosikan ke pgvector (kolom vector + indeks HNSW)")
    print(f"migrate OK -> {target}")
    return 0


async def _promote_vector(url: str) -> bool:
    from mcp_marketing.services.kb.vector import promote_to_pgvector

    db = Database(url, pool_size=1)
    try:
        async with db.engine.begin() as conn:
            exists = (await conn.execute(text("SELECT to_regclass('mcp.kb_chunks')"))).scalar()
            return bool(exists) and await promote_to_pgvector(conn)
    finally:
        await db.dispose()


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


# ------------------------------------------------------------------ model & KB (TASK-19/20)
def cmd_download_models(args: argparse.Namespace) -> int:
    from huggingface_hub import hf_hub_download

    from mcp_marketing.services.kb.embedder import LOCAL_FILES, LOCAL_REPO

    nlp = get_settings().nlp
    if not args.skip_embed:
        target = nlp.local_embed_model_dir
        target.mkdir(parents=True, exist_ok=True)
        for filename in LOCAL_FILES:
            path = hf_hub_download(repo_id=LOCAL_REPO, filename=filename, local_dir=str(target))
            print(f"OK {LOCAL_REPO}/{filename} -> {path}")
        print("Set EMBED_PROVIDER=local untuk koleksi baru (koleksi lama tetap pada modelnya).")
    if args.sentiment_repo:
        return _download_sentiment(args.sentiment_repo, nlp.sentiment_model_dir)
    return 0


def _download_sentiment(repo: str, target: Path) -> int:
    """Model sentimen 3 kelas yang SUDAH diekspor ke ONNX (lihat docs/eval/sentiment.md)."""
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import EntryNotFoundError

    from mcp_marketing.services.nlp.sentiment_model import MODEL_FILES, model_available

    target.mkdir(parents=True, exist_ok=True)
    for filename in ("tokenizer.json", "config.json"):
        hf_hub_download(repo_id=repo, filename=filename, local_dir=str(target))
    for filename in reversed(MODEL_FILES):  # utamakan versi terkuantisasi
        try:
            hf_hub_download(repo_id=repo, filename=filename, local_dir=str(target))
            break
        except EntryNotFoundError:
            continue
    if not model_available(target):
        print(f"GAGAL: {repo} tidak berisi model ONNX ({', '.join(MODEL_FILES)})", file=sys.stderr)
        return 1
    print(f"OK model sentimen {repo} -> {target}")
    return 0


async def _kb_reembed(collection_id: str) -> tuple[str, int]:
    from mcp_marketing.main import build_services, close_services
    from mcp_marketing.models.kb import KbCollection
    from mcp_marketing.services.kb.embedder import embedder_for
    from mcp_marketing.services.kb.identity import embed_identity
    from mcp_marketing.services.kb.store import get_collection, reembed_collection
    from mcp_marketing.utils.time import now

    settings = get_settings()
    services = build_services(settings)
    try:
        source = await get_collection(services.db, collection_id)
        model, dim = embed_identity(settings)
        async with services.db.session() as session:
            target = KbCollection(
                name=f"{source.name}@{now():%Y%m%d%H%M}"[:150],
                description=source.description,
                workspace_id=source.workspace_id,
                project_id=source.project_id,
                embed_model=model,
                embed_dim=dim,
            )
            session.add(target)
            await session.flush()
        chunks = await reembed_collection(
            services.db, source, target, embedder_for(services, model, dim)
        )
        return str(target.id), chunks
    finally:
        await close_services(services)


def cmd_kb_reembed(args: argparse.Namespace) -> int:
    new_id, chunks = asyncio.run(_kb_reembed(args.collection_id))
    print(f"Koleksi baru {new_id}: {chunks} chunk di-embed ulang dengan model saat ini.")
    print("Hapus koleksi lama dengan kb_delete setelah hasil pencarian diverifikasi.")
    return 0


def _secret_key_or_exit() -> str:
    key = get_settings().mcp_secret_key
    if key is None:
        print(
            "MCP_SECRET_KEY belum diisi. Buat: manage.py gateway-keys gen-secret", file=sys.stderr
        )
        raise SystemExit(2)
    return key.get_secret_value()


async def _gateway_provision(args: argparse.Namespace, admin_key: str) -> list[dict[str, str]]:
    import httpx

    from mcp_marketing.services.llm.gateway_admin import GatewayAdmin, admin_base_url, provision

    s = get_settings()
    db = Database(s.db.database_url, pool_size=2)
    try:
        async with httpx.AsyncClient() as http:
            admin = GatewayAdmin(
                http, args.admin_url or admin_base_url(s.llm.llm_gateway_url), admin_key
            )
            return await provision(
                db,
                _secret_key_or_exit(),
                admin,
                agents=[a.strip() for a in args.agents.split(",") if a.strip()],
                project_slug=args.project,
                rpm=args.rpm,
                force=args.force,
                daily_usd=args.daily_usd,
                monthly_usd=args.monthly_usd,
            )
    finally:
        await db.dispose()


def cmd_gateway_keys(args: argparse.Namespace) -> int:
    """Key LLM Gateway per agent (M3_TASK_009). Admin key Gateway TIDAK disimpan."""
    import getpass
    import os

    from mcp_marketing.services.llm.credentials import list_credentials, save_credential
    from mcp_marketing.utils.secretbox import generate_key

    if args.action == "gen-secret":
        print("Isi .env MCP: MCP_SECRET_KEY=" + generate_key())
        return 0
    if args.action == "list":
        for c in asyncio.run(_with_db(list_credentials)):
            print(
                f"{c.name:<24} {c.key_prefix:<14} {c.status:<8} "
                f"rotated={c.rotated_at or '-'} error={c.last_error or '-'}"
            )
        return 0
    secret = _secret_key_or_exit()
    if args.action == "provision":
        admin_key = os.environ.get("LLM_GATEWAY_ADMIN_KEY") or getpass.getpass(
            "Admin key LLM Gateway (lgw_..., tidak disimpan): "
        )
        for row in asyncio.run(_gateway_provision(args, admin_key.strip())):
            print(f"{row['name']:<24} {row['prefix']:<14} {row['action']}")
        print("Restart server & worker MCP agar key baru terbaca (cache 60 s).")
        return 0
    agent = None if args.agent in (None, "system") else args.agent
    if args.action == "import":
        full_key = getpass.getpass(f"Key LLM Gateway untuk {agent or 'system'} (lgw_...): ")

        async def save(session):  # type: ignore[no-untyped-def]
            row = await save_credential(
                session, secret, agent_key=agent, full_key=full_key, gateway_key_id=None
            )
            return row.key_prefix

        print("Tersimpan:", asyncio.run(_with_db(save)))
        return 0
    if args.action == "disable":

        async def disable(session):  # type: ignore[no-untyped-def]
            from sqlalchemy import update

            from mcp_marketing.models import LlmCredential
            from mcp_marketing.services.llm.credentials import credential_name

            res = await session.execute(
                update(LlmCredential)
                .where(LlmCredential.name == credential_name(agent))
                .values(status="revoked", last_error="dinonaktifkan lokal")
            )
            return res.rowcount

        print("Dinonaktifkan:", asyncio.run(_with_db(disable)), "baris (agent kembali ke fallback)")
        return 0
    return 2


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

    p = sub.add_parser("download-models", help="unduh model ONNX lokal")
    p.add_argument("--sentiment-repo", help="repo HF model sentimen 3 kelas berformat ONNX")
    p.add_argument("--skip-embed", action="store_true", help="lewati model embedding lokal")
    p.set_defaults(fn=cmd_download_models)
    p = sub.add_parser("kb-reembed", help="embed ulang koleksi ke model saat ini")
    p.add_argument("collection_id")
    p.set_defaults(fn=cmd_kb_reembed)

    p = sub.add_parser("gateway-keys", help="key LLM Gateway per agent (terenkripsi)")
    p.add_argument("action", choices=["gen-secret", "provision", "list", "import", "disable"])
    p.add_argument("--agents", default="researcher,analyst,strategist")
    p.add_argument("--agent", help="untuk import/disable: agent_key atau 'system'")
    p.add_argument("--project", default="marketing-mcp", help="slug project di Gateway")
    p.add_argument("--admin-url", help="default: LLM_GATEWAY_URL tanpa /v1")
    p.add_argument("--rpm", type=int, help="rpm_limit per key")
    p.add_argument("--daily-usd", type=float, help="kuota biaya harian project")
    p.add_argument("--monthly-usd", type=float, help="kuota biaya bulanan project")
    p.add_argument("--force", action="store_true", help="buat ulang semua key (key lama dicabut)")
    p.set_defaults(fn=cmd_gateway_keys)

    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
