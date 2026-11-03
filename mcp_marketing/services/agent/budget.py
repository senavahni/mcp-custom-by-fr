"""Budget token LLM tiga lapis (per tool call, per run, per klien per hari) + kuota unit eksternal.

Pola pakai: `res = await scope.reserve(estimate)` SEBELUM panggilan LLM -> panggil ->
`await scope.commit(res, actual)` (atau `release(res)` bila gagal). Melebihi batas ->
`BUDGET_EXCEEDED` sebelum token terbuang (MCP_SPEC §3 T8).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from mcp_marketing.core.errors import BudgetExceeded
from mcp_marketing.core.logging import get_logger
from mcp_marketing.utils.time import now

log = get_logger(__name__)

Loader = Callable[[], Awaitable[int]]
Persister = Callable[[int], Awaitable[None]]


class BudgetLayer:
    """Satu lapis budget dengan lock agar reserve paralel tidak melampaui batas."""

    def __init__(
        self,
        scope: str,
        limit: int | None,
        used: int = 0,
        persist: Persister | None = None,
    ) -> None:
        self.scope = scope
        self.limit = limit
        self.used = used
        self.reserved = 0
        self._persist = persist
        self._lock = asyncio.Lock()

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used - self.reserved)

    async def reserve(self, amount: int) -> None:
        async with self._lock:
            if self.limit is not None and self.used + self.reserved + amount > self.limit:
                raise BudgetExceeded(
                    f"Budget token {self.scope} tidak cukup",
                    details={
                        "used": self.used,
                        "reserved": self.reserved,
                        "requested": amount,
                        "limit": self.limit,
                        "scope": self.scope,
                    },
                )
            self.reserved += amount

    async def settle(self, reserved: int, actual: int) -> None:
        async with self._lock:
            self.reserved = max(0, self.reserved - reserved)
            self.used += actual
            used = self.used
        if self._persist is not None and actual:
            try:
                await self._persist(used)
            except Exception as exc:  # noqa: BLE001 - kegagalan persist tidak boleh menggagalkan tool
                log.warning("budget.persist_failed", scope=self.scope, error=str(exc))


@dataclass
class Reservation:
    amount: int
    layers: list[BudgetLayer]
    settled: bool = False


@dataclass
class BudgetScope:
    """Gabungan lapis yang berlaku untuk satu tool call / satu langkah agent."""

    layers: list[BudgetLayer] = field(default_factory=list)
    used: int = 0

    async def reserve(self, amount: int) -> Reservation:
        done: list[BudgetLayer] = []
        try:
            for layer in self.layers:
                await layer.reserve(amount)
                done.append(layer)
        except BudgetExceeded:
            for layer in done:
                await layer.settle(amount, 0)
            raise
        return Reservation(amount=amount, layers=done)

    async def commit(self, reservation: Reservation, actual: int) -> None:
        if reservation.settled:
            return
        reservation.settled = True
        self.used += actual
        for layer in reservation.layers:
            await layer.settle(reservation.amount, actual)

    async def release(self, reservation: Reservation) -> None:
        await self.commit(reservation, 0)

    def snapshot(self) -> dict[str, Any]:
        return {layer.scope: {"used": layer.used, "limit": layer.limit} for layer in self.layers}


class ExternalQuota:
    """Kuota unit eksternal harian (mis. post X yang dibaca, X_MAX_READS_PER_DAY)."""

    def __init__(self, kind: str, per_day: int) -> None:
        self.kind = kind
        self.per_day = per_day
        self._day: date | None = None
        self._used = 0
        self._lock = asyncio.Lock()

    async def reserve(self, units: int) -> None:
        async with self._lock:
            today = now().date()
            if today != self._day:
                self._day, self._used = today, 0
            if self._used + units > self.per_day:
                raise BudgetExceeded(
                    f"Kuota harian {self.kind} habis",
                    details={
                        "used": self._used,
                        "requested": units,
                        "limit": self.per_day,
                        "scope": f"external:{self.kind}",
                    },
                )
            self._used += units


class BudgetManager:
    """Menyimpan lapis budget klien (per hari) dan run agar dipakai bersama antar panggilan."""

    def __init__(
        self,
        *,
        client_used_loader: Callable[[str, date], Awaitable[int]] | None = None,
        run_loader: Callable[[str], Awaitable[tuple[int, int] | None]] | None = None,
        run_persister: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> None:
        self._client_layers: dict[tuple[str, date], BudgetLayer] = {}
        self._run_layers: dict[str, BudgetLayer] = {}
        self._client_used_loader = client_used_loader
        self._run_loader = run_loader
        self._run_persister = run_persister
        self._lock = asyncio.Lock()
        self.external: dict[str, ExternalQuota] = {}

    async def client_layer(self, client_id: str, daily_limit: int | None) -> BudgetLayer | None:
        if daily_limit is None:
            return None
        key = (client_id, now().date())
        async with self._lock:
            layer = self._client_layers.get(key)
            if layer is None:
                used = (
                    await self._client_used_loader(client_id, key[1])
                    if self._client_used_loader
                    else 0
                )
                # buang lapis hari sebelumnya
                self._client_layers = {
                    k: v for k, v in self._client_layers.items() if k[1] == key[1]
                }
                layer = BudgetLayer(f"client_daily:{client_id}", daily_limit, used)
                self._client_layers[key] = layer
            layer.limit = daily_limit
        return layer

    async def run_layer(self, run_id: str) -> BudgetLayer | None:
        async with self._lock:
            layer = self._run_layers.get(run_id)
            if layer is None and self._run_loader is not None:
                loaded = await self._run_loader(run_id)
                if loaded is None:
                    return None
                used, limit = loaded

                async def persist(value: int, _rid: str = run_id) -> None:
                    if self._run_persister is not None:
                        await self._run_persister(_rid, value)

                layer = BudgetLayer("run", limit, used, persist)
                self._run_layers[run_id] = layer
        return layer

    def forget_run(self, run_id: str) -> None:
        self._run_layers.pop(run_id, None)

    async def scope(
        self,
        *,
        client_id: str | None,
        daily_limit: int | None,
        run_id: str | None,
        tool_limit: int | None,
    ) -> BudgetScope:
        layers: list[BudgetLayer] = []
        if tool_limit is not None:
            layers.append(BudgetLayer("tool_call", tool_limit))
        if run_id:
            run_layer = await self.run_layer(run_id)
            if run_layer is not None:
                layers.append(run_layer)
        if client_id:
            client_layer = await self.client_layer(client_id, daily_limit)
            if client_layer is not None:
                layers.append(client_layer)
        return BudgetScope(layers=layers)

    def external_quota(self, kind: str, per_day: int) -> ExternalQuota:
        quota = self.external.get(kind)
        if quota is None or quota.per_day != per_day:
            quota = ExternalQuota(kind, per_day)
            self.external[kind] = quota
        return quota
