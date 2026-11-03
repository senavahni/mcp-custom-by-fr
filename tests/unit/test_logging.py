import json
import logging
from pathlib import Path

import pytest

from mcp_marketing.core import context
from mcp_marketing.core.logging import (
    DailyFileHandler,
    configure_logging,
    get_logger,
    redact,
    redact_text,
)

# dibangun dinamis agar file test sendiri tidak memicu secret_scan
FAKE_GATEWAY_KEY = "lgw_" + "abcd1234_" + "x" * 43
FAKE_CLIENT_KEY = "mcpk_" + "abcd1234_" + "y" * 43
FAKE_OPENAI = "sk-" + "z" * 30
FAKE_AWS = "AKIA" + "A" * 16


@pytest.mark.parametrize(
    "raw,secret",
    [
        ("Authorization: Bearer abc.def-123", "abc.def-123"),
        (f"key={FAKE_GATEWAY_KEY}", FAKE_GATEWAY_KEY),
        (f"client {FAKE_CLIENT_KEY} used", FAKE_CLIENT_KEY),
        (f"openai {FAKE_OPENAI}", FAKE_OPENAI),
        (f"aws {FAKE_AWS}", FAKE_AWS),
        ("postgresql+asyncpg://mcp_app:supersecret@127.0.0.1/db", "supersecret"),
        ("sig sha256=" + "a" * 64, "a" * 64),
    ],
)
def test_redact_text_patterns(raw: str, secret: str) -> None:
    assert secret not in redact_text(raw)


@pytest.mark.parametrize(
    "key", ["authorization", "token", "MCP_WEBHOOK_SECRET", "password", "cookie", "access_token"]
)
def test_redact_sensitive_keys(key: str) -> None:
    out = redact({key: "value-that-must-hide", "nested": {key: "v2"}})
    assert out[key] == "***"
    assert out["nested"][key] == "***"


def test_metric_keys_not_redacted() -> None:
    out = redact({"output_tokens": 10, "max_tokens": 5, "tokens_used": 3})
    assert out == {"output_tokens": 10, "max_tokens": 5, "tokens_used": 3}


def test_log_line_has_context_and_is_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    configure_logging("INFO", tmp_path)
    log = get_logger("t")
    with context.context_scope(request_id="req-1", client_id="c-1", tool="x_search", run_id="r-1"):
        log.info("tool.call", header="Authorization: Bearer abc123", api_key="raw", duration_ms=5)
    for handler in logging.getLogger().handlers:
        handler.flush()
    line = capsys.readouterr().out.strip().splitlines()[-1]
    data = json.loads(line)
    assert data["event"] == "tool.call"
    assert data["level"] == "info"
    assert data["request_id"] == "req-1"
    assert data["client_id"] == "c-1"
    assert data["tool"] == "x_search"
    assert data["run_id"] == "r-1"
    assert data["duration_ms"] == 5
    assert "ts" in data
    assert "abc123" not in line
    assert data["api_key"] == "***"
    files = list(tmp_path.glob("mcp-*.log"))
    assert len(files) == 1
    assert "abc123" not in files[0].read_text(encoding="utf-8")
    configure_logging("WARNING", None)


def test_daily_handler_keeps_limited_files(tmp_path: Path) -> None:
    for i in range(20):
        (tmp_path / f"mcp-2026-01-{i + 1:02d}.log").write_text("x")
    handler = DailyFileHandler(tmp_path, keep=14)
    handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None))
    handler.close()
    assert len(list(tmp_path.glob("mcp-*.log"))) == 14
