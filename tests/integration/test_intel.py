"""M3_TASK_007: toolset intel end-to-end di PostgreSQL — validasi agent, pipeline, event, RBAC.

Collector & browser dipalsukan (handler job anak + prepare_source); LLM memakai GatewayClient ASLI
dengan transport palsu sehingga header atribusi & `mcp.llm_calls` ikut teruji.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
import pytest_asyncio
from sqlalchemy import select

from mcp_marketing.models import IntelJob, Job, LlmCall, WebhookOutbox
from mcp_marketing.schemas.intel import IntelEventEnvelope, validate_intel_event_data
from mcp_marketing.server.app import build_asgi_app
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.pipeline import ToolPipeline
from mcp_marketing.server.services import Services
from mcp_marketing.services.dashboard.outbox import Outbox
from mcp_marketing.services.intel import estimate as estimate_mod
from mcp_marketing.services.intel import pipeline as pipeline_mod
from mcp_marketing.services.intel.sources import PreparedSource
from mcp_marketing.services.llm.gateway_client import GatewayClient
from mcp_marketing.services.seed import seed
from mcp_marketing.worker.handlers import default_registry
from mcp_marketing.worker.handlers.base import JobCtx, JobResult
from mcp_marketing.worker.queue import JobQueue
from mcp_marketing.worker.runner import Worker
from tests.fakes import FakeGateway, HashEmbedder, completion
from tests.integration.conftest import make_client
from tests.integration.helpers import FakeDashboard

WS = "00000000-0000-4000-8000-000000000001"
OTHER_WS = "00000000-0000-4000-8000-000000000002"
USER = "44444444-4444-4444-8444-444444444444"
PERIOD = {"since": "2026-10-01", "until": "2026-10-03"}
TEXTS = {
    "x": ["Kopinya enak banget", "kecewa, harganya mahal", "rasa mantap, pasti beli lagi"],
    "threads": ["kopi susu favorit", "pengiriman terlambat, kecewa"],
}


class FakeSocialHandler:
    """Pengganti x_search/threads_search: simpan post lewat jalur penyimpanan ASLI."""

    def __init__(self, source: str, *, gate: asyncio.Event | None = None) -> None:
        self.source = source
        self.kind = f"{source}_search"
        self.gate = gate
        self.runs = 0

    async def estimate(self, params: dict[str, Any]) -> Any:
        raise NotImplementedError

    async def run(self, job: JobCtx) -> JobResult:
        # impor lokal: mengimpor collectors.social lebih dulu dari worker.handlers memicu siklus
        from mcp_marketing.services.collectors import social
        from mcp_marketing.services.collectors.social import PostIn

        self.runs += 1
        texts = TEXTS[self.source]
        await job.progress(0, len(texts), "mulai")
        if self.gate is not None:
            await self.gate.wait()
            await job.raise_if_cancelled()
        posts = [
            PostIn(
                source=self.source,
                external_id=f"{self.source}-{i}-{job.job_id}",
                text=f"{t} #kopi",
                posted_at=datetime(2026, 10, 1 + i, 3, tzinfo=UTC),
                author_handle=f"akun{i}",
                url=f"https://{self.source}.test/{i}",
                likes=10 - i,
            )
            for i, t in enumerate(texts)
        ]
        await social.store_posts(job, posts, ["kopi"])
        await job.progress(len(texts), len(texts), "selesai")
        return await social.build_result(
            job, source=self.source, provider="browser", keywords=["kopi"], period=("a", "b")
        )


SYNTH = {
    "summary_md": "## Ringkasan\n- Sentimen dominan **positif**.",
    "topics": [{"label": "rasa kopi", "share": 0.6}, {"label": "pengiriman", "share": 0.2}],
    "recommendations": ["Perbaiki pengiriman", "Promo bundling"],
}


def gateway_reply(request: httpx2.Request) -> httpx2.Response:
    body = json.loads(request.content)
    if body.get("response_format", {}).get("type") == "json_object":
        return httpx2.Response(
            200, json=completion(json.dumps(SYNTH), prompt=900, completion_tokens=300)
        )
    content = body["messages"][-1]["content"]
    lines = []
    for m in re.finditer(r"^(\d+)\|(.*)$", content, re.M):
        label = "neg" if re.search(r"kecewa|mahal|terlambat", m.group(2)) else "pos"
        lines.append(f"{m.group(1)}|{label}|0.9")
    return httpx2.Response(200, json=completion("\n".join(lines), prompt=120, completion_tokens=30))


def embed_reply(request: httpx2.Request) -> httpx2.Response:
    texts = json.loads(request.content)["input"]
    vector = HashEmbedder().vector
    data = [
        {"object": "embedding", "index": i, "embedding": vector(t)} for i, t in enumerate(texts)
    ]
    usage = {"prompt_tokens": 12 * len(texts), "total_tokens": 12 * len(texts)}
    return httpx2.Response(
        200, json={"object": "list", "data": data, "model": "embed-default", "usage": usage}
    )


@dataclass
class Env:
    services: Services
    pipeline: ToolPipeline
    client: ClientIdentity
    dashboard: FakeDashboard
    outbox: Outbox
    gateway: FakeGateway
    handlers: dict[str, FakeSocialHandler] = field(default_factory=dict)

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        return await self.pipeline.call(tool, args, client=self.client)

    async def ok(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        out = await self.call(tool, args)
        assert not out.is_error, out.structured
        return out.structured  # type: ignore[no-any-return]

    async def wait_status(
        self, mcp_job_id: str, statuses: tuple[str, ...], timeout: float = 20
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            view = await self.ok("intel_job_get", {"workspace_id": WS, "mcp_job_id": mcp_job_id})
            if view["status"] in statuses:
                return view
            assert asyncio.get_running_loop().time() < deadline, view
            await asyncio.sleep(0.1)

    async def drain(self) -> None:
        for _ in range(50):
            if not await self.outbox.send_due():
                return


def spec(*sources: str, collect: str = "researcher", analyze: str = "analyst") -> dict[str, Any]:
    return {
        "title": "Riset kopi susu",
        "period": PERIOD,
        "sources": [{"type": s, "keywords": ["kopi"]} for s in sources],
        "stages": {"collect": {"agent_key": collect}, "analyze": {"agent_key": analyze}},
    }


def create_args(spec_: dict[str, Any], **over: Any) -> dict[str, Any]:
    args = {
        "contract_version": "1.2",
        "dashboard_job_id": str(uuid.uuid4()),
        "workspace_id": WS,
        "requested_by": {"user_id": USER, "name": "Member Demo"},
        "spec": spec_,
        "confirm_cost": False,
    }
    args.update(over)
    return args


@pytest_asyncio.fixture
async def env(services: Services, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    async with services.db.session() as s:
        await seed(s)
    handlers = {"x": FakeSocialHandler("x"), "threads": FakeSocialHandler("threads")}
    monkeypatch.setitem(pipeline_mod.CHILD_HANDLERS, "x_search", handlers["x"])
    monkeypatch.setitem(pipeline_mod.CHILD_HANDLERS, "threads_search", handlers["threads"])

    async def fake_prepare(_services: Any, src: Any, _period: Any) -> PreparedSource:
        usd = 2.5 if getattr(src, "provider", None) == "x_api" else 0.0
        units = len(TEXTS[src.type]) if usd else 0
        return PreparedSource(
            src.type, f"{src.type}_search", {}, "api" if usd else "browser",
            len(TEXTS[src.type]), 30, 60, external_units=units, usd_external=usd,
        )  # fmt: skip

    monkeypatch.setattr(pipeline_mod, "prepare_source", fake_prepare)
    monkeypatch.setattr(estimate_mod, "prepare_source", fake_prepare)

    gateway = (
        FakeGateway()
        .on("/chat/completions", gateway_reply)
        .on("/embeddings", embed_reply)
        .on(
            "/models",
            httpx2.Response(
                200, json={"object": "list", "data": [{"id": "fast"}, {"id": "smart"}]}
            ),
        )
    )
    original_llm = services.llm
    services.llm = GatewayClient(services.settings, db=services.db, http_client=gateway.client())
    dashboard = FakeDashboard(
        secret=services.settings.dashboard.mcp_webhook_secret.get_secret_value()
    )
    outbox = Outbox(services.db, dashboard.client(), services.settings)
    services.outbox = outbox
    registry = default_registry()
    services.jobs = JobQueue(services.db, estimator=registry)
    worker = Worker(
        services,
        services.jobs,
        registry,
        worker_id="w-intel",
        use_process_pool=False,
        poll_interval_s=0.05,
    )
    task = asyncio.create_task(worker.run())
    _, tool_pipeline, _ = build_asgi_app(services)
    _, client = await make_client(services, name="dashboard-intel", toolsets=["intel"])
    try:
        yield Env(services, tool_pipeline, client, dashboard, outbox, gateway, handlers)
    finally:
        worker.stop()
        await task
        await tool_pipeline.drain()
        await services.llm.drain()
        services.llm = original_llm


async def test_intel_client_cannot_call_internal_toolsets(env: Env) -> None:
    out = await env.call("x_search", {"keywords": ["kopi"], **PERIOD})
    assert out.is_error and out.structured["code"] == "FORBIDDEN"


async def test_agents_list_capabilities(env: Env) -> None:
    agents = {a["agent_key"]: a for a in (await env.ok("intel_agents_list", {}))["agents"]}
    assert agents["researcher"]["capabilities"] == ["collect", "analyze", "index"]
    assert agents["strategist"]["capabilities"] == ["index"]


async def test_estimate_and_validation_errors(env: Env) -> None:
    est = await env.ok("intel_job_estimate", {"workspace_id": WS, "spec": spec("x", "threads")})
    assert est["llm"]["by_stage"][0]["agent_key"] == "analyst" and est["seconds"] > 0
    for bad, code in (
        (spec("x", collect="strategist"), "AGENT_NOT_CAPABLE"),
        (spec("x", analyze="ghost_agent"), "UNKNOWN_AGENT"),
    ):
        out = await env.call("intel_job_create", create_args(bad))
        assert out.is_error and out.structured["code"] == code
    paid = spec("x")
    paid["sources"][0]["provider"] = "x_api"
    out = await env.call("intel_job_create", create_args(paid))
    assert out.is_error and out.structured["code"] == "COST_CONFIRMATION_REQUIRED"


async def test_full_pipeline_events_view_and_attribution(env: Env) -> None:
    args = create_args(spec("x", "threads"))
    accepted = await env.ok("intel_job_create", args)
    again = await env.ok("intel_job_create", args)  # idempoten
    assert again["mcp_job_id"] == accepted["mcp_job_id"]
    job_id = accepted["mcp_job_id"]

    view = await env.wait_status(job_id, ("completed", "failed"))
    assert view["status"] == "completed", view
    result = view["result"]
    assert result["totals"] == {"items": 5, "by_source": {"threads": 2, "x": 3}}
    dist = result["sentiment"]["distribution"]
    assert dist["negative"] == pytest.approx(0.4) and dist["positive"] == pytest.approx(0.6)
    assert result["summary_md"].startswith("## Ringkasan")
    assert result["trends"]["topics"][0] == {"label": "rasa kopi", "count": 3, "share": 0.6}
    assert result["trends"]["hashtags"][0] == ["kopi", 5]
    assert [s["status"] for s in view["stages"]] == ["completed", "completed"]
    assert view["cost"]["by_stage"][0]["agent_key"] == "analyst" and view["cost"]["llm_tokens"] > 0

    # event berurutan, valid kontrak, sequence ketat naik
    await env.drain()
    events = [p for p in env.dashboard.processed_order if p.get("mcp_job_id") == job_id]
    for p in events:
        env_ = IntelEventEnvelope.model_validate(p)
        validate_intel_event_data(env_.event_type, env_.data)
    assert [p["sequence"] for p in events] == list(range(1, len(events) + 1))
    types = [p["event_type"] for p in events]
    assert types[0] == "intel.job.started" and types[-1] == "intel.job.completed"
    assert types.index("intel.job.stage.started") < types.index("intel.job.stage.completed")
    assert all(p["job_id"] == args["dashboard_job_id"] for p in events)

    # atribusi Gateway: header + llm_calls per agent/tahap/job
    chat_calls = env.gateway.calls_to("/chat/completions")
    assert chat_calls and all(c.headers.get("x-caller-agent") == "analyst" for c in chat_calls)
    assert all(c.headers.get("x-caller-task") == f"intel:{job_id}:analyze" for c in chat_calls)
    await env.services.llm.drain()  # type: ignore[union-attr]
    async with env.services.db.session() as s:
        calls = (await s.scalars(select(LlmCall).where(LlmCall.job_id == job_id))).all()
        children = (await s.scalars(select(Job).where(Job.parent_job_id == job_id))).all()
    assert calls and {(c.agent_key, c.stage) for c in calls} == {("analyst", "analyze")}
    assert sorted(c.kind for c in children) == ["threads_search", "x_search"]
    assert all(c.status == "succeeded" for c in children)

    # data mentah: filter sentimen + teks, workspace lain ditolak
    neg = await env.ok(
        "intel_items",
        {"workspace_id": WS, "mcp_job_id": job_id, "filter": {"sentiment": "negative"}},
    )
    assert neg["total"] == 2 and all(
        r[neg["cols"].index("sentiment")] == "negative" for r in neg["rows"]
    )
    q = await env.ok(
        "intel_items", {"workspace_id": WS, "mcp_job_id": job_id, "filter": {"q": "MAHAL"}}
    )
    assert q["total"] == 1
    for tool in ("intel_job_get", "intel_items", "intel_job_cancel"):
        out = await env.call(tool, {"workspace_id": OTHER_WS, "mcp_job_id": job_id})
        assert out.is_error and out.structured["code"] == "NOT_FOUND"

    status = await env.ok("intel_llm_status", {"range_days": 7})
    analyst = next(a for a in status["agents"] if a["agent_key"] == "analyst")
    assert analyst["usage"]["calls"] == len(calls) and analyst["key_source"] == "env"


async def test_index_stage_saves_to_kb_scoped_and_purgeable(env: Env) -> None:
    spec_ = spec("x", "threads")
    spec_["stages"]["index"] = {"agent_key": "strategist", "enabled": True}
    est = await env.ok("intel_job_estimate", {"workspace_id": WS, "spec": spec_})
    assert [s["stage"] for s in est["llm"]["by_stage"]] == ["analyze", "index"]

    job_id = (await env.ok("intel_job_create", create_args(spec_)))["mcp_job_id"]
    view = await env.wait_status(job_id, ("completed", "failed"))
    assert view["status"] == "completed", view
    assert [(s["stage"], s["status"]) for s in view["stages"]] == [
        ("collect", "completed"),
        ("analyze", "completed"),
        ("index", "completed"),
    ]
    index_cost = next(c for c in view["cost"]["by_stage"] if c["stage"] == "index")
    assert index_cost["agent_key"] == "strategist" and index_cost["llm_tokens"] > 0

    # event tahap index: progres + statistik (5 post + 1 ringkasan; ringkasan bisa > 1 chunk)
    await env.drain()
    events = [p for p in env.dashboard.processed_order if p.get("mcp_job_id") == job_id]
    for p in events:
        validate_intel_event_data(p["event_type"], p["data"])
    done = [
        p["data"]
        for p in events
        if p["event_type"] == "intel.job.stage.completed" and p["data"]["stage"] == "index"
    ]
    stats = done[0]["stats"]
    assert stats["documents"] == stats["created"] == 6 and stats["skipped"] == 0
    assert stats["chunks"] >= 6 and stats["collection"] == f"intel:{WS}"
    index_progress = [
        p["data"]
        for p in events
        if p["event_type"] == "intel.job.progress" and p["data"]["stage"] == "index"
    ]
    assert index_progress
    # M3_TASK_011: kata terbaru untuk animasi stream (huruf saja, ≤ 20)
    tokens = [t for d in index_progress for t in d.get("sample_tokens") or []]
    assert tokens and all(re.fullmatch(r"[^\W\d_]{3,24}", t) for t in tokens)
    assert max(len(d.get("sample_tokens") or []) for d in index_progress) <= 20

    # M3_TASK_011: proyeksi PCA 3D atas embedding item job (ringkasan tidak ikut)
    ref = {"workspace_id": WS, "mcp_job_id": job_id}
    proj = await env.ok("intel_kb_projection", ref)
    assert proj["method"] == "pca" and len(proj["points"]) == 5
    point = proj["points"][0]
    assert set(point) == {"id", "x", "y", "z", "source", "sentiment", "label"}
    assert {p["source"] for p in proj["points"]} == {"x", "threads"}
    assert {p["sentiment"] for p in proj["points"]} == {"positive", "negative"}
    assert all(len(p["label"]) <= 60 and not p["label"].startswith("[") for p in proj["points"])
    assert len((await env.ok("intel_kb_projection", {**ref, "limit": 2}))["points"]) == 2
    other_ws = await env.call("intel_kb_projection", {**ref, "workspace_id": OTHER_WS})
    assert other_ws.is_error and other_ws.structured["code"] == "NOT_FOUND"

    # embedding memakai key/atribusi agent tahap index
    embeds = env.gateway.calls_to("/embeddings")
    assert embeds and all(c.headers.get("x-caller-agent") == "strategist" for c in embeds)
    await env.services.llm.drain()  # type: ignore[union-attr]
    async with env.services.db.session() as s:
        calls = (await s.scalars(select(LlmCall).where(LlmCall.job_id == job_id))).all()
    assert ("strategist", "index") in {(c.agent_key, c.stage) for c in calls}

    # kb_search: agent di workspace yang sama menemukan post; workspace lain tidak
    _, kb_client = await make_client(env.services, name="kb-ws", toolsets=["kb"], workspace_id=WS)
    _, outsider = await make_client(
        env.services, name="kb-luar", toolsets=["kb"], workspace_id=OTHER_WS
    )
    colls = await env.pipeline.call("kb_collections", {}, client=kb_client)
    names = colls.structured["items"]["cols"].index("name")
    coll_id = next(r[0] for r in colls.structured["items"]["rows"] if r[names] == f"intel:{WS}")
    query = {"collection_id": coll_id, "query": "pengiriman terlambat kecewa", "top_k": 3}
    hits = await env.pipeline.call("kb_search", query, client=kb_client)
    assert not hits.is_error and "pengiriman terlambat" in hits.structured["results"][0]["text"]
    blocked = await env.pipeline.call("kb_search", query, client=outsider)
    assert blocked.is_error and blocked.structured["code"] == "NOT_FOUND"

    # hapus job di Dashboard -> dokumen job dibuang dari koleksi (workspace lain ditolak)
    other = await env.call("intel_job_purge_index", {**ref, "workspace_id": OTHER_WS})
    assert other.is_error and other.structured["code"] == "NOT_FOUND"
    purged = await env.ok("intel_job_purge_index", ref)
    assert purged == {"deleted_documents": 6, "deleted_chunks": stats["chunks"]}
    after = await env.pipeline.call("kb_search", {**query, "mode": "keyword"}, client=kb_client)
    assert after.structured["results"] == []


async def test_one_source_fails_other_continues(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(FakeSocialHandler):
        async def run(self, job: JobCtx) -> JobResult:
            from mcp_marketing.core.errors import LoginRequired

            raise LoginRequired("profil logout")

    monkeypatch.setitem(pipeline_mod.CHILD_HANDLERS, "threads_search", Broken("threads"))
    accepted = await env.ok("intel_job_create", create_args(spec("x", "threads")))
    view = await env.wait_status(accepted["mcp_job_id"], ("completed", "failed"))
    assert view["status"] == "completed" and view["result"]["totals"]["items"] == 3
    assert any(w["code"] == "SOURCE_FAILED" for w in view["result"]["warnings"])


async def test_only_source_fails_job_failed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(FakeSocialHandler):
        async def run(self, job: JobCtx) -> JobResult:
            from mcp_marketing.core.errors import LoginRequired

            raise LoginRequired("profil logout")

    monkeypatch.setitem(pipeline_mod.CHILD_HANDLERS, "x_search", Broken("x"))
    accepted = await env.ok("intel_job_create", create_args(spec("x")))
    view = await env.wait_status(accepted["mcp_job_id"], ("completed", "failed"))
    assert view["status"] == "failed"
    assert view["error"]["code"] == "SOURCE_UNAVAILABLE" and view["error"]["stage"] == "collect"
    await env.drain()
    types = [
        p["event_type"]
        for p in env.dashboard.processed_order
        if p.get("mcp_job_id") == accepted["mcp_job_id"]
    ]
    assert types[-1] == "intel.job.failed"


async def test_gateway_key_rejected_maps_to_llm_key_invalid(env: Env) -> None:
    env.gateway.replies["/chat/completions"] = [
        httpx2.Response(401, json={"error": {"code": "invalid_api_key", "message": "x"}})
    ]
    accepted = await env.ok("intel_job_create", create_args(spec("x")))
    view = await env.wait_status(accepted["mcp_job_id"], ("completed", "failed"))
    assert view["status"] == "failed" and view["error"]["code"] == "LLM_KEY_INVALID"
    assert view["error"]["stage"] == "analyze"


async def test_cancel_while_collecting(env: Env) -> None:
    gate = asyncio.Event()
    env.handlers["x"].gate = gate
    accepted = await env.ok("intel_job_create", create_args(spec("x")))
    job_id = accepted["mcp_job_id"]
    await env.wait_status(job_id, ("collecting",))
    cancel = await env.ok("intel_job_cancel", {"workspace_id": WS, "mcp_job_id": job_id})
    assert cancel["cancelled"] is True
    gate.set()
    view = await env.wait_status(job_id, ("cancelled",))
    assert [s["status"] for s in view["stages"]] == ["cancelled", "cancelled"]
    again = await env.ok("intel_job_cancel", {"workspace_id": WS, "mcp_job_id": job_id})
    assert again == {"cancelled": False, "status": "cancelled"}
    await env.drain()
    types = [
        p["event_type"] for p in env.dashboard.processed_order if p.get("mcp_job_id") == job_id
    ]
    assert types[-1] == "intel.job.cancelled"


async def test_outbox_discards_job_stream_on_job_not_active(env: Env) -> None:
    from tests.integration.helpers import status

    env.dashboard.responses.append(status(409, "JOB_NOT_ACTIVE"))
    accepted = await env.ok("intel_job_create", create_args(spec("x")))
    await env.wait_status(accepted["mcp_job_id"], ("completed", "failed"))
    await env.drain()
    async with env.services.db.session() as s:
        rows = (
            await s.scalars(
                select(WebhookOutbox).where(WebhookOutbox.job_id == accepted["mcp_job_id"])
            )
        ).all()
        intel = await s.get(IntelJob, accepted["mcp_job_id"])
    assert rows and {r.status for r in rows} == {"discarded"}
    assert intel is not None and intel.status == "completed"  # job tetap selesai di MCP


# ---------------------------------------------------------------------------- login profil browser


class FakeWindow:
    """Jendela Chrome palsu: tertutup sendiri setelah `polls` kali dicek."""

    def __init__(self, polls: int) -> None:
        self.left = polls
        self.closed = False

    def running(self) -> bool:
        self.left -= 1
        return self.left > 0 and not self.closed

    def close(self) -> None:
        self.closed = True


async def _login_env(env: Env, tmp_path: Path, window: FakeWindow, sleep: Any) -> list[Any]:
    from mcp_marketing.services.browser.profiles import ProfileStore
    from tests.integration.helpers import FakeLauncher, FakePage, no_sleep

    page = FakePage({"https://myaccount.google.com/": "<html>myaccount.google.com</html>"})
    opened: list[Any] = []

    def opener(profile: Any, urls: list[str]) -> FakeWindow:
        opened.append((profile.name, urls))
        return window

    env.services.extras.update(
        browser_launcher=FakeLauncher(page),
        profile_store=ProfileStore(env.services.db, tmp_path, sleep=no_sleep),
        login_window_opener=opener,
        sleep=sleep,
    )
    return opened


async def _login_wait(env: Env, job_id: str) -> dict[str, Any]:
    for _ in range(200):
        view = await env.ok("intel_browser_login_get", {"job_id": job_id})
        if view["status"] in ("succeeded", "failed", "cancelled"):
            return view
        await asyncio.sleep(0.05)
    raise AssertionError(view)


async def test_browser_login_from_dashboard_then_web_uses_profile(
    env: Env, tmp_path: Path
) -> None:
    from mcp_marketing.services.intel.sources import web_profile
    from tests.integration.helpers import no_sleep

    opened = await _login_env(env, tmp_path, FakeWindow(polls=3), no_sleep)
    try:
        created = await env.ok(
            "intel_browser_profile_create", {"name": "riset-google", "sites": ["google"]}
        )
        assert created["headless"] is True and created["allowed_sites"] == ["trends.google.com"]
        assert await web_profile(env.services) == "default"  # belum login

        started = await env.ok(
            "intel_browser_login_start", {"profile": "riset-google", "sites": ["google"]}
        )
        view = await _login_wait(env, started["job_id"])
        assert view["status"] == "succeeded" and view["closed_by_user"] is True
        assert view["checks"] == [
            {"site": "trends.google.com", "logged_in": True, "detail": "penanda akun terdeteksi"}
        ]
        assert opened == [("riset-google", ["https://accounts.google.com/"])]  # Chrome biasa

        listed = await env.ok("intel_browser_profiles", {})
        google = next(p for p in listed["profiles"] if p["name"] == "riset-google")
        assert google["logged_in_sites"] == ["trends.google.com"] and google["locked"] is False
        assert "trends.google.com" in google["login_checked_at"]
        assert await web_profile(env.services) == "riset-google"  # scraper Web memakai profil ini
    finally:
        for key in ("browser_launcher", "profile_store", "login_window_opener", "sleep"):
            env.services.extras.pop(key, None)


async def test_browser_login_cancel_closes_window(env: Env, tmp_path: Path) -> None:
    window = FakeWindow(polls=10_000)

    async def short_sleep(_s: float) -> None:
        await asyncio.sleep(0.01)

    await _login_env(env, tmp_path, window, short_sleep)
    try:
        await env.ok("intel_browser_profile_create", {"name": "riset-batal", "sites": ["google"]})
        started = await env.ok(
            "intel_browser_login_start", {"profile": "riset-batal", "sites": ["google"]}
        )
        ref = {"job_id": started["job_id"]}
        for _ in range(100):
            if (await env.ok("intel_browser_login_get", ref))["status"] == "running":
                break
            await asyncio.sleep(0.05)
        await env.ok("intel_browser_login_cancel", ref)
        view = await _login_wait(env, started["job_id"])
        assert view["status"] == "cancelled" and window.closed
    finally:
        for key in ("browser_launcher", "profile_store", "login_window_opener", "sleep"):
            env.services.extras.pop(key, None)


# ---------------------------------------------------------------------------- misi (M3_TASK_013)

MISSION_PERIOD = {"start_date": "2026-11-10", "end_date": "2026-11-30"}


def plan_reply(start: str, end: str) -> httpx2.Response:
    plan = {
        "summary": "Kampanye konten kopi",
        "tasks": [
            {
                "ref": "t1",
                "title": "Riset kompetitor",
                "start_date": start,
                "end_date": end,
                "executor": "agent",
                "subtasks": [
                    {"ref": "t1.1", "title": "Kumpulkan post", "start_date": start, "end_date": start}
                ],
            },
            {"ref": "t2", "title": "Susun kalender", "start_date": start, "end_date": end,
             "depends_on": ["t1"]},
        ],
    }  # fmt: skip
    return httpx2.Response(
        200, json=completion(json.dumps(plan), prompt=500, completion_tokens=200)
    )


def mission_args(**over: Any) -> dict[str, Any]:
    return {
        "workspace_id": WS,
        "mission_id": str(uuid.uuid4()),
        "agent_key": "strategist",
        "title": "Tindak lanjut riset kopi",
        "instruction": "Buat rencana konten dari hasil riset",
        "period": MISSION_PERIOD,
        "context": {"recommendations": ["Promo bundling"], "existing_tasks": [{"title": "Brief"}]},
        "available_agents": ["strategist", "researcher", "assistant"],  # assistant: tanpa profil
        **over,
    }


async def test_mission_plan_repairs_invalid_output_and_attributes_cost(env: Env) -> None:
    env.gateway.replies["/chat/completions"] = [
        plan_reply("2026-11-01", "2026-11-12"),  # di luar timeline → loop perbaikan
        plan_reply("2026-11-10", "2026-11-12"),
    ]
    args = mission_args()
    out = await env.ok("intel_mission_plan", args)
    plan = out["plan"]
    assert [t["ref"] for t in plan["tasks"]] == ["t1", "t2"]
    assert plan["tasks"][0]["agent_key"] == "strategist"  # executor agent → agent planner
    assert plan["tasks"][1].get("agent_key") is None  # output mesin membuang field None
    assert [r["code"] for r in plan["risks"]] == ["TIMELINE_TIGHT"]  # t2 mulai sebelum t1 selesai
    # percobaan kedua = permintaan ulang planner (bukan repair 800 token) memuat alasan penolakan
    assert out["cost"]["llm_tokens"] == 700 and out["cost"]["usd"] > 0
    retry_prompt = env.gateway.bodies("/chat/completions")[1]["messages"][-1]["content"]
    assert "ditolak validasi" in retry_prompt and "di luar timeline" in retry_prompt
    calls = env.gateway.calls_to("/chat/completions")
    assert len(calls) == 2
    assert all(c.headers.get("x-caller-agent") == "strategist" for c in calls)
    assert all(c.headers.get("x-caller-task") == f"mission:{args['mission_id']}" for c in calls)
    first = env.gateway.bodies("/chat/completions")[0]
    assert "Promo bundling" in first["messages"][-1]["content"]
    assert "assistant" not in first["messages"][-1]["content"]  # agent tanpa profil MCP disaring
    assert first["reasoning_effort"] == "low"  # gpt-5: jatah token tidak habis untuk reasoning
    await env.services.llm.drain()  # type: ignore[union-attr]


async def test_mission_plan_rejects_agent_without_task_toolset_and_bad_input(env: Env) -> None:
    out = await env.call("intel_mission_plan", mission_args(agent_key="researcher"))
    assert out.is_error and out.structured["code"] == "AGENT_NOT_CAPABLE"
    out = await env.call("intel_mission_plan", mission_args(agent_key="ghost"))
    assert out.is_error and out.structured["code"] == "UNKNOWN_AGENT"
    too_long = {"start_date": "2026-01-01", "end_date": "2026-12-31"}
    out = await env.call("intel_mission_plan", mission_args(period=too_long))
    assert out.is_error and out.structured["code"] == "VALIDATION_ERROR"
    assert not env.gateway.calls_to("/chat/completions")
