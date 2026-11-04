"""Toolset `intel` (kontrak 1.2, Modul 3 Dashboard): job riset multi-tahap untuk Dashboard.

Hanya untuk klien `dashboard-intel` (toolset `intel`); output dibaca mesin (`machine_output`).
Dashboard melakukan RBAC; MCP mewajibkan `workspace_id` dan menolak job workspace lain.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from mcp_marketing.core.errors import (
    Conflict,
    CostConfirmationRequired,
    NotFound,
    ResultExpired,
    ValidationFailed,
)
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import AgentProfile, IntelJob, Job, LlmCall
from mcp_marketing.schemas import intel as c
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.browser.driver_pool import profile_store
from mcp_marketing.services.intel.analysis import ITEMS_PAGE_COLS
from mcp_marketing.services.intel.capabilities import capabilities, validate_stage_agents
from mcp_marketing.services.intel.estimate import build_estimate, route_tier, usd_for_tokens
from mcp_marketing.services.intel.pipeline import KIND
from mcp_marketing.services.intel.tracker import IntelTracker, initial_stages
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.utils.ids import new_job_id
from mcp_marketing.utils.time import now

log = get_logger(__name__)

TOOLSET = "intel"
ANALYSIS_TIMEOUT_S = 3_600
MAX_JOB_TIMEOUT_S = 6 * 3_600
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
                gateway_key=c.GatewayKeyInfo(source="env"),
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
            available=default is not None,
            mode="browser" if default is not None else "none",
            detail="profil browser 'default'" if default else "profil browser 'default' belum ada",
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
    stage_agents = {
        "collect": profiles["collect"].agent_key,
        "analyze": profiles["analyze"].agent_key,
    }
    timeout = min(MAX_JOB_TIMEOUT_S, sum(p.timeout_s for p in prepared) + ANALYSIS_TIMEOUT_S)
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
    try:
        resp = await ctx.services.http.get(
            url,
            params={"start": start.isoformat(), "end": end.isoformat()},
            headers={"Authorization": f"Bearer {llm.llm_gateway_api_key.get_secret_value()}"},
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
    today = now().date()
    return c.LlmStatusOut(
        agents=[
            c.AgentLlmStatus(
                agent_key=k, key_source="env", key_status="unknown", usage=c.AgentUsage(**u)
            )
            for k, u in sorted(usage.items())
        ],
        project=await _project_usage(ctx, today - timedelta(days=inp.range_days - 1), today),
    )
