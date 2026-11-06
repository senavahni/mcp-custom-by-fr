"""Admin Console: endpoint admin tambahan (rotasi klien, key LLM per agent, status .env, meta)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from pydantic import SecretStr

from mcp_marketing.server.services import Services
from mcp_marketing.services.agent.profile_admin import compose_prompt
from mcp_marketing.services.agent.profiles import get_active_profile
from mcp_marketing.services.seed import seed
from mcp_marketing.utils.secretbox import generate_key
from tests.integration.conftest import LiveServer, make_client
from tests.integration.test_gateway_keys import FakeAdminApi, fake_key


@pytest_asyncio.fixture
async def console(services: Services) -> AsyncIterator[tuple[Services, FakeAdminApi]]:
    async with services.db.session() as s:
        await seed(s)
    original_settings, original_http = services.settings, services.http
    services.settings = original_settings.model_copy(
        update={"mcp_secret_key": SecretStr(generate_key())}
    )
    api = FakeAdminApi()
    services.http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    try:
        yield services, api
    finally:
        await services.http.aclose()
        services.settings, services.http = original_settings, original_http


async def _admin(services: Services, live_server: LiveServer) -> httpx.AsyncClient:
    key, _ = await make_client(services, name="root", toolsets=["toolbox"], is_admin=True)
    return httpx.AsyncClient(base_url=live_server.url, headers={"Authorization": f"Bearer {key}"})


async def test_console_endpoints_require_admin(services: Services, live_server: LiveServer) -> None:
    key, _ = await make_client(services, name="plain", toolsets=["toolbox"])
    async with httpx.AsyncClient(base_url=live_server.url) as http:
        for path in ("/admin/meta", "/admin/env-status", "/admin/llm-credentials", "/admin/agents"):
            assert (await http.get(path)).status_code == 401
            r = await http.get(path, headers={"Authorization": f"Bearer {key}"})
            assert r.status_code == 403
        assert (await http.get("/console")).status_code == 200  # halaman statis tanpa data


async def test_rotate_client_returns_new_key_and_kills_old(
    services: Services, live_server: LiveServer
) -> None:
    async with await _admin(services, live_server) as http:
        created = (
            await http.post("/admin/clients", json={"name": "cli", "toolsets": ["toolbox"]})
        ).json()
        old_key = created["api_key"]
        r = await http.post(f"/admin/clients/{created['client']['id']}/rotate")
        assert r.status_code == 200
        new_key = r.json()["api_key"]
        assert new_key.startswith("mcpk_") and new_key != old_key
    async with httpx.AsyncClient(base_url=live_server.url) as http:
        old = await http.post("/mcp", json={}, headers={"Authorization": f"Bearer {old_key}"})
        assert old.status_code == 401
        new = await http.get("/admin/meta", headers={"Authorization": f"Bearer {new_key}"})
        assert new.status_code == 403  # dikenali (bukan 401), hanya tidak admin


async def test_llm_credentials_provision_import_disable(console, live_server: LiveServer) -> None:
    services, gateway_api = console
    async with await _admin(services, live_server) as http:
        meta = (await http.get("/admin/meta")).json()
        assert {"researcher", "analyst", "strategist"} <= {a["agent_key"] for a in meta["agents"]}
        before = (await http.get("/admin/llm-credentials")).json()
        assert before["enabled"] is True and before["credentials"] == []
        assert {a["source"] for a in before["effective"]} == {"env"}

        r = await http.post(
            "/admin/llm-credentials/provision",
            json={"admin_key": "lgw_admin_console", "daily_usd": 5, "monthly_usd": 50},
        )
        assert r.status_code == 200, r.text
        assert {row["action"] for row in r.json()["report"]} == {"created"}
        assert all(a == "Bearer lgw_admin_console" for a in gateway_api.auth)

        listed = (await http.get("/admin/llm-credentials")).json()
        assert {a["source"] for a in listed["effective"]} == {"agent"}
        full_keys = [k["key"] for k in gateway_api.keys.values()]
        assert not any(k in json.dumps(listed) for k in full_keys)

        manual = fake_key()
        r = await http.post(
            "/admin/llm-credentials/import", json={"agent": "analyst", "key": manual}
        )
        assert r.status_code == 200
        assert r.json()["credential"]["key_prefix"] == manual[:12]
        assert manual not in r.text
        bad = await http.post(
            "/admin/llm-credentials/import", json={"agent": "analyst", "key": "x"}
        )
        assert bad.status_code == 422
        unknown = await http.post(
            "/admin/llm-credentials/import", json={"agent": "hacker", "key": manual}
        )
        assert unknown.status_code == 422

        r = await http.post("/admin/llm-credentials/disable", json={"agent": "analyst"})
        assert r.status_code == 200
        after = {
            a["agent_key"]: a["source"]
            for a in (await http.get("/admin/llm-credentials")).json()["effective"]
        }
        assert after["analyst"] == "system"


async def test_provision_rejected_admin_key_and_missing_secret(
    console, live_server: LiveServer
) -> None:
    services, gateway_api = console
    gateway_api.handler = lambda request: httpx.Response(403, json={"error": {"code": "forbidden"}})  # type: ignore[method-assign]
    services.http = httpx.AsyncClient(transport=httpx.MockTransport(gateway_api.handler))
    async with await _admin(services, live_server) as http:
        r = await http.post(
            "/admin/llm-credentials/provision", json={"admin_key": "lgw_bukan_admin"}
        )
        assert r.status_code == 502
        assert "ditolak" in r.json()["error"]["message"]
        assert "lgw_bukan_admin" not in r.text
        services.settings = services.settings.model_copy(update={"mcp_secret_key": None})
        r = await http.post("/admin/llm-credentials/provision", json={"admin_key": "lgw_x"})
        assert r.status_code == 409
        assert "MCP_SECRET_KEY" in r.json()["error"]["message"]


NEW_AGENT = {
    "agent_key": "copywriter-free",
    "display_name": "Agent Copywriter (free)",
    "role_prompt": "Kamu copywriter pemasaran yang menulis caption singkat dan jelas.",
    "route": "openrouter/gemma-4-31b",
    "toolsets": ["pm_agent", "kb"],
    "run_token_budget": 60_000,
    "max_steps": 12,
}


async def test_create_agent_from_console_is_usable(console, live_server: LiveServer) -> None:
    services, _ = console
    async with await _admin(services, live_server) as http:
        r = await http.post("/admin/agents", json=NEW_AGENT)
        assert r.status_code == 201, r.text
        created = r.json()["agent"]
        assert created["route"] == "openrouter/gemma-4-31b"
        assert created["role_prompt"] == NEW_AGENT["role_prompt"]
        assert created["is_active"] is True and created["version"] == 1

        listed = (await http.get("/admin/agents")).json()
        assert "pm" not in listed["toolsets"]
        assert "copywriter-free" in {a["agent_key"] for a in listed["agents"]}
        # langsung dikenali fitur lain: meta, key LLM per agent, impor key
        meta = (await http.get("/admin/meta")).json()
        assert "copywriter-free" in {a["agent_key"] for a in meta["agents"]}
        imported = await http.post(
            "/admin/llm-credentials/import", json={"agent": "copywriter-free", "key": fake_key()}
        )
        assert imported.status_code == 200

        dup = await http.post("/admin/agents", json=NEW_AGENT)
        assert dup.status_code == 409

    async with services.db.session() as s:
        profile = await get_active_profile(s, "copywriter-free")
    assert profile is not None
    # aturan umum agent selalu digabung ke prompt peran
    assert profile.system_prompt == compose_prompt(NEW_AGENT["role_prompt"])
    assert profile.system_prompt != NEW_AGENT["role_prompt"]


async def test_update_and_deactivate_agent(console, live_server: LiveServer) -> None:
    services, _ = console
    async with await _admin(services, live_server) as http:
        assert (await http.post("/admin/agents", json=NEW_AGENT)).status_code == 201
        r = await http.patch(
            "/admin/agents/copywriter-free",
            json={
                "route": "openrouter/nemotron-3.5-lightning",
                "role_prompt": "Peran baru: penulis headline iklan.",
            },
        )
        assert r.status_code == 200, r.text
        agent = r.json()["agent"]
        assert agent["route"] == "openrouter/nemotron-3.5-lightning"
        assert agent["role_prompt"] == "Peran baru: penulis headline iklan."
        assert agent["toolsets"] == NEW_AGENT["toolsets"]  # field lain tidak berubah
        assert agent["version"] == 2

        off = await http.patch("/admin/agents/copywriter-free", json={"is_active": False})
        assert off.json()["agent"]["is_active"] is False
        assert (
            await http.patch("/admin/agents/tidak-ada", json={"max_steps": 5})
        ).status_code == 404
        assert (await http.patch("/admin/agents/copywriter-free", json={})).status_code == 422

    async with services.db.session() as s:
        assert await get_active_profile(s, "copywriter-free") is None


async def test_create_agent_rejects_invalid_input(console, live_server: LiveServer) -> None:
    services, _ = console
    bad_cases = [
        {**NEW_AGENT, "agent_key": "Bad Key!"},
        {**NEW_AGENT, "toolsets": ["pm"]},  # toolset Dashboard tidak untuk agent
        {**NEW_AGENT, "toolsets": ["tidak_ada"]},
        {**NEW_AGENT, "toolsets": []},
        {**NEW_AGENT, "route": "smart; drop"},
        {**NEW_AGENT, "role_prompt": "pendek"},
        {**NEW_AGENT, "max_steps": 0},
    ]
    async with await _admin(services, live_server) as http:
        for body in bad_cases:
            r = await http.post("/admin/agents", json=body)
            assert r.status_code == 422, body
        listed = (await http.get("/admin/agents")).json()["agents"]
    assert "copywriter-free" not in {a["agent_key"] for a in listed}


async def test_env_status_redacts_secrets(services: Services, live_server: LiveServer) -> None:
    async with await _admin(services, live_server) as http:
        r = await http.get("/admin/env-status")
    assert r.status_code == 200
    s = services.settings
    for secret in (
        s.mcp_key_pepper.get_secret_value(),
        s.dashboard.mcp_server_api_key.get_secret_value(),
        s.dashboard.mcp_service_token.get_secret_value(),
    ):
        assert secret not in r.text
