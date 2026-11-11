"""Webhook outbox MCP -> Dashboard (contract 1.0 §5): HMAC, idempoten, BERURUTAN per stream.

Stream = satu run (event `agent.*`, kontrak 1.0) atau satu job `intel` (event `intel.job.*`, 1.2).
- `emit()` / `emit_job()` memvalidasi data event, menyimpan baris `webhook_outbox` dengan `seq` =
  max+1 per stream (dikunci `SELECT … FOR UPDATE` pada baris `runs` / `intel_jobs`).
- Sender loop: hanya seq terkecil yang belum terkirim per stream yang boleh dikirim (satu in-flight
  per stream, maks 8 stream paralel). Status `sending` dianggap pending saat restart (event_id sama
  -> Dashboard membalas `duplicate` bila sudah pernah diproses).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import orjson
from sqlalchemy import select, text, update

from mcp_marketing.core.config import Settings
from mcp_marketing.core.database import Database
from mcp_marketing.core.errors import RunNotActive, ToolError, UpstreamTimeout, ValidationFailed
from mcp_marketing.core.logging import get_logger
from mcp_marketing.core.metrics import OUTBOX_PENDING, WEBHOOK_LATENCY, WEBHOOK_SENT
from mcp_marketing.models import IntelJob, Run, WebhookOutbox
from mcp_marketing.schemas.contract import validate_event_data
from mcp_marketing.schemas.intel import CONTRACT_VERSION as INTEL_CONTRACT_VERSION
from mcp_marketing.schemas.intel import validate_intel_event_data
from mcp_marketing.schemas.intel_v13 import CONTRACT_VERSION as CONTRACT_V13
from mcp_marketing.schemas.intel_v13 import EventEnvelope13, validate_event_data_v13
from mcp_marketing.services.dashboard import signer
from mcp_marketing.services.dashboard.origin import same_origin
from mcp_marketing.utils.text import truncate
from mcp_marketing.utils.time import now

log = get_logger(__name__)

MAX_PARALLEL_RUNS = 8
MAX_BACKOFF_S = 300
SEND_TIMEOUT_S = 20.0
IDLE_POLL_S = 0.5
INACTIVE_RUN_STATUSES = ("completed", "failed", "cancelled", "cancelled_by_dashboard")


class OutboxRejected(ToolError):
    """Dashboard menolak event (400/401/404/422) atau event gagal terkirim permanen."""

    code = "VALIDATION_ERROR"


@dataclass(frozen=True)
class EmittedEvent:
    id: int
    event_id: str
    seq: int
    run_id: str | None
    job_id: str | None = None


def _iso(dt: Any) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class Outbox:
    def __init__(self, db: Database, http: httpx.AsyncClient, settings: Settings) -> None:
        self.db = db
        self.http = http
        self.settings = settings
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._wake = asyncio.Event()
        self._inflight: set[str] = set()
        self._sem = asyncio.Semaphore(MAX_PARALLEL_RUNS)
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ emit
    async def emit(
        self,
        run_id: str,
        event_type: str,
        data: dict[str, Any],
        *,
        task_id: str | None = None,
        allow_inactive: bool = False,
        event_id: str | None = None,
        use_run_task_id: bool = True,
    ) -> EmittedEvent:
        try:
            clean = validate_event_data(event_type, data)
        except ValueError as exc:
            raise ValidationFailed(
                f"Data event {event_type} tidak valid: {truncate(str(exc), 300)}"
            ) from None
        async with self.db.session() as session:
            run = (
                await session.execute(
                    select(Run).where(Run.run_id == uuid.UUID(run_id)).with_for_update()
                )
            ).scalar_one_or_none()
            if run is None:
                raise ValidationFailed(f"Run {run_id} tidak dikenal MCP")
            if not allow_inactive and (run.cancel_requested or run.status in INACTIVE_RUN_STATUSES):
                raise RunNotActive(f"Run {run_id} sudah {run.status}; event tidak dikirim")
            seq = int(
                (
                    await session.execute(
                        text(
                            "SELECT COALESCE(max(seq), 0) + 1 FROM mcp.webhook_outbox "
                            "WHERE run_id = :r"
                        ),
                        {"r": run.run_id},
                    )
                ).scalar_one()
            )
            event_id = event_id or str(uuid.uuid4())
            target_task = (
                task_id
                if task_id is not None or not use_run_task_id
                else (str(run.task_id) if run.task_id else None)
            )
            payload = {
                "contract_version": self.settings.dashboard.contract_version,
                "event_id": event_id,
                "event_type": event_type,
                "occurred_at": _iso(now()),
                "run_id": str(run.run_id),
                "agent_id": str(run.agent_id),
                "task_id": target_task,
                "data": clean,
            }
            row = WebhookOutbox(
                event_id=uuid.UUID(event_id),
                run_id=run.run_id,
                seq=seq,
                event_type=event_type,
                payload=payload,
            )
            session.add(row)
            run.last_event_at = now()
            await session.flush()
            emitted = EmittedEvent(id=row.id, event_id=event_id, seq=seq, run_id=str(run.run_id))
        self._wake.set()
        return emitted

    async def emit_job(self, job_id: str, event_type: str, data: dict[str, Any]) -> EmittedEvent:
        """Event `intel.job.*` (kontrak 1.2); `sequence` di envelope = `seq` outbox per job."""
        try:
            clean = validate_intel_event_data(event_type, data)
        except ValueError as exc:
            raise ValidationFailed(
                f"Data event {event_type} tidak valid: {truncate(str(exc), 300)}"
            ) from None
        async with self.db.session() as session:
            job = (
                await session.execute(
                    select(IntelJob).where(IntelJob.id == job_id).with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                raise ValidationFailed(f"Job intel {job_id} tidak dikenal MCP")
            job.event_sequence += 1
            seq = job.event_sequence
            event_id = str(uuid.uuid4())
            payload = {
                "contract_version": INTEL_CONTRACT_VERSION,
                "event_id": event_id,
                "event_type": event_type,
                "occurred_at": _iso(now()),
                "job_id": str(job.dashboard_job_id),
                "mcp_job_id": job.id,
                "sequence": seq,
                "data": clean,
            }
            row = WebhookOutbox(
                event_id=uuid.UUID(event_id),
                job_id=job.id,
                seq=seq,
                event_type=event_type,
                payload=payload,
            )
            session.add(row)
            await session.flush()
            emitted = EmittedEvent(
                id=row.id, event_id=event_id, seq=seq, run_id=None, job_id=job_id
            )
        self._wake.set()
        return emitted

    async def emit_entity(
        self,
        stream: str,
        workspace_id: uuid.UUID,
        event_type: str,
        data: dict[str, Any],
        *,
        job_id: uuid.UUID | None = None,
        mcp_job_id: str | None = None,
    ) -> EmittedEvent:
        """Event entitas kontrak 1.3 (`kn.asset.status`, `ci.import.status`, ...) per `stream`
        (mis. `asset:<id>`); urutan dijaga per stream, envelope `EventEnvelope13`."""
        event_id = str(uuid.uuid4())
        try:
            clean = validate_event_data_v13(event_type, data)
            payload: dict[str, Any] = {
                "contract_version": CONTRACT_V13,
                "event_id": event_id,
                "event_type": event_type,
                "occurred_at": _iso(now()),
                "workspace_id": str(workspace_id),
                "job_id": str(job_id) if job_id else None,
                "mcp_job_id": mcp_job_id,
                "sequence": 1,
                "data": clean,
            }
        except ValueError as exc:
            raise ValidationFailed(
                f"Data event {event_type} tidak valid: {truncate(str(exc), 300)}"
            ) from None
        async with self.db.session() as session:
            # kunci advisory per stream: seq unik walau dua proses emit bersamaan
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:s))"), {"s": f"outbox:{stream}"}
            )
            seq = int(
                (
                    await session.execute(
                        text(
                            "SELECT COALESCE(max(seq), 0) + 1 FROM mcp.webhook_outbox "
                            "WHERE stream = :s"
                        ),
                        {"s": stream},
                    )
                ).scalar_one()
            )
            payload["sequence"] = seq
            EventEnvelope13.model_validate(payload)
            row = WebhookOutbox(
                event_id=uuid.UUID(event_id),
                stream=stream,
                seq=seq,
                event_type=event_type,
                payload=payload,
            )
            session.add(row)
            await session.flush()
            emitted = EmittedEvent(id=row.id, event_id=event_id, seq=seq, run_id=None)
        self._wake.set()
        return emitted

    async def discard_job(self, job_id: str, reason: str = "job_not_active") -> int:
        async with self.db.session() as session:
            result = await session.execute(
                update(WebhookOutbox)
                .where(
                    WebhookOutbox.job_id == job_id,
                    WebhookOutbox.status.in_(("pending", "sending")),
                )
                .values(status="discarded", last_error=reason)
            )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def emit_and_wait(
        self,
        run_id: str,
        event_type: str,
        data: dict[str, Any],
        *,
        task_id: str | None = None,
        timeout_s: float = 30.0,
        use_run_task_id: bool = True,
    ) -> dict[str, Any]:
        """Emit lalu tunggu respons Dashboard (`result` untuk peta subtask/conflict blok)."""
        event_id = str(uuid.uuid4())
        # daftarkan waiter SEBELUM emit: sender bisa mengirim sebelum baris berikutnya dijalankan
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[event_id] = future
        try:
            await self.emit(
                run_id,
                event_type,
                data,
                task_id=task_id,
                event_id=event_id,
                use_run_task_id=use_run_task_id,
            )
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
        except TimeoutError:
            raise UpstreamTimeout(
                f"Webhook {event_type} belum terkirim dalam {int(timeout_s)} s (tetap di antrean)",
                details={"event_id": event_id},
            ) from None
        finally:
            self._waiters.pop(event_id, None)
            if future.done() and not future.cancelled():
                future.exception()  # tandai sudah dibaca (hindari warning asyncio)

    async def discard_run(self, run_id: str, reason: str = "run_not_active") -> int:
        async with self.db.session() as session:
            result = await session.execute(
                update(WebhookOutbox)
                .where(
                    WebhookOutbox.run_id == uuid.UUID(run_id),
                    WebhookOutbox.status.in_(("pending", "sending")),
                )
                .values(status="discarded", last_error=reason)
                .returning(WebhookOutbox.event_id)
            )
            discarded = [str(r[0]) for r in result.all()]
        for event_id in discarded:
            self._resolve(event_id, error=RunNotActive("Run tidak aktif; event dibuang"))
        return len(discarded)

    # ------------------------------------------------------------------ sender
    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run_sender(self) -> None:
        log.info("outbox.sender_start")
        while not self._stop.is_set():
            try:
                sent = await self.send_due()
            except Exception as exc:  # noqa: BLE001 - loop sender tidak boleh mati
                log.exception("outbox.loop_error", error=type(exc).__name__)
                sent = 0
            if not sent:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=IDLE_POLL_S)
        log.info("outbox.sender_stopped")

    async def send_due(self) -> int:
        """Satu putaran: kirim kepala antrean tiap run yang jatuh tempo; kembalikan jumlahnya."""
        async with self.db.session() as session:
            heads = (
                await session.execute(
                    text(
                        """
                SELECT DISTINCT ON (stream) id, stream, next_attempt_at <= now() AS due
                FROM (
                  SELECT id, seq, next_attempt_at,
                         COALESCE(run_id::text, 'job:' || job_id, 'entity:' || stream) AS stream
                  FROM mcp.webhook_outbox WHERE status IN ('pending', 'sending')
                ) pending
                ORDER BY stream, seq
                """
                    )
                )
            ).all()
        OUTBOX_PENDING.set(len(heads))
        due = [h for h in heads if h.due and h.stream not in self._inflight]
        if not due:
            return 0
        results = await asyncio.gather(
            *(self._send_guarded(h.id, h.stream) for h in due), return_exceptions=True
        )
        for r in results:
            if isinstance(r, BaseException):
                log.warning("outbox.send_error", error=type(r).__name__)
        return sum(1 for r in results if r is True)

    async def _send_guarded(self, row_id: int, stream: str) -> bool:
        self._inflight.add(stream)
        try:
            async with self._sem:
                return await self._send(row_id)
        finally:
            self._inflight.discard(stream)

    async def _target_url(self, row: WebhookOutbox) -> str:
        callback = None
        if row.run_id is not None:
            async with self.db.session() as session:
                callback = await session.scalar(
                    select(Run.callback).where(Run.run_id == row.run_id)
                )
        api = self.settings.dashboard.dashboard_api_url
        url = (callback or {}).get("webhook_url")
        # webhook bertanda tangan hanya ke origin Dashboard yang dikonfigurasi
        if not url or not same_origin(str(url), api):
            url = f"{api.rstrip('/')}/webhooks/mcp/events"
        return str(url)

    async def _send(self, row_id: int) -> bool:
        async with self.db.session() as session:
            row = await session.get(WebhookOutbox, row_id)
            if row is None or row.status not in ("pending", "sending"):
                return False
            row.status = "sending"
            row.attempts += 1
            if row.first_attempt_at is None:
                row.first_attempt_at = now()
            await session.flush()  # status sending & attempts tersimpan sebelum kirim
            session.expunge(row)
        body = orjson.dumps(row.payload)
        headers = signer.signed_headers(
            self.settings.dashboard.mcp_webhook_secret.get_secret_value(), body
        )
        started = time.perf_counter()
        try:
            url = await self._target_url(row)
            resp = await self.http.post(url, content=body, headers=headers, timeout=SEND_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - jaringan/URL/dll: backoff, bukan retry panas
            await self._retry_later(row, None, f"send: {type(exc).__name__}")
            return True
        finally:
            WEBHOOK_LATENCY.observe(time.perf_counter() - started)
        await self._handle_response(row, resp)
        return True

    async def _handle_response(self, row: WebhookOutbox, resp: httpx.Response) -> None:
        status = resp.status_code
        event_id = str(row.event_id)
        body: dict[str, Any] = {}
        with contextlib.suppress(ValueError):
            parsed = resp.json()
            body = parsed if isinstance(parsed, dict) else {}
        raw_error = body.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        code = str(error.get("code") or "")
        message = truncate(str(error.get("message") or resp.reason_phrase), 500)

        if status == 200:
            dup = body.get("status") == "duplicate"
            raw_result = body.get("result")
            result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
            await self._finalize(
                row, "duplicate" if dup else "delivered", status, response=result, delivered=True
            )
            WEBHOOK_SENT.labels("duplicate" if dup else "delivered").inc()
            self._resolve(event_id, result={**result, "duplicate": True} if dup else result)
            return
        if (
            row.job_id is not None
            and status in (404, 409)
            and code in ("NOT_FOUND", "JOB_NOT_ACTIVE")
        ):
            # Dashboard tidak mengenal / sudah menutup job: event sisanya tidak berguna
            await self._finalize(row, "discarded", status, error=f"{code}: {message}")
            discarded = await self.discard_job(row.job_id, reason=code.lower())
            WEBHOOK_SENT.labels("job_not_active").inc()
            log.warning("outbox.job_not_active", job_id=row.job_id, discarded=discarded)
            self._resolve(
                event_id, error=OutboxRejected(f"Dashboard: {message}", code="JOB_NOT_ACTIVE")
            )
            return
        if status == 409 and code == "RUN_NOT_ACTIVE":
            await self._finalize(row, "discarded", status, error=message)
            discarded = await self.discard_run(str(row.run_id))
            WEBHOOK_SENT.labels("run_not_active").inc()
            log.warning("outbox.run_not_active", run_id=str(row.run_id), discarded=discarded)
            self._resolve(event_id, error=RunNotActive(f"Dashboard: {message}"))
            await self._mark_run_inactive(str(row.run_id))
            return
        if status == 429 or status >= 500:
            await self._retry_later(row, status, message)
            return
        # 400/401/404/422/409 lain: jangan retry
        await self._finalize(row, "failed", status, error=f"{code}: {message}")
        WEBHOOK_SENT.labels("rejected").inc()
        if status == 401:
            log.critical(
                "outbox.signature_rejected",
                run_id=str(row.run_id),
                hint="cek MCP_WEBHOOK_SECRET & jam server",
            )
            err: ToolError = OutboxRejected(
                "Signature webhook ditolak Dashboard",
                code="UPSTREAM_ERROR",
                retryable=False,
                details={"reason": "WEBHOOK_AUTH"},
            )
        else:
            log.warning("outbox.rejected", status=status, code=code, event_type=row.event_type)
            err = OutboxRejected(
                f"Dashboard menolak {row.event_type} ({status} {code}): {message}",
                details={
                    "status": status,
                    "dashboard_code": code,
                    "dashboard_details": error.get("details"),
                },
            )
        self._resolve(event_id, error=err)

    async def _retry_later(self, row: WebhookOutbox, status: int | None, error: str) -> None:
        first = row.first_attempt_at or now()
        limit = timedelta(minutes=self.settings.dashboard.webhook_max_retry_minutes)
        if now() - first > limit:
            await self._finalize(row, "failed", status, error=f"retry habis: {error}")
            WEBHOOK_SENT.labels("gave_up").inc()
            self._resolve(
                str(row.event_id),
                error=OutboxRejected(
                    "Webhook gagal terkirim setelah batas retry",
                    code="UPSTREAM_ERROR",
                    retryable=False,
                    details={"reason": "WEBHOOK_UNDELIVERABLE"},
                ),
            )
            return
        delay = min(MAX_BACKOFF_S, 2 ** max(0, row.attempts - 1))
        async with self.db.session() as session:
            await session.execute(
                update(WebhookOutbox)
                .where(WebhookOutbox.id == row.id)
                .values(
                    status="pending",
                    attempts=row.attempts,
                    first_attempt_at=first,
                    next_attempt_at=now() + timedelta(seconds=delay),
                    last_http_status=status,
                    last_error=truncate(error, 500),
                )
            )
        WEBHOOK_SENT.labels("retry").inc()
        log.info("outbox.retry", status=status, delay_s=delay, event_type=row.event_type)

    async def _finalize(
        self,
        row: WebhookOutbox,
        status: str,
        http_status: int | None,
        *,
        response: dict[str, Any] | None = None,
        error: str | None = None,
        delivered: bool = False,
    ) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(WebhookOutbox)
                .where(WebhookOutbox.id == row.id)
                .values(
                    status=status,
                    attempts=row.attempts,
                    first_attempt_at=row.first_attempt_at,
                    last_http_status=http_status,
                    response=response,
                    last_error=error,
                    delivered_at=now() if delivered else None,
                )
            )

    async def _mark_run_inactive(self, run_id: str) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(Run)
                .where(Run.run_id == uuid.UUID(run_id), Run.status.in_(("accepted", "running")))
                .values(
                    status="cancelled_by_dashboard",
                    cancel_requested=True,
                    cancel_reason="run_not_active",
                    finished_at=now(),
                )
            )

    def _resolve(
        self, event_id: str, *, result: dict[str, Any] | None = None, error: ToolError | None = None
    ) -> None:
        future = self._waiters.get(event_id)
        if future is None or future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result or {})
