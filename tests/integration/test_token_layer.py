"""TASK-05: result store, kompaksi otomatis, cache, kepemilikan handle, export."""

from __future__ import annotations

import asyncio
import random
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pandas as pd
import pytest
from pydantic import BaseModel

from mcp_marketing.core.errors import NotFound
from mcp_marketing.schemas.common import Envelope, ReadInput, ToolInput
from mcp_marketing.server.app import build_asgi_app
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.registry import ToolContext, ToolSpec, register, unregister
from mcp_marketing.server.services import Services
from mcp_marketing.services.results.result_store import Owner
from tests.integration.conftest import LiveServer, make_client

COLS = ["id", "created_at", "author", "text", "likes", "tags"]
BASE = datetime(2026, 10, 1, tzinfo=UTC)


def _rows(n: int) -> list[dict[str, Any]]:
    rng = random.Random(42)
    return [
        {
            "id": i,
            "created_at": BASE + timedelta(hours=rng.randint(0, 24 * 30)),
            "author": f"user{rng.randint(1, 50)}",
            "text": f"post nomor {i} tentang kopi susu " + "x" * rng.randint(0, 50),
            "likes": rng.randint(0, 500),
            "tags": [f"#t{rng.randint(1, 8)}" for _ in range(rng.randint(0, 3))],
        }
        for i in range(n)
    ]


async def _make_set(
    services: Services, owner: Owner, n: int = 10_000
) -> tuple[str, list[dict[str, Any]]]:
    rows = _rows(n)
    rs = await services.results.create(
        kind="social_posts",
        columns=COLS,
        rows=rows,
        owner=owner,
        sort_ts="created_at",
        sort_num="likes",
    )
    return rs.handle, rows


OWNER = Owner(client_id="00000000-0000-0000-0000-000000000001")


async def test_data_get_10k_filter_order_p95_and_stable_cursor(services: Services) -> None:
    handle, rows = await _make_set(services, OWNER)
    timings = []
    for _ in range(20):
        t = time.perf_counter()
        page = await services.results.page(
            handle,
            OWNER,
            flt={"likes": {"op": "gte", "value": 250}},
            order_by="-likes",
            limit=20,
            fields=["id", "likes"],
        )
        timings.append(time.perf_counter() - t)
    timings.sort()
    assert timings[18] < 0.300
    expected_total = sum(1 for r in rows if r["likes"] >= 250)
    assert page["total"] == expected_total

    seen: list[int] = []
    cursor = None
    while True:
        page = await services.results.page(
            handle,
            OWNER,
            order_by="-likes",
            limit=20,
            cursor=cursor,
            fields=["id", "likes"],
            flt={"likes": {"op": "gte", "value": 250}},
        )
        seen += [r[0] for r in page["items"]["rows"]]
        likes = [r[1] for r in page["items"]["rows"]]
        assert likes == sorted(likes, reverse=True)
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == expected_total


async def test_filters_variants(services: Services) -> None:
    handle, rows = await _make_set(services, OWNER, 500)
    page = await services.results.page(
        handle, OWNER, flt={"tags": {"op": "contains", "value": "#t3"}}, limit=20
    )
    assert page["total"] == sum(1 for r in rows if "#t3" in r["tags"])
    page = await services.results.page(
        handle, OWNER, flt={"author": {"op": "in", "value": ["user1", "user2"]}}
    )
    assert page["total"] == sum(1 for r in rows if r["author"] in ("user1", "user2"))
    page = await services.results.page(
        handle, OWNER, flt={"likes": {"op": "between", "value": [10, 20]}}
    )
    assert page["total"] == sum(1 for r in rows if 10 <= r["likes"] <= 20)
    page = await services.results.page(
        handle, OWNER, flt={"text": {"op": "icontains", "value": "KOPI"}}
    )
    assert page["total"] == 500
    with pytest.raises(Exception, match="fields"):
        await services.results.page(handle, OWNER, fields=["tidak_ada"])


