"""Eksekusi job anak di dalam job induk `intel_pipeline` (tanpa memakai slot worker lain).

Setiap sumber dijalankan sebagai baris `mcp.jobs` sendiri (`parent_job_id` = induk) agar hasil
koleksi tidak tercampur (mis. `job_posts` per job), pembatalan rekursif berlaku, dan riwayat job
tetap terlihat di `job_list`. Baris anak langsung berstatus `running` + dikunci induk sehingga
worker lain tidak mengambilnya; heartbeat dijaga di sini agar tidak di-reap.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp_marketing.core.errors import ToolError, UpstreamTimeout, to_tool_error
from mcp_marketing.models import Job
from mcp_marketing.utils.ids import new_job_id
from mcp_marketing.utils.time import now
from mcp_marketing.worker.handlers.base import Handler, JobCancelled, JobCtx, JobResult

CHILD_HEARTBEAT_S = 10.0

OnProgress = Callable[[int, int | None, str | None], Awaitable[None]]


@dataclass
class ChildCtx(JobCtx):
    on_progress: OnProgress | None = None

    async def progress(
        self, done: int, total: int | None = None, message: str | None = None
    ) -> None:
        await super().progress(done, total, message)
        if self.on_progress is not None:
            await self.on_progress(done, total, message)


def child_lock(parent_job_id: str) -> str:
    return f"intel:{parent_job_id}"


async def _heartbeat(parent: JobCtx, child_id: str, lock: str) -> None:
    while True:
        await asyncio.sleep(CHILD_HEARTBEAT_S)
        await parent.queue.heartbeat(child_id, lock)


async def run_child(
    parent: JobCtx,
    handler: Handler,
    params: dict[str, Any],
    *,
    timeout_s: int,
    on_progress: OnProgress | None = None,
) -> JobResult:
    """Jalankan `handler` sebagai job anak; status akhir dicatat di baris anak lalu diteruskan."""
    lock = child_lock(parent.job_id)
    child_id = new_job_id()
    started = now()
    async with parent.services.db.session() as session:
        session.add(
            Job(
                id=child_id,
                kind=handler.kind,
                status="running",
                params=params,
                client_id=uuid.UUID(parent.client_id) if parent.client_id else None,
                parent_job_id=parent.job_id,
                locked_by=lock,
                locked_at=started,
                heartbeat_at=started,
                started_at=started,
                attempts=1,
                max_attempts=1,
                timeout_s=timeout_s,
            )
        )
    ctx = ChildCtx(
        job_id=child_id,
        kind=handler.kind,
        params=dict(params),
        checkpoint={},
        attempts=1,
        client_id=parent.client_id,
        run_id=None,
        services=parent.services,
        queue=parent.queue,
        executor=parent.executor,
        on_progress=on_progress,
    )
    beat = asyncio.create_task(_heartbeat(parent, child_id, lock))
    try:
        result = await asyncio.wait_for(handler.run(ctx), timeout=timeout_s)
    except JobCancelled:
        await parent.queue.finish(child_id, lock, status="cancelled")
        raise
    except TimeoutError:
        error = UpstreamTimeout(f"Job {handler.kind} melebihi {timeout_s} s", retryable=False)
        await parent.queue.finish(child_id, lock, status="failed", error=error.to_structured())
        raise error from None
    except Exception as exc:
        err: ToolError = to_tool_error(exc)
        await parent.queue.finish(child_id, lock, status="failed", error=err.to_structured())
        raise err from exc
    finally:
        beat.cancel()
    await parent.queue.finish(
        child_id,
        lock,
        status="succeeded",
        result=result.result,
        result_handle=result.result_handle,
    )
    return result
