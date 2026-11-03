"""TASK-09 & TASK-10: tool kontrak pm_*, registry run, estimasi, dan agent runtime end-to-end."""

from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from mcp_marketing.core.errors import UpstreamError
from mcp_marketing.models import AgentProfile, Run, RunStep, WebhookOutbox
from mcp_marketing.server.app import build_asgi_app
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.pipeline import ToolOutcome, ToolPipeline
from mcp_marketing.server.services import Services
from mcp_marketing.services.agent.budget_store import db_budget_manager
from mcp_marketing.services.agent.runtime import AgentRuntime
from mcp_marketing.services.dashboard.agent_api import AgentApiClient
from mcp_marketing.services.dashboard.outbox import Outbox
from mcp_marketing.services.seed import seed
from mcp_marketing.worker.queue import JobQueue
from tests.fakes import ScriptedLLM, reply
from tests.integration.conftest import LiveServer, mcp_session
from tests.integration.helpers import AGENT_API, WEBHOOK, FakeDashboard

SAMPLE = json.loads(
    (Path(__file__).resolve().parents[1] / "contract" / "dispatch_sample.json").read_text("utf-8")
)
DASHBOARD = ClientIdentity(
    id=None, name="dashboard", kind="system", toolsets=frozenset({"pm"}), rpm_limit=10_000
)


@dataclass
class AgentEnv:
    services: Services
    pipeline: ToolPipeline
    runtime: AgentRuntime
    llm: ScriptedLLM
    dashboard: FakeDashboard

    async def call(self, tool: str, args: dict[str, Any]) -> ToolOutcome:
        return await self.pipeline.call(tool, args, client=DASHBOARD)

    async def dispatch(self, **over: Any) -> tuple[str, ToolOutcome]:
        payload = copy.deepcopy(SAMPLE)
        payload["run_id"] = str(uuid.uuid4())
        payload["callback"] = {"webhook_url": WEBHOOK, "agent_api_base_url": AGENT_API}
        payload.update(over)
        return payload["run_id"], await self.call("pm_dispatch_task_to_agent", payload)

    async def finish(self, run_id: str, timeout: float = 15) -> Run:
        await self.runtime.wait(run_id, timeout=timeout)
        await self.drain_outbox()
        async with self.services.db.session() as s:
            run = await s.get(Run, uuid.UUID(run_id))
        assert run is not None
        return run

    async def drain_outbox(self) -> None:
        assert self.services.outbox is not None
        for _ in range(50):
            async with self.services.db.session() as s:
                pending = await s.scalar(
                    select(func.count())
                    .select_from(WebhookOutbox)
                    .where(WebhookOutbox.status.in_(("pending", "sending")))
                )
            if not pending:
                return
            await asyncio.sleep(0.05)


@pytest_asyncio.fixture
async def env(services: Services) -> AsyncIterator[AgentEnv]:
    async with services.db.session() as s:
        await seed(s)
    dashboard = FakeDashboard(
        secret=services.settings.dashboard.mcp_webhook_secret.get_secret_value()
    )
    http = dashboard.client()
    services.budgets = db_budget_manager(services.db)
    services.outbox = Outbox(services.db, http, services.settings)
    services.agent_api = AgentApiClient(services.settings, http)
    services.jobs = JobQueue(services.db)
    llm = ScriptedLLM()
    services.llm = llm  # type: ignore[assignment]
    _, pipeline, _ = build_asgi_app(services)
    runtime = AgentRuntime(services, pipeline, llm=llm)
    services.runtime = runtime
    sender = asyncio.create_task(services.outbox.run_sender())
    yield AgentEnv(services, pipeline, runtime, llm, dashboard)
    await runtime.shutdown()
    services.outbox.stop()
    await sender
    await pipeline.drain()
    await http.aclose()