async def test_aggregate_matches_pandas(services: Services) -> None:
    handle, rows = await _make_set(services, OWNER, 3_000)
    out = await services.results.aggregate(
        handle, OWNER, group_by=["day"], metrics=["count", "avg:likes"], time_bucket="day"
    )
    df = pd.DataFrame(rows)
    df["day"] = (
        pd.to_datetime(df["created_at"]).dt.tz_convert("Asia/Jakarta").dt.strftime("%Y-%m-%d")
    )
    expected = df.groupby("day").agg(count=("id", "size"), avg=("likes", "mean")).reset_index()
    got = {r[0]: (r[1], r[2]) for r in out["rows"]}
    assert len(got) == len(expected)
    for _, e in expected.iterrows():
        count, avg = got[e["day"]]
        assert count == e["count"]
        assert avg == pytest.approx(e["avg"])
    top = await services.results.aggregate(handle, OWNER, group_by=[], metrics=["top:tags:3"])
    counts = pd.Series([t for r in rows for t in r["tags"]]).value_counts()
    assert top["rows"][0] == [counts.index[0], int(counts.iloc[0])]
    p90 = await services.results.aggregate(
        handle, OWNER, group_by=["author"], metrics=["p90:likes", "max:likes"], limit=5
    )
    assert len(p90["rows"]) == 5


async def test_handle_ownership(services: Services) -> None:
    handle, _ = await _make_set(services, OWNER, 10)
    other = Owner(client_id="00000000-0000-0000-0000-000000000002")
    with pytest.raises(NotFound):
        await services.results.page(handle, other)
    admin = Owner(client_id="00000000-0000-0000-0000-000000000009", is_admin=True)
    assert (await services.results.page(handle, admin))["total"] == 10
    run_owner = Owner(client_id=None, run_id="11111111-1111-1111-1111-111111111111")
    h2 = (
        await services.results.create(kind="k", columns=["a"], rows=[{"a": 1}], owner=run_owner)
    ).handle
    same_run = Owner(
        client_id="00000000-0000-0000-0000-000000000003",
        run_id="11111111-1111-1111-1111-111111111111",
    )
    assert (await services.results.page(h2, same_run))["total"] == 1


# ---------- tool contoh untuk pipeline ----------
class DumpIn(ReadInput):
    n: int = 5_000


class SlowIn(ReadInput):
    keyword: str


class WriteIn(ToolInput):
    value: int


class AnyOut(BaseModel):
    summary: str


@pytest.fixture
def sample_tools() -> Any:
    calls = {"slow": 0, "write": 0}

    async def dump(inp: DumpIn, ctx: ToolContext) -> dict[str, Any]:
        rows = [[i, f"teks panjang nomor {i} " * 5, i % 7] for i in range(inp.n)]
        return {
            "summary": f"{inp.n} baris",
            "total": inp.n,
            "items": {"cols": ["id", "text", "g"], "rows": rows},
            "stats": {},
            "cost": {"llm_tokens": 0, "external_units": 0, "cache_hit": False},
        }

    async def slow(inp: SlowIn, ctx: ToolContext) -> dict[str, Any]:
        calls["slow"] += 1
        await asyncio.sleep(2)
        return {
            "summary": f"trends {inp.keyword}",
            "cost": {"llm_tokens": 0, "external_units": 0, "cache_hit": False},
        }

    async def write(inp: WriteIn, ctx: ToolContext) -> dict[str, Any]:
        calls["write"] += 1
        return {"summary": "ditulis"}

    specs = [
        ToolSpec("test_dump", "data", "dump", "dump rows", DumpIn, Envelope, dump),
        ToolSpec(
            "test_trends", "data", "slow", "slow trends", SlowIn, AnyOut, slow, cache_ttl_s=86_400
        ),
        ToolSpec(
            "test_write",
            "data",
            "write",
            "write",
            WriteIn,
            AnyOut,
            write,
            cache_ttl_s=60,
            writes=True,
        ),
    ]
    for s in specs:
        register(s)
    yield calls
    for s in specs:
        unregister(s.name)


