"""TASK-07: antrean, worker paralel, resume checkpoint, pembatalan berantai, wait, scheduler."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text, update

from mcp_marketing.core.errors import UpstreamError
from mcp_marketing.models import Job, Schedule, ToolCall, UsageDaily
from mcp_marketing.schemas.common import JobEstimate
from mcp_marketing.server.app import build_asgi_app
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.services import Services
from mcp_marketing.worker.handlers import default_registry
from mcp_marketing.worker.handlers.base import HandlerRegistry, JobCtx, JobResult
from mcp_marketing.worker.queue import JobOwner, JobQueue
from mcp_marketing.worker.runner import Worker
from mcp_marketing.worker.scheduler import Scheduler

CLIENT = "00000000-0000-0000-0000-0000000000c1"


class Recorder:
    kind = "record"

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=1)

    async def run(self, job: JobCtx) -> JobResult:
        self.seen.append(job.job_id)
        await asyncio.sleep(0.005)
        return JobResult({"summary": "ok", "n": job.params["n"]}, result_handle=None)


class DaysCrawler:
    """Simulasi crawl 30 hari dengan checkpoint per hari."""

    kind = "crawl_days"

    def __init__(self) -> None:
        self.processed: list[int] = []
        self.block_after: int | None = None
        self.blocked = asyncio.Event()

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=30)

    async def run(self, job: JobCtx) -> JobResult:
        start = int(job.checkpoint.get("last_day", 0)) + 1
        for day in range(start, 31):
            self.processed.append(day)
            await job.save_checkpoint({"last_day": day})
            await job.progress(day, 30)
            if self.block_after is not None and day == self.block_after:
                self.blocked.set()
                await asyncio.Event().wait()  # "hang" sampai worker dimatikan
        return JobResult({"summary": "30 hari selesai"})


class Looper:
    kind = "looper"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=60)

    async def run(self, job: JobCtx) -> JobResult:
        while True:
            await job.raise_if_cancelled()
            await asyncio.sleep(0.1)


class Slow:
    kind = "slow"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=5)

    async def run(self, job: JobCtx) -> JobResult:
        job.partial_result = JobResult({"summary": "sebagian", "total": 3})
        await asyncio.sleep(float(job.params.get("sleep", 5)))
        return JobResult({"summary": "selesai"})


class Flaky:
    kind = "flaky"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=1)

    async def run(self, job: JobCtx) -> JobResult:
        if job.attempts == 1:
            raise UpstreamError("sementara gagal")
        return JobResult({"summary": "berhasil di percobaan 2"})


def registry(*handlers: Any) -> HandlerRegistry:
    reg = HandlerRegistry()
    for h in handlers:
        reg.register(h)
    return reg


def make_worker(services: Services, reg: HandlerRegistry, wid: str, **kw: Any) -> Worker:
    queue = JobQueue(services.db, estimator=reg)
    return Worker(
        services, queue, reg, worker_id=wid, use_process_pool=False, poll_interval_s=0.05, **kw
    )


async def _wait_status(
    services: Services, job_id: str, statuses: tuple[str, ...], timeout: float = 10
) -> Job:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with services.db.session() as s:
            job = await s.get(Job, job_id)
        if job is not None and job.status in statuses:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} tidak mencapai {statuses}")


async def test_two_workers_each_job_exactly_once(services: Services) -> None:
    rec = Recorder()
    reg = registry(rec)
    queue = JobQueue(services.db, estimator=reg)
    ids = [(await queue.enqueue("record", {"n": i}, client_id=CLIENT)).job_id for i in range(100)]
    w1, w2 = (
        make_worker(services, reg, "w1", concurrency=4),
        make_worker(services, reg, "w2", concurrency=4),
    )
    tasks = [asyncio.create_task(w1.run()), asyncio.create_task(w2.run())]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        async with services.db.session() as s:
            done = await s.scalar(
                select(func.count()).select_from(Job).where(Job.status == "succeeded")
            )
        if done == 100:
            break
        await asyncio.sleep(0.1)
    w1.stop()
    w2.stop()
    await asyncio.gather(*tasks)
    assert sorted(rec.seen) == sorted(ids)
    assert len(rec.seen) == len(set(rec.seen)) == 100


async def test_dedupe_returns_existing_job(services: Services) -> None:
    queue = JobQueue(services.db)
    a = await queue.enqueue("record", {"n": 1}, client_id=CLIENT)
    b = await queue.enqueue("record", {"n": 1}, client_id=CLIENT)
    c = await queue.enqueue("record", {"n": 2}, client_id=CLIENT)
    assert a.job_id == b.job_id != c.job_id


async def test_killed_worker_job_reaped_and_resumed_from_checkpoint(services: Services) -> None:
    crawler = DaysCrawler()
    crawler.block_after = 12
    reg = registry(crawler)
    w1 = make_worker(services, reg, "dead-worker")
    job_id = (await w1.queue.enqueue("crawl_days", {"keyword": "kopi"}, client_id=CLIENT)).job_id

    async def no_release(job_id: str, worker_id: str) -> None:  # worker "mati" tanpa cleanup
        return None

    w1.queue.release = no_release  # type: ignore[method-assign]
    task = asyncio.create_task(w1.run_one())
    await asyncio.wait_for(crawler.blocked.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    async with services.db.session() as s:
        await s.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(heartbeat_at=datetime.now(UTC) - timedelta(minutes=2))
        )
    assert await w1.queue.reap() == 1
    crawler.block_after = None
    w2 = make_worker(services, reg, "w2")
    assert await w2.run_one()
    job = await _wait_status(services, job_id, ("succeeded",))
    assert crawler.processed == list(range(1, 31))
    assert job.checkpoint == {"last_day": 30} and job.attempts == 2


async def test_reaper_fails_job_after_max_attempts(services: Services) -> None:
    queue = JobQueue(services.db)
    job_id = (await queue.enqueue("record", {"n": 9}, client_id=CLIENT, max_attempts=1)).job_id
    await queue.pick("ghost")
    async with services.db.session() as s:
        await s.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(heartbeat_at=datetime.now(UTC) - timedelta(minutes=5))
        )
    await queue.reap()
    job = await _wait_status(services, job_id, ("failed",))
    assert job.error["code"] == "WORKER_LOST"  # type: ignore[index]


async def test_cancel_parent_cancels_running_child_within_5s(services: Services) -> None:
    reg = registry(Looper())
    worker = make_worker(services, reg, "w-cancel")
    run_id = str(uuid.uuid4())
    parent = (
        await worker.queue.enqueue("looper", {"p": 1}, client_id=CLIENT, run_id=run_id)
    ).job_id
    child = (
        await worker.queue.enqueue("looper", {"c": 1}, client_id=CLIENT, parent_job_id=parent)
    ).job_id
    grandchild = (
        await worker.queue.enqueue("looper", {"g": 1}, client_id=CLIENT, parent_job_id=child)
    ).job_id
    task = asyncio.create_task(worker.run())
    await _wait_status(services, parent, ("running",))
    await _wait_status(services, child, ("running",))
    started = time.monotonic()
    cancelled = await worker.queue.cancel_run_jobs(run_id)
    assert set(cancelled) == {parent, child, grandchild}
    for jid in (parent, child, grandchild):
        await _wait_status(services, jid, ("cancelled",), timeout=5)
    assert time.monotonic() - started <= 5
    worker.stop()
    await task


async def test_timeout_keeps_partial_result(services: Services) -> None:
    reg = registry(Slow())
    worker = make_worker(services, reg, "w-timeout")
    job_id = (
        await worker.queue.enqueue("slow", {"sleep": 5}, client_id=CLIENT, timeout_s=1)
    ).job_id
    await worker.run_one()
    job = await _wait_status(services, job_id, ("failed",))
    assert job.error["code"] == "JOB_TIMEOUT"  # type: ignore[index]
    assert job.result == {"summary": "sebagian", "total": 3, "partial": True}


async def test_retryable_error_requeued_then_succeeds(services: Services) -> None:
    reg = registry(Flaky())
    worker = make_worker(services, reg, "w-flaky")
    job_id = (await worker.queue.enqueue("flaky", {}, client_id=CLIENT)).job_id
    await worker.run_one()
    job = await _wait_status(services, job_id, ("queued",))
    assert job.error["code"] == "UPSTREAM_ERROR"  # type: ignore[index]
    async with services.db.session() as s:
        await s.execute(update(Job).where(Job.id == job_id).values(run_after=datetime.now(UTC)))
    await worker.run_one()
    assert (await _wait_status(services, job_id, ("succeeded",))).attempts == 2


async def test_job_wait_returns_quickly_after_completion(services: Services) -> None:
    reg = registry(Slow())
    worker = make_worker(services, reg, "w-wait")
    job_id = (await worker.queue.enqueue("slow", {"sleep": 1.0}, client_id=CLIENT)).job_id
    owner = JobOwner(client_id=CLIENT)
    waiter = asyncio.create_task(worker.queue.wait(job_id, owner, 15))
    await worker.run_one()
    finished = time.monotonic()
    job = await waiter
    assert job.status == "succeeded"
    assert time.monotonic() - finished < 1.0


async def test_scheduler_one_job_per_slot_with_two_schedulers(services: Services) -> None:
    queue = JobQueue(services.db)
    async with services.db.session() as s:
        s.add(
            Schedule(
                name="every-minute",
                cron="*/1 * * * *",
                job_kind="record",
                params={"n": 0},
                last_run_at=datetime(2026, 11, 3, 2, 59, tzinfo=UTC),
            )
        )
        s.add(Schedule(name="baru", cron="15 2 * * *", job_kind="record", params={"n": 1}))
    s1, s2 = Scheduler(services.db, queue), Scheduler(services.db, queue)
    at = datetime(2026, 11, 3, 3, 0, 30, tzinfo=UTC)
    first = await asyncio.gather(s1.tick(at), s2.tick(at))
    assert sum(len(x) for x in first) == 1
    assert await s1.tick(at + timedelta(seconds=20)) == []
    assert len(await s2.tick(at + timedelta(seconds=40))) == 1  # slot menit berikutnya
    async with services.db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Job)) == 2
        # jadwal baru (last_run_at NULL) tidak menjalankan slot 02:15 yang sudah lewat
        baru = await s.scalar(select(Schedule).where(Schedule.name == "baru"))
    assert baru is not None and baru.last_run_at is not None


async def test_requeue_of_cancelled_job_becomes_cancelled(services: Services) -> None:
    reg = registry(Flaky())
    worker = make_worker(services, reg, "w-cancel-requeue")
    job_id = (await worker.queue.enqueue("flaky", {}, client_id=CLIENT)).job_id
    picked = await worker.queue.pick("w-cancel-requeue")
    assert picked is not None
    await worker.queue.cancel_job(job_id)
    await worker.queue.requeue(job_id, "w-cancel-requeue", delay_s=0, error=None)
    job = await _wait_status(services, job_id, ("cancelled",))
    assert job.finished_at is not None


async def test_job_tools_ownership_and_t12(services: Services) -> None:
    from mcp_marketing.services.results.result_store import Owner

    reg = registry(Recorder())
    services.jobs = JobQueue(services.db, estimator=reg)
    worker = Worker(services, services.jobs, reg, worker_id="w-tools", use_process_pool=False)
    _, pipeline, _ = build_asgi_app(services)
    me = ClientIdentity(
        id=CLIENT, name="me", kind="external", toolsets=frozenset({"job"}), rpm_limit=1000
    )
    other = ClientIdentity(
        id="00000000-0000-0000-0000-0000000000c2",
        name="other",
        kind="external",
        toolsets=frozenset({"job"}),
        rpm_limit=1000,
    )
    rs = await services.results.create(
        kind="k", columns=["a"], rows=[{"a": 1}], owner=Owner(client_id=CLIENT)
    )
    job_id = (await services.jobs.enqueue("record", {"n": 5}, client_id=CLIENT)).job_id

    queued = (await pipeline.call("job_status", {"job_id": job_id}, client=me)).structured
    assert queued["status"] == "queued" and "result" not in queued
    assert (
        await pipeline.call("job_status", {"job_id": job_id}, client=other)
    ).error_code == "NOT_FOUND"

    async def finish_with_handle(job: JobCtx) -> JobResult:
        return JobResult({"summary": "1 baris", "total": 1}, result_handle=rs.handle)

    reg.get("record").run = finish_with_handle  # type: ignore[union-attr]
    await worker.run_one()
    done = (
        await pipeline.call("job_wait", {"job_id": job_id, "timeout_s": 2}, client=me)
    ).structured
    assert done["status"] == "succeeded" and done["result"]["handle"] == rs.handle
    listed = (await pipeline.call("job_list", {}, client=me)).structured
    assert listed["items"]["rows"][0][0] == job_id
    assert (await pipeline.call("job_list", {}, client=other)).structured["items"]["rows"] == []
    j2 = (await services.jobs.enqueue("record", {"n": 6}, client_id=CLIENT)).job_id
    cancel = (await pipeline.call("job_cancel", {"job_id": j2}, client=me)).structured
    assert cancel["cancelled"] is True
    await pipeline.drain()


async def test_maintenance_handlers(services: Services) -> None:
    reg = default_registry()
    worker = make_worker(services, reg, "w-maint")
    async with services.db.session() as s:
        for status in ("ok", "ok", "cached", "error"):
            s.add(
                ToolCall(
                    request_id="r",
                    tool="x_search",
                    args_hash="0" * 64,
                    status=status,
                    duration_ms=5,
                    output_tokens=100,
                    baseline_tokens=1000,
                    llm_tokens=10,
                )
            )
        await s.execute(
            text(
                "INSERT INTO mcp.cache_entries (cache_key, namespace, value, size_bytes, expires_at) "
                "VALUES (repeat('a', 64), 'tool', '{}', 2, now() - interval '1 hour')"
            )
        )
    for kind in ("usage_rollup", "cache_cleanup", "retention"):
        await worker.queue.enqueue(kind, {}, client_id=None)
        assert await worker.run_one()
    async with services.db.session() as s:
        row = await s.scalar(select(UsageDaily).where(UsageDaily.tool == "x_search"))
        statuses = set((await s.scalars(select(Job.status))).all())
        cache_left = await s.scalar(text("SELECT count(*) FROM mcp.cache_entries"))
    assert row is not None and (row.calls, row.errors, row.cache_hits, row.output_tokens) == (
        4,
        1,
        1,
        400,
    )
    assert statuses == {"succeeded"}
    assert cache_left == 0


@pytest.mark.parametrize(
    "at,expected",
    [
        (datetime(2026, 11, 3, 2, 59, 59, tzinfo=UTC), datetime(2026, 11, 3, 2, 59, tzinfo=UTC)),
        (datetime(2026, 11, 3, 3, 0, 0, tzinfo=UTC), datetime(2026, 11, 3, 3, 0, tzinfo=UTC)),
    ],
)
def test_latest_slot_inclusive(at: datetime, expected: datetime) -> None:
    from mcp_marketing.worker.scheduler import latest_slot

    assert latest_slot("* * * * *", at, "Asia/Jakarta") == expected
    # harian 02:15 WIB = 19:15Z hari sebelumnya
    assert latest_slot(
        "15 2 * * *", datetime(2026, 11, 3, 0, 0, tzinfo=UTC), "Asia/Jakarta"
    ) == datetime(2026, 11, 2, 19, 15, tzinfo=UTC)