# ====================================================================== TASK-09: pm_*
async def test_dashboard_key_sees_exactly_five_pm_tools(
    services: Services, live_server: LiveServer
) -> None:
    key = services.settings.dashboard.mcp_server_api_key.get_secret_value()
    async with mcp_session(live_server.url, key) as client:
        names = sorted(t.name for t in (await client.list_tools()).tools)
    assert names == [
        "pm_cancel_agent_run",
        "pm_dispatch_task_to_agent",
        "pm_execute_prompt_block",
        "pm_notify_agent_mention",
        "pm_request_agent_estimation",
    ]


async def test_dispatch_idempotent_and_started_before_reply(env: AgentEnv) -> None:
    env.llm.script = [asyncio.Event()]  # tahan runtime agar tidak selesai
    run_id, first = await env.dispatch()
    assert first.is_error is False and first.structured["accepted"] is True
    async with env.services.db.session() as s:
        rows = (
            await s.scalars(select(WebhookOutbox).where(WebhookOutbox.run_id == uuid.UUID(run_id)))
        ).all()
    assert rows and rows[0].seq == 1 and rows[0].event_type == "agent.task.started"
    payload = copy.deepcopy(SAMPLE) | {
        "run_id": run_id,
        "callback": {"webhook_url": WEBHOOK, "agent_api_base_url": AGENT_API},
    }
    again = await env.call("pm_dispatch_task_to_agent", payload)
    assert again.structured["mcp_run_id"] == first.structured["mcp_run_id"]
    async with env.services.db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Run)) == 1


async def test_dispatch_rejections(env: AgentEnv) -> None:
    _, unknown = await env.dispatch(agent_key="tidak-ada")
    assert unknown.is_error and unknown.structured["code"] == "UNKNOWN_AGENT"
    assert unknown.structured["retryable"] is False
    _, v2 = await env.dispatch(contract_version="2.0")
    assert (
        v2.structured["code"] == "UNSUPPORTED_CONTRACT_VERSION"
        and v2.structured["retryable"] is False
    )
    _, bad = await env.dispatch(agent_id="bukan-uuid")
    assert bad.structured["code"] == "VALIDATION_ERROR"
    forbidden = await env.pipeline.call(
        "pm_cancel_agent_run",
        {"contract_version": "1.0", "run_id": str(uuid.uuid4())},
        client=ClientIdentity(id=None, name="x", kind="external", toolsets=frozenset({"data"})),
    )
    assert forbidden.structured["code"] == "FORBIDDEN"


async def test_dispatch_latency_p95_under_1s(env: AgentEnv) -> None:
    timings = []
    for _ in range(10):
        env.llm.script = [asyncio.Event()]
        t = time.perf_counter()
        _, out = await env.dispatch()
        timings.append(time.perf_counter() - t)
        assert out.is_error is False
    timings.sort()
    assert timings[8] < 1.0


async def test_dispatch_of_finished_run_conflicts(env: AgentEnv) -> None:
    env.llm.script = [reply(("finish", {"summary": "ok"}))]
    run_id, _ = await env.dispatch()
    await env.finish(run_id)
    payload = copy.deepcopy(SAMPLE) | {
        "run_id": run_id,
        "callback": {"webhook_url": WEBHOOK, "agent_api_base_url": AGENT_API},
    }
    out = await env.call("pm_dispatch_task_to_agent", payload)
    assert out.structured["code"] == "CONFLICT" and out.structured["retryable"] is False


def _estimation_payload(**over: Any) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "run_id": str(uuid.uuid4()),
        "agent_key": "strategist",
        "task": SAMPLE["task"],
        "task_description": "Buat kalender konten 3 minggu untuk launching produk.",
        "context_blocks": [],
        "earliest_start_date": "2026-10-30",
        "timezone": "Asia/Jakarta",
    } | over


async def test_estimation_with_llm_and_workdays(env: AgentEnv) -> None:
    env.llm.json_reply = {"estimated_days": 3, "confidence": 0.8, "message": "Siap, 3 hari kerja."}
    out = await env.call("pm_request_agent_estimation", _estimation_payload())
    sc = out.structured
    assert out.is_error is False
    assert (
        sc["start_date"] == "2026-10-30" and sc["end_date"] == "2026-11-03"
    )  # Jumat, Senin, Selasa
    assert (
        sc["estimated_days"] == 3
        and sc["confidence"] == 0.8
        and sc["message"] == "Siap, 3 hari kerja."
    )
    assert env.llm.calls[-1]["purpose"] == "estimate"


