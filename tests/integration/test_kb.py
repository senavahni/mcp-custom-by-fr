"""TASK-19 end-to-end: koleksi, ingest idempoten, pencarian, sinkron Dashboard, isolasi workspace.

Embedding memakai `HashEmbedder` (deterministik, tanpa Gateway). DB test tanpa pgvector ->
mode cadangan (`real[]`, kosinus di aplikasi).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import asyncpg
import pytest_asyncio
from sqlalchemy import func, select, text

from mcp_marketing.models import Job
from mcp_marketing.models.browser import WebPage
from mcp_marketing.models.reports import AnalysisReport
from mcp_marketing.server.app import build_asgi_app
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.pipeline import ToolPipeline
from mcp_marketing.server.services import Services
from mcp_marketing.services.kb.sync import maybe_schedule_sync, sync_mark_key
from mcp_marketing.services.kb.vector import reset_mode_cache, vector_mode
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.worker.handlers import default_registry
from mcp_marketing.worker.queue import JobQueue
from mcp_marketing.worker.runner import Worker
from tests.fakes import HashEmbedder
from tests.integration.conftest import admin_test_url, make_client

BRIEF_BLOCKS = [
    ("heading", "Target Audiens", 1),
    ("paragraph", "Mahasiswa dan pekerja muda urban yang menyukai kopi gula aren murah.", 1),
    ("heading", "Kanal", 1),
    ("paragraph", "Fokus TikTok dan Instagram Reels dengan kreator mikro.", 1),
]


@dataclass
class Env:
    services: Services
    pipeline: ToolPipeline
    client: ClientIdentity
    embedder: HashEmbedder
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    task_ids: list[uuid.UUID]
    admin: asyncpg.Connection

    async def call(self, tool: str, args: dict[str, Any], client: Any = None) -> Any:
        return await self.pipeline.call(tool, args, client=client or self.client)


async def _seed_dashboard(conn: asyncpg.Connection) -> dict[str, Any]:
    ids = {k: uuid.uuid4() for k in ("user", "ws", "project", "t1", "t2")}
    await conn.execute(
        "INSERT INTO public.users (id, email, username, display_name, password_hash) "
        "VALUES ($1, $2, $3, 'KB', 'x')",
        ids["user"],
        f"kb{ids['user'].hex[:8]}@test.local",
        f"kb{ids['user'].hex[:8]}",
    )
    await conn.execute(
        "INSERT INTO public.workspaces (id, name, owner_id) VALUES ($1, 'WS KB', $2)",
        ids["ws"],
        ids["user"],
    )
    await conn.execute(
        "INSERT INTO public.projects (id, workspace_id, title) VALUES ($1, $2, 'Launch Kopi')",
        ids["project"],
        ids["ws"],
    )
    for tid, title in ((ids["t1"], "Riset kompetitor"), (ids["t2"], "Konten minggu 1")):
        await conn.execute(
            "INSERT INTO public.tasks (id, project_id, title) VALUES ($1, $2, $3)",
            tid,
            ids["project"],
            title,
        )
    for i, (kind, body, level) in enumerate(BRIEF_BLOCKS):
        await conn.execute(
            "INSERT INTO public.document_blocks (id, project_id, block_type, plain_text, "
            "content_json, order_index) VALUES ($1, $2, $3, $4, $5::jsonb, $6)",
            uuid.uuid4(),
            ids["project"],
            kind,
            body,
            f'{{"props": {{"level": {level}}}, "content": []}}',
            i,
        )
    await conn.execute(
        "INSERT INTO public.document_blocks (id, project_id, task_id, block_type, plain_text, "
        "content_json, order_index) VALUES ($1, $2, $3, 'paragraph', $4, '{}'::jsonb, 0)",
        uuid.uuid4(),
        ids["project"],
        ids["t1"],
        "Bandingkan harga Kopi Kenangan dan Fore di ojol.",
    )
    return ids


async def _cleanup(conn: asyncpg.Connection, ids: dict[str, Any]) -> None:
    await conn.execute("DELETE FROM public.document_blocks WHERE project_id = $1", ids["project"])
    await conn.execute("DELETE FROM public.tasks WHERE project_id = $1", ids["project"])
    await conn.execute("DELETE FROM public.projects WHERE id = $1", ids["project"])
    await conn.execute("DELETE FROM public.workspaces WHERE id = $1", ids["ws"])
    await conn.execute("DELETE FROM public.users WHERE id = $1", ids["user"])


@pytest_asyncio.fixture
async def env(services: Services) -> AsyncIterator[Env]:
    reset_mode_cache()
    admin = await asyncpg.connect(admin_test_url())
    ids = await _seed_dashboard(admin)
    reg = default_registry()
    services.jobs = JobQueue(services.db, estimator=reg)
    embedder = HashEmbedder()
    services.extras["kb_embedder"] = embedder
    worker = Worker(
        services,
        services.jobs,
        reg,
        worker_id="w-kb",
        use_process_pool=False,
        poll_interval_s=0.05,
    )
    task = asyncio.create_task(worker.run())
    _, pipeline, _ = build_asgi_app(services)
    _, client = await make_client(
        services, name="kb-tester", toolsets=["kb", "job", "data"], workspace_id=ids["ws"]
    )
    try:
        yield Env(
            services,
            pipeline,
            client,
            embedder,
            ids["ws"],
            ids["project"],
            [ids["t1"], ids["t2"]],
            admin,
        )
    finally:
        worker.stop()
        await task
        await pipeline.drain()
        await _cleanup(admin, ids)
        await admin.close()


async def _collection(env: Env, name: str = "riset") -> str:
    out = (await env.call("kb_collection_create", {"name": name})).structured
    return str(out["id"])


async def _ingest(env: Env, cid: str, source: dict[str, Any], **kw: Any) -> dict[str, Any]:
    res = await env.call("kb_ingest", {"collection_id": cid, "source": source, "wait_s": 20, **kw})
    assert res.error_code is None, res.structured
    return dict(res.structured)


async def test_fallback_mode_without_pgvector(env: Env) -> None:
    async with env.services.db.session() as session:
        assert await vector_mode(session) == "array"


async def test_ingest_is_idempotent_and_search_modes(env: Env) -> None:
    cid = await _collection(env)
    doc = (
        "# Strategi Harga\n\nPromo bundling kopi gula aren 2 cup 30 ribu untuk mahasiswa.\n\n"
        "## Distribusi\n\nGerai dekat kampus dan ojol jam makan siang."
    )
    first = await _ingest(env, cid, {"type": "text", "value": doc}, metadata={"ref": "harga"})
    assert first["created"] == 1 and first["chunks"] >= 1
    calls = env.embedder.texts_embedded
    again = await _ingest(env, cid, {"type": "text", "value": doc}, metadata={"ref": "harga"})
    assert again["skipped"] == 1 and env.embedder.texts_embedded == calls
    changed = await _ingest(
        env, cid, {"type": "text", "value": doc + " Tambahan."}, metadata={"ref": "harga"}
    )
    assert changed["updated"] == 1
    await _ingest(
        env,
        cid,
        {"type": "file_text", "value": "Laporan cuaca Jakarta hujan deras sore hari."},
        metadata={"filename": "cuaca.txt", "topic": "cuaca"},
    )

    for mode in ("hybrid", "vector", "keyword"):
        out = (
            await env.call(
                "kb_search",
                {"collection_id": cid, "query": "promo bundling mahasiswa", "mode": mode},
            )
        ).structured
        assert out["results"], mode
        assert "bundling" in out["results"][0]["text"], mode
        assert all(len(r["text"]) <= 600 for r in out["results"])
    filtered = (
        await env.call(
            "kb_search",
            {"collection_id": cid, "query": "hujan", "filter": {"topic": "cuaca"}},
        )
    ).structured
    assert [r["document_title"] for r in filtered["results"]] == ["cuaca.txt"]

    listed = (await env.call("kb_collections", {})).structured
    assert listed["items"]["rows"][0][1:4] == ["riset", 2, 2]


async def test_ingest_handles_and_report(env: Env) -> None:
    cid = await _collection(env, "sumber")
    owner = Owner(client_id=env.client.id)
    rs_posts = await env.services.results.create(
        kind="social_posts",
        columns=["posted_at", "keywords", "text", "author", "likes", "source"],
        rows=[
            {
                "posted_at": "2026-10-01T02:00:00+00:00",
                "keywords": ["kopi"],
                "text": "enak",
                "author": "a",
                "likes": 3,
                "source": "x",
            },
            {
                "posted_at": "2026-10-02T02:00:00+00:00",
                "keywords": ["kopi"],
                "text": "mahal",
                "author": "b",
                "likes": 1,
                "source": "threads",
            },
        ],
        owner=owner,
    )
    posts = await _ingest(env, cid, {"type": "handle", "value": rs_posts.handle})
    assert posts["documents"] == 2

    page_id = uuid.uuid4()
    async with env.services.db.session() as session:
        session.add(
            WebPage(
                id=page_id,
                url="https://blog.test/a",
                domain="blog.test",
                status="ok",
                title="Artikel",
                content_md="# Artikel\n\nIsi artikel tentang tren kopi susu.",
            )
        )
    rs_pages = await env.services.results.create(
        kind="web_pages",
        columns=["url"],
        rows=[{"url": "https://blog.test/a", "page_id": str(page_id)}],
        owner=owner,
        ref_id="page_id",
    )
    pages = await _ingest(env, cid, {"type": "handle", "value": rs_pages.handle})
    assert pages["documents"] == 1

    async with env.services.db.session() as session:
        session.add(
            AnalysisReport(
                id="r_kbtest0001",
                kind="market",
                title="Pasar Kopi",
                params={},
                summary="Pasar tumbuh 18%.",
                content={"sections": [{"id": "swot", "title": "SWOT", "markdown": "Kuat."}]},
                client_id=uuid.UUID(env.client.id),
            )
        )
    report = await _ingest(env, cid, {"type": "report", "value": "r_kbtest0001"})
    assert report["documents"] == 1

    _, stranger = await make_client(
        env.services, name="asing", toolsets=["kb", "job"], workspace_id=env.workspace_id
    )
    hidden = await env.call(
        "kb_ingest",
        {
            "collection_id": cid,
            "source": {"type": "handle", "value": rs_posts.handle},
            "wait_s": 20,
        },
        client=stranger,
    )
    assert hidden.error_code == "NOT_FOUND"


async def test_dimension_mismatch_rejected(env: Env) -> None:
    cid = await _collection(env, "dim")
    env.services.extras["kb_embedder"] = HashEmbedder(dim=384)
    res = await env.call(
        "kb_ingest",
        {"collection_id": cid, "source": {"type": "text", "value": "isi"}, "wait_s": 20},
    )
    assert res.error_code == "VALIDATION_ERROR"


async def test_sync_dashboard_incremental(env: Env) -> None:
    first = (
        await env.call("kb_sync_dashboard", {"project_id": str(env.project_id), "wait_s": 20})
    ).structured
    assert first["documents"] == 3 and first["created"] == 3
    calls = env.embedder.texts_embedded

    again = (
        await env.call("kb_sync_dashboard", {"project_id": str(env.project_id), "wait_s": 20})
    ).structured
    assert again["skipped"] == 3 and env.embedder.texts_embedded == calls

    await env.admin.execute(
        "UPDATE public.document_blocks SET plain_text = 'Bandingkan harga Fore saja.', "
        "version = version + 1 WHERE task_id = $1",
        env.task_ids[0],
    )
    await env.admin.execute(
        "UPDATE public.tasks SET deleted_at = now() WHERE id = $1", env.task_ids[1]
    )
    third = (
        await env.call("kb_sync_dashboard", {"project_id": str(env.project_id), "wait_s": 20})
    ).structured
    assert (third["updated"], third["skipped"], third["deleted"]) == (1, 1, 1)
    assert env.embedder.texts_embedded - calls == third["chunks"]

    found = (
        await env.call(
            "kb_search", {"project_id": str(env.project_id), "query": "kreator TikTok Reels"}
        )
    ).structured
    assert found["results"][0]["document_title"].startswith("Project Brief")
    assert "[Project Brief: Launch Kopi > Kanal]" in found["results"][0]["text"]


async def test_schedule_sync_only_when_stale(env: Env) -> None:
    await env.services.cache.set(sync_mark_key(env.project_id), "kb_sync", {"at": "x"}, 60)
    assert not await maybe_schedule_sync(env.services, env.project_id, env.workspace_id)
    async with env.services.db.session() as session:
        await session.execute(text("DELETE FROM mcp.cache_entries WHERE namespace = 'kb_sync'"))
    assert await maybe_schedule_sync(env.services, env.project_id, env.workspace_id)
    async with env.services.db.session() as session:
        n = await session.scalar(
            select(func.count()).select_from(Job).where(Job.kind == "kb_sync_dashboard")
        )
    assert n == 1


async def test_workspace_isolation_and_delete(env: Env) -> None:
    cid = await _collection(env, "privat")
    out = await _ingest(env, cid, {"type": "text", "value": "Rahasia strategi Q4 kopi."})
    other_ws = uuid.uuid4()
    _, outsider = await make_client(
        env.services, name="luar", toolsets=["kb"], workspace_id=other_ws
    )
    denied = await env.call(
        "kb_search", {"collection_id": cid, "query": "rahasia"}, client=outsider
    )
    assert denied.error_code == "NOT_FOUND"
    sync_denied = await env.call(
        "kb_sync_dashboard", {"project_id": str(env.project_id)}, client=outsider
    )
    assert sync_denied.error_code == "NOT_FOUND"
    assert out["chunks"] == 1

    async with env.services.db.session() as session:
        doc_id = await session.scalar(
            text("SELECT id FROM mcp.kb_documents WHERE collection_id = :c"), {"c": cid}
        )
    deleted = (await env.call("kb_delete", {"document_id": str(doc_id)})).structured
    assert deleted == {"deleted_chunks": 1}
    gone = (await env.call("kb_delete", {"collection_id": cid})).structured
    assert gone == {"deleted_chunks": 0}
