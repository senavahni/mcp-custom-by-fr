"""Worker: slot asyncio, heartbeat, timeout per job, retry, pembatalan, graceful shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor

from mcp_marketing.core import context
from mcp_marketing.core.errors import ToolError
from mcp_marketing.core.logging import get_logger
from mcp_marketing.core.metrics import JOBS_TOTAL
from mcp_marketing.models import Job
from mcp_marketing.server.services import Services
from mcp_marketing.worker.handlers.base import HandlerRegistry, JobCancelled, JobCtx
from mcp_marketing.worker.queue import JobQueue
from mcp_marketing.worker.scheduler import Scheduler

log = get_logger(__name__)

HEARTBEAT_S = 10.0
REAP_EVERY_S = 15.0
SCHEDULER_EVERY_S = 60.0
RETRY_BASE_DELAY_S = 30.0
SHUTDOWN_GRACE_S = 20.0


class Worker:
    def __init__(
        self,
        services: Services,
        queue: JobQueue,
        registry: HandlerRegistry,
        *,
        worker_id: str | None = None,
        concurrency: int = 2,
        poll_interval_s: float = 0.5,
        heartbeat_s: float = HEARTBEAT_S,
        scheduler: Scheduler | None = None,
        use_process_pool: bool = True,
    ) -> None:
        self.services = services
        self.queue = queue
        self.registry = registry
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.concurrency = concurrency
        self.poll_interval_s = poll_interval_s
        self.heartbeat_s = heartbeat_s
        self.scheduler = scheduler
        self.executor = ProcessPoolExecutor(max_workers=1) if use_process_pool else None
        self._running: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()

    def _forget(self, job_id: str) -> Callable[[asyncio.Task[None]], None]:
        def done(_task: asyncio.Task[None]) -> None:
            self._running.pop(job_id, None)

        return done

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info(
            "worker.start",
            worker_id=self.worker_id,
            concurrency=self.concurrency,
            kinds=self.registry.kinds(),
        )
        loop = asyncio.get_running_loop()
        next_reap = 0.0
        next_schedule = 0.0
        try:
            while not self._stop.is_set():
                t = loop.time()
                if t >= next_reap:
                    await self._safe(self.queue.reap())
                    next_reap = t + REAP_EVERY_S
                if self.scheduler is not None and t >= next_schedule:
                    await self._safe(self.scheduler.tick())
                    next_schedule = t + SCHEDULER_EVERY_S
                picked = False
                while len(self._running) < self.concurrency and not self._stop.is_set():
                    job = await self._safe(self.queue.pick(self.worker_id, self.registry.kinds()))
                    if job is None:
                        break
                    picked = True
                    task = asyncio.create_task(self._execute(job))
                    self._running[job.id] = task
                    task.add_done_callback(self._forget(job.id))
                if not picked:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_s)
        finally:
            await self._shutdown()

    async def run_one(self) -> bool:
        """Ambil & jalankan tepat satu job (test/CLI). False bila antrean kosong."""
        job = await self.queue.pick(self.worker_id, self.registry.kinds())
        if job is None:
            return False
        await self._execute(job)
        return True

    async def _shutdown(self) -> None:
        if self._running:
            log.info("worker.draining", jobs=list(self._running))
            done, pending = await asyncio.wait(
                list(self._running.values()), timeout=SHUTDOWN_GRACE_S
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)
        log.info("worker.stopped", worker_id=self.worker_id)

    @staticmethod
    async def _safe(coro):  # type: ignore[no-untyped-def]
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001 - loop worker tidak boleh mati karena satu error
            log.exception("worker.loop_error", error=type(exc).__name__)
            return None

    async def _heartbeat(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                still_mine = await self.queue.heartbeat(job_id, self.worker_id)
            except Exception as exc:  # noqa: BLE001 - gangguan DB sesaat: coba lagi tick berikutnya
                log.warning("worker.heartbeat_failed", job_id=job_id, error=type(exc).__name__)
                continue
            if not still_mine:
                return

    async def _execute(self, job: Job) -> None:
        handler = self.registry.get(job.kind)
        ctx = JobCtx(
            job_id=job.id,
            kind=job.kind,
            params=dict(job.params),
            checkpoint=dict(job.checkpoint or {}),
            attempts=job.attempts,
            client_id=str(job.client_id) if job.client_id else None,
            run_id=str(job.run_id) if job.run_id else None,
            services=self.services,
            queue=self.queue,
            executor=self.executor,
        )
        beat = asyncio.create_task(self._heartbeat(job.id))
        with context.context_scope(job_id=job.id, run_id=ctx.run_id, client_id=ctx.client_id):
            try:
                if handler is None:
                    raise ToolError(f"Handler job '{job.kind}' tidak terdaftar", code="JOB_FAILED")
                log.info("job.start", kind=job.kind, attempt=job.attempts)
                result = await asyncio.wait_for(handler.run(ctx), timeout=job.timeout_s)
                status = "cancelled" if await ctx.is_cancelled() else "succeeded"
                await self.queue.finish(
                    job.id,
                    self.worker_id,
                    status=status,
                    result=result.result,
                    result_handle=result.result_handle,
                )
                log.info("job.done", kind=job.kind, status=status)
            except JobCancelled:
                await self._finish_partial(job, ctx, "cancelled", None)
            except TimeoutError:
                await self._finish_partial(
                    job,
                    ctx,
                    "failed",
                    {
                        "code": "JOB_TIMEOUT",
                        "message": f"Job melebihi {job.timeout_s} s",
                        "retryable": False,
                    },
                )
            except asyncio.CancelledError:
                # shutdown worker: kembalikan ke antrean, checkpoint tetap
                await self.queue.release(job.id, self.worker_id)
                raise
            except Exception as exc:  # noqa: BLE001 - semua kegagalan dicatat ke job
                await self._handle_failure(job, exc)
            finally:
                beat.cancel()
                JOBS_TOTAL.labels(job.kind, "done").inc()

    async def _finish_partial(
        self, job: Job, ctx: JobCtx, status: str, error: dict[str, object] | None
    ) -> None:
        partial = ctx.partial_result
        result = {**partial.result, "partial": True} if partial else None
        await self.queue.finish(
            job.id,
            self.worker_id,
            status=status,
            result=result,
            result_handle=partial.result_handle if partial else None,
            error=error,
        )
        log.info("job.done", kind=job.kind, status=status, partial=partial is not None)

    async def _handle_failure(self, job: Job, exc: Exception) -> None:
        if isinstance(exc, ToolError):
            error = exc.to_structured()
            retryable = exc.retryable
        else:
            log.exception("job.internal_error", kind=job.kind, error_type=type(exc).__name__)
            error = {"code": "INTERNAL", "message": "Kesalahan internal job", "retryable": True}
            retryable = True
        if retryable and job.attempts < job.max_attempts:
            delay = RETRY_BASE_DELAY_S * job.attempts
            log.warning(
                "job.retry", kind=job.kind, attempt=job.attempts, delay_s=delay, code=error["code"]
            )
            await self.queue.requeue(job.id, self.worker_id, delay_s=delay, error=error)
            return
        await self.queue.finish(job.id, self.worker_id, status="failed", error=error)
        log.warning("job.failed", kind=job.kind, code=error["code"])