async def test_estimation_clamped_and_fallback(env: AgentEnv) -> None:
    env.llm.json_reply = {"estimated_days": 40, "confidence": 0.9, "message": "40 hari"}
    clamped = (await env.call("pm_request_agent_estimation", _estimation_payload())).structured
    assert clamped["estimated_days"] == 8  # heuristik 4 hari x 2
    assert "40" not in clamped["message"]
    env.llm.json_reply = UpstreamError("gateway down")
    fallback = (
        await env.call(
            "pm_request_agent_estimation", _estimation_payload(earliest_start_date="2026-10-31")
        )
    ).structured
    assert (
        fallback["confidence"] <= 0.4 and fallback["start_date"] == "2026-11-02"
    )  # Sabtu -> Senin


async def test_mention_rate_limit_eleventh_rejected(env: AgentEnv) -> None:
    task_id = SAMPLE["task"]["id"]
    codes = []
    for _ in range(11):
        env.llm.script = [asyncio.Event()]
        payload = {
            "contract_version": "1.0",
            "run_id": str(uuid.uuid4()),
            "agent_key": "strategist",
            "agent_id": SAMPLE["agent_id"],
            "task": SAMPLE["task"],
            "project_id": SAMPLE["task"]["project_id"],
            "comment": {
                "id": str(uuid.uuid4()),
                "content": "@Agent tolong cek",
                "author": {"type": "user"},
            },
            "thread": [],
            "context_blocks": [],
            "callback": {"webhook_url": WEBHOOK, "agent_api_base_url": AGENT_API},
        }
        out = await env.call("pm_notify_agent_mention", payload)
        codes.append(out.structured.get("code"))
    assert codes[:10] == [None] * 10 and codes[10] == "RATE_LIMITED"
    async with env.services.db.session() as s:
        assert (
            await s.scalar(
                select(func.count()).select_from(Run).where(Run.task_id == uuid.UUID(task_id))
            )
            == 10
        )


async def test_cancel_unknown_run_is_ok(env: AgentEnv) -> None:
    out = await env.call(
        "pm_cancel_agent_run",
        {
            "contract_version": "1.0",
            "run_id": str(uuid.uuid4()),
            "mcp_run_id": None,
            "reason": "unassigned",
        },
    )
    assert out.structured == {"cancelled": True}


