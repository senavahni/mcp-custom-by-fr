"""M3_TASK_009: provisioning key Gateway per agent, pemilihan key per panggilan, key ditolak."""

from __future__ import annotations

import json
import secrets
import string
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import httpx2
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select

from mcp_marketing.core import context
from mcp_marketing.core.errors import UpstreamError
from mcp_marketing.models import LlmCredential
from mcp_marketing.server.app import build_asgi_app
from mcp_marketing.server.services import Services
from mcp_marketing.services.llm.gateway_admin import GatewayAdmin, provision
from mcp_marketing.services.llm.gateway_client import GatewayClient
from mcp_marketing.services.seed import seed
from mcp_marketing.utils.secretbox import generate_key
from tests.fakes import FakeGateway, completion
from tests.integration.conftest import make_client

AGENTS = ["researcher", "analyst", "strategist"]


def fake_key() -> str:
    prefix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    return f"lgw_{prefix}_{secrets.token_urlsafe(32)}"


@dataclass
class FakeAdminApi:
    """Admin API LLM Gateway palsu: project, key (dikembalikan sekali), revoke, kuota."""

    projects: list[dict[str, Any]] = field(default_factory=list)
    keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    revoked: list[str] = field(default_factory=list)
    quotas: list[dict[str, Any]] = field(default_factory=list)
    auth: list[str] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth.append(request.headers.get("authorization", ""))
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if path == "/admin/projects" and method == "GET":
            return httpx.Response(200, json={"data": self.projects, "next_cursor": None})
        if path == "/admin/projects" and method == "POST":
            project = {"id": str(uuid.uuid4()), **body}
            self.projects.append(project)
            return httpx.Response(201, json=project)
        if path.endswith("/keys") and method == "POST":
            key_id, full = str(uuid.uuid4()), fake_key()
            self.keys[key_id] = {"name": body["name"], "key": full, "scopes": body["scopes"]}
            return httpx.Response(201, json={"id": key_id, "key": full, "key_prefix": full[:12]})
        if path.endswith("/revoke"):
            self.revoked.append(path.split("/")[3])
            return httpx.Response(200, json={})
        if path.endswith("/quotas"):
            self.quotas.extend(body["quotas"])
            return httpx.Response(200, json={"data": body["quotas"]})
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    def key_named(self, name: str) -> str:
        return next(k["key"] for k in self.keys.values() if k["name"] == name)


@dataclass
class Env:
    services: Services
    secret: str
    admin_api: FakeAdminApi
    admin: GatewayAdmin
    gateway: FakeGateway


@pytest_asyncio.fixture
async def env(services: Services) -> AsyncIterator[Env]:
    async with services.db.session() as s:
        await seed(s)
    secret = generate_key()
    original_settings, original_llm = services.settings, services.llm
    services.settings = original_settings.model_copy(update={"mcp_secret_key": SecretStr(secret)})
    api = FakeAdminApi()
    http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    gateway = FakeGateway().on("/chat/completions", httpx2.Response(200, json=completion("ok")))
    services.llm = GatewayClient(services.settings, db=services.db, http_client=gateway.client())
    try:
        yield Env(services, secret, api, GatewayAdmin(http, "http://gw.test", "lgw_admin"), gateway)
    finally:
        await services.llm.drain()
        await http.aclose()
        services.settings, services.llm = original_settings, original_llm


async def creds(services: Services) -> dict[str, LlmCredential]:
    async with services.db.session() as s:
        return {c.name: c for c in (await s.scalars(select(LlmCredential))).all()}


def bearer(request: httpx2.Request) -> str:
    return request.headers["authorization"].removeprefix("Bearer ")


async def test_provision_creates_project_keys_encrypted_and_is_idempotent(env: Env) -> None:
    report = await provision(env.services.db, env.secret, env.admin, agents=AGENTS, daily_usd=5)
    assert [r["action"] for r in report] == ["created"] * 4
    assert env.admin_api.projects[0]["slug"] == "marketing-mcp"
    assert "smart" in env.admin_api.projects[0]["allowed_models"]
    assert env.admin_api.quotas == [{"period": "daily", "max_cost_usd": "5.00"}]
    assert all(a == "Bearer lgw_admin" for a in env.admin_api.auth)
    rows = await creds(env.services)
    assert set(rows) == {"system", "agent:researcher", "agent:analyst", "agent:strategist"}
    for row in rows.values():
        full = env.admin_api.key_named(f"mcp-{row.agent_key or 'system'}")
        assert full not in row.key_enc and row.key_prefix == full[:12]  # hanya terenkripsi
    for k in env.admin_api.keys.values():
        assert k["scopes"] == ["chat", "embeddings", "models:read", "usage:read"]

    again = await provision(env.services.db, env.secret, env.admin, agents=AGENTS)
    assert [r["action"] for r in again] == ["skipped"] * 4
    assert len(env.admin_api.projects) == 1 and len(env.admin_api.keys) == 4

    old_ids = {str(r.gateway_key_id) for r in rows.values()}
    forced = await provision(env.services.db, env.secret, env.admin, agents=["analyst"], force=True)
    assert [r["action"] for r in forced] == ["rotated", "rotated"]
    assert set(env.admin_api.revoked) <= old_ids and len(env.admin_api.revoked) == 2


