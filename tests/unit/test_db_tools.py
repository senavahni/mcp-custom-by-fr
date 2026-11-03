"""TASK-18: whitelist entitas, filter aman, validator SQL read-only."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from mcp_marketing.core.errors import Forbidden, ValidationFailed
from mcp_marketing.services.db import entities as reg
from mcp_marketing.services.db import query as q
from mcp_marketing.services.db.sql_readonly import validate_sql
from mcp_marketing.services.db.write import ensure_writable

DATABASE_MD = Path(__file__).resolve().parents[2] / "documentation" / "DATABASE.md"
WS = uuid.UUID("00000000-0000-0000-0000-00000000000a")


def _sql(spec: reg.EntitySpec, filters: dict) -> str:
    source = spec.source()
    conds = q.filter_conditions(spec, source.c, filters)
    stmt = q.base_select(spec, source, ["id"], conds)
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_registry_matches_database_md_section_5() -> None:
    text = DATABASE_MD.read_text(encoding="utf-8")
    section = text.split("## 5. Entitas untuk Tool `db_*`", 1)[1].split("\n## ", 1)[0]
    documented = {
        m.group(1): m.group(2).split()[0]
        for m in re.finditer(r"^\| `(\w+)` \| [^|]+ \| ([^|]+) \|", section, re.M)
    }
    access = {"r": "r", "rw": "rw", "r (+ delete)": "rd"}
    assert set(documented) == set(reg.ENTITIES)
    for name, spec in reg.ENTITIES.items():
        raw = section.split(f"| `{name}` |", 1)[1].split("|")[1].strip()
        assert access[raw] == spec.access, name


def test_describe_is_compact() -> None:
    from mcp_marketing.utils.tokens import count_tokens

    assert count_tokens(reg.describe()) <= 1_400
    tasks = next(e for e in reg.describe() if e["name"] == "tasks")
    assert tasks["write_via"].startswith("task_create")


def test_injection_value_is_bound_parameter() -> None:
    sql = _sql(reg.entity("tasks"), {"title": "x'; DROP TABLE tasks; --"})
    assert "DROP" not in sql and "%(title_1)s" in sql


@pytest.mark.parametrize(
    ("filters", "msg"),
    [
        ({"password_hash": "x"}, "tidak dikenal"),
        ({"title": {"op": "regex", "value": "x"}}, "operator"),
        ({"depth": {"op": "eq", "value": "dua"}}, "tipe int"),
        ({"status": "selesai"}, "salah satu"),
        ({"tags": {"op": "gt", "value": "a"}}, "array"),
        ({"id": {"op": "in", "value": list(range(101))}}, "1–100"),
        ({"end_date": {"op": "between", "value": ["2026-01-01"]}}, "between"),
        ({"title": {"op": "eq", "value": "x", "extra": 1}}, "kunci filter"),
        ({"depth": {"op": "icontains", "value": "1"}}, "teks"),
    ],
)
def test_invalid_filters_rejected(filters: dict, msg: str) -> None:
    with pytest.raises(ValidationFailed, match=msg):
        _sql(reg.entity("tasks"), filters)


def test_filter_operators_compile() -> None:
    spec = reg.entity("tasks")
    sql = _sql(
        spec,
        {
            "status": {"op": "in", "value": ["todo", "in_progress"]},
            "end_date": {"op": "between", "value": ["2026-01-01", "2026-12-31"]},
            "tags": {"op": "contains", "value": ["promo"]},
            "title": {"op": "icontains", "value": "50%_off"},
            "completed_at": {"op": "is_null", "value": True},
            "parent_task_id": {"op": "is_null", "value": False},
        },
    )
    assert "CAST(public.tasks.status AS VARCHAR) IN" in sql
    assert "public.tasks.tags @>" in sql and "ILIKE" in sql
    assert "tasks.completed_at IS NULL" in sql and "parent_task_id IS NOT NULL" in sql


def test_scope_conditions() -> None:
    tasks, comp = reg.entity("tasks"), reg.entity("competitors")
    with pytest.raises(Forbidden):
        q.scope_conditions(tasks, tasks.source().c, q.WorkspaceScope(None))
    assert q.scope_conditions(tasks, tasks.source().c, q.WorkspaceScope(None, is_admin=True))
    glob = q.scope_conditions(comp, comp.source().c, q.WorkspaceScope(None))
    assert "workspace_id IS NULL" in str(glob[-1])
    scoped = q.scope_conditions(tasks, tasks.source().c, q.WorkspaceScope(WS))
    assert "projects.workspace_id" in str(scoped[-1])


def test_projection_and_order_validation() -> None:
    spec = reg.entity("tasks")
    assert q.project_fields(spec, None) == list(spec.default_fields)
    with pytest.raises(ValidationFailed):
        q.project_fields(spec, ["password_hash"])
    with pytest.raises(ValidationFailed):
        q._order(spec, "-tags")
    assert q._order(spec, "-end_date") == ("end_date", True)
    with pytest.raises(ValidationFailed):
        q._decode_cursor("bukan-cursor!!")


def test_cursor_roundtrip() -> None:
    cur = q._encode_cursor("2026-01-01", str(WS))
    assert q._decode_cursor(cur) == ("2026-01-01", str(WS))


def test_write_guards() -> None:
    with pytest.raises(Forbidden, match="task_create"):
        ensure_writable(reg.entity("tasks"))
    with pytest.raises(Forbidden):
        ensure_writable(reg.entity("reports"))
    ensure_writable(reg.entity("reports"), deleting=True)
    ensure_writable(reg.entity("competitors"))


def test_write_models_validate() -> None:
    with pytest.raises(ValueError):
        reg.CompetitorWrite(name="A", website="ftp://x")
    with pytest.raises(ValueError):
        reg.CompetitorWrite(name="A", keywords=[f"k{i}" for i in range(51)])
    assert reg.CompetitorWrite(name="A", keywords=[" a ", "a", ""]).keywords == ["a"]
    with pytest.raises(ValueError):
        reg.WatchlistWrite(name="w", keywords=["a"], sources=["tiktok"])


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE mcp.competitors SET name = 'x'",
        "SELECT pg_sleep(10)",
        "SELECT 1; SELECT 2",
        "WITH x AS (DELETE FROM mcp.jobs RETURNING *) SELECT * FROM x",
        "SELECT * FROM pg_catalog.pg_authid",
        "SELECT * FROM pg_authid",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM mcp.jobs FOR UPDATE",
        "SELECT * INTO mcp.copy FROM mcp.jobs",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT dblink('x', 'y')",
        "SELECT lo_import('/tmp/x')",
        "SELECT set_config('statement_timeout', '0', false)",
        "SELECT query_to_xml('select * from pg_authid', true, true, '')",
        "",
        "SELEC broken",
    ],
)
def test_sql_readonly_rejects(sql: str) -> None:
    with pytest.raises(ValidationFailed):
        validate_sql(sql)


def test_sql_readonly_accepts_select_with_cte_and_union() -> None:
    assert validate_sql("SELECT id FROM public.tasks;") == "SELECT id FROM public.tasks"
    validate_sql(
        "WITH t AS (SELECT id, status FROM public.tasks) SELECT status, count(*) FROM t "
        "GROUP BY status UNION ALL SELECT 'x', 1"
    )
    validate_sql("SELECT name FROM mcp.competitors WHERE name = :name -- komentar")
