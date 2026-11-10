"""MCP-V1-01: embedder `embed-local` (Gateway LGW-V1-04) — input_type, X-Priority, batch, pause."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx2
import pytest

from mcp_marketing.core import context
from mcp_marketing.core.config import get_settings, load_settings
from mcp_marketing.core.errors import LocalModelUnavailable, ToolError
from mcp_marketing.services.kb import embedder as emb
from mcp_marketing.services.kb.identity import (
    EMBED_LOCAL_DIM,
    EMBED_LOCAL_MODEL,
    GATEWAY_MODEL,
    embed_identity,
    max_chunk_tokens,
)
from mcp_marketing.services.llm.gateway_client import GatewayClient
from mcp_marketing.worker import runner
from tests.fakes import FakeGateway, err

EMB = "/embeddings"


def _vectors(request: httpx2.Request) -> httpx2.Response:
    texts = json.loads(request.content)["input"]
    return httpx2.Response(
        200,
        json={
            "object": "list",
            "model": EMBED_LOCAL_MODEL,
            "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
            "data": [
                {"object": "embedding", "index": i, "embedding": [0.1] * EMBED_LOCAL_DIM}
                for i in range(len(texts))
            ],
        },
    )


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def client(gw: FakeGateway) -> GatewayClient:
    async def no_sleep(_: float) -> None:
        return None

    return GatewayClient(get_settings(), http_client=gw.client(), sleep=no_sleep)


# ---------------------------------------------------------------------------- identitas


def test_v1_collections_always_embed_local() -> None:
    s = get_settings()
    for name in ("books:ws1", "companies:ws1", "social:ws1"):
        assert embed_identity(s, collection_name=name) == (EMBED_LOCAL_MODEL, 1024)
    assert embed_identity(s, collection_name="intel:ws1")[0] == GATEWAY_MODEL
    assert embed_identity(s, embed_model="embed-local") == (EMBED_LOCAL_MODEL, 1024)
    assert embed_identity(s, embed_model="embed-default") == (GATEWAY_MODEL, s.nlp.embed_dim)


def test_embed_provider_local_still_selects_onnx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBED_PROVIDER", "local")
    assert embed_identity(load_settings())[1] == 384


def test_chunk_cap_only_for_embed_local() -> None:
    assert max_chunk_tokens(EMBED_LOCAL_MODEL, 2_000) == 1_024
    assert max_chunk_tokens(EMBED_LOCAL_MODEL, 800) == 800
    assert max_chunk_tokens(GATEWAY_MODEL, 2_000) == 2_000


# ---------------------------------------------------------------------------- Gateway client


async def test_local_document_bulk_batches_of_8(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(EMB, _vectors)
    vectors = await client.embed(
        [f"halaman {i}" for i in range(20)],
        model=EMBED_LOCAL_MODEL,
        input_type="document",
        priority="bulk",
    )
    assert len(vectors) == 20 and len(gw.calls_to(EMB)) == 3
    body = gw.bodies(EMB)[0]
    assert body["model"] == "embed-local"
    assert body["provider_options"] == {"input_type": "document", "truncate": True}
    assert gw.calls_to(EMB)[0].headers["x-priority"] == "bulk"


async def test_local_query_interactive(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(EMB, _vectors)
    await client.embed(
        ["kampus swasta"], model=EMBED_LOCAL_MODEL, input_type="query", priority="interactive"
    )
    assert gw.bodies(EMB)[0]["provider_options"]["input_type"] == "query"
    assert gw.calls_to(EMB)[0].headers["x-priority"] == "interactive"


async def test_remote_route_unchanged(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(EMB, _vectors)
    await client.embed(["teks"])
    body = gw.bodies(EMB)[0]
    assert body["model"] == "embed-default"
    assert "provider_options" not in body
    assert "x-priority" not in gw.calls_to(EMB)[0].headers


@pytest.mark.parametrize(
    "code", ["provider_unavailable", "model_load_failed", "device_unavailable", "device_oom"]
)
async def test_local_down_maps_to_local_model_unavailable(
    client: GatewayClient, gw: FakeGateway, code: str
) -> None:
    gw.on(EMB, err(503, code))
    with pytest.raises(LocalModelUnavailable) as exc:
        await client.embed(["x"], model=EMBED_LOCAL_MODEL, input_type="document")
    assert exc.value.retryable and exc.value.details == {"gateway_code": code}


async def test_model_busy_is_plain_upstream_error(client: GatewayClient, gw: FakeGateway) -> None:
    gw.on(EMB, err(503, "model_busy"))
    with pytest.raises(ToolError) as exc:
        await client.embed(["x"], model=EMBED_LOCAL_MODEL, input_type="document")
    assert exc.value.code == "UPSTREAM_ERROR"


async def test_remote_503_never_local_model_unavailable(
    client: GatewayClient, gw: FakeGateway
) -> None:
    gw.on(EMB, err(503, "provider_unavailable"))
    with pytest.raises(ToolError) as exc:
        await client.embed(["x"])
    assert exc.value.code == "UPSTREAM_ERROR"


# ---------------------------------------------------------------------------- embedder


class _Svc:
    def __init__(self, llm: Any) -> None:
        self.llm = llm
        self.extras: dict[str, Any] = {}
        self.settings = get_settings()


async def test_embedder_kind_to_input_type_and_priority() -> None:
    llm = AsyncMock()
    llm.embed.return_value = [[0.0] * EMBED_LOCAL_DIM]
    e = emb.embedder_for(_Svc(llm), EMBED_LOCAL_MODEL, EMBED_LOCAL_DIM)
    assert (e.model, e.dim) == (EMBED_LOCAL_MODEL, 1024)
    await e.embed(["q"], "query")
    kwargs = llm.embed.call_args.kwargs
    assert (kwargs["input_type"], kwargs["priority"]) == ("query", "interactive")
    assert kwargs["model"] == EMBED_LOCAL_MODEL
    await e.embed(["d"], "passage")
    assert llm.embed.call_args.kwargs["input_type"] == "document"
    assert llm.embed.call_args.kwargs["priority"] == "bulk"
    with context.context_scope(embed_priority="interactive"):
        await e.embed(["d"], "passage")
    assert llm.embed.call_args.kwargs["priority"] == "interactive"


async def test_embedder_rejects_wrong_dimension() -> None:
    llm = AsyncMock()
    llm.embed.return_value = [[0.0] * 1536]
    e = emb.embedder_for(_Svc(llm), EMBED_LOCAL_MODEL, EMBED_LOCAL_DIM)
    with pytest.raises(ToolError):
        await e.embed(["d"], "passage")


# ---------------------------------------------------------------------------- worker pause


class _Queue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def pause(self, job_id: str, worker_id: str, **kw: Any) -> None:
        self.calls.append(("pause", kw))

    async def requeue(self, job_id: str, worker_id: str, **kw: Any) -> None:
        self.calls.append(("requeue", kw))

    async def finish(self, job_id: str, worker_id: str, **kw: Any) -> None:
        self.calls.append(("finish", kw))


class _Job:
    id = "j_0000000001"
    kind = "kb_ingest"
    attempts = 3
    max_attempts = 3


async def test_worker_pauses_on_local_model_unavailable_even_after_last_attempt() -> None:
    queue = _Queue()
    worker = runner.Worker.__new__(runner.Worker)
    worker.queue = queue  # type: ignore[assignment]
    worker.worker_id = "w1"
    await worker._handle_failure(_Job(), LocalModelUnavailable("mati"))  # type: ignore[arg-type]
    assert queue.calls[0][0] == "pause"
    assert queue.calls[0][1]["delay_s"] == runner.PAUSE_DELAY_S
    assert queue.calls[0][1]["error"]["code"] == "LOCAL_MODEL_UNAVAILABLE"
