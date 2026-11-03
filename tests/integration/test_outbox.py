"""TASK-08: outbox webhook — urutan per run, retry, 409 discard, restart, emit_and_wait, signature."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from mcp_marketing.core.errors import ToolError
from mcp_marketing.models import Run, WebhookOutbox
from mcp_marketing.server.services import Services
from mcp_marketing.services.dashboard.outbox import Outbox
from tests.integration.helpers import FakeDashboard, make_run, status


@pytest.fixture
def dashboard(services: Services) -> FakeDashboard:
    return FakeDashboard(secret=services.settings.dashboard.mcp_webhook_secret.get_secret_value())


@pytest.fixture
def outbox(services: Services, dashboard: FakeDashboard) -> Outbox:
    return Outbox(services.db, dashboard.client(), services.settings)


async def _rows(services: Services, run_id: str) -> list[WebhookOutbox]:
    async with services.db.session() as s:
        return list(
            (
                await s.scalars(
                    select(WebhookOutbox)
                    .where(WebhookOutbox.run_id == uuid.UUID(run_id))
                    .order_by(WebhookOutbox.seq)
                )
            ).all()
        )


async def _drain(outbox: Outbox, rounds: int = 20) -> None:
    for _ in range(rounds):
        if not await outbox.send_due():
            return


async def test_emit_assigns_seq_and_envelope(
    services: Services, outbox: Outbox, dashboard: FakeDashboard
) -> None:
    run_id = await make_run(services)
    a = await outbox.emit(run_id, "agent.task.started", {})
    b = await outbox.emit(
        run_id, "agent.status.changed", {"status": "working", "message": "Membaca brief"}
    )
    assert (a.seq, b.seq) == (1, 2)
    await _drain(outbox)
    assert [p["event_type"] for p in dashboard.received] == [
        "agent.task.started",
        "agent.status.changed",
    ]
    env = dashboard.received[0]
    assert (
        env["contract_version"] == "1.0"
        and env["run_id"] == run_id
        and env["occurred_at"].endswith("Z")
    )
    assert env["task_id"] is not None
    assert [r.status for r in await _rows(services, run_id)] == ["delivered", "delivered"]


async def test_invalid_event_data_rejected_before_queue(services: Services, outbox: Outbox) -> None:
    run_id = await make_run(services)
    with pytest.raises(ToolError) as exc:
        await outbox.emit(run_id, "agent.status.changed", {"status": "idle"})
    assert exc.value.code == "VALIDATION_ERROR"
    assert await _rows(services, run_id) == []


async def test_retry_500_500_200_same_event_id(
    services: Services, outbox: Outbox, dashboard: FakeDashboard
) -> None:
    run_id = await make_run(services)
    dashboard.responses = [status(500), status(503)]
    await outbox.emit(run_id, "agent.task.started", {})
    for _ in range(3):
        await outbox.send_due()
        async with services.db.session() as s:  # lewati backoff
            await s.execute(update(WebhookOutbox).values(next_attempt_at=datetime.now(UTC)))
    rows = await _rows(services, run_id)
    assert rows[0].status == "delivered" and rows[0].attempts == 3
    assert len({p["event_id"] for p in dashboard.received}) == 1 and len(dashboard.received) == 3


async def test_seq2_waits_while_seq1_in_backoff(
    services: Services, outbox: Outbox, dashboard: FakeDashboard
) -> None:
    run_id = await make_run(services)
    other_run = await make_run(services)
    dashboard.responses = [status(500)]
    await outbox.emit(run_id, "agent.task.started", {})
    await outbox.emit(run_id, "agent.status.changed", {"status": "working"})
    await outbox.send_due()  # seq1 -> 500 (backoff)
    await outbox.emit(other_run, "agent.task.started", {})
    await outbox.send_due()
    await outbox.send_due()
    sent = [(p["run_id"], p["event_type"]) for p in dashboard.received]
    assert (run_id, "agent.status.changed") not in sent  # seq2 tidak mendahului seq1
    assert (other_run, "agent.task.started") in sent  # run lain tidak terblokir
    rows = await _rows(services, run_id)
    assert [r.status for r in rows] == ["pending", "pending"] and rows[0].attempts == 1


async def test_409_run_not_active_discards_all_pending(
    services: Services, outbox: Outbox, dashboard: FakeDashboard
) -> None:
    run_id = await make_run(services)
    dashboard.responses = [status(409, "RUN_NOT_ACTIVE")]
    for i in range(3):
        await outbox.emit(run_id, "agent.status.changed", {"status": "working", "message": f"m{i}"})
    await _drain(outbox)
    assert [r.status for r in await _rows(services, run_id)] == ["discarded"] * 3
    assert len(dashboard.received) == 1
    with pytest.raises(ToolError) as exc:  # tidak ada event setelah 409
        await outbox.emit(run_id, "agent.task.started", {})
    assert exc.value.code == "RUN_NOT_ACTIVE"
    async with services.db.session() as s:
        run = await s.get(Run, uuid.UUID(run_id))
    assert run is not None and run.status == "cancelled_by_dashboard"


@pytest.mark.parametrize(
    "code,expected_status", [(400, "failed"), (422, "failed"), (404, "failed"), (401, "failed")]
)
async def test_non_retryable_responses(
    services: Services, outbox: Outbox, dashboard: FakeDashboard, code: int, expected_status: str
) -> None:
    run_id = await make_run(services)
    dashboard.responses = [status(code, "VALIDATION_ERROR")]
    await outbox.emit(run_id, "agent.task.started", {})
    await _drain(outbox)
    rows = await _rows(services, run_id)
    assert rows[0].status == expected_status and rows[0].last_http_status == code
    assert len(dashboard.received) == 1


async def test_wrong_secret_gets_401(services: Services, dashboard: FakeDashboard) -> None:
    run_id = await make_run(services)
    dashboard.secret = "secret-lain-yang-dipakai-dashboard-xxxxxxxx"
    outbox = Outbox(services.db, dashboard.client(), services.settings)
    await outbox.emit(run_id, "agent.task.started", {})
    await _drain(outbox)
    assert (await _rows(services, run_id))[0].last_http_status == 401


async def test_gives_up_after_max_retry_window(
    services: Services, outbox: Outbox, dashboard: FakeDashboard
) -> None:
    run_id = await make_run(services)
    dashboard.responses = [status(502)]
    await outbox.emit(run_id, "agent.task.started", {})
    async with services.db.session() as s:
        await s.execute(
            update(WebhookOutbox).values(first_attempt_at=datetime.now(UTC) - timedelta(minutes=31))
        )
    await outbox.send_due()
    assert (await _rows(services, run_id))[0].status == "failed"


async def test_restart_with_pending_events_delivers_in_order(
    services: Services, dashboard: FakeDashboard
) -> None:
    run_id = await make_run(services)
    first = Outbox(services.db, dashboard.client(), services.settings)
    for i in range(5):
        await first.emit(
            run_id, "agent.status.changed", {"status": "working", "message": f"langkah {i}"}
        )
    async with services.db.session() as s:  # simulasi crash saat mengirim seq 1
        await s.execute(
            update(WebhookOutbox).where(WebhookOutbox.seq == 1).values(status="sending")
        )
    restarted = Outbox(services.db, dashboard.client(), services.settings)
    await _drain(restarted)
    assert [p["data"]["message"] for p in dashboard.received] == [f"langkah {i}" for i in range(5)]
    assert {r.status for r in await _rows(services, run_id)} == {"delivered"}


async def test_dashboard_down_10_minutes_then_up(
    services: Services, outbox: Outbox, dashboard: FakeDashboard
) -> None:
    run_id = await make_run(services)
    dashboard.responses = [status(503)] * 6
    for i in range(3):
        await outbox.emit(run_id, "agent.status.changed", {"status": "working", "message": str(i)})
    for _ in range(12):
        await outbox.send_due()
        async with services.db.session() as s:
            await s.execute(update(WebhookOutbox).values(next_attempt_at=datetime.now(UTC)))
    assert [p["data"]["message"] for p in dashboard.processed_order] == ["0", "1", "2"]
    assert len(dashboard.processed) == 3
    assert {r.status for r in await _rows(services, run_id)} == {"delivered"}


async def test_emit_and_wait_returns_dashboard_result(services: Services, outbox: Outbox) -> None:
    run_id = await make_run(services)
    sender = asyncio.create_task(outbox.run_sender())
    try:
        result = await outbox.emit_and_wait(
            run_id,
            "agent.subtask.created",
            {
                "subtasks": [
                    {"client_ref": "week-1", "title": "Week 1"},
                    {"client_ref": "week-2", "title": "Week 2"},
                ]
            },
            timeout_s=5,
        )
        assert [c["client_ref"] for c in result["created"]] == ["week-1", "week-2"]
        comment = await outbox.emit_and_wait(
            run_id, "agent.comment.added", {"content": "Selesai"}, timeout_s=5
        )
        assert "comment_id" in comment
    finally:
        outbox.stop()
        await sender


async def test_emit_and_wait_rejection_raises(
    services: Services, outbox: Outbox, dashboard: FakeDashboard
) -> None:
    run_id = await make_run(services)
    dashboard.responses = [status(422, "HIERARCHY_DEPTH_EXCEEDED")]
    sender = asyncio.create_task(outbox.run_sender())
    try:
        with pytest.raises(ToolError) as exc:
            await outbox.emit_and_wait(
                run_id, "agent.subtask.created", {"subtasks": [{"title": "x"}]}, timeout_s=5
            )
        assert exc.value.details["dashboard_code"] == "HIERARCHY_DEPTH_EXCEEDED"  # type: ignore[index]
    finally:
        outbox.stop()
        await sender


async def test_emit_refused_after_cancel(services: Services, outbox: Outbox) -> None:
    run_id = await make_run(services, cancel_requested=True)
    with pytest.raises(ToolError) as exc:
        await outbox.emit(run_id, "agent.task.started", {})
    assert exc.value.code == "RUN_NOT_ACTIVE"
    async with services.db.session() as s:
        assert (
            await s.scalar(select(Run.status).where(Run.run_id == uuid.UUID(run_id))) == "running"
        )
