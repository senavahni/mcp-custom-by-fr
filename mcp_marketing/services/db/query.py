"""Query builder aman untuk `db_*`: filter terstruktur -> SQLAlchemy Core (parameter binding).

Tidak ada string SQL dari input: nama field divalidasi terhadap whitelist entitas, nilai selalu
di-bind. Baris dibatasi workspace klien/run dan `deleted_at IS NULL`.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import orjson
from sqlalchemy import (
    ColumnElement,
    FromClause,
    Select,
    String,
    and_,
    cast,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_marketing.core.errors import Forbidden, ValidationFailed
from mcp_marketing.models.dashboard import Project
from mcp_marketing.services.db.entities import EntitySpec

OPS = frozenset(
    {
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "in",
        "contains",
        "icontains",
        "starts_with",
        "is_null",
        "between",
    }
)
ORDERABLE = frozenset({"uuid", "str", "int", "float", "bool", "date", "datetime"})
MAX_IN = 100
MAX_LIMIT = 100
MAX_ROWS_COUNTED = 10_000
STATEMENT_TIMEOUT = "5s"
CELL_MAX_CHARS = 1_000


@dataclass(frozen=True)
class WorkspaceScope:
    workspace_id: uuid.UUID | None
    is_admin: bool = False


@dataclass
class QueryResult:
    cols: list[str]
    rows: list[dict[str, Any]]
    total: int
    total_capped: bool
    next_cursor: str | None


# ------------------------------------------------------------------ nilai & tipe
def _bad(field: str, msg: str) -> ValidationFailed:
    return ValidationFailed(f"{field}: {msg}", details={"field": field})


def coerce(spec: EntitySpec, field: str, value: Any) -> Any:
    kind = spec.field_type(field)
    if value is None:
        return None
    try:
        if kind == "uuid":
            return uuid.UUID(str(value))
        if kind == "int":
            if isinstance(value, bool):
                raise ValueError
            return int(value)
        if kind == "float":
            return float(value)
        if kind == "bool":
            if not isinstance(value, bool):
                raise ValueError
            return value
        if kind == "date":
            return date.fromisoformat(str(value)[:10])
        if kind == "datetime":
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if kind.startswith("enum:"):
            allowed = kind[5:].split(",")
            if value not in allowed:
                raise _bad(field, f"harus salah satu dari {allowed}")
            return value
        if kind in ("str", "str[]"):
            if not isinstance(value, str | int | float):
                raise ValueError
            return str(value)
    except (ValueError, TypeError):
        raise _bad(field, f"nilai tidak cocok dengan tipe {kind}") from None
    raise _bad(field, f"tipe {kind} tidak dapat difilter dengan nilai")


def _col(spec: EntitySpec, cols: Any, field: str) -> Any:
    """Kolom enum Postgres (task_status, …) dipakai sebagai teks agar bind varchar valid."""
    col = cols[field]
    return cast(col, String) if spec.field_type(field).startswith("enum:") else col


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _condition(spec: EntitySpec, cols: Any, field: str, op: str, value: Any) -> ColumnElement[Any]:
    if field not in spec.fields:
        raise _bad(field, "field tidak dikenal / tidak diizinkan")
    if op not in OPS:
        raise _bad(field, f"operator '{op}' tidak dikenal")
    kind = spec.field_type(field)
    col = _col(spec, cols, field)
    if op == "is_null":
        if not isinstance(value, bool):
            raise _bad(field, "is_null butuh nilai true/false")
        return col.is_(None) if value else col.is_not(None)
    if kind == "json":
        raise _bad(field, "field json hanya mendukung is_null")
    if kind == "str[]":
        if op not in ("contains", "eq"):
            raise _bad(field, "field array hanya mendukung contains / eq")
        values = value if isinstance(value, list) else [value]
        if not values or len(values) > MAX_IN:
            raise _bad(field, f"butuh 1–{MAX_IN} nilai")
        items = [coerce(spec, field, v) for v in values]
        return col.contains(items) if op == "contains" else col == items
    if op in ("in", "between"):
        if not isinstance(value, list):
            raise _bad(field, f"{op} butuh list")
        if op == "between":
            if len(value) != 2:
                raise _bad(field, "between butuh [awal, akhir]")
            return col.between(coerce(spec, field, value[0]), coerce(spec, field, value[1]))
        if not 1 <= len(value) <= MAX_IN:
            raise _bad(field, f"in butuh 1–{MAX_IN} nilai")
        return col.in_([coerce(spec, field, v) for v in value])
    if op in ("contains", "icontains", "starts_with"):
        if kind != "str":
            raise _bad(field, f"{op} hanya untuk field teks")
        needle = _escape_like(str(coerce(spec, field, value)))
        if op == "starts_with":
            return col.like(f"{needle}%", escape="\\")
        pattern = f"%{needle}%"
        return (
            col.ilike(pattern, escape="\\") if op == "icontains" else col.like(pattern, escape="\\")
        )
    v = coerce(spec, field, value)
    return {
        "eq": col == v,
        "ne": col != v,
        "lt": col < v,
        "lte": col <= v,
        "gt": col > v,
        "gte": col >= v,
    }[op]


def filter_conditions(
    spec: EntitySpec, cols: Any, filters: dict[str, Any] | None
) -> list[ColumnElement[Any]]:
    """`{field: value}` (= eq) atau `{field: {"op": ..., "value": ...}}`."""
    out: list[ColumnElement[Any]] = []
    for field, cond in (filters or {}).items():
        if isinstance(cond, dict) and "op" in cond:
            extra = set(cond) - {"op", "value"}
            if extra:
                raise _bad(field, f"kunci filter tidak dikenal {sorted(extra)}")
            out.append(_condition(spec, cols, field, str(cond["op"]), cond.get("value")))
        else:
            out.append(_condition(spec, cols, field, "eq", cond))
    return out


def scope_conditions(spec: EntitySpec, cols: Any, scope: WorkspaceScope) -> list[Any]:
    conds: list[Any] = []
    if spec.soft_delete:
        conds.append(cols["deleted_at"].is_(None))
    for name, value in spec.hidden_filters.items():
        conds.append(cols[name] == value)
    if spec.scope == "none":
        return conds
    ws = scope.workspace_id
    if ws is None:
        if scope.is_admin:
            return conds
        if spec.schema == "public":
            raise Forbidden("Klien MCP belum dibatasi ke workspace Dashboard")
        return [*conds, cols["workspace_id"].is_(None)]  # data global milik MCP
    if spec.scope == "self":
        return [*conds, cols["id"] == ws]
    if spec.scope == "column":
        return [*conds, cols["workspace_id"] == ws]
    projects = select(Project.id).where(Project.workspace_id == ws, Project.deleted_at.is_(None))
    return [*conds, cols["project_id"].in_(projects)]


# ------------------------------------------------------------------ cursor keyset
def _encode_cursor(order_value: Any, id_value: Any) -> str:
    raw = orjson.dumps([order_value, id_value], default=str)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[Any, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value, ident = orjson.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError, orjson.JSONDecodeError):
        raise ValidationFailed("cursor tidak valid", details={"field": "cursor"}) from None
    return value, ident


def _order(spec: EntitySpec, order_by: str | None) -> tuple[str, bool]:
    if not order_by:
        return spec.id_field, False
    desc = order_by.startswith("-")
    name = order_by.lstrip("-")
    if name not in spec.fields or spec.field_type(name).split(":")[0] not in ORDERABLE | {"enum"}:
        raise _bad("order_by", f"tidak bisa mengurutkan berdasarkan '{name}'")
    return name, desc


def _keyset(spec: EntitySpec, cols: Any, field: str, desc: bool, cursor: str) -> ColumnElement[Any]:
    value, ident = _decode_cursor(cursor)
    col, id_col = _col(spec, cols, field), cols[spec.id_field]
    id_v = coerce(spec, spec.id_field, ident)
    if field == spec.id_field:
        return id_col < id_v if desc else id_col > id_v
    if value is None:  # NULLS LAST: sudah di bagian null
        return and_(col.is_(None), id_col > id_v)
    v = coerce(spec, field, value)
    beyond = col < v if desc else col > v
    return or_(beyond, and_(col == v, id_col > id_v), col.is_(None))


# ------------------------------------------------------------------ eksekusi
def _cell(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, str) and len(value) > CELL_MAX_CHARS:
        return value[:CELL_MAX_CHARS] + "…"
    return value


def project_fields(spec: EntitySpec, fields: list[str] | None) -> list[str]:
    chosen = list(fields or spec.default_fields)
    unknown = [f for f in chosen if f not in spec.fields]
    if unknown:
        raise ValidationFailed(
            f"field tidak dikenal: {unknown}", details={"allowed": list(spec.fields)}
        )
    return list(dict.fromkeys(chosen))


async def set_timeout(session: AsyncSession) -> None:
    await session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))


def base_select(
    spec: EntitySpec, source: FromClause, fields: list[str], conds: list[Any]
) -> Select[Any]:
    cols = source.c
    selected = list(dict.fromkeys([*fields, spec.id_field]))
    return select(*(cols[f] for f in selected)).where(*conds)


async def run_query(
    session: AsyncSession,
    spec: EntitySpec,
    scope: WorkspaceScope,
    *,
    filters: dict[str, Any] | None = None,
    fields: list[str] | None = None,
    search: str | None = None,
    order_by: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> QueryResult:
    if not 1 <= limit <= MAX_LIMIT:
        raise _bad("limit", f"1–{MAX_LIMIT}")
    source = spec.source()
    cols = source.c
    chosen = project_fields(spec, fields)
    conds = [*scope_conditions(spec, cols, scope), *filter_conditions(spec, cols, filters)]
    if search:
        if not spec.searchable:
            raise _bad("search", f"entitas {spec.name} tidak mendukung pencarian teks")
        needle = f"%{_escape_like(search.strip())}%"
        conds.append(or_(*(cols[f].ilike(needle, escape="\\") for f in spec.searchable)))
    order_field, desc = _order(spec, order_by)
    base = base_select(spec, source, chosen, conds)
    await set_timeout(session)
    counted = await session.scalar(
        select(func.count()).select_from(base.limit(MAX_ROWS_COUNTED + 1).subquery())
    )
    total = int(counted or 0)
    page = base
    if cursor:
        page = page.where(_keyset(spec, cols, order_field, desc, cursor))
    order_col = _col(spec, cols, order_field)
    ordering = [order_col.desc().nulls_last() if desc else order_col.asc().nulls_last()]
    if order_field != spec.id_field:
        ordering.append(cols[spec.id_field].asc())
    rows = (await session.execute(page.order_by(*ordering).limit(limit + 1))).mappings().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        _encode_cursor(_cell(rows[-1][order_field]), _cell(rows[-1][spec.id_field]))
        if has_more and rows
        else None
    )
    return QueryResult(
        cols=chosen,
        rows=[{f: _cell(r[f]) for f in chosen} for r in rows],
        total=min(total, MAX_ROWS_COUNTED),
        total_capped=total > MAX_ROWS_COUNTED,
        next_cursor=next_cursor,
    )


async def get_one(
    session: AsyncSession,
    spec: EntitySpec,
    scope: WorkspaceScope,
    ident: Any,
    fields: list[str] | None = None,
) -> dict[str, Any] | None:
    source = spec.source()
    cols = source.c
    chosen = project_fields(spec, fields or list(spec.fields))
    conds = [
        *scope_conditions(spec, cols, scope),
        cols[spec.id_field] == coerce(spec, spec.id_field, ident),
    ]
    await set_timeout(session)
    row = (await session.execute(base_select(spec, source, chosen, conds))).mappings().first()
    return {f: _cell(row[f]) for f in chosen} if row else None
