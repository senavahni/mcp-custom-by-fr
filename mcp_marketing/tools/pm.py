"""Toolset `pm` — kontrak Dashboard 1.0 (M1_MCP_INTEGRATION §3.1–3.5).

NAMA & SCHEMA TIDAK BOLEH DIUBAH.

Hanya klien sistem `dashboard`. Versi major ≠ 1 -> UNSUPPORTED_CONTRACT_VERSION (non-retryable).
"""

from __future__ import annotations

from mcp_marketing.core.errors import UnknownAgent, UnsupportedContractVersion
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import AgentProfile
from mcp_marketing.schemas.contract import (
    CancelInput,
    CancelOutput,
    ContractInput,
    DispatchInput,
    DispatchOutput,
    EstimationInput,
    EstimationOutput,
    MentionInput,
    PromptBlockInput,
    is_supported_contract,
)
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.agent import estimation
from mcp_marketing.services.agent.profiles import get_active_profile
from mcp_marketing.services.agent.runs import RunInput, RunRegistry
from mcp_marketing.services.dashboard.origin import require_dashboard_origin

log = get_logger(__name__)


def _check_version(inp: ContractInput) -> None:
    if not is_supported_contract(inp.contract_version):
        raise UnsupportedContractVersion(
            f"contract_version {inp.contract_version!r} tidak didukung (MCP mendukung 1.x)",
            retryable=False,
        )


async def _profile(ctx: ToolContext, agent_key: str) -> AgentProfile:
    async with ctx.services.db.session() as session:
        profile = await get_active_profile(session, agent_key)
    if profile is None:
        raise UnknownAgent(f"agent_key '{agent_key}' tidak dikenal/aktif di MCP", retryable=False)
    return profile


async def _start_run(trigger: str, inp: RunInput, ctx: ToolContext) -> DispatchOutput:
    _check_version(inp)
    base = ctx.settings.dashboard.dashboard_api_url
    require_dashboard_origin(inp.callback.webhook_url, base, "callback.webhook_url")
    require_dashboard_origin(inp.callback.agent_api_base_url, base, "callback.agent_api_base_url")
    profile = await _profile(ctx, inp.agent_key)
    registry = RunRegistry(ctx.services.db)
    accepted = await registry.accept(trigger, inp, profile)
    if accepted.created or accepted.needs_start:
        outbox = ctx.services.outbox
        if outbox is not None:
            # event pertama (seq 1) dicatat SEBELUM membalas Dashboard
            await outbox.emit(accepted.run_id, "agent.task.started", {})
        if ctx.services.runtime is not None:
            ctx.services.runtime.start(accepted.run_id)
    return DispatchOutput(accepted=True, mcp_run_id=accepted.mcp_run_id)


@tool(
    name="pm_dispatch_task_to_agent",
    toolset="pm",
    summary="Dashboard: tugaskan task ke agent (async)",
    description="Dashboard contract 1.0: accept a task assignment for an AI agent and start the "
    "run in "
    "the background. Idempotent per run_id.",
    input_model=DispatchInput,
    output_model=DispatchOutput,
    writes=True,
    timeout_s=14,
)
async def pm_dispatch_task_to_agent(inp: DispatchInput, ctx: ToolContext) -> DispatchOutput:
    return await _start_run("assignment", inp, ctx)


@tool(
    name="pm_request_agent_estimation",
    toolset="pm",
    summary="Dashboard: estimasi tanggal mulai/selesai task",
    description="Dashboard contract 1.0: estimate start/end dates (Mon-Fri workdays) for a task "
    "using a "
    "heuristic plus one fast LLM call. Does not modify the Dashboard.",
    input_model=EstimationInput,
    output_model=EstimationOutput,
    timeout_s=28,
)
async def pm_request_agent_estimation(inp: EstimationInput, ctx: ToolContext) -> EstimationOutput:
    _check_version(inp)
    await _profile(ctx, inp.agent_key)
    output, tokens = await estimation.estimate(inp, ctx.services.llm, budget=ctx.budget)
    ctx.llm_tokens += tokens
    return output


@tool(
    name="pm_notify_agent_mention",
    toolset="pm",
    summary="Dashboard: agent di-mention di komentar (async)",
    description="Dashboard contract 1.0: start a mention run; the agent replies in the same "
    "comment "
    "thread. Max 10 mention runs per task per hour.",
    input_model=MentionInput,
    output_model=DispatchOutput,
    writes=True,
    timeout_s=14,
)
async def pm_notify_agent_mention(inp: MentionInput, ctx: ToolContext) -> DispatchOutput:
    return await _start_run("mention", inp, ctx)


@tool(
    name="pm_execute_prompt_block",
    toolset="pm",
    summary="Dashboard: jalankan AI Prompt Block (async)",
    description="Dashboard contract 1.0: run an AI prompt block; results are appended after the "
    "prompt "
    "block via agent.block.appended.",
    input_model=PromptBlockInput,
    output_model=DispatchOutput,
    writes=True,
    timeout_s=14,
)
async def pm_execute_prompt_block(inp: PromptBlockInput, ctx: ToolContext) -> DispatchOutput:
    return await _start_run("prompt_block", inp, ctx)


@tool(
    name="pm_cancel_agent_run",
    toolset="pm",
    summary="Dashboard: batalkan run agent",
    description="Dashboard contract 1.0: stop an agent run, cancel its jobs and drop pending "
    "webhooks. "
    "Idempotent; unknown runs also return cancelled=true.",
    input_model=CancelInput,
    output_model=CancelOutput,
    writes=True,
    timeout_s=5,
)
async def pm_cancel_agent_run(inp: CancelInput, ctx: ToolContext) -> CancelOutput:
    _check_version(inp)
    known = await RunRegistry(ctx.services.db).mark_cancelled(inp.run_id, inp.reason)
    if known:
        if ctx.services.runtime is not None:
            ctx.services.runtime.cancel(inp.run_id)
        if ctx.services.jobs is not None:
            await ctx.services.jobs.cancel_run_jobs(inp.run_id)
        if ctx.services.outbox is not None:
            await ctx.services.outbox.discard_run(inp.run_id, reason=f"cancel:{inp.reason}")
    log.info("run.cancel", run_id=inp.run_id, known=known, reason=inp.reason)
    return CancelOutput(cancelled=True)
