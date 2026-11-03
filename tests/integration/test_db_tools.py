"""TASK-18 terhadap PostgreSQL: isolasi workspace, UC-12, CRUD, optimistic locking, SQL read-only."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import asyncpg
import pytest_asyncio

from mcp_marketing.server.app import build_asgi_app
from mcp_marketing.server.pipeline import ToolPipeline
from mcp_marketing.server.services import Services
from tests.integration.conftest import admin_test_url, make_client

TODAY = date.today()


@dataclass
class Dash:
    ws_a: uuid.UUID
    ws_b: uuid.UUID
    project_a: uuid.UUID
    overdue: uuid.UUID
    done: uuid.UUID
    deleted: uuid.UUID
    other_ws_task: uuid.UUID


@pytest_asyncio.fixture
async def dash(services: Services) -> AsyncIterator[Dash]:
    ids = {k: uuid.uuid4() for k in ("user", "ws_a", "ws_b", "pa", "pb", "t1", "t2", "t3", "t4")}
    conn = await asyncpg.connect(admin_test_url())
    try:
        await conn.execute(
            "INSERT INTO public.users (id, email, username, display_name, password_hash) "
            "VALUES ($1, $2, $3, 'Penguji', 'x')",
            ids["user"],
            f"u{ids['user'].hex[:8]}@test.local",
            f"u{ids['user'].hex[:8]}",
        )
        for ws, name in ((ids["ws_a"], "WS A"), (ids["ws_b"], "WS B")):
            await conn.execute(
                "INSERT INTO public.workspaces (id, name, owner_id) VALUES ($1, $2, $3)",
                ws,
                name,
                ids["user"],
            )
        await conn.execute(
            "INSERT INTO public.workspace_members (workspace_id, user_id, role) "
            "VALUES ($1, $2, 'owner')",
            ids["ws_a"],
            ids["user"],
        )
        for pid, ws in ((ids["pa"], ids["ws_a"]), (ids["pb"], ids["ws_b"])):
            await conn.execute(
                "INSERT INTO public.projects (id, workspace_id, title) VALUES ($1, $2, 'Launch')",
                pid,
                ws,
            )
        rows = [
            (ids["t1"], ids["pa"], "Overdue brief", "todo", TODAY - timedelta(days=3), None),
            (ids["t2"], ids["pa"], "Selesai", "done", TODAY - timedelta(days=5), None),
            (ids["t3"], ids["pa"], "Terhapus", "todo", TODAY - timedelta(days=2), "now"),
            (ids["t4"], ids["pb"], "Rahasia WS B", "todo", TODAY - timedelta(days=1), None),
        ]
        for tid, pid, title, status, end, deleted in rows:
            await conn.execute(
                "INSERT INTO public.tasks (id, project_id, title, status, end_date, deleted_at) "
                "VALUES ($1, $2, $3, $4::task_status, $5, "
                "CASE WHEN $6::text IS NULL THEN NULL ELSE now() END)",
                tid,
                pid,
                title,
                status,
                end,
                deleted,
            )
        yield Dash(ids["ws_a"], ids["ws_b"], ids["pa"], ids["t1"], ids["t2"], ids["t3"], ids["t4"])
    finally:
        await conn.execute(
            "DELETE FROM public.tasks WHERE project_id = ANY($1::uuid[])", [ids["pa"], ids["pb"]]
        )
        await conn.execute(
            "DELETE FROM public.projects WHERE id = ANY($1::uuid[])", [ids["pa"], ids["pb"]]
        )
        await conn.execute("DELETE FROM public.workspace_members WHERE user_id = $1", ids["user"])
        await conn.execute(
            "DELETE FROM public.workspaces WHERE id = ANY($1::uuid[])", [ids["ws_a"], ids["ws_b"]]
        )
        await conn.execute("DELETE FROM public.users WHERE id = $1", ids["user"])
        await conn.close()


async def _pipeline(services: Services, **client_kw: Any) -> tuple[ToolPipeline, Any]:
    _, pipeline, _ = build_asgi_app(services)
    _, client = await make_client(
        services,
        name=f"db-{uuid.uuid4().hex[:6]}",
        toolsets=["db", "db_admin", "data"],
        **client_kw,
    )
    return pipeline, client


async def test_workspace_isolation_and_soft_delete(services: Services, dash: Dash) -> None:
    pipeline, client = await _pipeline(services, workspace_id=dash.ws_a)
    out = (
        await pipeline.call(
            "db_query", {"entity": "tasks", "fields": ["id", "title"]}, client=client
        )
    ).structured
    ids = {r[0] for r in out["items"]["rows"]}
    assert ids == {str(dash.overdue), str(dash.done)}
    other = await pipeline.call(
        "db_get", {"entity": "tasks", "id": str(dash.other_ws_task)}, client=client
    )
    assert other.error_code == "NOT_FOUND"
    members = (await pipeline.call("db_query", {"entity": "members"}, client=client)).structured
    assert members["items"]["rows"][0][1] == "Penguji"
    ws = (await pipeline.call("db_query", {"entity": "workspaces"}, client=client)).structured
    assert [r[1] for r in ws["items"]["rows"]] == ["WS A"]


async def test_uc12_overdue_matches_manual_sql(services: Services, dash: Dash) -> None:
    pipeline, client = await _pipeline(services, workspace_id=dash.ws_a)
    out = (
        await pipeline.call(
            "db_query",
            {
                "entity": "tasks",
                "filter": {
                    "end_date": {"op": "lt", "value": TODAY.isoformat()},
                    "status": {"op": "ne", "value": "done"},
                },
                "order_by": "end_date",
            },
            client=client,
        )
    ).structured
    conn = await asyncpg.connect(admin_test_url())
    try:
        manual = await conn.fetch(
            "SELECT t.id FROM public.tasks t JOIN public.projects p ON p.id = t.project_id "
            "WHERE p.workspace_id = $1 AND t.deleted_at IS NULL AND t.end_date < CURRENT_DATE "
            "AND t.status <> 'done'",
            dash.ws_a,
        )
    finally:
        await conn.close()
    assert [r[0] for r in out["items"]["rows"]] == [str(r["id"]) for r in manual]


async def test_injection_and_unknown_field(services: Services, dash: Dash) -> None:
    pipeline, client = await _pipeline(services, workspace_id=dash.ws_a)
    evil = await pipeline.call(
        "db_query",
        {"entity": "tasks", "filter": {"title": "x'; DROP TABLE public.tasks; --"}},
        client=client,
    )
    assert evil.structured["total"] == 0
    still = (await pipeline.call("db_query", {"entity": "tasks"}, client=client)).structured
    assert still["total"] == 2
    bad = await pipeline.call(
        "db_query", {"entity": "members", "fields": ["password_hash"]}, client=client
    )
    assert bad.error_code == "VALIDATION_ERROR"
    write = await pipeline.call(
        "db_create", {"entity": "tasks", "data": {"title": "x"}}, client=client
    )
    assert write.error_code == "FORBIDDEN"


async def test_competitor_crud_and_optimistic_locking(services: Services, dash: Dash) -> None:
    pipeline, client = await _pipeline(services, workspace_id=dash.ws_a)
    created = (
        await pipeline.call(
            "db_create",
            {
                "entity": "competitors",
                "data": {"name": "Kopi Kenangan", "website": "https://kk.test", "keywords": ["kk"]},
            },
            client=client,
        )
    ).structured
    cid = created["id"]
    dup = await pipeline.call(
        "db_create", {"entity": "competitors", "data": {"name": "Kopi Kenangan"}}, client=client
    )
    assert dup.error_code == "CONFLICT"
    bad = await pipeline.call(
        "db_create",
        {"entity": "competitors", "data": {"name": "X", "website": "javascript:alert(1)"}},
        client=client,
    )
    assert bad.error_code == "VALIDATION_ERROR"

    row = (
        await pipeline.call(
            "db_get", {"entity": "competitors", "id": cid, "fields": ["updated_at"]}, client=client
        )
    ).structured["data"]
    stamp = row["updated_at"]

    async def patch(notes: str) -> Any:
        return await pipeline.call(
            "db_update",
            {
                "entity": "competitors",
                "id": cid,
                "patch": {"notes": notes},
                "expected_updated_at": stamp,
            },
            client=client,
        )

    first, second = await asyncio.gather(patch("satu"), patch("dua"))
    outcomes = sorted([first.error_code or "ok", second.error_code or "ok"])
    assert outcomes == ["CONFLICT", "ok"]

    hard = await pipeline.call(
        "db_delete", {"entity": "competitors", "id": cid, "hard": True}, client=client
    )
    assert hard.error_code == "FORBIDDEN"
    soft = (
        await pipeline.call("db_delete", {"entity": "competitors", "id": cid}, client=client)
    ).structured
    assert soft == {"deleted": True, "mode": "soft"}
    gone = await pipeline.call("db_get", {"entity": "competitors", "id": cid}, client=client)
    assert gone.error_code == "NOT_FOUND"

    other_pipeline, other = await _pipeline(services, workspace_id=dash.ws_b)
    listed = (
        await other_pipeline.call("db_query", {"entity": "competitors"}, client=other)
    ).structured
    assert listed["total"] == 0


async def test_large_result_goes_to_handle_with_cursor(services: Services, dash: Dash) -> None:
    pipeline, client = await _pipeline(services, workspace_id=dash.ws_a)
    for i in range(30):
        await pipeline.call(
            "db_create",
            {"entity": "watchlists", "data": {"name": f"w{i:02d}", "keywords": ["k"]}},
            client=client,
        )
    page = (
        await pipeline.call(
            "db_query",
            {"entity": "watchlists", "fields": ["name"], "order_by": "name", "limit": 25},
            client=client,
        )
    ).structured
    assert page["handle"].startswith("h_") and len(page["items"]["rows"]) == 5
    assert page["total"] == 30 and page["next_cursor"]
    rest = (
        await pipeline.call(
            "db_query",
            {
                "entity": "watchlists",
                "fields": ["name"],
                "order_by": "name",
                "limit": 25,
                "cursor": page["next_cursor"],
            },
            client=client,
        )
    ).structured
    assert [r[0] for r in rest["items"]["rows"]] == [f"w{i:02d}" for i in range(25, 30)]
    data = (
        await pipeline.call("data_get", {"handle": page["handle"], "limit": 3}, client=client)
    ).structured
    assert data["total"] == 25


async def test_kb_collection_create_uses_embed_defaults(services: Services, dash: Dash) -> None:
    pipeline, client = await _pipeline(services, workspace_id=dash.ws_a)
    out = (
        await pipeline.call(
            "db_create", {"entity": "kb_collections", "data": {"name": "brief"}}, client=client
        )
    ).structured
    got = (
        await pipeline.call("db_get", {"entity": "kb_collections", "id": out["id"]}, client=client)
    ).structured["data"]
    assert got["embed_model"] == "embed-default" and got["workspace_id"] == str(dash.ws_a)


async def test_sql_readonly(services: Services, dash: Dash) -> None:
    pipeline, admin = await _pipeline(services, is_admin=True)
    ok = (
        await pipeline.call(
            "db_sql_readonly",
            {
                "sql": "SELECT title FROM public.tasks WHERE project_id = :p ORDER BY title",
                "params": {"p": str(dash.project_a)},
            },
            client=admin,
        )
    ).structured
    assert [r[0] for r in ok["items"]["rows"]] == ["Overdue brief", "Selesai", "Terhapus"]
    many = (
        await pipeline.call(
            "db_sql_readonly",
            {"sql": "SELECT generate_series(1, 50) AS n", "limit": 30},
            client=admin,
        )
    ).structured
    assert many["handle"] and many["truncated"] is True and many["total"] == 30
    for sql in ("UPDATE public.tasks SET title = 'x'", "SELECT pg_sleep(10)", "SELECT 1; SELECT 2"):
        res = await pipeline.call("db_sql_readonly", {"sql": sql}, client=admin)
        assert res.error_code == "VALIDATION_ERROR", sql
    timeout = await pipeline.call(
        "db_sql_readonly",
        {"sql": "SELECT count(*) FROM generate_series(1, 10000000) a, generate_series(1, 1000) b"},
        client=admin,
    )
    assert timeout.error_code == "UPSTREAM_TIMEOUT"

    user_pipeline, user = await _pipeline(services, workspace_id=dash.ws_a)
    denied = await user_pipeline.call("db_sql_readonly", {"sql": "SELECT 1"}, client=user)
    assert denied.error_code == "FORBIDDEN"
