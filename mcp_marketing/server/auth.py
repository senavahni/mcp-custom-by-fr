"""Autentikasi API key klien MCP + rate limit per klien (MCP_SPEC §1.2, README D13).

- `Bearer <MCP_SERVER_API_KEY>` -> klien sistem `dashboard` (toolset `pm`), constant-time.
- `Bearer mcpk_<prefix8>_<secret43>` -> lookup `api_clients` by prefix (cache TTL 60 s), verifikasi
  `HMAC_SHA256(pepper, key)`, cek status & kedaluwarsa.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, update

from mcp_marketing.core.config import Settings
from mcp_marketing.core.database import Database
from mcp_marketing.core.errors import RateLimited
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import ApiClient
from mcp_marketing.services.clients import SYSTEM_DASHBOARD
from mcp_marketing.utils import crypto
from mcp_marketing.utils.time import now

log = get_logger(__name__)

CACHE_TTL_S = 60.0
# batas entri cache prefix (termasuk hasil negatif) agar token acak tidak membengkakkan memori
CACHE_MAX_ENTRIES = 10_000
LAST_USED_THROTTLE_S = 60.0


@dataclass(frozen=True)
class ClientIdentity:
    id: str | None
    name: str
    kind: str
    toolsets: frozenset[str]
    is_admin: bool = False
    workspace_id: str | None = None
    dashboard_agent_id: str | None = None
    rpm_limit: int = 120
    daily_token_budget: int | None = None

    @classmethod
    def from_row(cls, row: ApiClient) -> ClientIdentity:
        return cls(
            id=str(row.id),
            name=row.name,
            kind=row.kind,
            toolsets=frozenset(row.toolsets or []),
            is_admin=row.is_admin,
            workspace_id=str(row.workspace_id) if row.workspace_id else None,
            dashboard_agent_id=str(row.dashboard_agent_id) if row.dashboard_agent_id else None,
            rpm_limit=row.rpm_limit,
            daily_token_budget=row.daily_token_budget,
        )

    def can_use(self, toolset: str, *, admin_only: bool = False) -> bool:
        return toolset in self.toolsets and (self.is_admin or not admin_only)


@dataclass
class _CacheEntry:
    row: ApiClient | None
    expires: float


class TokenBucket:
    def __init__(self, rpm: int) -> None:
        self.capacity = max(1, rpm)
        self.tokens = float(self.capacity)
        self.rate = self.capacity / 60.0
        self.updated = time.monotonic()

    def take(self) -> bool:
        t = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (t - self.updated) * self.rate)
        self.updated = t
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


@dataclass
class Authenticator:
    settings: Settings
    db: Database | None
    cache_ttl_s: float = CACHE_TTL_S
    _cache: dict[str, _CacheEntry] = field(default_factory=dict)
    _buckets: dict[str, TokenBucket] = field(default_factory=dict)
    _last_used: dict[str, float] = field(default_factory=dict)
    _dashboard: ClientIdentity | None = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    async def authenticate(self, authorization: str | None) -> ClientIdentity | None:
        if not authorization or not authorization.lower().startswith("bearer "):
            return None
        token = authorization[7:].strip()
        if not token:
            return None
        server_key = self.settings.dashboard.mcp_server_api_key.get_secret_value()
        if crypto.constant_time_eq(token, server_key):
            return await self._dashboard_identity()
        prefix = crypto.parse_client_key(token)
        if prefix is None:
            return None
        row = await self._lookup(prefix)
        if row is None or row.kind == "system":
            return None
        expected = crypto.hash_client_key(self.settings.mcp_key_pepper.get_secret_value(), token)
        if not crypto.constant_time_eq(expected, row.key_hash.strip()):
            return None
        if row.status != "active" or (row.expires_at is not None and row.expires_at <= now()):
            return None
        self._touch(str(row.id))
        return ClientIdentity.from_row(row)

    async def _dashboard_identity(self) -> ClientIdentity:
        if self._dashboard is None:
            row = None
            if self.db is not None:
                async with self.db.session() as session:
                    row = await session.scalar(
                        select(ApiClient).where(ApiClient.name == SYSTEM_DASHBOARD)
                    )
            self._dashboard = (
                ClientIdentity.from_row(row)
                if row is not None
                else ClientIdentity(
                    id=None,
                    name=SYSTEM_DASHBOARD,
                    kind="system",
                    toolsets=frozenset({"pm"}),
                    rpm_limit=600,
                )
            )
        return self._dashboard

    async def _lookup(self, prefix: str) -> ApiClient | None:
        entry = self._cache.get(prefix)
        if entry is not None and entry.expires > time.monotonic():
            return entry.row
        row = None
        if self.db is not None:
            async with self.db.session() as session:
                row = await session.scalar(select(ApiClient).where(ApiClient.key_prefix == prefix))
                if row is not None:
                    session.expunge(row)
        if len(self._cache) >= CACHE_MAX_ENTRIES:
            self._cache.clear()
        self._cache[prefix] = _CacheEntry(row, time.monotonic() + self.cache_ttl_s)
        return row

    def invalidate(self, prefix: str | None = None) -> None:
        if prefix is None:
            self._cache.clear()
        else:
            self._cache.pop(prefix, None)

    def check_rate(self, client: ClientIdentity) -> None:
        key = client.id or client.name
        bucket = self._buckets.get(key)
        if bucket is None or bucket.capacity != max(1, client.rpm_limit):
            bucket = TokenBucket(client.rpm_limit)
            self._buckets[key] = bucket
        if not bucket.take():
            raise RateLimited(
                f"Batas {client.rpm_limit} request/menit terlampaui",
                details={"rpm_limit": client.rpm_limit},
            )

    def _touch(self, client_id: str) -> None:
        t = time.monotonic()
        if t - self._last_used.get(client_id, 0) < LAST_USED_THROTTLE_S or self.db is None:
            return
        self._last_used[client_id] = t
        task = asyncio.create_task(self._update_last_used(client_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _update_last_used(self, client_id: str) -> None:
        assert self.db is not None
        try:
            async with self.db.session() as session:
                await session.execute(
                    update(ApiClient)
                    .where(ApiClient.id == uuid.UUID(client_id))
                    .values(last_used_at=now())
                )
        except Exception as exc:  # noqa: BLE001 - metadata, tidak kritis
            log.warning("auth.last_used_failed", error=str(exc))
