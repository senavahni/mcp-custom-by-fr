"""Typed client for the Dashboard MCP Service API (contract 1.1)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from mcp_marketing.core.config import Settings
from mcp_marketing.core.errors import (
    Conflict,
    Forbidden,
    HierarchyDepthExceeded,
    NotFound,
    RateLimited,
    ToolError,
    UpstreamError,
    UpstreamTimeout,
    ValidationFailed,
)
from mcp_marketing.services.dashboard.origin import same_origin
from mcp_marketing.utils.text import truncate

TIMEOUT_S = 20.0
RPS_PER_AGENT = 2.0


@dataclass
class _AgentLimiter:
    _next: dict[str, float] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def wait(self, agent_id: str) -> None:
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(agent_id, now))
            self._next[agent_id] = slot + 1 / RPS_PER_AGENT
        if slot > now:
            await asyncio.sleep(slot - now)


class DashboardServiceApi:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http
        self._limiter = _AgentLimiter()

    async def request(
        self,
        method: str,
        agent_id: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        await self._limiter.wait(agent_id)
        base = self.settings.dashboard.dashboard_api_url.rstrip("/")
        if not same_origin(base, self.settings.dashboard.dashboard_api_url):
            raise ValidationFailed("DASHBOARD_API_URL tidak valid")
        token = self.settings.dashboard.mcp_service_token.get_secret_value()
        try:
            response = await self.http.request(
                method,
                f"{base}/mcp-api/{path.lstrip('/')}",
                json=json,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-MCP-Agent-Id": agent_id,
                    "Accept": "application/json",
                },
                timeout=TIMEOUT_S,
            )
        except httpx.TimeoutException:
            raise UpstreamTimeout("Dashboard Service API timeout") from None
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Dashboard Service API tidak dapat dihubungi ({type(exc).__name__})"
            ) from None
        if response.status_code < 300:
            version = response.headers.get("X-Contract-Version")
            if version != "1.1":
                raise UpstreamError(
                    "Dashboard tidak mengirim X-Contract-Version 1.1",
                    retryable=False,
                    details={"contract_version": version},
                )
            if response.status_code == 204:
                return {}
            try:
                return response.json()
            except ValueError:
                raise UpstreamError("Dashboard mengembalikan JSON tidak valid") from None
        raise self._error(response)

    @staticmethod
    def _error(response: httpx.Response) -> ToolError:
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}
        code = str(error.get("code") or "")
        message = truncate(str(error.get("message") or response.reason_phrase), 300)
        if response.status_code == 404:
            return NotFound(message)
        if response.status_code == 409:
            return Conflict(message, details={"dashboard_code": code})
        if response.status_code == 422 and code == "HIERARCHY_DEPTH_EXCEEDED":
            return HierarchyDepthExceeded(message)
        if response.status_code in (400, 422):
            return ValidationFailed(message, details={"dashboard_code": code})
        if response.status_code == 429:
            return RateLimited("Dashboard membatasi laju Service API")
        if response.status_code == 403:
            return Forbidden(message)
        if response.status_code == 401:
            return UpstreamError("MCP_SERVICE_TOKEN atau X-MCP-Agent-Id ditolak", retryable=False)
        return UpstreamError(
            f"Dashboard Service API error {response.status_code}",
            details={"dashboard_code": code},
        )

    async def list_projects(self, agent_id: str, workspace_id: str, **params: Any) -> Any:
        return await self.request(
            "GET", agent_id, f"workspaces/{workspace_id}/projects", params=params
        )

    async def create_project(self, agent_id: str, body: dict[str, Any]) -> Any:
        return await self.request("POST", agent_id, "projects", json=body)

    async def list_tasks(self, agent_id: str, project_id: str, **params: Any) -> Any:
        return await self.request("GET", agent_id, f"projects/{project_id}/tasks", params=params)

    async def create_tasks(
        self, agent_id: str, project_id: str, tasks: list[dict[str, Any]]
    ) -> Any:
        return await self.request(
            "POST", agent_id, f"projects/{project_id}/tasks", json={"tasks": tasks}
        )

    async def create_subtasks(
        self, agent_id: str, task_id: str, tasks: list[dict[str, Any]]
    ) -> Any:
        return await self.request(
            "POST", agent_id, f"tasks/{task_id}/subtasks", json={"tasks": tasks}
        )

    async def update_task(self, agent_id: str, task_id: str, body: dict[str, Any]) -> Any:
        return await self.request("PATCH", agent_id, f"tasks/{task_id}", json=body)

    async def move_task(self, agent_id: str, task_id: str, body: dict[str, Any]) -> Any:
        return await self.request("PATCH", agent_id, f"tasks/{task_id}/move", json=body)

    async def reschedule_task(self, agent_id: str, task_id: str, body: dict[str, Any]) -> Any:
        return await self.request(
            "PATCH", agent_id, f"tasks/{task_id}/reschedule", json=body
        )

    async def apply_block_operations(self, agent_id: str, body: dict[str, Any]) -> Any:
        return await self.request("POST", agent_id, "blocks/ops", json=body)

    async def create_comment(self, agent_id: str, body: dict[str, Any]) -> Any:
        return await self.request("POST", agent_id, "comments", json=body)
