"""Toolset `intel` (kontrak 1.2, Modul 3 Dashboard): job riset multi-tahap untuk Dashboard.

Hanya untuk klien `dashboard-intel` (toolset `intel`); output dibaca mesin (`machine_output`).
Dashboard melakukan RBAC; MCP mewajibkan `workspace_id` dan menolak job workspace lain.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from mcp_marketing.core import context
from mcp_marketing.core.errors import (
    AgentNotCapable,
    Conflict,
    CostConfirmationRequired,
    NotFound,
    ResultExpired,
    UnknownAgent,
    ValidationFailed,
)
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import AgentProfile, IntelJob, Job, LlmCall, LlmCredential, Run
from mcp_marketing.schemas import intel as c
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.agent.budget import BudgetLayer, BudgetScope
from mcp_marketing.services.agent.profiles import get_active_profile
from mcp_marketing.services.browser.driver_pool import profile_store
from mcp_marketing.services.browser.profiles import profile_view
from mcp_marketing.services.intel import kb_index, mission_plan
from mcp_marketing.services.intel.analysis import ITEMS_PAGE_COLS
from mcp_marketing.services.intel.capabilities import capabilities, validate_stage_agents
from mcp_marketing.services.intel.estimate import build_estimate, route_tier, usd_for_tokens
from mcp_marketing.services.intel.pipeline import KIND
from mcp_marketing.services.intel.projection import pca_3d
from mcp_marketing.services.intel.tracker import IntelTracker, initial_stages
from mcp_marketing.services.llm.credentials import key_info, list_credentials
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.utils.ids import new_job_id
from mcp_marketing.utils.time import now, today_in
from mcp_marketing.worker.handlers.web import SITE_ALIASES
from mcp_marketing.worker.queue import JobOwner

log = get_logger(__name__)

TOOLSET = "intel"
ANALYSIS_TIMEOUT_S = 3_600
INDEX_TIMEOUT_S = 1_800
MAX_JOB_TIMEOUT_S = 6 * 3_600
# 2 panggilan gpt-5 (permintaan ulang saat output tidak valid) masing-masing bisa ±90 s
MISSION_PLAN_TIMEOUT_S = 360
ORDER_COLUMN = {
    "-created_at": "-posted_at",
    "created_at": "posted_at",
    "-engagement": "-engagement",
}


def _owner(ctx: ToolContext) -> Owner:
    return Owner(client_id=ctx.client.id)


async def _intel_job(ctx: ToolContext, workspace_id: uuid.UUID, mcp_job_id: str) -> IntelJob:
    async with ctx.services.db.session() as session:
        job = await session.get(IntelJob, mcp_job_id)
    if job is None or job.workspace_id != workspace_id:
        raise NotFound(f"Job {mcp_job_id} tidak ditemukan")
    return job


async def _reconcile(ctx: ToolContext, intel: IntelJob) -> IntelJob:
    """Job induk sudah berakhir tanpa memperbarui intel_jobs (worker mati, batal saat antre)."""
    if intel.status in c.TERMINAL_STATUSES:
        return intel
    async with ctx.services.db.session() as session:
        parent = await session.get(Job, intel.id)
    if parent is None or parent.status not in ("failed", "cancelled"):
        return intel
    tracker = IntelTracker(ctx.services, intel.id)
    if parent.status == "cancelled":
        await tracker.cancelled()
    else:
        err = parent.error or {}
        await tracker.failed(
            next((s["stage"] for s in intel.stages if s["status"] == "running"), None),
            {
                "code": str(err.get("code") or "INTERNAL"),
                "message": str(err.get("message") or "Job gagal"),
                "retryable": bool(err.get("retryable")),
            },
        )
    return await tracker.load()


def _view(intel: IntelJob) -> c.JobView:
    return c.JobView.model_validate(
        {
            "mcp_job_id": intel.id,
            "dashboard_job_id": intel.dashboard_job_id,
            "status": intel.status,
            "stages": intel.stages,
            "result": intel.result,
            "cost": intel.cost or {},
            "error": intel.error,
        }
    )


# ---------------------------------------------------------------------------- agent & sumber


async def _credentials(ctx: ToolContext) -> tuple[dict[str, LlmCredential], bool]:
    """Credential per nama + apakah fitur key per agent aktif (`MCP_SECRET_KEY` terisi)."""
    async with ctx.services.db.session() as session:
        creds = {cred.name: cred for cred in await list_credentials(session)}
    return creds, ctx.settings.mcp_secret_key is not None


@tool(
    name="intel_agents_list",
    toolset=TOOLSET,
    summary="Agent MCP + kapabilitas tahap riset (collect/analyze/index)",
    description="List MCP agent profiles with research stage capabilities, LLM route and budget.",
    input_model=c.AgentsListIn,
    output_model=c.AgentsListOut,
    machine_output=True,
)
async def intel_agents_list(inp: c.AgentsListIn, ctx: ToolContext) -> c.AgentsListOut:
    async with ctx.services.db.session() as session:
        rows = (await session.scalars(select(AgentProfile).order_by(AgentProfile.agent_key))).all()
    creds, enabled = await _credentials(ctx)

    def gateway_key(agent_key: str) -> c.GatewayKeyInfo:
        source, cred = key_info(creds, agent_key, enabled=enabled)
        return c.GatewayKeyInfo(
            source=source, prefix=cred.key_prefix if source != "env" and cred else None
        )

    return c.AgentsListOut(
        agents=[
            c.AgentInfo(
                agent_key=p.agent_key,
                display_name=p.display_name,
                is_active=p.is_active,
                route=p.route,
                run_token_budget=p.run_token_budget,
                toolsets=list(p.toolsets),
                capabilities=capabilities(p.toolsets, p.tool_denylist),
                gateway_key=gateway_key(p.agent_key),
            )
            for p in rows
        ]
    )


async def _llm_routes(ctx: ToolContext) -> list[c.RouteStatus]:
    llm = ctx.settings.llm
    wanted = [llm.llm_route_fast, llm.llm_route_smart, llm.llm_route_embed]
    available: set[str] = set()
    if ctx.services.llm is not None:
        try:
            available = set(await ctx.services.llm.list_models())
        except Exception as exc:  # noqa: BLE001 - status: Gateway tidak terjangkau = route tidak tersedia
            log.warning("intel.models_unavailable", error=type(exc).__name__)
    return [c.RouteStatus(route=r, available=r in available) for r in wanted]


@tool(
    name="intel_sources_status",
    toolset=TOOLSET,
    summary="Status sumber riset (X, Threads, Web, Trends) + route LLM",
    description="Research source availability (API token / logged-in browser) and LLM routes.",
    input_model=c.SourcesStatusIn,
    output_model=c.SourcesStatusOut,
    machine_output=True,
)
async def intel_sources_status(inp: c.SourcesStatusIn, ctx: ToolContext) -> c.SourcesStatusOut:
    s, store, at = ctx.settings, profile_store(ctx.services), now()
    x_browser = await store.for_site("x.com")
    threads_browser = await store.for_site("threads.net")
    default = await store.find("default")
    google = await store.for_site("trends.google.com")
    web_detail = (
        f"profil browser '{google.name}' (login Google)"
        if google
        else "profil browser 'default' (tanpa login)"
        if default
        else "profil browser 'default' belum ada"
    )

    def status(source: str, api: bool, browser: Any, api_detail: str) -> c.SourceStatus:
        if api:
            return c.SourceStatus(
                source=source, available=True, mode="api", detail=api_detail, checked_at=at
            )  # type: ignore[arg-type]
        if browser is not None:
            detail = f"profil browser '{browser.name}' login"
            return c.SourceStatus(
                source=source, available=True, mode="browser", detail=detail, checked_at=at
            )  # type: ignore[arg-type]
        detail = "token API kosong dan tidak ada profil browser yang login"
        return c.SourceStatus(
            source=source, available=False, mode="none", detail=detail, checked_at=at
        )  # type: ignore[arg-type]

    trends_mode = {"unofficial": "unofficial", "browser": "browser", "official": "api"}
    sources = [
        status("x", s.social.x_bearer_token is not None, x_browser, "X API (≤ 7 hari terakhir)"),
        status(
            "threads", s.social.threads_access_token is not None, threads_browser, "Threads API"
        ),
        c.SourceStatus(
            source="web",
            available=default is not None or google is not None,
            mode="browser" if default is not None or google is not None else "none",
            detail=web_detail,
            checked_at=at,
        ),
        c.SourceStatus(
            source="trends",
            available=True,
            mode=trends_mode[s.trends.trends_provider],  # type: ignore[arg-type]
            detail=f"provider {s.trends.trends_provider}",
            checked_at=at,
        ),
    ]
    return c.SourcesStatusOut(
        sources=sources, llm=c.LlmRoutes(routes=await _llm_routes(ctx)), checked_at=at
    )


# ---------------------------------------------------------------------------- job


@tool(
    name="intel_job_estimate",
    toolset=TOOLSET,
    summary="Estimasi durasi & biaya job riset sebelum dijalankan",
    description="Validate a research job spec (stage agents, sources); estimate time and cost.",
    input_model=c.JobEstimateIn,
    output_model=c.Estimate,
    machine_output=True,
)
async def intel_job_estimate(inp: c.JobEstimateIn, ctx: ToolContext) -> c.Estimate:
    async with ctx.services.db.session() as session:
        profiles = await validate_stage_agents(session, inp.spec)
    estimate, _ = await build_estimate(ctx.services, inp.spec, profiles)
    return estimate


@tool(
    name="intel_job_create",
    toolset=TOOLSET,
    summary="Buat & antrekan job riset (idempoten per dashboard_job_id)",
    description="Create a research job (collect -> analyze) with chosen agents; idempotent.",
    input_model=c.JobCreateIn,
    output_model=c.JobAcceptedOut,
    machine_output=True,
    writes=True,
)
async def intel_job_create(inp: c.JobCreateIn, ctx: ToolContext) -> c.JobAcceptedOut:
    async with ctx.services.db.session() as session:
        existing = await session.scalar(
            select(IntelJob).where(IntelJob.dashboard_job_id == inp.dashboard_job_id)
        )
        if existing is not None:
            if existing.workspace_id != inp.workspace_id:
                raise Conflict("dashboard_job_id sudah dipakai workspace lain")
            return c.JobAcceptedOut(
                mcp_job_id=existing.id, estimate=c.Estimate.model_validate(existing.estimate)
            )
        profiles = await validate_stage_agents(session, inp.spec)
    estimate, prepared = await build_estimate(ctx.services, inp.spec, profiles)
    if estimate.requires_confirmation and not inp.confirm_cost:
        raise CostConfirmationRequired(
            "Estimasi biaya perlu dikonfirmasi (confirm_cost=true)",
            details={
                "usd_external": sum(s.usd_external for s in estimate.sources),
                "usd_llm": estimate.llm.usd_est,
            },
        )
    stage_agents = {stage: profile.agent_key for stage, profile in profiles.items()}
    index_s = INDEX_TIMEOUT_S if "index" in profiles else 0
    timeout = min(
        MAX_JOB_TIMEOUT_S, sum(p.timeout_s for p in prepared) + ANALYSIS_TIMEOUT_S + index_s
    )
    job_id = new_job_id()
    async with ctx.services.db.session() as session:
        # job induk + intel_jobs satu transaksi: worker tak bisa ambil job tanpa barisnya
        session.add(
            Job(
                id=job_id,
                kind=KIND,
                params={"dashboard_job_id": str(inp.dashboard_job_id)},
                client_id=uuid.UUID(ctx.client.id) if ctx.client.id else None,
                timeout_s=timeout,
                max_attempts=1,
                dedupe_key=f"intel:{inp.dashboard_job_id}",
                estimate={
                    "seconds": estimate.seconds,
                    "external_units": sum(s.external_units for s in estimate.sources),
                    "usd": round(
                        sum(s.usd_external for s in estimate.sources) + estimate.llm.usd_est, 4
                    ),
                },
            )
        )
        await session.flush()
        session.add(
            IntelJob(
                id=job_id,
                dashboard_job_id=inp.dashboard_job_id,
                workspace_id=inp.workspace_id,
                client_id=uuid.UUID(ctx.client.id) if ctx.client.id else None,
                requested_by=inp.requested_by.model_dump(mode="json"),
                spec=inp.spec.model_dump(mode="json"),
                stage_agents=stage_agents,
                stages=initial_stages(stage_agents),
                estimate=estimate.model_dump(mode="json"),
            )
        )
    ctx.job_id = job_id
    log.info("intel.job_created", mcp_job_id=job_id, dashboard_job_id=str(inp.dashboard_job_id))
    return c.JobAcceptedOut(mcp_job_id=job_id, estimate=estimate)


@tool(
    name="intel_job_get",
    toolset=TOOLSET,
    summary="Status, tahap, hasil & biaya job riset",
    description="Get a research job: status per stage, final insight result and actual cost.",
    input_model=c.JobRefIn,
    output_model=c.JobView,
    machine_output=True,
)
async def intel_job_get(inp: c.JobRefIn, ctx: ToolContext) -> c.JobView:
    intel = await _reconcile(ctx, await _intel_job(ctx, inp.workspace_id, inp.mcp_job_id))
    return _view(intel)


@tool(
    name="intel_job_cancel",
    toolset=TOOLSET,
    summary="Batalkan job riset (idempoten)",
    description="Request cancellation of a research job and its child jobs. Idempotent.",
    input_model=c.JobRefIn,
    output_model=c.CancelOut,
    machine_output=True,
    writes=True,
)
async def intel_job_cancel(inp: c.JobRefIn, ctx: ToolContext) -> c.CancelOut:
    intel = await _intel_job(ctx, inp.workspace_id, inp.mcp_job_id)
    if intel.status in c.TERMINAL_STATUSES:
        return c.CancelOut(cancelled=False, status=intel.status)  # type: ignore[arg-type]
    queue = ctx.services.jobs
    if queue is None:
        raise ValidationFailed("Job queue tidak aktif di proses ini")
    await queue.cancel_job(intel.id)
    async with ctx.services.db.session() as session:
        parent = await session.get(Job, intel.id)
    tracker = IntelTracker(ctx.services, intel.id)
    if parent is not None and parent.status == "cancelled":
        await tracker.cancelled()  # masih di antrean: pipeline tidak akan pernah berjalan
        status = "cancelled"
    else:
        status = await tracker.mark_cancelling()
    return c.CancelOut(cancelled=True, status=status)  # type: ignore[arg-type]


@tool(
    name="intel_job_purge_index",
    toolset=TOOLSET,
    summary="Hapus dokumen KB hasil job riset (saat job dihapus)",
    description="Remove the job's documents (items + summary) from the workspace KB collection "
    "intel:<workspace_id>. Idempotent; items shared with an earlier job stay with that job.",
    input_model=c.JobRefIn,
    output_model=c.PurgeIndexOut,
    machine_output=True,
    writes=True,
)
async def intel_job_purge_index(inp: c.JobRefIn, ctx: ToolContext) -> c.PurgeIndexOut:
    intel = await _intel_job(ctx, inp.workspace_id, inp.mcp_job_id)
    docs, chunks = await kb_index.purge_job(ctx.services, intel.workspace_id, intel.id)
    log.info("intel.index_purged", mcp_job_id=intel.id, documents=docs, chunks=chunks)
    return c.PurgeIndexOut(deleted_documents=docs, deleted_chunks=chunks)


PROJECTION_CACHE_TTL_S = 3_600


@tool(
    name="intel_kb_projection",
    toolset=TOOLSET,
    summary="Awan titik 3D (PCA) dari embedding item job di Knowledge Base",
    description="3D point cloud for the research visualization: PCA (3 components) over the KB "
    "embeddings of the job's items, with source, sentiment and a short label per point. Cached "
    "per job once the job is finished.",
    input_model=c.ProjectionIn,
    output_model=c.ProjectionOut,
    machine_output=True,
)
async def intel_kb_projection(inp: c.ProjectionIn, ctx: ToolContext) -> c.ProjectionOut:
    intel = await _intel_job(ctx, inp.workspace_id, inp.mcp_job_id)
    key = f"intel_projection:{intel.id}:{inp.limit}"
    final = intel.status in c.TERMINAL_STATUSES
    if final and (hit := await ctx.services.cache.get(key)) is not None:
        return c.ProjectionOut.model_validate(hit)
    rows = await kb_index.projection_rows(ctx.services, intel.workspace_id, intel.id, inp.limit)
    coords = await asyncio.to_thread(pca_3d, [r["vector"] for r in rows])
    out = c.ProjectionOut(
        points=[
            c.ProjectionPoint(
                id=r["id"],
                x=round(x, 5),
                y=round(y, 5),
                z=round(z, 5),
                source=r["source"],
                sentiment=r["sentiment"],
                label=r["label"],
            )
            for r, (x, y, z) in zip(rows, coords, strict=True)
        ],
        generated_at=now(),
    )
    if final and rows:
        await ctx.services.cache.set(
            key, "intel", out.model_dump(mode="json"), PROJECTION_CACHE_TTL_S
        )
    return out


@tool(
    name="intel_items",
    toolset=TOOLSET,
    summary="Halaman data mentah job riset (filter sumber/sentimen/teks)",
    description="Paginated raw items of a research job with source/sentiment/text filters.",
    input_model=c.ItemsIn,
    output_model=c.ItemsOut,
    machine_output=True,
)
async def intel_items(inp: c.ItemsIn, ctx: ToolContext) -> c.ItemsOut:
    intel = await _intel_job(ctx, inp.workspace_id, inp.mcp_job_id)
    if not intel.items_handle:
        return c.ItemsOut(cols=ITEMS_PAGE_COLS, rows=[], total=0)
    flt: dict[str, Any] = {}
    if inp.filter.source:
        flt["source"] = {"op": "eq", "value": inp.filter.source}
    if inp.filter.sentiment:
        flt["sentiment"] = {"op": "eq", "value": inp.filter.sentiment}
    if inp.filter.q:
        flt["text"] = {"op": "icontains", "value": inp.filter.q}
    try:
        page = await ctx.services.results.page(
            intel.items_handle,
            Owner(client_id=str(intel.client_id) if intel.client_id else None),
            fields=ITEMS_PAGE_COLS,
            flt=flt,
            order_by=ORDER_COLUMN[inp.order_by],
            limit=inp.limit,
            cursor=inp.cursor,
            text_max_chars=c.SUMMARY_MD_MAX_CHARS,
        )
    except NotFound:
        raise ResultExpired("Data mentah job sudah kedaluwarsa (retensi MCP)") from None
    expires = (intel.result or {}).get("items_expire_at")
    return c.ItemsOut(
        cols=page["items"]["cols"],
        rows=page["items"]["rows"],
        total=page["total"],
        next_cursor=page["next_cursor"],
        expires_at=expires,
    )


# ---------------------------------------------------------------------------- LLM


async def _project_usage(ctx: ToolContext, start: date, end: date) -> c.ProjectUsage | None:
    llm = ctx.settings.llm
    url = f"{llm.llm_gateway_url.rstrip('/')}/usage"
    # key `system` (scope usage:read) bila sudah diprovisikan, selain itu key env
    key = (
        (await ctx.services.llm.key_for(None)).key
        if ctx.services.llm is not None
        else llm.llm_gateway_api_key.get_secret_value()
    )
    try:
        resp = await ctx.services.http.get(
            url,
            params={"start": start.isoformat(), "end": end.isoformat()},
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - Gateway tak terjangkau: pemakaian project tidak tersedia
        log.warning("intel.gateway_usage_unavailable", error=type(exc).__name__)
        return None
    if resp.status_code != 200:
        return None
    body = resp.json()
    return c.ProjectUsage(
        slug=body.get("project"), start=start, end=end, data=body.get("data") or []
    )


@tool(
    name="intel_llm_status",
    toolset=TOOLSET,
    summary="Pemakaian LLM per agent (token, biaya) + pemakaian project Gateway",
    description="LLM usage per agent (calls, tokens, estimated USD) and LLM Gateway project usage.",
    input_model=c.LlmStatusIn,
    output_model=c.LlmStatusOut,
    machine_output=True,
)
async def intel_llm_status(inp: c.LlmStatusIn, ctx: ToolContext) -> c.LlmStatusOut:
    since = now() - timedelta(days=inp.range_days)
    async with ctx.services.db.session() as session:
        profiles = (await session.scalars(select(AgentProfile.agent_key))).all()
        rows = (
            await session.execute(
                select(
                    LlmCall.agent_key,
                    LlmCall.route,
                    func.count(),
                    func.coalesce(func.sum(LlmCall.prompt_tokens), 0),
                    func.coalesce(func.sum(LlmCall.completion_tokens), 0),
                    func.sum(LlmCall.cost_usd),
                )
                .where(LlmCall.created_at >= since, LlmCall.agent_key.is_not(None))
                .group_by(LlmCall.agent_key, LlmCall.route)
            )
        ).all()
    usage: dict[str, dict[str, Any]] = {
        k: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "usd": 0.0} for k in profiles
    }
    llm = ctx.settings.llm
    for agent_key, route, calls, prompt, completion, cost_usd in rows:
        u = usage.setdefault(
            str(agent_key), {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "usd": 0.0}
        )
        u["calls"] += int(calls)
        u["prompt_tokens"] += int(prompt)
        u["completion_tokens"] += int(completion)
        tokens = int(prompt) + int(completion)
        usd = (
            float(cost_usd)
            if isinstance(cost_usd, Decimal)
            else usd_for_tokens(tokens, route_tier(route, llm))
        )
        u["usd"] = round(u["usd"] + usd, 4)
    creds, enabled = await _credentials(ctx)

    def agent_status(agent_key: str, u: dict[str, Any]) -> c.AgentLlmStatus:
        source, cred = key_info(creds, agent_key, enabled=enabled)
        return c.AgentLlmStatus(
            agent_key=agent_key,
            key_source=source,
            key_prefix=cred.key_prefix if cred else None,
            key_status=cred.status if cred else "unknown",  # type: ignore[arg-type]
            last_error=cred.last_error if cred else None,
            usage=c.AgentUsage(**u),
        )

    today = now().date()
    return c.LlmStatusOut(
        agents=[agent_status(k, u) for k, u in sorted(usage.items())],
        project=await _project_usage(ctx, today - timedelta(days=inp.range_days - 1), today),
    )


# ---------------------------------------------------------------------------- misi (M3_TASK_013)


@tool(
    name="intel_mission_plan",
    toolset=TOOLSET,
    summary="Rencana misi agent: task + subtask bertanggal di dalam timeline",
    description="Generate a dated task/subtask plan for a Dashboard mission with the planner "
    "agent's LLM route and Gateway key. Validated deterministically; does not write tasks.",
    input_model=c.MissionPlanIn,
    output_model=c.MissionPlanOut,
    machine_output=True,
    timeout_s=MISSION_PLAN_TIMEOUT_S,
)
async def intel_mission_plan(inp: c.MissionPlanIn, ctx: ToolContext) -> c.MissionPlanOut:
    if ctx.services.llm is None:
        raise ValidationFailed("LLM Gateway tidak dikonfigurasi")
    async with ctx.services.db.session() as session:
        profile = await get_active_profile(session, inp.agent_key)
        active_keys = set(
            (
                await session.scalars(
                    select(AgentProfile.agent_key).where(AgentProfile.is_active.is_(True))
                )
            ).all()
        )
    if profile is None:
        raise UnknownAgent(f"Agent {inp.agent_key} tidak dikenal atau nonaktif di MCP")
    if "task" not in profile.toolsets:
        raise AgentNotCapable("Agent planner wajib memiliki toolset task")
    # hanya agent yang profilnya aktif di MCP yang boleh ditugasi (mis. bukan "assistant")
    agents = [k for k in inp.available_agents if k in active_keys] or [profile.agent_key]
    start, end = inp.period.start_date, inp.period.end_date
    prompt = mission_plan.build_prompt(
        title=inp.title,
        instruction=inp.instruction,
        start=start,
        end=end,
        today=today_in(ctx.settings.timezone),
        context=inp.context.model_dump(mode="json"),
        available_agents=agents,
        feedback=inp.feedback,
        previous_plan=inp.previous_plan,
    )
    budget = BudgetScope(
        layers=[BudgetLayer(f"mission:{profile.agent_key}", profile.run_token_budget)]
    )
    with context.context_scope(
        agent_key=profile.agent_key, caller_task=f"mission:{inp.mission_id}"
    ):
        plan, tokens = await mission_plan.generate_plan(
            ctx.services, prompt=prompt, start=start, end=end, route=profile.route, budget=budget
        )
    ctx.llm_tokens += tokens
    final = mission_plan.finalize_plan(
        plan, default_agent_key=profile.agent_key, allowed_agents={*agents, profile.agent_key}
    )
    usd = usd_for_tokens(tokens, route_tier(profile.route, ctx.settings.llm))
    return c.MissionPlanOut(plan=final, cost=c.MissionPlanCost(llm_tokens=tokens, usd=usd))


# ---------------------------------------------------------------------------- biaya run agent


@tool(
    name="intel_run_costs",
    toolset=TOOLSET,
    summary="Biaya LLM per run agent (token & USD) untuk budget misi",
    description="LLM tokens and estimated USD per agent run (Dashboard run ids) in a workspace.",
    input_model=c.RunCostsIn,
    output_model=c.RunCostsOut,
    machine_output=True,
)
async def intel_run_costs(inp: c.RunCostsIn, ctx: ToolContext) -> c.RunCostsOut:
    async with ctx.services.db.session() as session:
        # hanya run milik workspace tsb (run_id Dashboard = mcp.runs.run_id)
        owned = set(
            (
                await session.scalars(
                    select(Run.run_id).where(
                        Run.run_id.in_(inp.run_ids), Run.workspace_id == inp.workspace_id
                    )
                )
            ).all()
        )
        rows = (
            (
                await session.execute(
                    select(
                        LlmCall.run_id,
                        LlmCall.route,
                        func.coalesce(func.sum(LlmCall.prompt_tokens), 0),
                        func.coalesce(func.sum(LlmCall.completion_tokens), 0),
                        func.sum(LlmCall.cost_usd),
                    )
                    .where(LlmCall.run_id.in_(owned))
                    .group_by(LlmCall.run_id, LlmCall.route)
                )
            ).all()
            if owned
            else []
        )
    llm = ctx.settings.llm
    per_run: dict[str, dict[str, float]] = {}
    for run_id, route, prompt, completion, cost_usd in rows:
        tokens = int(prompt) + int(completion)
        usd = (
            float(cost_usd)
            if isinstance(cost_usd, Decimal)
            else usd_for_tokens(tokens, route_tier(route, llm))
        )
        agg = per_run.setdefault(str(run_id), {"tokens": 0, "usd": 0.0})
        agg["tokens"] += tokens
        agg["usd"] += usd
    runs = [
        c.RunCost(run_id=rid, llm_tokens=int(v["tokens"]), usd=round(v["usd"], 6))
        for rid, v in sorted(per_run.items())
    ]
    return c.RunCostsOut(runs=runs, total_usd=round(sum(r.usd for r in runs), 6))


# ---------------------------------------------------------------------------- profil browser

LOGIN_JOB_KIND = "browser_profile_login"
LOGIN_JOB_TIMEOUT_S = 1_200  # jendela login ≤ 15 menit + cek login


def _profile_info(p: Any) -> c.BrowserProfileInfo:
    view = profile_view(p)
    checked = {
        site: str(v.get("checked_at"))
        for site, v in (p.login_status or {}).items()
        if isinstance(v, dict) and v.get("checked_at")
    }
    return c.BrowserProfileInfo(
        name=view["name"],
        purpose=view["purpose"],
        allowed_sites=view["allowed_sites"],
        logged_in_sites=view["logged_in_sites"],
        login_checked_at=checked,
        headless=view["headless"],
        locked=view["locked"],
        last_used=view["last_used"],
    )


def _canonical_sites(sites: list[str]) -> list[str]:
    return list(dict.fromkeys(SITE_ALIASES.get(s, s) for s in sites))


@tool(
    name="intel_browser_profiles",
    toolset=TOOLSET,
    summary="Profil Chrome MCP + status login per situs",
    description="List active MCP browser profiles with allowed sites and login status.",
    input_model=c.BrowserProfilesIn,
    output_model=c.BrowserProfilesOut,
    machine_output=True,
)
async def intel_browser_profiles(
    inp: c.BrowserProfilesIn, ctx: ToolContext
) -> c.BrowserProfilesOut:
    return c.BrowserProfilesOut(
        profiles=[_profile_info(p) for p in await profile_store(ctx.services).list()]
    )


@tool(
    name="intel_browser_profile_create",
    toolset=TOOLSET,
    summary="Buat profil Chrome baru (headless saat scraping) untuk login manual",
    description="Create a browser profile (headless for scraping) to be logged in manually.",
    input_model=c.BrowserProfileCreateIn,
    output_model=c.BrowserProfileInfo,
    machine_output=True,
    writes=True,
)
async def intel_browser_profile_create(
    inp: c.BrowserProfileCreateIn, ctx: ToolContext
) -> c.BrowserProfileInfo:
    profile = await profile_store(ctx.services).add(
        name=inp.name,
        purpose="Login manual dari Dashboard",
        sites=_canonical_sites(list(inp.sites)),
        headless=True,
    )
    return _profile_info(profile)


@tool(
    name="intel_browser_login_start",
    toolset=TOOLSET,
    summary="Buka jendela Chrome di mesin MCP untuk login manual ke profil",
    description="Open a normal Chrome window on the MCP host for manual login into a profile; "
    "login status is checked after the window is closed.",
    input_model=c.BrowserLoginStartIn,
    output_model=c.BrowserLoginOut,
    machine_output=True,
    writes=True,
)
async def intel_browser_login_start(
    inp: c.BrowserLoginStartIn, ctx: ToolContext
) -> c.BrowserLoginOut:
    await profile_store(ctx.services).get(inp.profile)
    if ctx.services.jobs is None:
        raise ValidationFailed("Job queue tidak aktif di proses ini")
    accepted = await ctx.services.jobs.enqueue(
        LOGIN_JOB_KIND,
        {"profile": inp.profile, "sites": _canonical_sites(list(inp.sites))},
        client_id=ctx.client.id,
        timeout_s=LOGIN_JOB_TIMEOUT_S,
        dedupe_key=f"browser-login:{inp.profile}",  # satu jendela login per profil
        max_attempts=1,
    )
    return c.BrowserLoginOut(job_id=accepted.job_id, profile=inp.profile, status=accepted.status)


async def _login_job(ctx: ToolContext, job_id: str) -> Job:
    if ctx.services.jobs is None:
        raise ValidationFailed("Job queue tidak aktif di proses ini")
    owner = JobOwner(client_id=ctx.client.id, run_id=None, is_admin=ctx.client.is_admin)
    job = await ctx.services.jobs.get(job_id, owner)
    if job.kind != LOGIN_JOB_KIND:
        raise NotFound(f"Job login {job_id} tidak ditemukan")
    return job


def _login_view(job: Job) -> c.BrowserLoginOut:
    result = job.result or {}
    status = (
        job.status if job.status in ("queued", "running", "succeeded", "failed") else "cancelled"
    )
    return c.BrowserLoginOut(
        job_id=job.id,
        profile=str((job.params or {}).get("profile") or ""),
        status=status,  # type: ignore[arg-type]
        message=job.message,
        checks=[c.BrowserLoginCheck(**chk) for chk in result.get("checks") or []],
        closed_by_user=result.get("closed_by_user"),
        error=str((job.error or {}).get("message") or "") or None,
    )


@tool(
    name="intel_browser_login_get",
    toolset=TOOLSET,
    summary="Status sesi login manual profil Chrome",
    description="Status of a manual browser login session (job) and login checks when finished.",
    input_model=c.BrowserLoginJobIn,
    output_model=c.BrowserLoginOut,
    machine_output=True,
)
async def intel_browser_login_get(inp: c.BrowserLoginJobIn, ctx: ToolContext) -> c.BrowserLoginOut:
    return _login_view(await _login_job(ctx, inp.job_id))


@tool(
    name="intel_browser_login_cancel",
    toolset=TOOLSET,
    summary="Batalkan sesi login manual (jendela Chrome ditutup)",
    description="Cancel a manual browser login session; the Chrome window is closed.",
    input_model=c.BrowserLoginJobIn,
    output_model=c.BrowserLoginOut,
    machine_output=True,
    writes=True,
)
async def intel_browser_login_cancel(
    inp: c.BrowserLoginJobIn, ctx: ToolContext
) -> c.BrowserLoginOut:
    job = await _login_job(ctx, inp.job_id)
    assert ctx.services.jobs is not None
    await ctx.services.jobs.cancel_job(job.id)
    return _login_view(await _login_job(ctx, inp.job_id))
