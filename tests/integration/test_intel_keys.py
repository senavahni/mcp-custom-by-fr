"""M3_TASK_009 P1: rotasi key Gateway per agent lewat tool `intel_llm_key_rotate` (Admin API palsu)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import httpx2
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select

from mcp_marketing.core import context
from mcp_marketing.models import LlmCredential
from mcp_marketing.server.app import build_asgi_app
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.pipeline import ToolPipeline
from mcp_marketing.server.services import Services
from mcp_marketing.services.llm.gateway_admin import GatewayAdmin, provision
from mcp_marketing.services.llm.gateway_client import GatewayClient
from mcp_marketing.services.seed import seed
from mcp_marketing.utils.secretbox import generate_key, open_sealed
from tests.fakes import FakeGateway, completion
from tests.integration.conftest import make_client
from tests.integration.test_gateway_keys import FakeAdminApi, bearer

ADMIN_KEY = "lgw_admin_rotate_test"
TOOL = "intel_llm_key_rotate"


@dataclass
class Env:
    services: Services
    pipeline: ToolPipeline
    client: ClientIdentity
    secret: str
    admin_api: FakeAdminApi
    gateway: FakeGateway

    async def rotate(self, agent_key: str) -> Any:
        return await self.pipeline.call(TOOL, {"agent_key": agent_key}, client=self.client)

    async def credential(self, name: str) -> LlmCredential:
        async with self.services.db.session() as session:
            return (
                await session.scalars(select(LlmCredential).where(LlmCredential.name == name))
            ).one()

    def use_admin_transport(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.services.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def env(services: Services) -> AsyncIterator[Env]:
    async with services.db.session() as s:
        await seed(s)
    secret = generate_key()
    original = (services.settings, services.llm, services.http)
    llm_settings = services.settings.llm.model_copy(
        update={
            "llm_gateway_admin_key": SecretStr(ADMIN_KEY),
            "llm_gateway_url": "http://gw.test/v1",
        }
    )
    services.settings = services.settings.model_copy(
        update={"mcp_secret_key": SecretStr(secret), "llm": llm_settings}
    )
    api = FakeAdminApi()
    gateway = FakeGateway().on("/chat/completions", httpx2.Response(200, json=completion("ok")))
    services.llm = GatewayClient(services.settings, db=services.db, http_client=gateway.client())
    services.http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    admin = GatewayAdmin(services.http, "http://gw.test", ADMIN_KEY)
    await provision(services.db, secret, admin, agents=["analyst", "researcher"])
    _, pipeline, _ = build_asgi_app(services)
    _, client = await make_client(services, name="dashboard-intel", toolsets=["intel"])
    try:
        yield Env(services, pipeline, client, secret, api, gateway)
    finally:
        await pipeline.drain()
        await services.llm.drain()
        await services.http.aclose()
        services.settings, services.llm, services.http = original


async def test_rotate_creates_new_key_stores_encrypted_and_revokes_old(env: Env) -> None:
    before = await env.credential("agent:analyst")
    old_key = env.admin_api.key_named("mcp-analyst")
    keys_before = set(env.admin_api.keys)

    out = await env.rotate("analyst")

    assert not out.is_error, out.structured
    (new_id,) = set(env.admin_api.keys) - keys_before
    new_key = env.admin_api.keys[new_id]["key"]
    assert out.structured["agent_key"] == "analyst"
    assert out.structured["key_prefix"] == new_key[:12] != old_key[:12]
    assert out.structured["old_key_revoked"] is True
    assert out.structured["rotated_at"]
    assert env.admin_api.revoked == [str(before.gateway_key_id)]
    for secret in (new_key, old_key, ADMIN_KEY):
        assert secret not in json.dumps(out.structured)
    row = await env.credential("agent:analyst")
    assert row.status == "active" and row.key_prefix == new_key[:12]
    assert new_key not in row.key_enc
    assert open_sealed(env.secret, row.key_enc, row.name) == new_key
    assert str(row.gateway_key_id) == new_id and row.rotated_at is not None
    # agent lain tidak tersentuh
    other = await env.credential("agent:researcher")
    assert other.key_prefix == env.admin_api.key_named("mcp-researcher")[:12]


async def test_next_llm_call_uses_rotated_key(env: Env) -> None:
    llm = env.services.llm
    assert llm is not None
    with context.context_scope(agent_key="analyst"):
        await llm.chat("summarize_short", [{"role": "user", "content": "a"}])
    await env.rotate("analyst")
    with context.context_scope(agent_key="analyst"):
        await llm.chat("summarize_short", [{"role": "user", "content": "b"}])
    first, second = env.gateway.calls_to("/chat/completions")
    assert bearer(first) != bearer(second)
    assert bearer(second) in {k["key"] for k in env.admin_api.keys.values()}


async def test_rotate_system_key(env: Env) -> None:
    out = await env.rotate("system")
    assert not out.is_error, out.structured
    assert out.structured["agent_key"] == "system"
    assert (await env.credential("system")).rotated_at is not None


async def test_rejection_right_after_rotation_does_not_invalidate_new_key(env: Env) -> None:
    await env.rotate("analyst")
    row = await env.credential("agent:analyst")
    store = env.services.llm.credentials  # type: ignore[union-attr]
    await store.mark_invalid(row.id, "worker masih memakai key lama")
    assert (await env.credential("agent:analyst")).status == "active"


@pytest.mark.parametrize(
    ("agent_key", "code"),
    [
        ("ghost_agent", "UNKNOWN_AGENT"),
        ("strategist", "NOT_FOUND"),
        ("Bad Key!", "VALIDATION_ERROR"),
    ],
)
async def test_rotate_rejects_unknown_or_unprovisioned_agent(
    env: Env, agent_key: str, code: str
) -> None:
    calls_before = len(env.admin_api.auth)
    out = await env.rotate(agent_key)
    assert out.is_error and out.structured["code"] == code
    assert len(env.admin_api.auth) == calls_before  # ditolak sebelum menyentuh Admin API


async def test_rotate_requires_secret_and_admin_key(env: Env) -> None:
    settings = env.services.settings
    env.services.settings = settings.model_copy(
        update={"llm": settings.llm.model_copy(update={"llm_gateway_admin_key": None})}
    )
    out = await env.rotate("analyst")
    assert out.is_error and out.structured["code"] == "CONFLICT"
    assert "LLM_GATEWAY_ADMIN_KEY" in out.structured["message"]
    env.services.settings = settings.model_copy(update={"mcp_secret_key": None})
    out = await env.rotate("analyst")
    assert out.is_error and "MCP_SECRET_KEY" in out.structured["message"]


async def test_rotate_with_rejected_admin_key_keeps_old_key(env: Env) -> None:
    before = await env.credential("agent:analyst")
    env.use_admin_transport(
        lambda _r: httpx.Response(401, json={"error": {"code": "invalid_api_key"}})
    )
    out = await env.rotate("analyst")
    assert out.is_error and out.structured["code"] == "UPSTREAM_ERROR"
    assert "ditolak" in out.structured["message"] and ADMIN_KEY not in json.dumps(out.structured)
    after = await env.credential("agent:analyst")
    assert (after.key_prefix, after.key_enc, after.status) == (
        before.key_prefix,
        before.key_enc,
        "active",
    )


async def test_rotate_when_gateway_unreachable(env: Env) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    env.use_admin_transport(down)
    out = await env.rotate("analyst")
    assert out.is_error and out.structured["code"] == "UPSTREAM_ERROR"
    assert out.structured["retryable"] is True


async def test_rotate_reports_when_old_key_cannot_be_revoked(env: Env) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/revoke"):
            return httpx.Response(500, json={"error": {"code": "internal"}})
        return env.admin_api.handler(request)

    env.use_admin_transport(handler)
    out = await env.rotate("analyst")
    assert not out.is_error, out.structured
    assert out.structured["old_key_revoked"] is False
    new_key = (await env.credential("agent:analyst")).key_prefix
    assert new_key == out.structured["key_prefix"]


async def test_new_key_is_revoked_when_it_cannot_be_stored(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_marketing.services.llm import gateway_admin

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(gateway_admin, "save_credential", boom)
    keys_before = set(env.admin_api.keys)
    out = await env.rotate("analyst")
    assert out.is_error
    (new_id,) = set(env.admin_api.keys) - keys_before
    assert env.admin_api.revoked == [new_id]
