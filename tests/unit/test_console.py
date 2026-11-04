"""Admin Console: hanya dari localhost, header keamanan ketat, status .env tanpa nilai secret."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr
from starlette.applications import Starlette

from mcp_marketing.core.config import get_settings
from mcp_marketing.server.console import build_console_routes
from mcp_marketing.services.env_status import (
    env_vars,
    gateway_key_state,
    secret_key_state,
    secret_state,
)
from mcp_marketing.utils.secretbox import generate_key

SENTINEL = "x-bearer-SENTINEL-must-never-leak-0123456789"


def _client(peer: str = "127.0.0.1", host: str = "127.0.0.1:4100") -> httpx.AsyncClient:
    app = Starlette(routes=build_console_routes())
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 50000)), base_url=f"http://{host}"
    )


async def test_console_served_to_localhost_with_strict_headers() -> None:
    async with _client() as c:
        r = await c.get("/console")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("host", ["localhost:4100", "[::1]:4100", "127.0.0.1"])
async def test_console_accepts_local_host_names(host: str) -> None:
    async with _client(host=host) as c:
        assert (await c.get("/console/console.js")).status_code == 200


async def test_console_rejects_remote_peer() -> None:
    async with _client(peer="10.0.0.7") as c:
        assert (await c.get("/console")).status_code == 403


async def test_console_rejects_foreign_host_header() -> None:
    async with _client(host="evil.example:4100") as c:
        assert (await c.get("/console")).status_code == 403


@pytest.mark.parametrize("name", ["index.html", ".env", "..%2F.env", "x.py"])
async def test_console_serves_only_whitelisted_assets(name: str) -> None:
    async with _client() as c:
        assert (await c.get(f"/console/{name}")).status_code == 404


def test_secret_states() -> None:
    assert secret_state(None) == "missing"
    assert secret_state("CHANGE_ME_x") == "placeholder"
    assert secret_state("short", min_len=32) == "weak"
    assert secret_key_state(None) == "missing"
    assert secret_key_state(SecretStr("bukan-base64!")) == "invalid"
    assert secret_key_state(SecretStr(generate_key())) == "ok"
    assert gateway_key_state(SecretStr("lgw_CHANGE_ME")) == "placeholder"
    assert gateway_key_state(SecretStr("lgw_test_key")) == "invalid"
    assert gateway_key_state(SecretStr("lgw_abcd1234_" + "A" * 43)) == "ok"


def test_env_vars_never_expose_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_BEARER_TOKEN", SENTINEL)
    monkeypatch.setenv("MCP_SECRET_KEY", generate_key())
    get_settings.cache_clear()
    settings = get_settings()
    rows = env_vars(settings)
    dumped = json.dumps(rows)
    for secret in (
        SENTINEL,
        settings.mcp_key_pepper.get_secret_value(),
        settings.dashboard.mcp_server_api_key.get_secret_value(),
        settings.dashboard.mcp_webhook_secret.get_secret_value(),
        settings.dashboard.mcp_service_token.get_secret_value(),
        settings.llm.llm_gateway_api_key.get_secret_value(),
        settings.mcp_secret_key.get_secret_value(),  # type: ignore[union-attr]
    ):
        assert secret not in dumped
    by_name = {r["name"]: r for r in rows}
    assert by_name["X_BEARER_TOKEN"]["state"] == "ok"
    assert by_name["MCP_SECRET_KEY"]["state"] == "ok"
    assert all(r["value"] is None for r in rows if r["secret"])
