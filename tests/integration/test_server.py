"""TASK-04: transport Streamable HTTP, auth, visibilitas toolset, pipeline & audit."""

from __future__ import annotations

import time

import httpx
from sqlalchemy import select

from mcp_marketing.models import ToolCall
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.services import Services
from mcp_marketing.services.clients import revoke_client
from tests.integration.conftest import LiveServer, make_client, mcp_session


async def test_initialize_list_call_with_client_key(
    services: Services, live_server: LiveServer
) -> None:
    key, _ = await make_client(services, toolsets=["toolbox", "data"])
    for mode in ("legacy", "auto"):
        async with mcp_session(live_server.url, key, mode=mode) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"toolbox_list", "data_get", "data_aggregate", "toolbox_ping"} <= names
            assert not any(n.startswith("job_") for n in names)
            result = await client.call_tool("toolbox_ping", {"message": "halo"})
            assert result.is_error is False
            assert result.structured_content["pong"] is True
            assert result.content[0].text == "pong: halo"


async def test_toolbox_list_groups_visible_tools(
    services: Services, live_server: LiveServer
) -> None:
    key, _ = await make_client(services, toolsets=["toolbox", "data"])
    async with mcp_session(live_server.url, key) as client:
        out = (await client.call_tool("toolbox_list", {})).structured_content
    names = {ts["name"] for ts in out["toolsets"]}
    assert names == {"toolbox", "data"}
    data = next(ts for ts in out["toolsets"] if ts["name"] == "data")
    assert {t["name"] for t in data["tools"]} == {"data_get", "data_aggregate", "data_export"}
    assert all("summary" in t and "input_schema" not in t for t in data["tools"])


async def test_query_toolsets_narrows(services: Services, live_server: LiveServer) -> None:
    key, _ = await make_client(services, toolsets=["toolbox", "data"])
    async with mcp_session(live_server.url, key, path="/mcp?toolsets=data") as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {"data_get", "data_aggregate", "data_export"}


async def test_out_of_scope_call_forbidden(services: Services, live_server: LiveServer) -> None:
    key, _ = await make_client(services, toolsets=["toolbox"])
    async with mcp_session(live_server.url, key) as client:
        result = await client.call_tool("data_get", {"handle": "h_0123456789"})
    assert result.is_error is True
    assert result.structured_content["code"] == "FORBIDDEN"


async def test_validation_error_structured(services: Services, live_server: LiveServer) -> None:
    key, _ = await make_client(services, toolsets=["toolbox", "data"])
    async with mcp_session(live_server.url, key) as client:
        result = await client.call_tool("data_get", {"handle": "bukan-handle", "limit": 99})
    assert result.is_error is True
    sc = result.structured_content
    assert sc["code"] == "VALIDATION_ERROR" and sc["retryable"] is False
    assert {f["field"] for f in sc["details"]["fields"]} == {"handle", "limit"}


async def test_missing_or_bad_auth_401(services: Services, live_server: LiveServer) -> None:
    async with httpx.AsyncClient() as http:
        r = await http.post(
            live_server.url + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
        )
        assert r.status_code == 401
        assert r.json() == {"error": {"code": "UNAUTHORIZED"}}
        bad = "mcpk_" + "abcdefgh_" + "x" * 43
        r = await http.post(
            live_server.url + "/mcp", json={}, headers={"Authorization": f"Bearer {bad}"}
        )
        assert r.status_code == 401
        r = await http.get(live_server.url + "/health/live")
        assert r.status_code == 200 and "x-request-id" in r.headers
        r = await http.get(live_server.url + "/health/ready")
        assert r.status_code == 200 and r.json()["db"] is True


