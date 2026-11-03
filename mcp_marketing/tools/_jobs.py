"""Helper tool berbasis job: enqueue ke worker lalu tunggu ≤ `wait_s` (MCP_SPEC §2.4).

Selesai dalam batas waktu -> hasil akhir (envelope) langsung; selain itu `JobAccepted` dengan
estimasi agar pemanggil memakai `job_wait`/`job_status`.
"""

from __future__ import annotations

from typing import Any

from mcp_marketing.core.errors import JobFailed, ToolError, ValidationFailed
from mcp_marketing.schemas.common import JobAccepted
from mcp_marketing.server.registry import ToolContext
from mcp_marketing.worker.queue import JobOwner

MAX_WAIT_S = 20


async def enqueue_and_wait(
    ctx: ToolContext,
    kind: str,
    params: dict[str, Any],
    *,
    wait_s: int = 0,
    timeout_s: int | None = None,
    priority: int = 5,
) -> dict[str, Any] | JobAccepted:
    queue = ctx.services.jobs
    if queue is None:
        raise ValidationFailed("Job queue tidak aktif di proses ini")
    accepted = await queue.enqueue(
        kind,
        params,
        client_id=ctx.client.id,
        run_id=ctx.run_id,
        timeout_s=timeout_s,
        priority=priority,
    )
    ctx.job_id = accepted.job_id
    if wait_s <= 0:
        return accepted
    owner = JobOwner(client_id=ctx.client.id, run_id=ctx.run_id, is_admin=ctx.client.is_admin)
    job = await queue.wait(accepted.job_id, owner, min(MAX_WAIT_S, wait_s))
    if job.status == "succeeded":
        return {
            **(job.result or {}),
            "handle": job.result_handle or (job.result or {}).get("handle"),
        }
    if job.status == "failed":
        err = job.error or {}
        if err.get("code") and err.get("code") != "INTERNAL":
            raise ToolError(
                str(err.get("message") or "Job gagal"),
                code=err["code"],  # type: ignore[arg-type]
                retryable=bool(err.get("retryable")),
                details={"job_id": job.id},
            )
        raise JobFailed(f"Job {job.id} gagal", details={"job_id": job.id})
    return accepted
