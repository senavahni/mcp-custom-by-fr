"""TASK-06 (DB): cache LLM & pencatatan llm_calls; smoke `main.create_app` (wiring lifespan)."""

from __future__ import annotations

import asyncio
import socket
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn
from sqlalchemy import select

from mcp_marketing.core import context
from mcp_marketing.core.config import load_settings
from mcp_marketing.core.errors import ToolError
from mcp_marketing.core.logging import configure_logging, get_logger
from mcp_marketing.models import LlmCall
from mcp_marketing.server.services import Services
from mcp_marketing.services.agent.budget import BudgetLayer, BudgetScope
from mcp_marketing.services.llm.gateway_client import GatewayClient
from mcp_marketing.services.seed import seed
from tests.fakes import FakeGateway, completion, ok
from tests.integration.conftest import app_url, mcp_session, owner_url

CHAT = "/chat/completions"


async def test_llm_cache_and_usage_recorded(services: Services) -> None:
    gw = FakeGateway().on(
        CHAT, ok(completion("hasil", prompt=40, completion_tokens=10), {"x-request-id": "gw-1"})
    )
    client = GatewayClient(
        services.settings, db=services.db, cache=services.cache, http_client=gw.client()
    )
    msgs = [{"role": "user", "content": "klasifikasi ini"}]
    run_id = str(uuid.uuid4())
    with context.context_scope(
        run_id=run_id, client_id="00000000-0000-0000-0000-0000000000d1", client_name="t"
    ):
        first = await client.chat("classify", msgs, prompt_version="v1")
        layer = BudgetLayer("run", 1_000)
        second = await client.chat(
            "classify", msgs, prompt_version="v1", budget=BudgetScope([layer])
        )
        agent = await client.chat("agent_loop", msgs)  # temperature > 0 -> tidak di-cache
    await client.drain()
    assert first.cached is False and second.cached is True and second.content == "hasil"
    assert layer.used == 0  # cache hit tidak memakai budget
    assert len(gw.calls_to(CHAT)) == 2 and agent.cached is False
    assert gw.bodies(CHAT)[0]["user"] == f"t:{run_id}"
    async with services.db.session() as s:
        rows = (await s.scalars(select(LlmCall))).all()
    # insert pencatatan berjalan async paralel: bandingkan tanpa urutan
    assert sorted(
        (r.purpose, r.cached, r.prompt_tokens, r.completion_tokens) for r in rows
    ) == sorted(
        [("classify@v1", False, 40, 10), ("classify@v1", True, 0, 0), ("agent_loop", False, 40, 10)]
    )
    real = next(r for r in rows if r.purpose == "classify@v1" and not r.cached)
    assert real.gateway_request_id == "gw-1" and str(real.run_id) == run_id
    assert real.model_resolved == "openai/gpt-test"
    await client.close()


async def test_llm_error_recorded(services: Services) -> None:
    from tests.fakes import err

    async def no_sleep(_s: float) -> None:
        return None

    gw = FakeGateway().on(CHAT, err(400, "invalid_request"))
    client = GatewayClient(
        services.settings, db=services.db, http_client=gw.client(), sleep=no_sleep
    )
    with pytest.raises(ToolError):
        await client.chat("classify", [{"role": "user", "content": "x"}])
    await client.drain()
    async with services.db.session() as s:
        row = await s.scalar(select(LlmCall))
    assert row is not None and row.status == "error" and row.error_code == "UPSTREAM_ERROR"


async def test_create_app_smoke(
    services: Services, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async with services.db.session() as s:
        await seed(s)
    monkeypatch.setenv("DATABASE_URL", app_url())
    monkeypatch.setenv("MIGRATION_DATABASE_URL", owner_url())
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://127.0.0.1:9/v1")  # sengaja tidak bisa dihubungi
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    settings = load_settings()
    from mcp_marketing.main import create_app

    app = create_app(settings)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.02)
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient() as http:
            ready = await http.get(f"{base}/health/ready")
            assert ready.status_code == 200 and ready.json()["db"] is True
            metrics = await http.get(f"{base}/metrics")
            assert "mcp_tool_calls_total" in metrics.text
        key = settings.dashboard.mcp_server_api_key.get_secret_value()
        async with mcp_session(base, key) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert "pm_request_agent_estimation" in tools
            est = await client.call_tool(
                "pm_request_agent_estimation",
                {
                    "contract_version": "1.0",
                    "run_id": str(uuid.uuid4()),
                    "agent_key": "strategist",
                    "task": {
                        "id": str(uuid.uuid4()),
                        "project_id": str(uuid.uuid4()),
                        "title": "Riset kompetitor",
                    },
                    "task_description": "",
                    "earliest_start_date": "2026-11-02",
                    "timezone": "Asia/Jakarta",
                },
            )
        # Gateway tidak bisa dihubungi -> fallback heuristik (confidence <= 0.4), tetap valid untuk Dashboard
        assert est.is_error is False
        assert est.structured_content["start_date"] == "2026-11-02"
        assert est.structured_content["confidence"] <= 0.4
        get_logger("smoke").warning("smoke.check", api_key="rahasia")
        files = list(tmp_path.glob("mcp-*.log"))
        assert files and "rahasia" not in files[0].read_text(encoding="utf-8")
    finally:
        server.should_exit = True
        await task
        configure_logging("WARNING", None)  # lepas file handler sebelum tmp_path dibersihkan
