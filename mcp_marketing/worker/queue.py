"""Antrean job di PostgreSQL (`SELECT … FOR UPDATE SKIP LOCKED`), tanpa infrastruktur baru (D10)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import asyncpg
from sqlalchemy import case, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_marketing.core.database import Database, asyncpg_dsn
from mcp_marketing.core.errors import NotFound, ValidationFailed
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import Job
from mcp_marketing.schemas.common import JobAccepted, JobEstimate
from mcp_marketing.services.results.cache import canonical_json
from mcp_marketing.utils.crypto import sha256_hex
from mcp_marketing.utils.ids import new_job_id
from mcp_marketing.utils.text import truncate
from mcp_marketing.utils.time import now

log = get_logger(__name__)

NOTIFY_CHANNEL = "mcp_job_done"
STALE_HEARTBEAT_S = 60
POLL_FALLBACK_S = 0.5
MAX_WAIT_S = 20
TERMINAL = ("succeeded", "failed", "cancelled")

_DESCENDANTS = """
WITH RECURSIVE tree AS (
  SELECT id FROM mcp.jobs WHERE {root}
  UNION
  SELECT j.id FROM mcp.jobs j JOIN tree t ON j.parent_job_id = t.id
)
UPDATE mcp.jobs SET
  cancel_requested = true,
  status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
  finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END,
  locked_by = CASE WHEN status = 'queued' THEN NULL ELSE locked_by END
