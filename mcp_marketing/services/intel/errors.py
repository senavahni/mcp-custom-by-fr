"""Pemetaan ToolError internal -> kode error kontrak 1.2 (M3_MCP_INTEGRATION §4–5)."""

from __future__ import annotations

from typing import Any

from mcp_marketing.core.errors import ToolError

PASSTHROUGH = frozenset(
    {
        "VALIDATION_ERROR",
        "UNKNOWN_AGENT",
        "AGENT_INACTIVE",
        "AGENT_NOT_CAPABLE",
        "SOURCE_UNAVAILABLE",
        "WORKER_LOST",
        "NOT_FOUND",
    }
)
LLM_TRANSIENT = frozenset({"UPSTREAM_ERROR", "UPSTREAM_TIMEOUT", "RATE_LIMITED"})


def _info(code: str, message: str, retryable: bool) -> dict[str, Any]:
    return {"code": code, "message": message[:500], "retryable": retryable}


def map_error(exc: ToolError, stage: str | None) -> dict[str, Any]:
    details = exc.details or {}
    if exc.code in PASSTHROUGH:
        return _info(exc.code, exc.message, exc.retryable)
    if exc.code == "BUDGET_EXCEEDED":
        if details.get("scope") == "gateway":
            return _info("LLM_QUOTA_EXCEEDED", "Kuota project di LLM Gateway habis", False)
        return _info("BUDGET_EXCEEDED", exc.message, False)
    if details.get("reason") == "gateway_auth":
        return _info("LLM_KEY_INVALID", "API key LLM Gateway ditolak", False)
    if stage == "collect":
        return _info("SOURCE_UNAVAILABLE", exc.message, exc.retryable)
    if exc.code in LLM_TRANSIENT and exc.retryable:
        return _info("LLM_UNAVAILABLE", "LLM Gateway/provider tidak tersedia", True)
    if exc.code == "UPSTREAM_ERROR" and details.get("status") == 400:
        return _info("LLM_REQUEST_INVALID", exc.message, False)
    return _info(exc.code, exc.message, exc.retryable)
