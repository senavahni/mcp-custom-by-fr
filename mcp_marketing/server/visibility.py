"""Aturan visibilitas tool per klien (MCP_SPEC §1.3) — dipakai tools/list dan toolbox_list."""

from __future__ import annotations

from collections.abc import Iterable

from mcp_marketing.core.config import Settings
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.registry import ToolSpec, all_tools

TOOLSET_DESCRIPTIONS: dict[str, str] = {
    "toolbox": "Daftar toolset & tool yang tersedia",
    "data": "Baca/agregasi/export data besar lewat handle",
    "job": "Status, tunggu, batal, daftar job asinkron",
    "pm": "Kontrak Dashboard (dispatch/estimasi/mention/prompt/cancel)",
    "pm_agent": "Konteks project Dashboard untuk agent",
    "tpl": "Template dokumen Notion-like",
    "task": "Task & subtask Dashboard",
    "web": "Scraping web via profil Chrome",
    "social": "Crawling X/Twitter & Threads",
    "trends": "Google Trends",
    "db": "CRUD database Dashboard (whitelist)",
    "db_admin": "SQL read-only bebas (admin)",
    "kb": "Vector DB / knowledge base",
    "analysis": "Sentimen, pasar, campaign, strategi digital",
}


def parse_toolsets(raw: str | None) -> frozenset[str] | None:
    if not raw:
        return None
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def is_visible(
    spec: ToolSpec, client: ClientIdentity, settings: Settings, narrow: frozenset[str] | None = None
) -> bool:
    if spec.agent_only or (spec.dev_only and not settings.enable_dev_tools):
        return False
    if not client.can_use(spec.toolset, admin_only=spec.admin_only):
        return False
    return narrow is None or spec.toolset in narrow


def visible_tools(
    client: ClientIdentity,
    settings: Settings,
    narrow: frozenset[str] | None = None,
    tools: Iterable[ToolSpec] | None = None,
) -> list[ToolSpec]:
    return [
        s
        for s in (tools if tools is not None else all_tools())
        if is_visible(s, client, settings, narrow)
    ]
