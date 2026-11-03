"""TASK-08 (unit): signer setara Dashboard, schema blok & kontrak, chunker, Agent API client."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mcp_marketing.core.config import get_settings
from mcp_marketing.core.errors import ToolError
from mcp_marketing.schemas.blocks import (
    BlockValidationError,
    extract_plain_text,
    validate_block_content,
)
from mcp_marketing.schemas.contract import (
    DispatchInput,
    EstimationOutput,
    is_supported_contract,
    validate_event_data,
)
from mcp_marketing.services.dashboard import signer
from mcp_marketing.services.dashboard.agent_api import AgentApiClient, outline_blocks
from mcp_marketing.services.dashboard.chunking import (
    chain_append_ops,
    chunk_block_ops,
    chunk_subtasks,
)
from mcp_marketing.services.templates.md_to_blocks import markdown_to_blocks, parse_inline

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "contract" / "fixtures"
SECRET = "test-webhook-secret-0123456789abcdefghij"


# ---------- signer ----------
def test_signature_matches_dashboard_vector() -> None:
    body = '{"event_id":"x","data":{"a":"café"}}'.encode()
    # dihitung dengan signMcpPayload() dari build Dashboard (crypto.util.js)
    expected = "sha256=265d521a48d4c407986337e0e14a6784daf061d9cca23568253ba54dad7bdde5"
    assert signer.sign(SECRET, "1762140000", body) == expected
    assert signer.verify([SECRET], "1762140000", expected, body, now=1762140100) == "ok"


def test_signature_verify_cases() -> None:
    body = b"{}"
    headers = signer.signed_headers(SECRET, body, now=1000)
    ts, sig = headers["X-MCP-Timestamp"], headers["X-MCP-Signature"]
    assert headers["Content-Type"] == "application/json"
    assert signer.verify(["old-secret", SECRET], ts, sig, body, now=1100) == "ok"
    assert signer.verify([SECRET], ts, sig, body, now=1400) == "expired"
    assert signer.verify(["lain"], ts, sig, body, now=1000) == "invalid"
    assert signer.verify([SECRET], None, sig, body) == "missing"
    assert signer.verify([SECRET], ts, sig, b"{ }", now=1000) == "invalid"


# ---------- blocks ----------
def test_block_strips_unknown_and_applies_defaults() -> None:
    out = validate_block_content(
        "heading", {"props": {"foo": 1}, "content": [{"type": "text", "text": "Hi\x07"}], "x": 2}
    )
    assert out == {"props": {"level": 1}, "content": [{"type": "text", "text": "Hi", "styles": {}}]}
    assert validate_block_content("checkListItem", {}) == {
        "props": {"checked": False},
        "content": [],
    }
    assert validate_block_content("divider", {"content": [1]}) == {"props": {}}
    assert validate_block_content("aiPrompt", None)["props"] == {"prompt": "", "status": "idle"}


@pytest.mark.parametrize(
    "block_type,content",
    [
        ("heading", {"props": {"level": 4}}),
        ("image", {"props": {"url": "javascript:alert(1)"}}),
        ("paragraph", {"props": {"textColor": "red;"}}),
        ("paragraph", {"content": [{"type": "link", "href": "ftp://x", "content": []}]}),
        (
            "paragraph",
            {
                "content": [
                    {
                        "type": "mention",
                        "props": {"mention_type": "user", "id": "bukan-uuid", "label": "a"},
                    }
                ]
            },
        ),
        ("codeBlock", {"props": {"language": "py thon"}}),
        ("table", {"content": {"type": "tableContent", "rows": [{"cells": [[]] * 21}]}}),
        ("video", {}),
        ("paragraph", {"content": [{"type": "text", "text": "x" * 10_001}]}),
    ],
)
def test_block_invalid(block_type: str, content: Any) -> None:
    with pytest.raises(BlockValidationError):
        validate_block_content(block_type, content)


def test_block_size_limit() -> None:
    big = {"content": [{"type": "text", "text": "x" * 9_000} for _ in range(8)]}
    with pytest.raises(BlockValidationError, match="byte"):
        validate_block_content("paragraph", big)


def test_extract_plain_text_table_and_bookmark() -> None:
    table = validate_block_content(
        "table",
        {
            "content": {
                "type": "tableContent",
                "rows": [
                    {
                        "cells": [
                            [{"type": "text", "text": "a"}],
                            {"type": "tableCell", "content": [{"type": "text", "text": "b"}]},
                        ]
                    }
                ],
            }
        },
    )
    assert extract_plain_text("table", table) == "a | b"
    bm = validate_block_content("bookmark", {"props": {"url": "https://x.id", "title": "T"}})
    assert extract_plain_text("bookmark", bm) == "T https://x.id"


# ---------- kontrak ----------
def test_contract_version_check() -> None:
    assert is_supported_contract("1.0") and is_supported_contract("1.12")
    assert not is_supported_contract("2.0") and not is_supported_contract("")


def test_dispatch_input_tolerates_extra_and_nulls() -> None:
    raw = json.loads(
        (ROOT / "tests" / "contract" / "dispatch_sample.json").read_text(encoding="utf-8")
    )
    raw["future_field"] = {"x": 1}
    parsed = DispatchInput.model_validate(raw)
    assert parsed.task.title == "Generate Content Calendar"
    assert parsed.dispatched_by is None
    assert parsed.agent_id == parsed.agent_id.lower()


def test_estimation_output_bounds() -> None:
    with pytest.raises(ValueError):
        EstimationOutput(start_date="2026-11-03", end_date="2026-11-04", confidence=2)


def test_event_data_validation_strips_none() -> None:
    out = validate_event_data(
        "agent.task.failed", {"error_code": "X", "message": "m", "retryable": None}
    )
    assert out == {"error_code": "X", "message": "m"}
    with pytest.raises(ValueError):
        validate_event_data("agent.comment.added", {"content": "  "})


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_fixture_python_validator_agrees(path: Path) -> None:
    fx = json.loads(path.read_text(encoding="utf-8"))
    if fx["kind"] == "output":
        from mcp_marketing.schemas.contract import CancelOutput, DispatchOutput

        model = {
            "dispatch": DispatchOutput,
            "estimation": EstimationOutput,
            "cancel": CancelOutput,
        }[fx["schema"]]
        call = lambda: model.model_validate(fx["payload"])  # noqa: E731
    else:
        from mcp_marketing.schemas.contract import WebhookEnvelope

        def call() -> Any:
            env = WebhookEnvelope.model_validate(fx["payload"])
            return validate_event_data(env.event_type, env.data)

    if fx["valid"]:
        call()
    else:
        with pytest.raises(ValueError):
            call()


@pytest.mark.contract
def test_fixtures_accepted_by_dashboard_zod() -> None:
    node = shutil.which("node") or r"D:\Data.Fahrul\Documents\PortableDev\node\node.exe"
    backend = ROOT.parent / "marketingdashboardjtp" / "backend"
    if (
        not Path(node).exists()
        or not (backend / "dist" / "shared" / "schemas" / "mcp.schema.js").exists()
    ):
        pytest.skip("node atau build dist/ Dashboard tidak tersedia")
    proc = subprocess.run(
        [
            node,
            str(ROOT / "tests" / "contract" / "validate_with_zod.cjs"),
            str(backend),
            str(FIXTURES),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    results = json.loads(proc.stdout)
    mismatches = [r for r in results if r["expected"] != r["valid"]]
    assert results and mismatches == []


# ---------- chunking ----------
def test_chunk_block_ops_by_count_and_size() -> None:
    ops = [{"op": "append", "block": {"block_type": "divider"}}] * 120
    assert [len(c) for c in chunk_block_ops(ops)] == [50, 50, 20]
    big = [{"op": "append", "x": "y" * 400_000}] * 3
    assert [len(c) for c in chunk_block_ops(big)] == [2, 1]
    assert [len(c) for c in chunk_subtasks(list(range(45)))] == [20, 20, 5]


def test_chain_append_ops_after_prompt_block() -> None:
    ops = chain_append_ops(
        [{"block_type": "divider"}, {"block_type": "divider"}], after_block_id="p1"
    )
    assert ops[0]["after_block_id"] == "p1"
    assert ops[1]["after_block_id"] == ops[0]["block"]["id"]
    tail = chain_append_ops([{"block_type": "divider"}], after_block_id=None)
    assert "after_block_id" not in tail[0]


# ---------- md_to_blocks ----------
MD_CASES = [
    ("# Judul", ["heading"]),
    ("#### Sub dalam", ["heading"]),
    ("Paragraf biasa\nlanjut baris", ["paragraph"]),
    ("- a\n- b\n  - nested", ["bulletListItem"] * 3),
    ("1. satu\n2. dua", ["numberedListItem"] * 2),
    ("- [ ] todo\n- [x] done", ["checkListItem"] * 2),
    ("> kutipan\n> baris 2", ["quote"]),
    ("```js\nconst a = 1;\n```", ["codeBlock"]),
    ("| a | b |\n|---|---|\n| 1 | 2 |", ["table"]),
    ("---", ["divider"]),
    ("![logo](https://x.id/a.png)", ["image"]),
    ("![logo](javascript:x)", ["paragraph"]),
    ("**tebal** dan *miring* `kode` ~~coret~~", ["paragraph"]),
    ("[tautan](https://example.com) dan [jahat](javascript:alert)", ["paragraph"]),
    ("## H2\nteks\n\n- poin", ["heading", "paragraph", "bulletListItem"]),
    ("```\ntanpa bahasa\n```", ["codeBlock"]),
    ("x" * 20_000, ["paragraph", "paragraph", "paragraph"]),
    ("| h |\n|---|\n" + "\n".join(f"| {i} |" for i in range(120)), ["table"] * 3),
    ("", []),
    ("Teks _miring_ dan snake_case_var", ["paragraph"]),
]


@pytest.mark.parametrize("md,types", MD_CASES)
def test_md_to_blocks_types_and_valid(md: str, types: list[str]) -> None:
    blocks = markdown_to_blocks(md)
    assert [b["block_type"] for b in blocks] == types
    for b in blocks:
        assert validate_block_content(b["block_type"], b["content_json"]) == b["content_json"]


def test_md_inline_styles() -> None:
    inline = parse_inline("**tebal** *miring* `kode` [web](https://a.id)")
    assert inline[0] == {"type": "text", "text": "tebal", "styles": {"bold": True}}
    assert {"type": "text", "text": "miring", "styles": {"italic": True}} in inline
    assert {"type": "text", "text": "kode", "styles": {"code": True}} in inline
    assert inline[-1]["type"] == "link" and inline[-1]["href"] == "https://a.id"
    unsafe = parse_inline("[x](javascript:alert(1))")
    assert all(i["type"] == "text" for i in unsafe)
    snake = parse_inline("snake_case_var")
    assert snake == [{"type": "text", "text": "snake_case_var", "styles": {}}]
    check = markdown_to_blocks("- [x] selesai")[0]
    assert check["content_json"]["props"]["checked"] is True


# ---------- Agent API ----------
API = "http://dashboard.test/api/v1/agent-api/runs/r1"


@pytest.fixture
def api() -> AgentApiClient:
    return AgentApiClient(get_settings(), httpx.AsyncClient())


@respx.mock
async def test_agent_api_query_trims_and_sends_token(api: AgentApiClient) -> None:
    route = respx.post(f"{API}/context/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "block_id": "b1",
                        "task_id": None,
                        "task_title": None,
                        "source": "project_brief",
                        "block_type": "paragraph",
                        "text": "x" * 2000,
                        "rank": 0.4,
                    }
                ]
            },
        )
    )
    results = await api.query_context("r1", "tone", top_k=50)
    assert len(results[0]["text"]) == 600
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer test-service-token-0123456789abcdefghijk"
    assert json.loads(req.content)["top_k"] == 20


@respx.mock
async def test_agent_api_task_outline(api: AgentApiClient) -> None:
    respx.get(f"{API}/tasks/t1").mock(
        return_value=httpx.Response(
            200,
            json={
                "task": {"id": "t1", "title": "T"},
                "blocks": [
                    {
                        "id": "b1",
                        "block_type": "heading",
                        "version": 1,
                        "content_json": {
                            "props": {"level": 2},
                            "content": [{"type": "text", "text": "Brief"}],
                        },
                        "children": [
                            {
                                "id": "b2",
                                "block_type": "paragraph",
                                "version": 2,
                                "content_json": {
                                    "content": [{"type": "text", "text": "isi " * 400}]
                                },
                                "children": [],
                            }
                        ],
                    }
                ],
                "subtasks": [],
                "comments": [
                    {"id": f"c{i}", "content": "k", "author": {"type": "user", "name": "U"}}
                    for i in range(50)
                ],
            },
        )
    )
    data = await api.get_task("r1", "t1")
    assert data["blocks"][0] == {
        "id": "b1",
        "type": "heading",
        "text": "Brief",
        "version": 1,
        "level": 2,
    }
    assert data["blocks"][1]["depth"] == 1 and len(data["blocks"][1]["text"]) == 600
    assert len(data["comments"]) == 10 and data["comments"][-1]["id"] == "c49"


@respx.mock
@pytest.mark.parametrize(
    "status,code,expected",
    [
        (409, "RUN_NOT_ACTIVE", "RUN_NOT_ACTIVE"),
        (404, "NOT_FOUND", "NOT_FOUND"),
        (429, "RATE_LIMITED", "RATE_LIMITED"),
        (500, "INTERNAL_ERROR", "UPSTREAM_ERROR"),
        (401, "UNAUTHENTICATED", "UPSTREAM_ERROR"),
    ],
)
async def test_agent_api_errors(api: AgentApiClient, status: int, code: str, expected: str) -> None:
    respx.get(API).mock(
        return_value=httpx.Response(status, json={"error": {"code": code, "message": "m"}})
    )
    with pytest.raises(ToolError) as exc:
        await api.get_run("r1")
    assert exc.value.code == expected


def test_outline_uses_plain_text_when_present() -> None:
    out = outline_blocks(
        [{"id": "x", "block_type": "paragraph", "plain_text": "teks", "children": []}]
    )
    assert out == [{"id": "x", "type": "paragraph", "text": "teks", "version": None}]
