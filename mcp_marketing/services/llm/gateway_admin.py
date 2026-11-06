"""Provisioning project & key LLM Gateway untuk agent MCP lewat Admin API Gateway (M3_TASK_009).

Admin key Gateway untuk `manage.py gateway-keys provision` hanya dipakai selama perintah berjalan,
tidak pernah disimpan. Rotasi dari Dashboard (`intel_llm_key_rotate`) membacanya dari
`LLM_GATEWAY_ADMIN_KEY` di environment MCP; nilainya tidak pernah ditulis ke DB atau log.
Hasil: project `marketing-mcp` + satu key per agent + key `system`,
disimpan terenkripsi di `mcp.llm_credentials`.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select

from mcp_marketing.core.errors import NotFound, UpstreamError
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import LlmCredential
from mcp_marketing.services.llm.credentials import (
    credential_name,
    list_credentials,
    save_credential,
)
from mcp_marketing.utils.time import now

log = get_logger(__name__)

DEFAULT_PROJECT = "marketing-mcp"
KEY_SCOPES = ["chat", "embeddings", "models:read", "usage:read"]
# route DAN kandidatnya harus diizinkan (Gateway docs/admin-api.http)
ALLOWED_MODELS = [
    "fast",
    "smart",
    "embed-default",
    "openai/*",
    "anthropic/*",
    "gemini/*",
    "openrouter/*",
]


def admin_base_url(gateway_v1_url: str) -> str:
    base = gateway_v1_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


@dataclass
class GatewayAdmin:
    http: httpx.AsyncClient
    base_url: str
    admin_key: str

    async def _req(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        resp = await self.http.request(
            method,
            f"{self.base_url}{path}",
            json=body,
            headers={"Authorization": f"Bearer {self.admin_key}"},
            timeout=30,
        )
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error") or {}
            except ValueError:
                err = {}
            raise UpstreamError(
                f"Admin API Gateway {method} {path} -> {resp.status_code} {err.get('code') or ''}",
                retryable=False,
                details={"status": resp.status_code, "code": err.get("code")},
            )
        return resp.json() if resp.content else {}

    async def ensure_project(self, slug: str) -> dict[str, Any]:
        projects = (await self._req("GET", "/admin/projects")).get("data") or []
        found = next((p for p in projects if p.get("slug") == slug), None)
        if found is not None:
            return found  # type: ignore[no-any-return]
        return await self._req(  # type: ignore[no-any-return]
            "POST",
            "/admin/projects",
            {
                "slug": slug,
                "name": "MCP Marketing agents",
                "owner": "mcp-marketing",
                "log_payloads": False,
                "allowed_models": ALLOWED_MODELS,
            },
        )

    async def create_key(self, project_id: str, name: str, *, rpm: int | None) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "scopes": KEY_SCOPES}
        if rpm:
            body["rpm_limit"] = rpm
        return await self._req("POST", f"/admin/projects/{project_id}/keys", body)  # type: ignore[no-any-return]

    async def revoke_key(self, key_id: str, reason: str) -> None:
        await self._req("POST", f"/admin/keys/{key_id}/revoke", {"reason": reason})

    async def set_quotas(
        self, project_id: str, *, daily_usd: float | None, monthly_usd: float | None
    ) -> None:
        quotas = []
        if daily_usd is not None:
            quotas.append({"period": "daily", "max_cost_usd": f"{daily_usd:.2f}"})
        if monthly_usd is not None:
            quotas.append({"period": "monthly", "max_cost_usd": f"{monthly_usd:.2f}"})
        if quotas:
            await self._req("PUT", f"/admin/projects/{project_id}/quotas", {"quotas": quotas})


async def provision(
    db: Any,
    secret_key: str,
    admin: GatewayAdmin,
    *,
    agents: list[str],
    project_slug: str = DEFAULT_PROJECT,
    rpm: int | None = None,
    force: bool = False,
    daily_usd: float | None = None,
    monthly_usd: float | None = None,
) -> list[dict[str, Any]]:
    """Buat key yang belum ada (atau semua bila `force`, key lama dicabut). Idempoten."""
    project = await admin.ensure_project(project_slug)
    await admin.set_quotas(project["id"], daily_usd=daily_usd, monthly_usd=monthly_usd)
    async with db.session() as session:
        existing = {c.name: c for c in await list_credentials(session)}
    report = []
    for agent_key in [None, *agents]:
        name = credential_name(agent_key)
        current = existing.get(name)
        if current is not None and current.status == "active" and not force:
            report.append({"name": name, "prefix": current.key_prefix, "action": "skipped"})
            continue
        created = await admin.create_key(project["id"], f"mcp-{agent_key or 'system'}", rpm=rpm)
        async with db.session() as session:
            row = await save_credential(
                session,
                secret_key,
                agent_key=agent_key,
                full_key=created["key"],
                gateway_key_id=uuid.UUID(created["id"]),
            )
            prefix = row.key_prefix
        action = "rotated" if current else "created"
        if current is not None and current.gateway_key_id is not None:
            try:
                await admin.revoke_key(str(current.gateway_key_id), f"diganti -> {prefix}")
            except UpstreamError as exc:
                # key baru sudah tersimpan; key lama mungkin sudah dicabut di Gateway
                action = f"rotated (cabut key lama gagal: {exc.message})"
        report.append({"name": name, "prefix": prefix, "action": action})
    return report


@dataclass(frozen=True)
class RotatedKey:
    """Hasil rotasi; sengaja tanpa key lengkap."""

    prefix: str
    rotated_at: datetime
    old_key_revoked: bool


# satu rotasi sekali jalan: dua rotasi paralel untuk agent sama akan menyisakan key yatim di Gateway
_ROTATE_LOCK = asyncio.Lock()


async def rotate_credential(
    db: Any,
    secret_key: str,
    admin: GatewayAdmin,
    *,
    agent_key: str | None,
    project_slug: str = DEFAULT_PROJECT,
) -> RotatedKey:
    """Ganti key milik `agent_key` (None = system): buat baru, simpan, baru cabut yang lama.

    Urutan ini menjaga agent tetap punya key valid bila ada langkah yang gagal. Key lama yang
    gagal dicabut (atau hasil impor tanpa id Gateway) dilaporkan lewat `old_key_revoked=False`.
    """
    name = credential_name(agent_key)
    async with _ROTATE_LOCK:
        async with db.session() as session:
            current = await session.scalar(select(LlmCredential).where(LlmCredential.name == name))
            old_key_id = current.gateway_key_id if current is not None else None
        if current is None:
            raise NotFound(f"Belum ada key untuk {agent_key or 'system'}; jalankan provision dulu")
        project = await admin.ensure_project(project_slug)
        created = await admin.create_key(project["id"], f"mcp-{agent_key or 'system'}", rpm=None)
        try:
            async with db.session() as session:
                row = await save_credential(
                    session,
                    secret_key,
                    agent_key=agent_key,
                    full_key=created["key"],
                    gateway_key_id=uuid.UUID(created["id"]),
                )
                prefix, rotated_at = row.key_prefix, row.rotated_at or now()
        except Exception:
            # key baru belum tersimpan di mana pun: cabut agar tidak jadi key yatim
            with contextlib.suppress(UpstreamError):
                await admin.revoke_key(created["id"], "rotasi gagal disimpan")
            raise
        revoked = False
        if old_key_id is not None:
            try:
                await admin.revoke_key(str(old_key_id), f"diganti -> {prefix}")
                revoked = True
            except UpstreamError as exc:
                log.warning("llm.rotate_revoke_old_failed", name=name, status=exc.details)
    return RotatedKey(prefix=prefix, rotated_at=rotated_at, old_key_revoked=revoked)