WHERE id IN (SELECT id FROM tree) AND status IN ('queued', 'running')
RETURNING id
"""


@dataclass(frozen=True)
class JobOwner:
    client_id: str | None
    run_id: str | None = None
    is_admin: bool = False

    def can_read(self, job: Job) -> bool:
        if self.is_admin:
            return True
        if self.client_id and job.client_id and str(job.client_id) == self.client_id:
            return True
        return bool(self.run_id and job.run_id and str(job.run_id) == self.run_id)


def _queued_unless_cancelled() -> Any:
    return case((Job.cancel_requested, "cancelled"), else_="queued")


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def job_view(job: Job) -> dict[str, Any]:
    """Status ringkas (T12): hasil hanya saat succeeded; error ringkas saat failed."""
    out: dict[str, Any] = {
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "progress": {"done": job.progress_done, "total": job.progress_total},
        "message": job.message,
    }
    if job.status == "succeeded":
        out["result"] = {**(job.result or {}), "handle": job.result_handle}
    elif job.status == "failed" and job.error:
        out["error"] = {
            k: job.error.get(k) for k in ("code", "message", "retryable") if k in job.error
        }
        if job.result:
            out["result"] = {**job.result, "handle": job.result_handle, "partial": True}
    return out


class JobQueue:
    def __init__(
        self, db: Database, *, default_timeout_s: int = 1_800, estimator: Any = None
    ) -> None:
        self.db = db
        self.default_timeout_s = default_timeout_s
        # estimator: HandlerRegistry (opsional) untuk mengisi estimate saat enqueue
        self.estimator = estimator

    # ------------------------------------------------------------------ enqueue
    async def enqueue(
        self,
        kind: str,
        params: dict[str, Any],
        *,
        client_id: str | None,
        run_id: str | None = None,
        parent_job_id: str | None = None,
        priority: int = 5,
        run_after: datetime | None = None,
        timeout_s: int | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 3,
    ) -> JobAccepted:
        key = dedupe_key or sha256_hex(f"{kind}|{canonical_json(params)}|{client_id}|{run_id}")
        estimate = None
        handler = self.estimator.get(kind) if self.estimator is not None else None
        if handler is not None:
            estimate = await handler.estimate(params)
        async with self.db.session() as session:
            # serialisasi enqueue identik agar dedupe aman dari race
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
            existing = await session.scalar(
                select(Job).where(Job.dedupe_key == key, Job.status.in_(("queued", "running")))
            )
            if existing is not None:
                return JobAccepted(
                    job_id=existing.id,
                    status=existing.status,  # type: ignore[arg-type]
                    estimate=JobEstimate(**existing.estimate) if existing.estimate else None,
                )
            job = Job(
                id=new_job_id(),
                kind=kind,
                params=params,
                client_id=_uuid(client_id),
                run_id=_uuid(run_id),
                parent_job_id=parent_job_id,
                priority=priority,
                run_after=run_after or now(),
                timeout_s=timeout_s or self.default_timeout_s,
                dedupe_key=key,
                max_attempts=max_attempts,
                estimate=estimate.model_dump() if estimate else None,
            )
            session.add(job)
        poll = max(2, min(30, (estimate.seconds // 6) if estimate else 10))
        return JobAccepted(job_id=job.id, status="queued", estimate=estimate, poll_after_s=poll)

    # ------------------------------------------------------------------ worker side
    async def pick(self, worker_id: str, kinds: list[str] | None = None) -> Job | None:
        kind_filter = "AND kind = ANY(:kinds)" if kinds else ""
        sql = text(
            f"""
            UPDATE mcp.jobs SET status = 'running', locked_by = :w, locked_at = now(),
                   heartbeat_at = now(), attempts = attempts + 1,
                   started_at = COALESCE(started_at, now())
            WHERE id = (
              SELECT id FROM mcp.jobs
              WHERE status = 'queued' AND run_after <= now() AND NOT cancel_requested {kind_filter}
              ORDER BY priority, run_after
              FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING id
            """
        )
        async with self.db.session() as session:
            row = (await session.execute(sql, {"w": worker_id, "kinds": kinds or []})).first()
            if row is None:
                return None
            return await session.get(Job, row.id)

    async def heartbeat(self, job_id: str, worker_id: str) -> bool:
        """Perbarui heartbeat; False bila job bukan lagi milik worker ini."""
        async with self.db.session() as session:
            result = await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.locked_by == worker_id, Job.status == "running")
                .values(heartbeat_at=now())
            )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def reap(self, stale_after_s: int = STALE_HEARTBEAT_S) -> int:
        """Job `running` tanpa heartbeat -> kembali ke antrean (atau failed bila attempts habis)."""
        sql = text(
            """
            UPDATE mcp.jobs SET
              status = CASE WHEN attempts < max_attempts AND NOT cancel_requested THEN 'queued'
                            WHEN cancel_requested THEN 'cancelled' ELSE 'failed' END,
              error = CASE WHEN attempts < max_attempts OR cancel_requested THEN error
                           ELSE jsonb_build_object('code', 'WORKER_LOST',
                                'message', 'Worker berhenti saat job berjalan',
                                'retryable', true) END,
              finished_at = CASE WHEN attempts < max_attempts AND NOT cancel_requested
                                 THEN NULL ELSE now() END,
              locked_by = NULL, locked_at = NULL
            WHERE status = 'running' AND heartbeat_at < now() - make_interval(secs => :s)
            RETURNING id, status
            """
        )
        async with self.db.session() as session:
            rows = (await session.execute(sql, {"s": stale_after_s})).all()
        for row in rows:
            log.warning("job.reaped", job_id=row.id, status=row.status)
        return len(rows)

    async def progress(
        self, job_id: str, done: int, total: int | None, message: str | None
    ) -> None:
        values: dict[str, Any] = {"progress_done": done, "heartbeat_at": now()}
        if total is not None:
            values["progress_total"] = total
        if message is not None:
            values["message"] = truncate(message, 300)
        async with self.db.session() as session:
            await session.execute(update(Job).where(Job.id == job_id).values(**values))

    async def save_checkpoint(self, job_id: str, checkpoint: dict[str, Any]) -> None:
        async with self.db.session() as session:
            await session.execute(update(Job).where(Job.id == job_id).values(checkpoint=checkpoint))

    async def cancel_requested(self, job_id: str) -> bool:
        async with self.db.session() as session:
            return bool(await session.scalar(select(Job.cancel_requested).where(Job.id == job_id)))

    async def finish(
        self,
        job_id: str,
        worker_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        result_handle: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.locked_by == worker_id)
                .values(
                    status=status,
                    result=result,
                    result_handle=result_handle,
                    error=error,
                    finished_at=now(),
                    locked_by=None,
                    locked_at=None,
                )
            )

    async def requeue(
        self, job_id: str, worker_id: str, *, delay_s: float, error: dict[str, Any] | None
    ) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.locked_by == worker_id)
                .values(
                    # job yang diminta batal tidak boleh kembali ke antrean (pick() melewatinya)
                    status=_queued_unless_cancelled(),
                    finished_at=case((Job.cancel_requested, now()), else_=None),
                    locked_by=None,
                    locked_at=None,
                    error=error,
                    run_after=now() + timedelta(seconds=delay_s),
                )
            )

    async def pause(
        self, job_id: str, worker_id: str, *, delay_s: float, error: dict[str, Any]
    ) -> None:
        """Jeda job (mis. `LOCAL_MODEL_UNAVAILABLE`): kembali ke antrean setelah `delay_s` tanpa
        menghabiskan percobaan; checkpoint tetap sehingga lanjut tanpa kehilangan progres."""
        async with self.db.session() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.locked_by == worker_id)
                .values(
                    status=_queued_unless_cancelled(),
                    finished_at=case((Job.cancel_requested, now()), else_=None),
                    locked_by=None,
                    locked_at=None,
                    attempts=Job.attempts - 1,
                    error=error,
                    run_after=now() + timedelta(seconds=delay_s),
                )
            )

    async def release(self, job_id: str, worker_id: str) -> None:
        """Shutdown worker: kembalikan job ke antrean tanpa menghabiskan percobaan."""
        async with self.db.session() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.locked_by == worker_id, Job.status == "running")
                .values(
                    status=_queued_unless_cancelled(),
                    finished_at=case((Job.cancel_requested, now()), else_=None),
                    locked_by=None,
                    locked_at=None,
                    attempts=Job.attempts - 1,
                )
            )

    # ------------------------------------------------------------------ pembatalan
    async def cancel_job(self, job_id: str) -> list[str]:
        """Batalkan job + seluruh turunannya (rekursif)."""
        async with self.db.session() as session:
            rows = (
                await session.execute(text(_DESCENDANTS.format(root="id = :id")), {"id": job_id})
            ).all()
        return [r.id for r in rows]

    async def cancel_run_jobs(self, run_id: str) -> list[str]:
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    text(_DESCENDANTS.format(root="run_id = :rid")), {"rid": _uuid(run_id)}
                )
            ).all()
        return [r.id for r in rows]

    # ------------------------------------------------------------------ baca (tool)
    async def get(self, job_id: str, owner: JobOwner) -> Job:
        async with self.db.session() as session:
            job = await session.get(Job, job_id)
        if job is None or not owner.can_read(job):
            raise NotFound(f"Job {job_id} tidak ditemukan")
        return job

    async def list_jobs(
        self,
        owner: JobOwner,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[Job]:
        stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if not owner.is_admin:
            conds = []
            if owner.client_id:
                conds.append(Job.client_id == _uuid(owner.client_id))
            if owner.run_id:
                conds.append(Job.run_id == _uuid(owner.run_id))
            if not conds:
                return []
            from sqlalchemy import or_

            stmt = stmt.where(or_(*conds))
        if status:
            stmt = stmt.where(Job.status == status)
        if kind:
            stmt = stmt.where(Job.kind == kind)
        async with self.db.session() as session:
            return list((await session.scalars(stmt)).all())

    async def wait(self, job_id: str, owner: JobOwner, timeout_s: float) -> Job:
        """Tunggu sampai job selesai atau timeout (LISTEN/NOTIFY + polling cadangan)."""
        if timeout_s > MAX_WAIT_S:
            raise ValidationFailed(
                f"timeout_s maksimal {MAX_WAIT_S}", details={"field": "timeout_s"}
            )
        job = await self.get(job_id, owner)
        if job.status in TERMINAL or timeout_s <= 0:
            return job
        deadline = asyncio.get_running_loop().time() + timeout_s
        async with self._listener(job_id) as done:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(done.wait(), timeout=min(POLL_FALLBACK_S, remaining))
                job = await self.get(job_id, owner)
                if job.status in TERMINAL:
                    return job
        return job

    @contextlib.asynccontextmanager
    async def _listener(self, job_id: str) -> AsyncIterator[asyncio.Event]:
        event = asyncio.Event()
        conn: asyncpg.Connection | None = None

        def on_notify(_conn: Any, _pid: int, _channel: str, payload: str) -> None:
            if payload == job_id:
                event.set()

        try:
            conn = await asyncpg.connect(asyncpg_dsn(self.db.url))
            await conn.add_listener(NOTIFY_CHANNEL, on_notify)
        except Exception as exc:  # noqa: BLE001 - polling tetap berjalan
            log.warning("job.listen_failed", error=type(exc).__name__)
            conn = None
        try:
            yield event
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()


async def retry_job(session: AsyncSession, job_id: str) -> str:
    """`manage.py job-retry`: kembalikan job failed/cancelled ke antrean dengan percobaan baru."""
    job = await session.get(Job, job_id)
    if job is None:
        raise NotFound(f"Job {job_id} tidak ditemukan")
    if job.status not in ("failed", "cancelled"):
        raise ValidationFailed(
            f"Job berstatus {job.status}; hanya failed/cancelled yang bisa di-retry"
        )
    job.status = "queued"
    job.attempts = 0
    job.cancel_requested = False
    job.error = None
    job.finished_at = None
    job.run_after = now()
    return job.id
