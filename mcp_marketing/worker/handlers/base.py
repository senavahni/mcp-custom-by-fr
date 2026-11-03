"""Antarmuka handler job + konteks eksekusi (TASK-07)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from mcp_marketing.schemas.common import JobEstimate

if TYPE_CHECKING:
    from mcp_marketing.server.services import Services
    from mcp_marketing.worker.queue import JobQueue

R = TypeVar("R")
CANCEL_CACHE_S = 2.0


class JobCancelled(Exception):
    """Dilempar handler (atau `JobCtx.raise_if_cancelled`) saat pembatalan diminta."""


@dataclass
class JobResult:
    result: dict[str, Any]  # envelope TANPA items (summary, total, stats, cost)
    result_handle: str | None = None


class Handler(Protocol):
    kind: str

    async def estimate(self, params: dict[str, Any]) -> JobEstimate: ...

    async def run(self, job: JobCtx) -> JobResult: ...


@dataclass
class JobCtx:
    job_id: str
    kind: str
    params: dict[str, Any]
    checkpoint: dict[str, Any]
    attempts: int
    client_id: str | None
    run_id: str | None
    services: Services
    queue: JobQueue
    executor: Executor | None = None
    partial_result: JobResult | None = None
    _cancel_checked: float = field(default=0.0, repr=False)
    _cancelled: bool = field(default=False, repr=False)

    async def progress(
        self, done: int, total: int | None = None, message: str | None = None
    ) -> None:
        await self.queue.progress(self.job_id, done, total, message)

    async def save_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        self.checkpoint = dict(checkpoint)
        await self.queue.save_checkpoint(self.job_id, self.checkpoint)

    async def is_cancelled(self) -> bool:
        """Baca `cancel_requested` (di-cache 2 s agar tidak membebani DB)."""
        if self._cancelled:
            return True
        now = time.monotonic()
        if now - self._cancel_checked >= CANCEL_CACHE_S:
            self._cancel_checked = now
            self._cancelled = await self.queue.cancel_requested(self.job_id)
        return self._cancelled

    async def raise_if_cancelled(self) -> None:
        if await self.is_cancelled():
            raise JobCancelled(self.job_id)

    async def run_cpu(self, fn: Callable[..., R], *args: Any) -> R:
        """Jalankan pekerjaan CPU-bound (mis. ONNX) di ProcessPoolExecutor(1)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, fn, *args)


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, handler: Handler) -> None:
        self._handlers[handler.kind] = handler

    def get(self, kind: str) -> Handler | None:
        return self._handlers.get(kind)

    def kinds(self) -> list[str]:
        return sorted(self._handlers)
