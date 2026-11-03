import asyncio

import pytest

from mcp_marketing.core.errors import BudgetExceeded
from mcp_marketing.services.agent.budget import BudgetLayer, BudgetManager, BudgetScope
from mcp_marketing.services.results.compact import compact
from mcp_marketing.utils.tokens import count_tokens


def _big_output(rows: int = 20, text_len: int = 3_000) -> dict:
    return {
        "summary": "x" * 400,
        "handle": "h_0123456789",
        "total": 5000,
        "stats": {"top_hashtags": [f"#tag{i}" for i in range(40)], "avg": 1.5},
        "items": {
            "cols": ["id", "text", "likes"],
            "rows": [[i, "kata panjang " * (text_len // 13), i] for i in range(rows)],
        },
        "extra_notes": "catatan opsional " * 50,
    }


def test_compact_9000_tokens_to_soft_limit_in_order() -> None:
    out = _big_output()
    assert count_tokens(out) > 9_000
    result = compact(out, soft_tokens=1_500, hard_tokens=4_000, drop_first=("extra_notes",))
    assert result.tokens_after <= 1_500
    ladder = ("text", "items", "stats", "drop")
    assert result.steps == ladder[: len(result.steps)]
    assert len(result.output["summary"]) <= 300
    assert result.output.get("truncated") is not True


def test_compact_reduces_items_to_five_when_text_not_enough() -> None:
    out = _big_output(rows=200, text_len=300)
    result = compact(out, soft_tokens=1_500, hard_tokens=40_000)
    assert result.steps[:2] == ("text", "items")
    assert len(result.output["items"]["rows"]) == 5
    assert result.tokens_after <= 1_500


def test_compact_drop_first_used_last() -> None:
    out = {"summary": "s", "items": {"cols": ["a"], "rows": [[1]]}, "notes": "n " * 3000}
    result = compact(out, soft_tokens=500, hard_tokens=10_000, drop_first=("notes",))
    assert "notes" not in result.output
    assert result.steps == ("text", "items", "stats", "drop")


def test_compact_hard_limit_any_mode_sets_truncated() -> None:
    out = {"summary": "s", "data": [{"t": "teks " * 50} for _ in range(400)]}
    result = compact(out, response_format="detailed", soft_tokens=1_500, hard_tokens=4_000)
    assert result.tokens_after <= 4_000
    assert result.output["truncated"] is True
    assert "data_get" in result.output["hint"]


def test_compact_detailed_under_hard_untouched() -> None:
    out = _big_output(rows=3, text_len=300)
    result = compact(out, response_format="detailed", soft_tokens=100, hard_tokens=40_000)
    assert result.output["items"]["rows"] == out["items"]["rows"]
    assert result.steps == ()


def test_compact_does_not_mutate_input() -> None:
    out = _big_output()
    rows_before = len(out["items"]["rows"])
    compact(out)
    assert len(out["items"]["rows"]) == rows_before


# ---------- budget ----------
async def test_run_budget_reserve_exceeds() -> None:
    scope = BudgetScope([BudgetLayer("run", 1_000)])
    with pytest.raises(BudgetExceeded) as exc:
        await scope.reserve(1_200)
    assert exc.value.details["scope"] == "run"  # type: ignore[index]
    assert exc.value.retryable is False


async def test_commit_reduces_remaining() -> None:
    layer = BudgetLayer("run", 1_000)
    scope = BudgetScope([layer])
    res = await scope.reserve(600)
    assert layer.remaining == 400
    await scope.commit(res, 250)
    assert layer.used == 250 and layer.reserved == 0 and layer.remaining == 750
    await scope.commit(res, 999)  # commit ganda diabaikan
    assert layer.used == 250


async def test_parallel_reserve_never_exceeds_limit() -> None:
    layer = BudgetLayer("run", 1_000)
    scope = BudgetScope([layer])

    async def attempt() -> bool:
        try:
            res = await scope.reserve(150)
        except BudgetExceeded:
            return False
        await asyncio.sleep(0.01)
        await scope.commit(res, 150)
        return True

    results = await asyncio.gather(*(attempt() for _ in range(10)))
    assert sum(results) == 6
    assert layer.used == 900 <= 1_000


async def test_failed_layer_rolls_back_earlier_layers() -> None:
    tool_layer = BudgetLayer("tool_call", 10_000)
    run_layer = BudgetLayer("run", 100)
    scope = BudgetScope([tool_layer, run_layer])
    with pytest.raises(BudgetExceeded):
        await scope.reserve(500)
    assert tool_layer.reserved == 0


async def test_manager_persists_run_usage() -> None:
    saved: dict[str, int] = {}

    async def loader(run_id: str) -> tuple[int, int]:
        return 100, 1_000

    async def persister(run_id: str, used: int) -> None:
        saved[run_id] = used

    mgr = BudgetManager(run_loader=loader, run_persister=persister)
    scope = await mgr.scope(client_id=None, daily_limit=None, run_id="r1", tool_limit=20_000)
    res = await scope.reserve(300)
    await scope.commit(res, 200)
    assert saved == {"r1": 300}
    again = await mgr.scope(client_id=None, daily_limit=None, run_id="r1", tool_limit=None)
    assert again.layers[0].used == 300  # lapis run dipakai bersama


async def test_client_daily_layer_loaded_once() -> None:
    calls = 0

    async def used_loader(client_id: str, day) -> int:  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return 900

    mgr = BudgetManager(client_used_loader=used_loader)
    s1 = await mgr.scope(client_id="c", daily_limit=1_000, run_id=None, tool_limit=None)
    await mgr.scope(client_id="c", daily_limit=1_000, run_id=None, tool_limit=None)
    assert calls == 1
    with pytest.raises(BudgetExceeded):
        await s1.reserve(200)
    assert await mgr.scope(
        client_id="c", daily_limit=None, run_id=None, tool_limit=None
    ) == BudgetScope([])


async def test_external_quota() -> None:
    mgr = BudgetManager()
    quota = mgr.external_quota("x_reads", 100)
    await quota.reserve(60)
    with pytest.raises(BudgetExceeded):
        await quota.reserve(50)
