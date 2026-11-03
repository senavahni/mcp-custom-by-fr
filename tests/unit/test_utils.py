from datetime import UTC, date, datetime

import httpx
import pytest
from freezegun import freeze_time
from pydantic import BaseModel, ValidationError

from mcp_marketing.core import context
from mcp_marketing.core.errors import (
    BudgetExceeded,
    NotFound,
    RateLimited,
    ToolError,
    to_tool_error,
)
from mcp_marketing.utils import crypto, ids, text, tokens
from mcp_marketing.utils.time import add_workdays, next_workday, now, parse_period, today_in


# ---------- time ----------
def test_parse_period_dates_jakarta() -> None:
    start, end = parse_period("2026-10-01", "2026-10-30", "Asia/Jakarta")
    assert start == datetime(2026, 9, 30, 17, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 30, 16, 59, 59, 999999, tzinfo=UTC)


def test_parse_period_iso_and_naive() -> None:
    # until tanpa offset ditafsirkan Asia/Jakarta (UTC+7): 20:00 WIB = 13:00Z
    start, end = parse_period("2026-10-01T10:00:00Z", "2026-10-01T20:00:00", "Asia/Jakarta")
    assert start == datetime(2026, 10, 1, 10, tzinfo=UTC)
    assert end == datetime(2026, 10, 1, 13, tzinfo=UTC)


@pytest.mark.parametrize(
    "since,until", [("2026-10-05", "2026-10-04"), ("2026-10-01T10:00Z", "2026-10-01T10:00Z")]
)
def test_parse_period_until_must_be_after_since(since: str, until: str) -> None:
    with pytest.raises(ToolError) as exc:
        parse_period(since, until)
    assert exc.value.code == "VALIDATION_ERROR"


def test_parse_period_bad_format() -> None:
    with pytest.raises(ToolError, match="since"):
        parse_period("01/10/2026", "2026-10-02")


@freeze_time("2026-11-03 20:00:00")
def test_now_and_today_frozen() -> None:
    assert now() == datetime(2026, 11, 3, 20, tzinfo=UTC)
    assert today_in("Asia/Jakarta") == date(2026, 11, 4)


def test_workdays() -> None:
    friday = date(2026, 10, 30)
    assert add_workdays(friday, 3) == date(2026, 11, 3)  # Jumat, Senin, Selasa
    assert next_workday(date(2026, 10, 31)) == date(2026, 11, 2)
    assert add_workdays(date(2026, 10, 31), 1) == date(2026, 11, 2)


# ---------- ids ----------
def test_ids_format_and_unique() -> None:
    handles = {ids.new_handle() for _ in range(500)}
    assert len(handles) == 500
    assert all(ids.is_short_id(h, "h") for h in handles)
    assert ids.is_short_id(ids.new_job_id(), "j")
    assert ids.is_short_id(ids.new_report_id(), "r")
    assert ids.is_short_id(ids.new_mcp_run_id(), "mr")
    assert not ids.is_short_id("h_ILOU000000", "h")


# ---------- tokens ----------
def test_count_tokens_deterministic() -> None:
    assert tokens.count_tokens("Halo dunia, ini MCP Marketing.") == tokens.count_tokens(
        "Halo dunia, ini MCP Marketing."
    )
    assert tokens.count_tokens("") == 0
    assert tokens.count_tokens({"a": [1, 2, 3]}) > 0


def test_count_tokens_large_text_estimates() -> None:
    assert tokens.count_tokens("a" * 400_000) == 100_000


def test_truncate_to_tokens() -> None:
    long = "kata " * 500
    cut = tokens.truncate_to_tokens(long, 20)
    assert tokens.count_tokens(cut) <= 21
    assert cut.endswith("…")
    assert tokens.truncate_to_tokens("pendek", 20) == "pendek"


def test_count_tokens_pydantic_model() -> None:
    class M(BaseModel):
        x: int

    assert tokens.count_tokens(M(x=1)) > 0


# ---------- text ----------
def test_truncate_unicode_safe() -> None:
    assert text.truncate("abcdef", 4) == "abc…"
    assert text.truncate("abc", 10) == "abc"
    assert text.truncate("abc", 0) == ""
    s = "café latte"
    assert "́" not in text.truncate(s, 5)[-2:]


def test_strip_and_normalize() -> None:
    assert text.strip_control_chars("a\x00b\x07c\nd") == "abc\nd"
    assert text.normalize_ws("  a   b \n\n\n\n c ") == "a b\n\nc"


# ---------- crypto ----------
def test_client_key_roundtrip() -> None:
    key, prefix = crypto.generate_client_key()
    assert crypto.parse_client_key(key) == prefix
    assert len(key) == len("mcpk_") + 8 + 1 + 43
    assert crypto.parse_client_key("mcpk_bad") is None
    h = crypto.hash_client_key("pepper", key)
    assert len(h) == 64
    assert crypto.constant_time_eq(h, crypto.hash_client_key("pepper", key))
    assert not crypto.constant_time_eq(h, crypto.hash_client_key("other", key))


def test_hmac_vector() -> None:
    # vektor RFC 4231 test case 2
    assert (
        crypto.hmac_sha256_hex("Jefe", "what do ya want for nothing?")
        == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
    )


# ---------- errors ----------
def test_tool_error_structured() -> None:
    err = BudgetExceeded("habis", details={"used": 10, "limit": 5, "scope": "run"})
    assert err.to_structured() == {
        "code": "BUDGET_EXCEEDED",
        "message": "habis",
        "retryable": False,
        "details": {"used": 10, "limit": 5, "scope": "run"},
    }
    assert RateLimited("x").retryable is True
    assert NotFound("x").to_structured()["retryable"] is False


def test_to_tool_error_mappings() -> None:
    class M(BaseModel):
        a: int
        b: int
        c: int
        d: int
        e: int
        f: int

    with pytest.raises(ValidationError) as exc:
        M.model_validate({})
    mapped = to_tool_error(exc.value)
    assert mapped.code == "VALIDATION_ERROR"
    assert len(mapped.details["fields"]) == 5  # type: ignore[index]
    assert to_tool_error(httpx.ReadTimeout("t")).code == "UPSTREAM_TIMEOUT"
    assert to_tool_error(TimeoutError()).code == "UPSTREAM_TIMEOUT"
    internal = to_tool_error(RuntimeError("secret detail sk-xxx"))
    assert internal.code == "INTERNAL"
    assert "secret" not in internal.message


# ---------- context ----------
def test_context_scope_and_headers() -> None:
    assert context.outgoing_headers() == {}
    with context.context_scope(request_id="r1", run_id="run"):
        assert context.outgoing_headers() == {"X-Request-Id": "r1"}
        assert context.snapshot() == {"request_id": "r1", "run_id": "run"}
    assert context.get("request_id") is None
    with pytest.raises(KeyError):
        context.bind_context(unknown="x")
