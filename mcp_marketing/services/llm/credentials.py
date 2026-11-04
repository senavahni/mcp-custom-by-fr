"""Key LLM Gateway per agent (M3_TASK_009): simpan terenkripsi + pilih key per panggilan.

Urutan pemilihan: key milik agent (konteks `agent_key`) -> key `system` -> `LLM_GATEWAY_API_KEY`.
Tanpa `MCP_SECRET_KEY` fitur ini nonaktif (semua panggilan memakai key env, perilaku lama).
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import LlmCredential
from mcp_marketing.utils.secretbox import SecretBoxError, open_sealed, seal
from mcp_marketing.utils.time import now

log = get_logger(__name__)

SYSTEM = "system"
CACHE_TTL_S = 60.0
GATEWAY_KEY_RE = re.compile(r"^(lgw_[a-z0-9]{8})_[A-Za-z0-9_\-]{43}$")
KeySource = Literal["agent", "system", "env"]


def credential_name(agent_key: str | None) -> str:
    return f"agent:{agent_key}" if agent_key else SYSTEM


def gateway_key_prefix(full_key: str) -> str:
    match = GATEWAY_KEY_RE.match(full_key.strip())
    if match is None:
        raise ValidationFailed("Format key LLM Gateway tidak valid (lgw_<8>_<43>)")
    return match.group(1)


@dataclass(frozen=True)
class GatewayKey:
    key: str
    source: KeySource
    prefix: str | None = None
    credential_id: uuid.UUID | None = None


@dataclass(frozen=True)
class _Active:
    id: uuid.UUID
    prefix: str
    key: str


class CredentialStore:
    def __init__(self, db: Any, settings: Any) -> None:
        self.db = db
        self.settings = settings
        self._cache: tuple[float, dict[str, _Active]] | None = None

    @property
    def secret_key(self) -> str | None:
        key = self.settings.mcp_secret_key
        return key.get_secret_value() if key is not None else None

    def env_key(self) -> GatewayKey:
        return GatewayKey(self.settings.llm.llm_gateway_api_key.get_secret_value(), "env")

    def invalidate(self) -> None:
        self._cache = None

    async def _active(self) -> dict[str, _Active]:
        if self._cache is not None and time.monotonic() - self._cache[0] < CACHE_TTL_S:
            return self._cache[1]
        secret = self.secret_key
        out: dict[str, _Active] = {}
        if secret is not None and self.db is not None:
            async with self.db.session() as session:
                rows = (
                    await session.scalars(
                        select(LlmCredential).where(LlmCredential.status == "active")
                    )
                ).all()
            for row in rows:
                try:
                    key = open_sealed(secret, row.key_enc, row.name)
                except SecretBoxError:
                    log.error("llm.credential_undecryptable", name=row.name)
                    continue
                out[row.name] = _Active(row.id, row.key_prefix, key)
        self._cache = (time.monotonic(), out)
        return out

    async def resolve(self, agent_key: str | None) -> GatewayKey:
        if self.secret_key is None:
            return self.env_key()
        active = await self._active()
        if agent_key and (cred := active.get(credential_name(agent_key))):
            return GatewayKey(cred.key, "agent", cred.prefix, cred.id)
        if cred := active.get(SYSTEM):
            return GatewayKey(cred.key, "system", cred.prefix, cred.id)
        return self.env_key()

    async def mark_invalid(self, credential_id: uuid.UUID, error: str) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(LlmCredential)
                .where(LlmCredential.id == credential_id)
                .values(status="invalid", last_error=error[:300], last_checked_at=now())
            )
        self.invalidate()
        log.critical("llm.credential_invalid", credential_id=str(credential_id))


async def save_credential(
    session: AsyncSession,
    secret_key: str,
    *,
    agent_key: str | None,
    full_key: str,
    gateway_key_id: uuid.UUID | None,
) -> LlmCredential:
    """Simpan/ganti key (upsert per nama); baris lama ditandai `rotated_at`."""
    name = credential_name(agent_key)
    prefix = gateway_key_prefix(full_key)
    row = await session.scalar(select(LlmCredential).where(LlmCredential.name == name))
    if row is None:
        row = LlmCredential(name=name, agent_key=agent_key, key_prefix=prefix, key_enc="")
        session.add(row)
    else:
        row.rotated_at = now()
    row.key_prefix = prefix
    row.key_enc = seal(secret_key, full_key.strip(), name)
    row.gateway_key_id = gateway_key_id
    row.status = "active"
    row.last_error = None
    row.last_checked_at = now()
    await session.flush()
    return row


async def list_credentials(session: AsyncSession) -> list[LlmCredential]:
    return list((await session.scalars(select(LlmCredential).order_by(LlmCredential.name))).all())


def key_info(
    creds: dict[str, LlmCredential], agent_key: str, *, enabled: bool
) -> tuple[KeySource, LlmCredential | None]:
    """Sumber key yang AKAN dipakai agent (untuk tampilan; sama dengan `resolve`)."""
    if not enabled:
        return "env", None
    own = creds.get(credential_name(agent_key))
    if own is not None and own.status == "active":
        return "agent", own
    system = creds.get(SYSTEM)
    if system is not None and system.status == "active":
        return "system", own or system
    return "env", own
