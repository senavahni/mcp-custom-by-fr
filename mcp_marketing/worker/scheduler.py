"""Scheduler cron (`mcp.schedules`, zona TIMEZONE): tepat satu job per slot walau banyak worker.

Klaim slot atomik: `UPDATE schedules SET last_run_at = slot WHERE last_run_at < slot` — hanya
worker yang berhasil meng-update yang meng-enqueue job.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select, text

from mcp_marketing.core.database import Database
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import Schedule
from mcp_marketing.utils.crypto import sha256_hex
from mcp_marketing.utils.time import now
from mcp_marketing.worker.queue import JobQueue

log = get_logger(__name__)


def latest_slot(cron: str, at: datetime, tz: str) -> datetime:
    """Slot cron terakhir yang <= `at` (UTC aware)."""
    # get_prev() eksklusif terhadap base; geser 1 detik agar slot tepat `at` ikut terhitung
    local = at.astimezone(ZoneInfo(tz)).replace(microsecond=0) + timedelta(seconds=1)
    return croniter(cron, local).get_prev(datetime).astimezone(ZoneInfo("UTC"))  # type: ignore[no-any-return]


def next_slot(cron: str, at: datetime, tz: str) -> datetime:
    local = at.astimezone(ZoneInfo(tz))
    return croniter(cron, local).get_next(datetime).astimezone(ZoneInfo("UTC"))


class Scheduler:
    def __init__(self, db: Database, queue: JobQueue, *, timezone: str = "Asia/Jakarta") -> None:
        self.db = db
        self.queue = queue
        self.timezone = timezone

    async def tick(self, at: datetime | None = None) -> list[str]:
        at = at or now()
        enqueued: list[str] = []
        async with self.db.session() as session:
            schedules = list(
                (await session.scalars(select(Schedule).where(Schedule.is_enabled.is_(True)))).all()
            )
        for sched in schedules:
            try:
                slot = latest_slot(sched.cron, at, self.timezone)
            except (ValueError, KeyError) as exc:
                log.warning("scheduler.bad_cron", schedule=sched.name, error=str(exc))
                continue
            async with self.db.session() as session:
                claimed = (
                    await session.execute(
                        text(
                            "UPDATE mcp.schedules SET last_run_at = :slot, next_run_at = :next "
                            "WHERE id = :id AND (last_run_at IS NULL OR last_run_at < :slot) "
                            "RETURNING id"
                        ),
                        {
                            "slot": slot,
                            "next": next_slot(sched.cron, at, self.timezone),
                            "id": sched.id,
                        },
                    )
                ).first()
            if claimed is None:
                continue
            if sched.last_run_at is None:
                # deploy pertama: tandai slot sekarang tanpa menjalankan slot yang sudah lewat
                continue
            try:
                accepted = await self.queue.enqueue(
                    sched.job_kind,
                    dict(sched.params or {}),
                    client_id=None,
                    dedupe_key=sha256_hex(f"{sched.id}|{slot.isoformat()}"),
                    priority=8,
                    max_attempts=2,
                )
            except Exception as exc:  # noqa: BLE001 - kembalikan klaim agar slot dicoba lagi
                log.warning(
                    "scheduler.enqueue_failed", schedule=sched.name, error=type(exc).__name__
                )
                async with self.db.session() as session:
                    await session.execute(
                        text(
                            "UPDATE mcp.schedules SET last_run_at = :prev WHERE id = :id "
                            "AND last_run_at = :slot"
                        ),
                        {"prev": sched.last_run_at, "id": sched.id, "slot": slot},
                    )
                continue
            enqueued.append(accepted.job_id)
            log.info(
                "scheduler.enqueued",
                schedule=sched.name,
                slot=slot.isoformat(),
                job_id=accepted.job_id,
            )
        return enqueued
