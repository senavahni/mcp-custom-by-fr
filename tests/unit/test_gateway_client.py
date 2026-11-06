"""TASK-06: klien Gateway — sukses, retry, error mapping, JSON repair, tool calling, budget."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args

import httpx2
import pytest
from pydantic import BaseModel

from mcp_marketing.core.config import get_settings, load_settings
from mcp_marketing.core.errors import ToolError
from mcp_marketing.services.agent.budget import BudgetLayer, BudgetScope
from mcp_marketing.services.llm import prompts
from mcp_marketing.services.llm.gateway_client import GatewayClient, parse_json_text
from mcp_marketing.services.llm.route_policy import POLICY, Purpose, resolve
from tests.fakes import FakeGateway, completion, err, ok, tool_call

CHAT = "/chat/completions"
MSG = [{"role": "user", "content": "hai"}]


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def client(gw: FakeGateway, sleeps: list[float]) -> GatewayClient:
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    return GatewayClient(get_settings(), http_client=gw.client(), sleep=fake_sleep)


async def test_success_parses_usage_and_headers(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(CHAT, ok(completion(), {"x-request-id": "gw-1"}))
    result = await client.chat("summarize_short", MSG)
    assert result.content == "halo"
    assert (result.prompt_tokens, result.completion_tokens) == (10, 5)
    assert result.model_resolved == "openai/gpt-test"
    assert result.gateway_request_id == "gw-1"
    body = gw.bodies(CHAT)[0]
    assert body["model"] == "fast"
    assert body["temperature"] == 0 and body["max_tokens"] == 800
    assert body["response_format"] == {"type": "json_object"}
    assert body["user"] == "-:-"
    assert len(gw.calls_to(CHAT)[0].headers["x-request-id"]) == 36
    assert str(gw.calls_to(CHAT)[0].url).startswith("http://gateway.test/v1/")


async def test_unique_request_id_per_call(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(CHAT, ok(completion()))
    await client.chat("agent_loop", MSG)
    await client.chat("agent_loop", MSG)
    assert len({c.headers["x-request-id"] for c in gw.calls_to(CHAT)}) == 2


async def test_429_retry_after_then_success(
    client: GatewayClient, gw: FakeGateway, sleeps: list[float]
) -> None:
    gw.on(CHAT, err(429, "rate_limited", {"Retry-After": "2"}), ok(completion()))
    assert (await client.chat("classify", MSG)).content == "halo"
    assert sleeps == [2.0]


async def test_429_exhausted_is_rate_limited(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(CHAT, err(429, "rate_limited", {"Retry-After": "1"}))
    with pytest.raises(ToolError) as exc:
        await client.chat("classify", MSG)
    assert exc.value.code == "RATE_LIMITED" and exc.value.retryable
    assert len(gw.calls_to(CHAT)) == 3


async def test_quota_exceeded_is_budget_non_retryable(
    client: GatewayClient, gw: FakeGateway, sleeps: list[float]
) -> None:
    gw.on(CHAT, err(429, "quota_exceeded"))
    with pytest.raises(ToolError) as exc:
        await client.chat("classify", MSG)
    assert exc.value.code == "BUDGET_EXCEEDED" and not exc.value.retryable
    assert sleeps == []


async def test_502_then_success_backoff(
    client: GatewayClient, gw: FakeGateway, sleeps: list[float]
) -> None:
    gw.on(CHAT, err(502, "all_candidates_failed"), err(502, "upstream_error"), ok(completion()))
    assert (await client.chat("classify", MSG)).content == "halo"
    assert sleeps == [1.0, 3.0]


async def test_502_exhausted(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(CHAT, err(502, "all_candidates_failed"))
    with pytest.raises(ToolError) as exc:
        await client.chat("classify", MSG)
    assert exc.value.code == "UPSTREAM_ERROR" and exc.value.retryable
    assert exc.value.details["reason"] == "all_candidates_failed"  # type: ignore[index]


async def test_504_retry_with_reduced_max_tokens(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(CHAT, err(504, "upstream_timeout"), ok(completion()))
    await client.chat("strategy", MSG)
    assert [b["max_tokens"] for b in gw.bodies(CHAT)] == [4_000, 2_800]


async def test_504_twice_is_timeout(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(CHAT, err(504, "upstream_timeout"))
    with pytest.raises(ToolError) as exc:
        await client.chat("strategy", MSG)
    assert exc.value.code == "UPSTREAM_TIMEOUT"


async def test_connection_error_retried_then_upstream_error(
    gw: FakeGateway, sleeps: list[float]
) -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    gw.on(CHAT, boom)
    client = GatewayClient(get_settings(), http_client=gw.client(), sleep=fake_sleep)
    with pytest.raises(ToolError) as exc:
        await client.chat("classify", MSG)
    assert exc.value.code == "UPSTREAM_ERROR" and sleeps == [1.0, 3.0]


@pytest.mark.parametrize("status", [400, 422, 401, 403])
async def test_client_errors_not_retried(
    client: GatewayClient, gw: FakeGateway, status: int
) -> None:
    gw.on(CHAT, err(status, "invalid_request"))
    with pytest.raises(ToolError) as exc:
        await client.chat("classify", MSG)
    assert exc.value.code == "UPSTREAM_ERROR" and exc.value.retryable is False
    assert len(gw.calls_to(CHAT)) == 1


async def test_tool_calls_parsed(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(
        CHAT,
        ok(
            completion(
                None,
                tool_calls=[
                    tool_call("c1", "data_get", {"handle": "h_0123456789"}),
                    tool_call("c2", "finish", "{bad"),
                ],
                finish="tool_calls",
            )
        ),
    )
    result = await client.chat(
        "agent_loop",
        MSG,
        tools=[
            {"type": "function", "function": {"name": "data_get", "parameters": {"type": "object"}}}
        ],
    )
    assert [t.name for t in result.tool_calls] == ["data_get", "finish"]
    assert result.tool_calls[0].arguments == {"handle": "h_0123456789"}
    assert result.tool_calls[1].parse_error
    body = gw.bodies(CHAT)[0]
    assert "response_format" not in body and body["tools"]


class Est(BaseModel):
    estimated_days: float
    confidence: float


async def test_chat_json_repair_success(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(
        CHAT,
        ok(completion("ini bukan json")),
        ok(completion('```json\n{"estimated_days": 2, "confidence": 0.7}\n```')),
    )
    parsed, result = await client.chat_json("estimate", MSG, Est)
    assert parsed.estimated_days == 2
    assert result.prompt_tokens == 20
    assert "bukan JSON yang valid" in gw.bodies(CHAT)[1]["messages"][0]["content"]


async def test_chat_json_repair_fails(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(CHAT, ok(completion('{"estimated_days": "x"}')))
    with pytest.raises(ToolError) as exc:
        await client.chat_json("estimate", MSG, Est)
    assert exc.value.code == "UPSTREAM_ERROR"
    assert exc.value.details["reason"] == "invalid_json"  # type: ignore[index]


async def test_budget_committed_with_actual_usage(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(CHAT, ok(completion(prompt=120, completion_tokens=30)))
    layer = BudgetLayer("run", 10_000)
    await client.chat("classify", MSG, budget=BudgetScope([layer]))
    assert layer.used == 150 and layer.reserved == 0


async def test_budget_exceeded_before_any_http_call(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(CHAT, ok(completion()))
    with pytest.raises(ToolError) as exc:
        await client.chat("strategy", MSG, budget=BudgetScope([BudgetLayer("run", 1_000)]))
    assert exc.value.code == "BUDGET_EXCEEDED"
    assert gw.calls == []


async def test_budget_released_on_error(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(CHAT, err(400, "bad"))
    layer = BudgetLayer("run", 10_000)
    with pytest.raises(ToolError):
        await client.chat("classify", MSG, budget=BudgetScope([layer]))
    assert layer.used == 0 and layer.reserved == 0


async def test_embed_batches_of_64(client: GatewayClient, gw: FakeGateway) -> None:
    def reply(request: httpx2.Request) -> httpx2.Response:
        texts = json.loads(request.content)["input"]
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "model": "embed",
                "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
                "data": [
                    {"object": "embedding", "index": i, "embedding": [float(i), 1.0]}
                    for i in range(len(texts))
                ],
            },
        )

    gw.on("/embeddings", reply)
    vectors = await client.embed([f"teks {i}" for i in range(130)])
    assert len(vectors) == 130 and len(gw.calls_to("/embeddings")) == 3
    assert gw.bodies("/embeddings")[0]["model"] == "embed-default"


def test_route_policy_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ROUTE_SMART", "smart-premium")
    s = load_settings().llm
    assert resolve("strategy", s)[0] == "smart-premium"
    assert resolve("classify", s)[0] == "fast"
    assert resolve("classify", s, route_override="smart")[0] == "smart-premium"
    assert resolve("agent_loop", s, route_override="custom-route")[0] == "custom-route"
    with pytest.raises(ValueError):
        resolve("tidak-ada", s)


def test_route_policy_task_breakdown() -> None:
    route, cfg = resolve("task_breakdown", load_settings().llm)
    assert route == "fast"
    assert (cfg.tier, cfg.temperature, cfg.max_tokens, cfg.json) == ("fast", 0.2, 2_000, True)
    assert cfg.reasoning_effort == "low"


def test_every_literal_purpose_in_code_is_known_to_policy() -> None:
    root = Path(__file__).resolve().parents[2] / "mcp_marketing"
    used = {
        match
        for path in root.rglob("*.py")
        for match in re.findall(r"\.chat(?:_json)?\(\s*\"([a-z_]+)\"", path.read_text("utf-8"))
    }
    # tpl_generate: bug serupa yang sudah ada, di luar cakupan perbaikan task_breakdown
    assert used and used - set(POLICY) <= {"tpl_generate"}
    assert set(get_args(Purpose)) == set(POLICY)


def test_prompts_render_and_missing_var() -> None:
    text = prompts.render("estimate", title="T", description="D", subtasks="-", heuristic_days=2)
    assert "T" in text and "{{" not in text
    with pytest.raises(KeyError):
        prompts.render("estimate", title="T")
    assert prompts.tag("estimate") == "estimate@v1"


def test_parse_json_text_variants() -> None:
    assert parse_json_text('```json\n{"a":1}\n```') == {"a": 1}
    assert parse_json_text('Berikut: {"a": 2} ok') == {"a": 2}


async def test_model_catalog_returns_full_objects_and_maps_errors(
    client: GatewayClient, gw: FakeGateway
) -> None:
    gw.on(
        "/models",
        httpx2.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "openrouter/gemma-4-31b",
                        "object": "model",
                        "created": 0,
                        "owned_by": "openrouter",
                        "x_pricing": {"kind": "free", "estimate": True},
                    }
                ],
            },
        ),
        httpx2.Response(502, json={"error": {"code": "upstream_error", "message": "x"}}),
    )
    models = await client.model_catalog()
    assert models[0]["id"] == "openrouter/gemma-4-31b"
    assert models[0]["x_pricing"]["kind"] == "free"  # field tambahan Gateway ikut terbawa
    with pytest.raises(ToolError) as exc:
        await client.model_catalog()
    assert exc.value.code == "UPSTREAM_ERROR" and "Gateway" in str(exc.value)


def test_openai_imported_only_in_gateway_client() -> None:
    root = Path(__file__).resolve().parents[2] / "mcp_marketing"
    offenders = [
        p
        for p in root.rglob("*.py")
        if p.name != "gateway_client.py"
        and any(
            line.strip().startswith(("import openai", "from openai"))
            for line in p.read_text(encoding="utf-8").splitlines()
        )
    ]
    assert offenders == []
