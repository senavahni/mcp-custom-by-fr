"""Regresi temuan code/security review (2026-11-03)."""

from __future__ import annotations

import pytest

from mcp_marketing.core.config import ConfigError, load_settings
from mcp_marketing.core.logging import redact, redact_text
from mcp_marketing.models import RunStep
from mcp_marketing.services.agent.memory import messages_from_steps
from mcp_marketing.services.dashboard.origin import require_dashboard_origin, same_origin

API = "http://localhost:3000/api/v1"


@pytest.mark.parametrize(
    "url,ok",
    [
        ("http://localhost:3000/api/v1/webhooks/mcp/events", True),
        ("http://LOCALHOST:3000/api/v1/agent-api", True),
        ("http://localhost:3001/api/v1/agent-api", False),
        ("https://localhost:3000/api/v1", False),
        ("http://attacker.example/agent-api", False),
        ("http://127.0.0.1:8000/v1", False),
        ("file:///etc/passwd", False),
        ("not a url", False),
    ],
)
def test_callback_origin(url: str, ok: bool) -> None:
    assert same_origin(url, API) is ok


def test_require_origin_raises_validation() -> None:
    with pytest.raises(Exception) as exc:
        require_dashboard_origin("http://evil/x", API, "callback.webhook_url")
    assert getattr(exc.value, "code", None) == "VALIDATION_ERROR"


def _row(step: int, role: str, content: dict) -> RunStep:
    return RunStep(run_id=None, step=step, role=role, content=content)


def test_second_resume_keeps_steps_after_dangling() -> None:
    head = [{"role": "system", "content": "s"}, {"role": "user", "content": "b"}]
    rows = [
        _row(
            1,
            "assistant",
            {"content": None, "tool_calls": [{"id": "a", "name": "x", "arguments": {}}]},
        ),
        _row(2, "tool", {"tool_call_id": "a", "content": "r1"}),
        # crash: dua tool call, hanya satu hasil tersimpan
        _row(
            3,
            "assistant",
            {
                "content": None,
                "tool_calls": [
                    {"id": "b", "name": "y", "arguments": {}},
                    {"id": "c", "name": "z", "arguments": {}},
                ],
            },
        ),
        _row(4, "tool", {"tool_call_id": "b", "content": "parsial"}),
        # setelah resume pertama
        _row(
            5,
            "assistant",
            {"content": None, "tool_calls": [{"id": "d", "name": "y", "arguments": {}}]},
        ),
        _row(6, "tool", {"tool_call_id": "d", "content": "r3"}),
    ]
    msgs = messages_from_steps(head, rows)
    contents = [m.get("content") for m in msgs if m["role"] == "tool"]
    assert contents == ["r1", "r3"]
    assert sum(1 for m in msgs if m["role"] == "assistant") == 2


def test_redaction_export_sig_and_depth() -> None:
    url = "/admin/exports/h_x.csv?exp=1&sig=" + "a" * 64
    assert "a" * 64 not in redact_text(url)
    nested: dict = {"k": "v"}
    for _ in range(10):
        nested = {"n": nested}
    out = redact(nested)
    for _ in range(7):
        out = out["n"]
    assert out == "***"


def test_staging_rejects_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:CHANGE_ME@h/db")
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings()


def test_pepper_must_differ_from_dashboard_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_KEY_PEPPER", "test-server-api-key-0123456789abcdefghij")
    with pytest.raises(ConfigError, match="MCP_KEY_PEPPER"):
        load_settings()
