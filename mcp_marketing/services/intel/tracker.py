"""Status job `intel` (baris `mcp.intel_jobs`) + event `intel.job.*` ke Dashboard."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select

from mcp_marketing.core.errors import NotFound
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import IntelJob
from mcp_marketing.schemas.intel import TERMINAL_STATUSES
from mcp_marketing.utils.time import now

log = get_logger(__name__)

STATUS_BY_STAGE = {"collect": "collecting", "analyze": "analyzing", "index": "indexing"}
PROGRESS_EVENT_STEP = 0.10
PROGRESS_EVENT_INTERVAL_S = 30.0
PROGRESS_DB_INTERVAL_S = 2.0


def _iso(dt: Any) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def initial_stages(stage_agents: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "stage": stage,
            "agent_key": agent_key,
            "status": "pending",
            "progress": {"done": 0, "total": None},
            "started_at": None,
            "finished_at": None,
            "error": None,
        }
        for stage, agent_key in stage_agents.items()
    ]


class IntelTracker:
    """Satu instance per eksekusi pipeline; semua perubahan status lewat sini."""

    def __init__(self, services: Any, job_id: str) -> None:
        self.services = services
        self.job_id = job_id
        self._last_event: dict[str, tuple[float, float]] = {}  # stage -> (rasio, waktu)
        self._last_db = 0.0

    async def load(self) -> IntelJob:
        async with self.services.db.session() as session:
            job = await session.get(IntelJob, self.job_id)
        if job is None:
            raise NotFound(f"Job intel {self.job_id} tidak ditemukan")
        return job

    async def _update(self, mutate: Any) -> IntelJob:
        async with self.services.db.session() as session:
            job = (
                await session.execute(
                    select(IntelJob).where(IntelJob.id == self.job_id).with_for_update()
                )
            ).scalar_one()
            mutate(job)
            job.updated_at = now()
        return job

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        outbox = self.services.outbox
        if outbox is None:
            log.warning("intel.outbox_missing", job_id=self.job_id, event_type=event_type)
            return
        await outbox.emit_job(self.job_id, event_type, data)

    @staticmethod
    def _set_stage(job: IntelJob, stage: str, **changes: Any) -> None:
        stages = [dict(s) for s in job.stages]
        for s in stages:
            if s["stage"] == stage:
                s.update(changes)
        job.stages = stages

    # ------------------------------------------------------------------ tahap
    async def stage_started(self, stage: str, agent_key: str) -> None:
        def mutate(job: IntelJob) -> None:
            job.status = STATUS_BY_STAGE[stage]
            self._set_stage(job, stage, status="running", started_at=_iso(now()))

        await self._update(mutate)
        await self.emit("intel.job.stage.started", {"stage": stage, "agent_key": agent_key})

    async def progress(
        self,
        stage: str,
        done: int,
        total: int | None,
        message: str | None,
        *,
        sample_tokens: list[str] | None = None,
    ) -> None:
        t = time.monotonic()
        if t - self._last_db >= PROGRESS_DB_INTERVAL_S or (total is not None and done >= total):
            self._last_db = t
            await self._update(
                lambda job: self._set_stage(job, stage, progress={"done": done, "total": total})
            )
        ratio = done / total if total else 0.0
        last_ratio, last_t = self._last_event.get(stage, (-1.0, 0.0))
        due = (
            ratio - last_ratio >= PROGRESS_EVENT_STEP
            or t - last_t >= PROGRESS_EVENT_INTERVAL_S
            or (total is not None and done >= total)
        )
        if not due:
            return
        self._last_event[stage] = (ratio, t)
        data: dict[str, Any] = {"stage": stage, "done": done, "total": total}
        if message:
            data["message"] = message[:300]
        if sample_tokens:
            data["sample_tokens"] = sample_tokens[-20:]
        await self.emit("intel.job.progress", data)

    async def stage_completed(
        self,
        stage: str,
        *,
        skipped: bool = False,
        stats: dict[str, Any] | None = None,
        warnings: list[dict[str, str]] | None = None,
    ) -> None:
        status = "skipped" if skipped else "completed"
        await self._update(
            lambda job: self._set_stage(job, stage, status=status, finished_at=_iso(now()))
        )
        data: dict[str, Any] = {"stage": stage, "status": status, "warnings": warnings or []}
        if stats is not None:
            data["stats"] = stats
        await self.emit("intel.job.stage.completed", data)

    # ------------------------------------------------------------------ akhir
    async def completed(self, result: dict[str, Any], cost: dict[str, Any]) -> None:
        def mutate(job: IntelJob) -> None:
            job.status = "completed"
            job.result = result
            job.items_handle = result.get("items_handle")
            job.cost = cost
            job.finished_at = now()

        await self._update(mutate)
        preview = (result.get("summary_md") or "").replace("\n", " ")[:500]
        await self.emit(
            "intel.job.completed",
            {
                "summary_preview": preview,
                "totals": result["totals"],
                "cost": cost,
                "partial": bool(result.get("partial")),
            },
        )

    async def failed(
        self,
        stage: str | None,
        error: dict[str, Any],
        *,
        result: dict[str, Any] | None = None,
        cost: dict[str, Any] | None = None,
    ) -> None:
        changed = False

        def mutate(job: IntelJob) -> None:
            nonlocal changed
            if job.status in TERMINAL_STATUSES:
                return
            changed = True
            job.status = "failed"
            job.error = {**error, "stage": stage}
            if result is not None:
                job.result = result
                job.items_handle = result.get("items_handle")
            if cost is not None:
                job.cost = cost
            job.finished_at = now()
            if stage is not None:
                self._set_stage(job, stage, status="failed", finished_at=_iso(now()), error=error)

        await self._update(mutate)
        if changed:
            await self.emit("intel.job.failed", {"stage": stage, "error": error})

    async def mark_cancelling(self) -> str:
        def mutate(job: IntelJob) -> None:
            if job.status not in TERMINAL_STATUSES:
                job.status = "cancelling"

        return (await self._update(mutate)).status

    async def cancelled(self) -> None:
        changed = False

        def mutate(job: IntelJob) -> None:
            nonlocal changed
            if job.status in TERMINAL_STATUSES:
                return
            changed = True
            job.status = "cancelled"
            job.finished_at = now()
            open_stages = [s["stage"] for s in job.stages if s["status"] in ("pending", "running")]
            for stage in open_stages:
                self._set_stage(job, stage, status="cancelled", finished_at=_iso(now()))

        await self._update(mutate)
        if changed:
            await self.emit("intel.job.cancelled", {})
