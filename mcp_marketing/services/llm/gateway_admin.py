"""Provisioning project & key LLM Gateway untuk agent MCP lewat Admin API Gateway (M3_TASK_009).

Admin key Gateway hanya dipakai selama perintah berjalan (`manage.py gateway-keys provision`),
tidak pernah disimpan. Hasil: project `marketing-mcp` + satu key per agent + key `system`,
disimpan terenkripsi di `mcp.llm_credentials`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from mcp_marketing.core.errors import UpstreamError
from mcp_marketing.services.llm.credentials import (
    credential_name,
    list_credentials,
    save_credential,
)

DEFAULT_PROJECT = "marketing-mcp"
KEY_SCOPES = ["chat", "embeddings", "models:read", "usage:read"]
# route DAN kandidatnya harus diizinkan (Gateway docs/admin-api.http)
ALLOWED_MODELS = ["fast", "smart", "embed-default", "openai/*", "anthropic/*", "gemini/*"]


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
