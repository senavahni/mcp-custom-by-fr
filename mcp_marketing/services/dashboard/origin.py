"""Batasi URL callback dari Dashboard ke origin `DASHBOARD_API_URL`.

Callback (`webhook_url`, `agent_api_base_url`) datang dari pemanggil tool `pm_*`; tanpa pengecekan,
MCP_SERVICE_TOKEN dan webhook bertanda tangan bisa dikirim ke host lain (SSRF / kebocoran token).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from mcp_marketing.core.errors import ValidationFailed

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or _DEFAULT_PORTS.get(scheme, 0)
    return scheme, (parts.hostname or "").lower(), port


def same_origin(url: str, allowed_base: str) -> bool:
    try:
        target = _origin(url)
    except ValueError:
        return False
    return target[0] in _DEFAULT_PORTS and target == _origin(allowed_base)


def require_dashboard_origin(url: str, allowed_base: str, field: str) -> str:
    if not same_origin(url, allowed_base):
        raise ValidationFailed(
            f"{field} harus ber-origin sama dengan DASHBOARD_API_URL",
            retryable=False,
            details={"field": field},
        )
    return url