def _client(
    ws: str | None = None, cid: str = "00000000-0000-0000-0000-0000000000aa"
) -> ClientIdentity:
    return ClientIdentity(
        id=cid,
        name="t",
        kind="external",
        toolsets=frozenset({"data"}),
        workspace_id=ws,
        rpm_limit=10_000,
    )


async def test_dump_5000_rows_becomes_handle(services: Services, sample_tools: Any) -> None:
    _, pipeline, _ = build_asgi_app(services)
    out = await pipeline.call("test_dump", {}, client=_client())
    sc = out.structured
    assert out.status == "ok"
    assert sc["handle"].startswith("h_") and sc["total"] == 5_000
    assert len(sc["items"]["rows"]) == 5
    assert out.output_tokens <= 1_500
    page = await services.results.page(sc["handle"], Owner(client_id=_client().id))
    assert page["total"] == 5_000
    await pipeline.drain()


async def test_cache_hit_fast_scoped_and_bypass(services: Services, sample_tools: Any) -> None:
    _, pipeline, _ = build_asgi_app(services)
    first = await pipeline.call("test_trends", {"keyword": "kopi"}, client=_client("ws1"))
    assert first.status == "ok"
    t = time.perf_counter()
    second = await pipeline.call(
        "test_trends", {"keyword": "kopi", "response_format": "detailed"}, client=_client("ws1")
    )
    assert time.perf_counter() - t < 0.05
    assert second.status == "cached" and second.structured["cost"]["cache_hit"] is True
    assert sample_tools["slow"] == 1
    other_ws = await pipeline.call("test_trends", {"keyword": "kopi"}, client=_client("ws2"))
    assert other_ws.status == "ok" and sample_tools["slow"] == 2
    bypass = await pipeline.call(
        "test_trends", {"keyword": "kopi", "cache": "bypass"}, client=_client("ws1")
    )
    assert bypass.status == "ok" and sample_tools["slow"] == 3
    await pipeline.call("test_write", {"value": 1}, client=_client("ws1"))
    await pipeline.call("test_write", {"value": 1}, client=_client("ws1"))
    assert sample_tools["write"] == 2
    await pipeline.drain()


async def test_client_daily_budget_exhausted_blocks_before_handler(
    services: Services, sample_tools: Any
) -> None:
    async def used(client_id: str, day: Any) -> int:
        return 1_000

    services.budgets._client_used_loader = used  # noqa: SLF001
    _, pipeline, _ = build_asgi_app(services)
    client = ClientIdentity(
        id="00000000-0000-0000-0000-0000000000bb",
        name="b",
        kind="external",
        toolsets=frozenset({"data"}),
        daily_token_budget=1_000,
        rpm_limit=1_000,
    )
    out = await pipeline.call("test_write", {"value": 1}, client=client)
    assert out.error_code == "BUDGET_EXCEEDED"
    assert sample_tools["write"] == 0
    await pipeline.drain()


async def test_data_tools_and_export_via_mcp(services: Services, live_server: LiveServer) -> None:
    from tests.integration.conftest import mcp_session

    key, identity = await make_client(services, toolsets=["data"])
    handle, _ = await _make_set(services, Owner(client_id=identity.id), 100)
    async with mcp_session(live_server.url, key) as client:
        got = (
            await client.call_tool(
                "data_get", {"handle": handle, "limit": 3, "fields": ["id", "likes"]}
            )
        ).structured_content
        assert got["items"]["cols"] == ["id", "likes"] and len(got["items"]["rows"]) == 3
        agg = (
            await client.call_tool(
                "data_aggregate",
                {"handle": handle, "group_by": ["author"], "metrics": ["count"], "limit": 3},
            )
        ).structured_content
        assert agg["cols"] == ["author", "count"]
        exp = await client.call_tool("data_export", {"handle": handle, "format": "csv"})
        url = exp.structured_content["download_url"]
    assert url.startswith(services.settings.public_base_url)
    url = url.replace(services.settings.public_base_url, live_server.url)
    async with httpx.AsyncClient() as http:
        r = await http.get(url)
        assert r.status_code == 200 and r.text.count("\n") == 101
        tampered = url.replace("sig=", "sig=0")
        assert (await http.get(tampered)).status_code == 403