# ====================================================================== TASK-10: runtime
async def test_uc01_content_calendar_end_to_end(env: AgentEnv) -> None:
    env.dashboard.context_results = [
        {
            "block_id": "b1",
            "task_id": None,
            "task_title": None,
            "source": "project_brief",
            "block_type": "paragraph",
            "text": "Tone santai, target Gen Z.",
            "rank": 0.5,
        }
    ]
    calendar = "## Minggu 1\n- Senin: teaser\n- [ ] Draft konten\n\n| Hari | Konten |\n|---|---|\n| Sen | Teaser |"
    env.llm.script = [
        reply(("pm_query_project_context", {"query": "tone brand"})),
        reply(("write_blocks", {"markdown": calendar})),
        reply(
            (
                "create_subtasks",
                {
                    "subtasks": [
                        {
                            "client_ref": f"week-{i}",
                            "title": f"Konten Minggu {i}",
                            "start_date": "2026-11-02",
                            "end_date": "2026-11-06",
                            "markdown": f"Fokus minggu {i}",
                        }
                        for i in (1, 2, 3)
                    ]
                },
            )
        ),
        reply(("finish", {"summary": "Kalender 3 minggu dan 3 subtask dibuat."})),
    ]
    run_id, out = await env.dispatch()
    assert out.structured["accepted"] is True
    run = await env.finish(run_id)
    assert run.status == "completed" and run.summary == "Kalender 3 minggu dan 3 subtask dibuat."
    assert run.tokens_used > 0 and run.steps == 4
    assert set(run.subtask_refs) == {"week-1", "week-2", "week-3"}
    events = env.dashboard.events(run_id)
    assert events[0] == "agent.task.started"
    assert events[1:3] == ["agent.status.changed", "agent.status.changed"]  # thinking -> working
    assert events[3:] == [
        "agent.block.appended",
        "agent.subtask.created",
        "agent.comment.added",
        "agent.task.completed",
    ]
    blocks = next(
        p for p in env.dashboard.processed_order if p["event_type"] == "agent.block.appended"
    )
    assert [o["block"]["block_type"] for o in blocks["data"]["operations"]] == [
        "heading",
        "bulletListItem",
        "checkListItem",
        "table",
    ]
    # tool hanya dari toolset profil; pm_* kontrak tidak diberikan ke LLM
    tools = env.llm.agent_calls()[0]["tools"]
    assert "pm_query_project_context" in tools and "write_blocks" in tools
    assert not any(t.startswith("pm_dispatch") for t in tools)
    tool_msg = env.llm.agent_calls()[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "Tone santai" in tool_msg["content"]
    async with env.services.db.session() as s:
        roles = [r.role for r in (await s.scalars(select(RunStep).order_by(RunStep.step))).all()]
    assert roles.count("assistant") == 4 and roles.count("tool") == 4


async def test_budget_exhausted_mid_run_fails_cleanly(env: AgentEnv) -> None:
    async with env.services.db.session() as s:
        await s.execute(update(AgentProfile).values(run_token_budget=2_500))
    env.llm.script = [reply(("report_progress", {"message": "mulai"}), tokens=1_000)] * 5
    run_id, _ = await env.dispatch()
    run = await env.finish(run_id)
    assert run.status == "failed" and run.error["code"] == "BUDGET_EXCEEDED"  # type: ignore[index]
    failed = [p for p in env.dashboard.processed_order if p["event_type"] == "agent.task.failed"][0]
    assert (
        failed["data"]["error_code"] == "BUDGET_EXCEEDED" and failed["data"]["retryable"] is False
    )


async def test_max_steps(env: AgentEnv) -> None:
    async with env.services.db.session() as s:
        await s.execute(update(AgentProfile).values(max_steps=3))
    env.llm.script = [reply(("report_progress", {"message": f"langkah {i}"})) for i in range(10)]
    run_id, _ = await env.dispatch()
    run = await env.finish(run_id)
    assert run.status == "failed" and run.error["code"] == "MAX_STEPS"  # type: ignore[index]
    assert len(env.llm.agent_calls()) == 3


async def test_text_only_replies_nudged_then_auto_finish(env: AgentEnv) -> None:
    env.llm.script = [
        reply(content="Saya sedang berpikir"),
        reply(content="Masih berpikir"),
        reply(content="Jawaban akhir: gunakan tone santai."),
    ]
    run_id, _ = await env.dispatch()
    run = await env.finish(run_id)
    assert run.status == "completed"
    comments = [
        p["data"]["content"]
        for p in env.dashboard.processed_order
        if p["event_type"] == "agent.comment.added"
    ]
    assert comments[0] == "Jawaban akhir: gunakan tone santai."
    nudges = [m for m in env.llm.agent_calls()[-1]["messages"] if m["role"] == "user"]
    assert len(nudges) == 3  # brief + 2 dorongan


async def test_cancel_at_step_3_stops_without_more_webhooks(env: AgentEnv) -> None:
    gate = asyncio.Event()
    env.llm.script = [
        reply(("report_progress", {"message": "1"})),
        reply(("report_progress", {"message": "2"})),
        gate,
        reply(("write_blocks", {"markdown": "## tidak boleh terkirim"})),
    ]
    run_id, _ = await env.dispatch()
    for _ in range(100):
        if len(env.llm.agent_calls()) >= 3:
            break
        await asyncio.sleep(0.02)
    started = time.monotonic()
    out = await env.call(
        "pm_cancel_agent_run", {"contract_version": "1.0", "run_id": run_id, "reason": "unassigned"}
    )
    assert out.structured == {"cancelled": True}
    await env.finish(run_id, timeout=5)
    assert time.monotonic() - started < 5
    gate.set()
    await asyncio.sleep(0.2)
    async with env.services.db.session() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        statuses = set(
            (
                await s.scalars(
                    select(WebhookOutbox.status).where(WebhookOutbox.run_id == uuid.UUID(run_id))
                )
            ).all()
        )
    assert run is not None and run.status == "cancelled"
    assert "agent.block.appended" not in env.dashboard.events(run_id)
    assert "agent.task.completed" not in env.dashboard.events(run_id)
    assert statuses <= {"delivered", "discarded"}


async def test_forbidden_task_write_returned_to_llm(env: AgentEnv) -> None:
    env.llm.script = [
        reply(("write_blocks", {"markdown": "x", "task_id": str(uuid.uuid4())})),
        reply(("finish", {"summary": "ok"})),
    ]
    run_id, _ = await env.dispatch()
    await env.finish(run_id)
    tool_msg = env.llm.agent_calls()[1]["messages"][-1]
    assert '"FORBIDDEN"' in tool_msg["content"]
    assert "agent.block.appended" not in env.dashboard.events(run_id)


async def test_prompt_block_writes_after_prompt_block_in_light_mode(env: AgentEnv) -> None:
    prompt_id = str(uuid.uuid4())
    env.llm.script = [
        reply(("write_blocks", {"markdown": "## Ide\n- satu"})),
        reply(("write_blocks", {"markdown": "- dua"})),
        reply(("finish", {"summary": "ok"})),
    ]
    payload = {
        "contract_version": "1.0",
        "run_id": str(uuid.uuid4()),
        "agent_key": "strategist",
        "agent_id": SAMPLE["agent_id"],
        "task": SAMPLE["task"],
        "project_id": SAMPLE["task"]["project_id"],
        "prompt_block": {"id": prompt_id, "prompt": "Beri 3 ide caption", "version": 1},
        "context_blocks": [],
        "callback": {"webhook_url": WEBHOOK, "agent_api_base_url": AGENT_API},
    }
    out = await env.call("pm_execute_prompt_block", payload)
    run = await env.finish(payload["run_id"])
    assert out.is_error is False and run.status == "completed"
    appended = [
        p for p in env.dashboard.processed_order if p["event_type"] == "agent.block.appended"
    ]
    ops = [o for p in appended for o in p["data"]["operations"]]
    assert ops[0]["after_block_id"] == prompt_id
    for prev, cur in zip(ops, ops[1:], strict=False):
        assert cur["after_block_id"] == prev["block"]["id"]
    first_call = env.llm.agent_calls()[0]
    assert first_call["route_override"] == "fast"
    assert set(first_call["tools"]) <= {
        "pm_query_project_context",
        "kb_collections",
        "kb_search",
        "report_progress",
        "write_blocks",
        "create_subtasks",
        "post_comment",
        "finish",
        "fail",
    }
    assert "agent.comment.added" not in env.dashboard.events(payload["run_id"])


async def test_mention_reply_in_thread(env: AgentEnv) -> None:
    comment_id = str(uuid.uuid4())
    env.llm.script = [
        reply(("post_comment", {"content": "Siap, tone Week 2 jadi formal."})),
        reply(("finish", {"summary": "dibalas"})),
    ]
    payload = {
        "contract_version": "1.0",
        "run_id": str(uuid.uuid4()),
        "agent_key": "strategist",
        "agent_id": SAMPLE["agent_id"],
        "task": SAMPLE["task"],
        "project_id": SAMPLE["task"]["project_id"],
        "comment": {
            "id": comment_id,
            "parent_comment_id": None,
            "content": "@Agent ubah tone Week 2",
            "author": {"type": "user", "display_name": "Manager"},
        },
        "thread": [],
        "context_blocks": [],
        "callback": {"webhook_url": WEBHOOK, "agent_api_base_url": AGENT_API},
    }
    await env.call("pm_notify_agent_mention", payload)
    await env.finish(payload["run_id"])
    comments = [
        p for p in env.dashboard.processed_order if p["event_type"] == "agent.comment.added"
    ]
    assert len(comments) == 1 and comments[0]["data"]["parent_comment_id"] == comment_id


async def test_resume_after_restart_continues_from_next_step(env: AgentEnv) -> None:
    gate = asyncio.Event()
    env.llm.script = [
        reply(("report_progress", {"message": "langkah 1"})),
        reply(("pm_query_project_context", {"query": "brief"})),
        reply(("report_progress", {"message": "langkah 3"})),
        gate,
    ]
    env.dashboard.context_results = [
        {"block_id": "b", "text": "isi brief", "source": "project_brief"}
    ]
    run_id, _ = await env.dispatch()
    for _ in range(100):
        if len(env.llm.agent_calls()) >= 4:
            break
        await asyncio.sleep(0.02)
    await env.runtime.shutdown()  # "kill server" di langkah 4
    async with env.services.db.session() as s:
        assert (await s.get(Run, uuid.UUID(run_id))).status == "running"  # type: ignore[union-attr]
    llm2 = ScriptedLLM(script=[reply(("finish", {"summary": "dilanjutkan"}))])
    runtime2 = AgentRuntime(env.services, env.pipeline, llm=llm2)
    result = await runtime2.supervise_once()
    assert result["resumed"] == [run_id]
    await runtime2.wait(run_id, timeout=10)
    await env.drain_outbox()
    async with env.services.db.session() as s:
        run = await s.get(Run, uuid.UUID(run_id))
    assert run is not None and run.status == "completed" and run.steps == 4
    resumed_msgs = llm2.agent_calls()[0]["messages"]
    assert any(m["role"] == "tool" and "isi brief" in m["content"] for m in resumed_msgs)
    assert sum(1 for m in resumed_msgs if m["role"] == "assistant") == 3


async def test_supervisor_times_out_silent_run(env: AgentEnv) -> None:
    env.llm.script = [asyncio.Event()]
    run_id, _ = await env.dispatch()
    await env.drain_outbox()
    async with env.services.db.session() as s:
        await s.execute(
            update(Run)
            .where(Run.run_id == uuid.UUID(run_id))
            .values(
                last_event_at=datetime.now(UTC) - timedelta(minutes=200),
                started_at=datetime.now(UTC) - timedelta(minutes=200),
            )
        )
    result = await env.runtime.supervise_once()
    assert result["timed_out"] == [run_id]
    await env.drain_outbox()
    failed = [p for p in env.dashboard.processed_order if p["event_type"] == "agent.task.failed"]
    assert (
        failed
        and failed[0]["data"]["error_code"] == "RUN_TIMEOUT"
        and failed[0]["data"]["retryable"] is True
    )


async def test_history_compaction_keeps_handles(
    env: AgentEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env.services.settings.agent, "agent_history_compact_at_tokens", 1_000)
    long_progress = [
        reply(("report_progress", {"message": "x"}), content="analisis panjang " * 150)
        for _ in range(4)
    ]
    env.llm.script = [*long_progress, reply(("finish", {"summary": "ok"}))]
    run_id, _ = await env.dispatch()
    await env.finish(run_id)
    assert any(c["purpose"] == "compact" for c in env.llm.calls)
    last = env.llm.agent_calls()[-1]["messages"]
    assert any(
        m["role"] == "user"
        and m["content"].startswith("[Ringkasan")
        and "h_0123456789" in m["content"]
        for m in last
    )
    async with env.services.db.session() as s:
        assert (
            await s.scalar(
                select(func.count()).select_from(RunStep).where(RunStep.role == "summary")
            )
            >= 1
        )