async def test_each_agent_calls_gateway_with_own_key(env: Env) -> None:
    await provision(env.services.db, env.secret, env.admin, agents=["analyst"])
    llm = env.services.llm
    assert llm is not None
    with context.context_scope(agent_key="analyst", stage="analyze", job_id="j_0000000001"):
        await llm.chat("summarize_short", [{"role": "user", "content": "a"}])
    with context.context_scope(agent_key="researcher"):  # belum punya key -> system
        await llm.chat("summarize_short", [{"role": "user", "content": "b"}])
    await llm.chat("summarize_short", [{"role": "user", "content": "c"}])  # tanpa agent -> system
    calls = env.gateway.calls_to("/chat/completions")
    assert [bearer(c) for c in calls] == [
        env.admin_api.key_named("mcp-analyst"),
        env.admin_api.key_named("mcp-system"),
        env.admin_api.key_named("mcp-system"),
    ]
    assert calls[0].headers["x-caller-agent"] == "analyst"


async def test_without_secret_key_env_key_is_used(env: Env) -> None:
    await provision(env.services.db, env.secret, env.admin, agents=["analyst"])
    settings = env.services.settings.model_copy(update={"mcp_secret_key": None})
    llm = GatewayClient(settings, db=env.services.db, http_client=env.gateway.client())
    with context.context_scope(agent_key="analyst"):
        await llm.chat("summarize_short", [{"role": "user", "content": "a"}])
    assert bearer(env.gateway.calls_to("/chat/completions")[-1]) == "lgw_test_key"
    await llm.close()


async def test_rejected_agent_key_marked_invalid_then_fallback(env: Env) -> None:
    await provision(env.services.db, env.secret, env.admin, agents=["analyst"])
    env.gateway.replies["/chat/completions"] = [
        httpx2.Response(401, json={"error": {"code": "invalid_api_key", "message": "x"}}),
        httpx2.Response(200, json=completion("ok")),
    ]
    llm = env.services.llm
    assert llm is not None
    with context.context_scope(agent_key="analyst"), pytest.raises(UpstreamError) as exc:
        await llm.chat("summarize_short", [{"role": "user", "content": "a"}])
    assert exc.value.details and exc.value.details["reason"] == "gateway_auth"
    row = (await creds(env.services))["agent:analyst"]
    assert row.status == "invalid" and "401" in (row.last_error or "")
    with context.context_scope(agent_key="analyst"):
        await llm.chat("summarize_short", [{"role": "user", "content": "b"}])
    assert bearer(env.gateway.calls_to("/chat/completions")[-1]) == env.admin_api.key_named(
        "mcp-system"
    )


async def test_intel_tools_report_key_sources(env: Env) -> None:
    await provision(env.services.db, env.secret, env.admin, agents=["analyst"])
    _, pipeline, _ = build_asgi_app(env.services)
    _, client = await make_client(env.services, name="dashboard-intel", toolsets=["intel"])
    agents = {
        a["agent_key"]: a["gateway_key"]
        for a in (await pipeline.call("intel_agents_list", {}, client=client)).structured["agents"]
    }
    rows = await creds(env.services)
    assert agents["analyst"] == {"source": "agent", "prefix": rows["agent:analyst"].key_prefix}
    assert agents["researcher"] == {"source": "system", "prefix": rows["system"].key_prefix}
    status = (await pipeline.call("intel_llm_status", {"range_days": 7}, client=client)).structured
    by_agent = {a["agent_key"]: a for a in status["agents"]}
    assert (
        by_agent["analyst"]["key_status"] == "active"
        and by_agent["analyst"]["key_source"] == "agent"
    )
    dumped = json.dumps(status) + json.dumps(agents)
    assert all(k["key"] not in dumped for k in env.admin_api.keys.values())  # hanya prefix
    await pipeline.drain()