async def test_revoked_key_rejected_after_cache_ttl(
    services: Services, live_server: LiveServer
) -> None:
    key, identity = await make_client(services, toolsets=["toolbox"])
    async with mcp_session(live_server.url, key) as client:
        await client.list_tools()
    async with services.db.session() as session:
        await revoke_client(session, identity.name)
    live_server.authenticator.cache_ttl_s = 0  # simulasi TTL 60 s berlalu
    live_server.authenticator.invalidate()
    async with httpx.AsyncClient() as http:
        r = await http.post(
            live_server.url + "/mcp", json={}, headers={"Authorization": f"Bearer {key}"}
        )
    assert r.status_code == 401


async def test_body_limit_413(services: Services, live_server: LiveServer) -> None:
    key, _ = await make_client(services, toolsets=["toolbox"])
    async with httpx.AsyncClient() as http:
        r = await http.post(
            live_server.url + "/mcp",
            content=b"x" * (2 * 1024 * 1024 + 1),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
    assert r.status_code == 413


async def test_admin_endpoints_require_admin(services: Services, live_server: LiveServer) -> None:
    key, _ = await make_client(services, name="plain", toolsets=["toolbox"])
    admin_key, _ = await make_client(services, name="root", toolsets=["toolbox"], is_admin=True)
    async with httpx.AsyncClient(base_url=live_server.url) as http:
        assert (
            await http.get("/admin/clients", headers={"Authorization": f"Bearer {key}"})
        ).status_code == 403
        headers = {"Authorization": f"Bearer {admin_key}"}
        created = await http.post(
            "/admin/clients",
            json={"name": "claude-desktop", "toolsets": ["toolbox"]},
            headers=headers,
        )
        assert created.status_code == 201
        new_key = created.json()["api_key"]
        assert new_key.startswith("mcpk_")
        listed = await http.get("/admin/clients", headers=headers)
        assert {c["name"] for c in listed.json()["clients"]} >= {"plain", "root", "claude-desktop"}
        cid = created.json()["client"]["id"]
        revoked = await http.post(f"/admin/clients/{cid}/revoke", headers=headers)
        assert revoked.json()["client"]["status"] == "revoked"
        r = await http.post("/mcp", json={}, headers={"Authorization": f"Bearer {new_key}"})
        assert r.status_code == 401


async def test_every_call_audited(services: Services, live_server: LiveServer) -> None:
    key, identity = await make_client(services, toolsets=["toolbox"])
    async with mcp_session(live_server.url, key) as client:
        await client.call_tool("toolbox_ping", {})
        await client.call_tool("data_get", {"handle": "h_0123456789"})
    await live_server.pipeline.drain()
    async with services.db.session() as session:
        rows = (await session.scalars(select(ToolCall).order_by(ToolCall.created_at))).all()
    assert [(r.tool, r.status) for r in rows] == [("toolbox_ping", "ok"), ("data_get", "error")]
    assert all(
        str(r.client_id) == identity.id and r.request_id and r.duration_ms >= 0 for r in rows
    )
    assert rows[1].error_code == "FORBIDDEN"


async def test_rate_limit(services: Services) -> None:
    from mcp_marketing.server.app import build_asgi_app

    _, pipeline, auth = build_asgi_app(services)
    client = ClientIdentity(
        id=None, name="slow", kind="external", toolsets=frozenset({"toolbox"}), rpm_limit=2
    )
    codes = []
    for _ in range(3):
        out = await pipeline.call("toolbox_ping", {}, client=client)
        codes.append(out.error_code)
    assert codes == [None, None, "RATE_LIMITED"]


async def test_pipeline_overhead_noop_p95(services: Services) -> None:
    from mcp_marketing.server.app import build_asgi_app

    _, pipeline, _ = build_asgi_app(services)
    client = ClientIdentity(
        id=None, name="perf", kind="external", toolsets=frozenset({"toolbox"}), rpm_limit=10_000
    )
    timings = []
    for _ in range(100):
        t = time.perf_counter()
        await pipeline.call("toolbox_ping", {}, client=client)
        timings.append(time.perf_counter() - t)
    timings.sort()
    assert timings[94] < 0.020
