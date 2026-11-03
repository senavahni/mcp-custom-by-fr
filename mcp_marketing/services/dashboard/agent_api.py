"""Klien Agent API Dashboard (M1_MCP_INTEGRATION §6): konteks run, dipangkas sebelum ke LLM."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import httpx

from mcp_marketing.core import context
from mcp_marketing.core.config import Settings
from mcp_marketing.core.errors import (
    NotFound,
    RateLimited,
    RunNotActive,
    ToolError,
    UpstreamError,
    UpstreamTimeout,
    ValidationFailed,
)
from mcp_marketing.schemas.blocks import extract_plain_text
from mcp_marketing.services.dashboard.origin import same_origin
from mcp_marketing.utils.text import truncate

BLOCK_TEXT_MAX = 600
COMMENTS_KEEP = 10
COMMENT_MAX = 1_000
RPS_PER_RUN = 4.0
TIMEOUT_S = 15.0


class _RunLimiter:
    """Token bucket lokal 4 rps per run (Dashboard: 300 req/menit/run)."""

    def __init__(self, rps: float = RPS_PER_RUN) -> None:
        self.rps = rps
        self._next: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait(self, run_id: str) -> None:
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(run_id, now))
            self._next[run_id] = slot + 1.0 / self.rps
        delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)


def _walk(blocks: list[dict[str, Any]], depth: int = 0) -> Iterator[tuple[int, dict[str, Any]]]:
    for b in blocks:
        yield depth, b
        yield from _walk(b.get("children") or [], depth + 1)


def outline_blocks(
    blocks: list[dict[str, Any]], *, text_max: int = BLOCK_TEXT_MAX
) -> list[dict[str, Any]]:
    """Tree blok -> daftar datar ringkas `{id, type, depth, text, version}` (teks ≤ 600 char)."""
    out = []
    for depth, b in _walk(blocks):
        content = b.get("content_json") if isinstance(b.get("content_json"), dict) else {}
        text = b.get("plain_text") or extract_plain_text(str(b.get("block_type")), content or {})
        item: dict[str, Any] = {
            "id": b.get("id"),
            "type": b.get("block_type"),
            "text": truncate(str(text), text_max),
            "version": b.get("version"),
        }
        if depth:
            item["depth"] = depth
        if b.get("block_type") == "heading":
            item["level"] = (content or {}).get("props", {}).get("level", 1)
        out.append(item)
    return out


def _trim_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed = []
    for c in comments[-COMMENTS_KEEP:]:
        author = c.get("author") or {}
        trimmed.append(
            {
                "id": c.get("id"),
                "author": author.get("name") or author.get("display_name"),
                "author_type": author.get("type"),
                "content": truncate(str(c.get("content", "")), COMMENT_MAX),
                "created_at": c.get("created_at"),
            }
        )
    return trimmed


class AgentApiClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http
        self._limiter = _RunLimiter()

    def _base(self, base_url: str | None) -> str:
        api = self.settings.dashboard.dashboard_api_url
        # token layanan hanya boleh dikirim ke origin Dashboard yang dikonfigurasi
        if not base_url or not same_origin(base_url, api):
            base_url = f"{api.rstrip('/')}/agent-api"
        return base_url.rstrip("/")

    async def _request(
        self,
        method: str,
        run_id: str,
        path: str,
        *,
        base_url: str | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        await self._limiter.wait(run_id)
        url = f"{self._base(base_url)}/runs/{run_id}{path}"
        token = self.settings.dashboard.mcp_service_token.get_secret_value()
        headers = {
            "Authorization": f"Bearer {token}",
            **context.outgoing_headers(),
        }
        try:
            resp = await self.http.request(
                method, url, json=json, headers=headers, timeout=TIMEOUT_S
            )
        except httpx.TimeoutException:
            raise UpstreamTimeout("Agent API Dashboard timeout") from None
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Agent API Dashboard tidak dapat dihubungi ({type(exc).__name__})"
            ) from None
        if resp.status_code < 300:
            return resp.json()
        raise self._error(resp)

    @staticmethod
    def _error(resp: httpx.Response) -> ToolError:
        try:
            body = resp.json().get("error", {})
        except ValueError:
            body = {}
        code = str(body.get("code") or "")
        message = truncate(str(body.get("message") or resp.reason_phrase), 300)
        if resp.status_code == 409 and code == "RUN_NOT_ACTIVE":
            return RunNotActive(f"Run tidak aktif di Dashboard: {message}")
        if resp.status_code == 404:
            return NotFound(message)
        if resp.status_code == 429:
            return RateLimited("Agent API membatasi laju request")
        if resp.status_code in (400, 422):
            return ValidationFailed(message, details={"dashboard_code": code})
        if resp.status_code in (401, 403):
            return UpstreamError("MCP_SERVICE_TOKEN ditolak Dashboard", retryable=False)
        return UpstreamError(
            f"Agent API error {resp.status_code}", details={"dashboard_code": code}
        )

    # ------------------------------------------------------------------ endpoint
    async def get_run(self, run_id: str, *, base_url: str | None = None) -> dict[str, Any]:
        data = await self._request("GET", run_id, "", base_url=base_url)
        comment = data.get("trigger_comment")
        if isinstance(comment, dict):
            data["trigger_comment"] = _trim_comments([comment])[0]
        return data  # type: ignore[no-any-return]

    async def get_task(
        self, run_id: str, task_id: str, *, base_url: str | None = None
    ) -> dict[str, Any]:
        data = await self._request("GET", run_id, f"/tasks/{task_id}", base_url=base_url)
        return {
            "task": data.get("task"),
            "blocks": outline_blocks(data.get("blocks") or []),
            "subtasks": data.get("subtasks") or [],
            "comments": _trim_comments(data.get("comments") or []),
        }

    async def get_project_brief(
        self, run_id: str, *, base_url: str | None = None
    ) -> dict[str, Any]:
        data = await self._request("GET", run_id, "/project-brief", base_url=base_url)
        return {
            "project_id": data.get("project_id"),
            "blocks": outline_blocks(data.get("blocks") or []),
        }

    async def query_context(
        self,
        run_id: str,
        query: str,
        *,
        scope: str = "project",
        task_id: str | None = None,
        top_k: int = 8,
        base_url: str | None = None,
    ) -> list[dict[str, Any]]:
        body = {
            "query": query[:500],
            "scope": scope,
            "task_id": task_id,
            "top_k": max(1, min(20, top_k)),
        }
        data = await self._request("POST", run_id, "/context/query", base_url=base_url, json=body)
        return [
            {
                "block_id": r.get("block_id"),
                "task_title": r.get("task_title"),
                "source": r.get("source"),
                "text": truncate(str(r.get("text", "")), BLOCK_TEXT_MAX),
                "rank": r.get("rank"),
            }
            for r in data.get("results", [])
        ]
