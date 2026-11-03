"""Toolset `job`: status/tunggu/batal/daftar job asinkron (MCP_SPEC §2.4, §4.1, T12)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.schemas.common import Table, ToolInput
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.worker.queue import JobOwner, JobQueue, job_view

JOB_PATTERN = r"^j_[0-9a-z]{10}$"
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


def _queue(ctx: ToolContext) -> JobQueue:
    if ctx.services.jobs is None:
        raise ValidationFailed("Job queue tidak aktif di proses ini")
    return ctx.services.jobs


def _owner(ctx: ToolContext) -> JobOwner:
    return JobOwner(client_id=ctx.client.id, run_id=ctx.run_id, is_admin=ctx.client.is_admin)


class Progress(BaseModel):
    done: int
    total: int | None = None


class JobStatusOut(BaseModel):
    summary: str
    job_id: str
    kind: str
    status: JobStatus
    progress: Progress
    message: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def _status_out(job: Any) -> JobStatusOut:
    view = job_view(job)
    done, total = view["progress"]["done"], view["progress"]["total"]
    pct = f" {done}/{total}" if total else ""
    return JobStatusOut(summary=f"Job {job.id} ({job.kind}) {job.status}{pct}", **view)


class JobStatusIn(ToolInput):
    job_id: str = Field(pattern=JOB_PATTERN)


@tool(
    name="job_status",
    toolset="job",
    summary="Status & progres job; hasil hanya saat selesai",
    description="Get job status and progress. The full result (handle + summary) is returned only "
    "once the job has succeeded.",
    input_model=JobStatusIn,
    output_model=JobStatusOut,
)
async def job_status(inp: JobStatusIn, ctx: ToolContext) -> JobStatusOut:
    return _status_out(await _queue(ctx).get(inp.job_id, _owner(ctx)))


class JobWaitIn(ToolInput):
    job_id: str = Field(pattern=JOB_PATTERN)
    timeout_s: int = Field(default=15, ge=0, le=20)


@tool(
    name="job_wait",
    toolset="job",
    summary="Tunggu job selesai (maks 20 s) tanpa polling berulang",
    description="Block up to timeout_s (<=20) until the job finishes, then return the same payload "
    "as job_status. Avoids repeated polling.",
    input_model=JobWaitIn,
    output_model=JobStatusOut,
    timeout_s=24,
)
async def job_wait(inp: JobWaitIn, ctx: ToolContext) -> JobStatusOut:
    return _status_out(await _queue(ctx).wait(inp.job_id, _owner(ctx), inp.timeout_s))


class JobCancelOut(BaseModel):
    summary: str
    cancelled: bool
    jobs: list[str] = Field(default_factory=list)


@tool(
    name="job_cancel",
    toolset="job",
    summary="Batalkan job beserta job turunannya",
    description="Request cancellation of a job and all its child jobs.",
    input_model=JobStatusIn,
    output_model=JobCancelOut,
    writes=True,
)
async def job_cancel(inp: JobStatusIn, ctx: ToolContext) -> JobCancelOut:
    queue = _queue(ctx)
    await queue.get(inp.job_id, _owner(ctx))  # cek kepemilikan
    ids = await queue.cancel_job(inp.job_id)
    return JobCancelOut(summary=f"{len(ids)} job dibatalkan", cancelled=bool(ids), jobs=ids)


class JobListIn(ToolInput):
    status: JobStatus | None = None
    kind: str | None = Field(default=None, max_length=32)
    limit: int = Field(default=10, ge=1, le=20)


class JobListOut(BaseModel):
    summary: str
    items: Table


@tool(
    name="job_list",
    toolset="job",
    summary="Daftar job milik klien/run ini",
    description="List recent jobs owned by this client or run, optionally filtered by status/kind.",
    input_model=JobListIn,
    output_model=JobListOut,
)
async def job_list(inp: JobListIn, ctx: ToolContext) -> JobListOut:
    jobs = await _queue(ctx).list_jobs(
        _owner(ctx), status=inp.status, kind=inp.kind, limit=inp.limit
    )
    cols = ["job_id", "kind", "status", "done", "total", "created_at"]
    rows = [
        [
            j.id,
            j.kind,
            j.status,
            j.progress_done,
            j.progress_total,
            j.created_at.isoformat() if j.created_at else None,
        ]
        for j in jobs
    ]
    return JobListOut(summary=f"{len(rows)} job", items=Table(cols=cols, rows=rows))
