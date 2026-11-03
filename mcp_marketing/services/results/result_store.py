"""Result store: data besar disimpan sebagai handle (`h_…`) dan dibaca bertahap (MCP_SPEC §3 T2–T4).

Baris disimpan di `mcp.result_items.data` (jsonb, kolom terproyeksi). Paginasi keyset
`(data->'col', idx)` dengan cursor base64; agregasi dilakukan di SQL.
"""

from __future__ import annotations

import base64
import csv
import io
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import orjson
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_marketing.core.database import Database
from mcp_marketing.core.errors import NotFound, ValidationFailed
from mcp_marketing.models import ResultItem, ResultSet
from mcp_marketing.utils.ids import new_handle
from mcp_marketing.utils.text import truncate
from mcp_marketing.utils.time import now

BATCH = 1_000
FILTER_OPS = frozenset(
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
TIME_BUCKETS = ("hour", "day", "week", "month")
# tanpa ':' (text() SQLAlchemy menganggap ':MI' sebagai bind parameter)
_BUCKET_FMT = {
    "hour": 'YYYY-MM-DD"T"HH24"h"',
    "day": "YYYY-MM-DD",
    "week": "YYYY-MM-DD",
    "month": "YYYY-MM",
}
SIMPLE_METRICS = ("sum", "avg", "min", "max", "p50", "p90")
MAX_GROUP_BY = 3


@dataclass(frozen=True)
class Owner:
    client_id: str | None
    run_id: str | None = None
    is_admin: bool = False


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


def _to_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _to_num(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    return None


def encode_cursor(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(orjson.dumps(payload)).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = orjson.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except ValueError:
        raise ValidationFailed("cursor tidak valid", details={"field": "cursor"}) from None


class ResultStore:
    def __init__(
        self,
        db: Database,
        *,
        ttl_days: int = 30,
        timezone: str = "Asia/Jakarta",
        export_dir: Path | None = None,
    ) -> None:
        self.db = db
        self.ttl_days = ttl_days
        self.timezone = timezone
        self.export_dir = export_dir

    # ------------------------------------------------------------------ create
    async def create(
        self,
        *,
        kind: str,
        columns: list[str],
        rows: Iterable[dict[str, Any]],
        stats: dict[str, Any] | None = None,
        source_ref: dict[str, Any] | None = None,
        owner: Owner,
        job_id: str | None = None,
        sort_ts: str | None = None,
        sort_num: str | None = None,
        ref_id: str | None = None,
        ttl_days: int | None = None,
    ) -> ResultSet:
        handle = new_handle()
        expires = now() + timedelta(days=ttl_days or self.ttl_days)
        async with self.db.session() as session:
            rs = ResultSet(
                handle=handle,
                kind=kind,
                client_id=uuid.UUID(owner.client_id) if owner.client_id else None,
                run_id=uuid.UUID(owner.run_id) if owner.run_id else None,
                job_id=job_id,
                columns=list(columns),
                total=0,
                stats=_json_safe(stats or {}),
                source_ref=_json_safe(source_ref or {}),
                expires_at=expires,
            )
            session.add(rs)
            await session.flush()
            total = 0
            batch: list[dict[str, Any]] = []
            for row in rows:
                batch.append(
                    {
                        "handle": handle,
                        "idx": total,
                        "ref_id": row.get(ref_id) if ref_id else None,
                        "data": _json_safe({c: row.get(c) for c in columns}),
                        "sort_ts": _to_ts(row.get(sort_ts)) if sort_ts else None,
                        "sort_num": _to_num(row.get(sort_num)) if sort_num else None,
                    }
                )
                total += 1
                if len(batch) >= BATCH:
                    await session.execute(insert(ResultItem), batch)
                    batch = []
            if batch:
                await session.execute(insert(ResultItem), batch)
            rs.total = total
        return rs

    # ------------------------------------------------------------------ read
    async def get_set(self, session: AsyncSession, handle: str, owner: Owner) -> ResultSet:
        rs = await session.get(ResultSet, handle)
        if rs is None or rs.expires_at <= now() or not self._can_read(rs, owner):
            # tidak membedakan "tidak ada" dan "bukan milikmu" agar handle tidak bisa ditebak
            raise NotFound(f"Handle {handle} tidak ditemukan atau kedaluwarsa")
        return rs

    @staticmethod
    def _can_read(rs: ResultSet, owner: Owner) -> bool:
        if owner.is_admin:
            return True
        if owner.client_id and rs.client_id and str(rs.client_id) == owner.client_id:
            return True
        return bool(owner.run_id and rs.run_id and str(rs.run_id) == owner.run_id)

    @staticmethod
    def _check_fields(fields: Iterable[str], columns: list[str], param: str) -> None:
        unknown = [f for f in fields if f not in columns]
        if unknown:
            raise ValidationFailed(
                f"{param} tidak dikenal: {', '.join(unknown)}",
                details={"field": param, "allowed": columns},
            )

    def _filter_sql(
        self, flt: dict[str, Any], columns: list[str], params: dict[str, Any]
    ) -> list[str]:
        self._check_fields(flt.keys(), columns, "filter")
        clauses: list[str] = []
        for i, (field, cond) in enumerate(flt.items()):
            if not isinstance(cond, dict):
                cond = {"op": "eq", "value": cond}
            op = cond.get("op", "eq")
            value = cond.get("value")
            if op not in FILTER_OPS:
                raise ValidationFailed(
                    f"operator filter tidak dikenal: {op}", details={"field": "filter"}
                )
            fk, pk = f"f{i}", f"v{i}"
            params[fk] = field
            txt = f"(data->>:{fk})"
            num = (
                f"(CASE WHEN jsonb_typeof(data->:{fk}) = 'number' THEN (data->>:{fk})::numeric END)"
            )
            numeric = isinstance(value, int | float) and not isinstance(value, bool)
            col = num if numeric else txt
            sql_ops = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
            if op in sql_ops:
                params[pk] = (
                    value
                    if numeric
                    else (
                        None
                        if value is None
                        else str(value).lower()
                        if isinstance(value, bool)
                        else str(value)
                    )
                )
                clauses.append(f"{col} {sql_ops[op]} :{pk}")
            elif op == "in":
                if not isinstance(value, list) or not value:
                    raise ValidationFailed("filter 'in' butuh list", details={"field": "filter"})
                params[pk] = [str(v) for v in value]
                clauses.append(f"{txt} = ANY(:{pk})")
            elif op in ("contains", "icontains", "starts_with"):
                params[pk] = str(value)
                like = "ILIKE" if op == "icontains" else "LIKE"
                pattern = f"'%' || :{pk} || '%'" if op != "starts_with" else f":{pk} || '%'"
                # untuk array (mis. hashtags) contains = elemen ada di array
                clauses.append(
                    f"(CASE WHEN jsonb_typeof(data->:{fk}) = 'array' "
                    f"THEN (data->:{fk}) ? :{pk} ELSE {txt} {like} {pattern} END)"
                )
            elif op == "is_null":
                clauses.append(
                    f"(data->:{fk} IS NULL OR data->:{fk} = 'null'::jsonb)"
                    if value in (None, True)
                    else f"(data->:{fk} IS NOT NULL AND data->:{fk} <> 'null'::jsonb)"
                )
            elif op == "between":
                if not isinstance(value, list) or len(value) != 2:
                    raise ValidationFailed(
                        "filter 'between' butuh [a, b]", details={"field": "filter"}
                    )
                lo, hi = value
                both_num = all(
                    isinstance(v, int | float) and not isinstance(v, bool) for v in value
                )
                c = num if both_num else txt
                params[f"{pk}a"], params[f"{pk}b"] = (lo, hi) if both_num else (str(lo), str(hi))
                clauses.append(f"{c} BETWEEN :{pk}a AND :{pk}b")
        return clauses

    async def page(
        self,
        handle: str,
        owner: Owner,
        *,
        fields: list[str] | None = None,
        flt: dict[str, Any] | None = None,
        order_by: str | None = None,
        limit: int = 10,
        cursor: str | None = None,
        text_max_chars: int = 280,
    ) -> dict[str, Any]:
        async with self.db.session() as session:
            rs = await self.get_set(session, handle, owner)
            columns = list(rs.columns)
            cols = fields or columns
            self._check_fields(cols, columns, "fields")
            params: dict[str, Any] = {"h": handle, "lim": limit + 1}
            where = ["handle = :h"]
            where += self._filter_sql(flt or {}, columns, params)
            desc = bool(order_by and order_by.startswith("-"))
            key_col = order_by.lstrip("-+") if order_by else None
            if key_col:
                self._check_fields([key_col], columns, "order_by")
                params["ok"] = key_col
                key_expr = "COALESCE(data->:ok, 'null'::jsonb)"
            else:
                key_expr = "to_jsonb(idx)"
            if cursor:
                cur = decode_cursor(cursor)
                if cur.get("o") != (order_by or ""):
                    raise ValidationFailed("cursor milik urutan lain", details={"field": "cursor"})
                params["ck"] = orjson.dumps(cur.get("k")).decode()
                params["ci"] = int(cur.get("i", -1))
                cmp = "<" if desc else ">"
                where.append(f"({key_expr}, idx) {cmp} (CAST(:ck AS jsonb), :ci)")
            direction = "DESC" if desc else "ASC"
            sql = (
                f"SELECT idx, data, {key_expr} AS k FROM mcp.result_items "
                f"WHERE {' AND '.join(where)} ORDER BY {key_expr} {direction}, idx {direction} "
                "LIMIT :lim"
            )
            rows = (await session.execute(text(sql), params)).all()
            filtered_total = rs.total
            if flt:
                count_where = " AND ".join(where[: 1 + len(flt)])
                count_sql = f"SELECT count(*) FROM mcp.result_items WHERE {count_where}"
                count_params = {k: v for k, v in params.items() if k not in ("ck", "ci", "lim")}
                filtered_total = int(
                    (await session.execute(text(count_sql), count_params)).scalar() or 0
                )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            encode_cursor({"o": order_by or "", "k": rows[-1].k, "i": rows[-1].idx})
            if has_more and rows
            else None
        )
        table_rows = [
            [
                truncate(v, text_max_chars) if isinstance(v, str) else v
                for v in (r.data.get(c) for c in cols)
            ]
            for r in rows
        ]
        return {
            "summary": f"{len(table_rows)} dari {filtered_total} baris handle {handle}",
            "handle": None,
            "total": filtered_total,
            "stats": {},
            "items": {"cols": cols, "rows": table_rows},
            "next_cursor": next_cursor,
            "truncated": False,
        }

    # ------------------------------------------------------------------ aggregate
    def _group_expr(
        self, g: str, columns: list[str], time_bucket: str | None, i: int, params: dict[str, Any]
    ) -> str:
        if g in TIME_BUCKETS or g == "time":
            bucket = time_bucket or (g if g in TIME_BUCKETS else "day")
            params["tz"] = self.timezone
            fmt = _BUCKET_FMT[bucket]
            return f"to_char(date_trunc('{bucket}', sort_ts AT TIME ZONE :tz), '{fmt}')"
        self._check_fields([g], columns, "group_by")
        params[f"g{i}"] = g
        if time_bucket:
            # kolom waktu eksplisit dikelompokkan per bucket bila berisi timestamp
            params["tz"] = self.timezone
            return (
                f"(CASE WHEN (data->>:g{i}) ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' "
                f"THEN to_char(date_trunc("
                f"'{time_bucket}', ((data->>:g{i})::timestamptz) AT TIME ZONE :tz), "
                f"'{_BUCKET_FMT[time_bucket]}') ELSE data->>:g{i} END)"
            )
        return f"(data->>:g{i})"

    def _metric_expr(self, metric: str, columns: list[str], i: int, params: dict[str, Any]) -> str:
        if metric == "count":
            return "count(*)"
        kind, _, col = metric.partition(":")
        if kind not in SIMPLE_METRICS or not col:
            raise ValidationFailed(f"metrik tidak dikenal: {metric}", details={"field": "metrics"})
        self._check_fields([col], columns, "metrics")
        params[f"m{i}"] = col
        num = f"(CASE WHEN jsonb_typeof(data->:m{i}) = 'number' THEN (data->>:m{i})::numeric END)"
        if kind in ("p50", "p90"):
            q = 0.5 if kind == "p50" else 0.9
            return f"percentile_cont({q}) WITHIN GROUP (ORDER BY {num})"
        return f"{kind}({num})"

    async def aggregate(
        self,
        handle: str,
        owner: Owner,
        *,
        group_by: list[str],
        metrics: list[str],
        time_bucket: Literal["hour", "day", "week", "month"] | None = None,
        limit: int = 50,
        flt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(group_by) > MAX_GROUP_BY:
            raise ValidationFailed("group_by maksimal 3", details={"field": "group_by"})
        async with self.db.session() as session:
            rs = await self.get_set(session, handle, owner)
            columns = list(rs.columns)
            params: dict[str, Any] = {"h": handle, "lim": limit}
            where = ["handle = :h", *self._filter_sql(flt or {}, columns, params)]
            tops = [m for m in metrics if m.startswith("top:")]
            if tops:
                return await self._top(session, rs, tops, group_by, where, params)
            gexprs = [
                self._group_expr(g, columns, time_bucket, i, params) for i, g in enumerate(group_by)
            ]
            mexprs = [
                self._metric_expr(m, columns, i, params) for i, m in enumerate(metrics or ["count"])
            ]
            select_cols = [
                *(f"{e} AS g{i}" for i, e in enumerate(gexprs)),
                *(f"{e} AS m{i}" for i, e in enumerate(mexprs)),
            ]
            sql = (
                f"SELECT {', '.join(select_cols)} FROM mcp.result_items WHERE {' AND '.join(where)}"
            )
            if gexprs:
                order = ", ".join(f"g{i}" for i in range(len(gexprs)))
                sql += f" GROUP BY {order} ORDER BY {order}"
            sql += " LIMIT :lim"
            rows = (await session.execute(text(sql), params)).all()
        cols = [*group_by, *(metrics or ["count"])]
        out_rows = [[_num(v) for v in row] for row in rows]
        return {
            "summary": f"{len(out_rows)} grup dari {rs.total} baris handle {handle}",
            "cols": cols,
            "rows": out_rows,
        }

    async def _top(
        self,
        session: AsyncSession,
        rs: ResultSet,
        tops: list[str],
        group_by: list[str],
        where: list[str],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if group_by or len(tops) != 1:
            raise ValidationFailed(
                "top:x:n hanya satu metrik dan tanpa group_by", details={"field": "metrics"}
            )
        parts = tops[0].split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            raise ValidationFailed(
                "format metrik top harus top:<kolom>:<n>", details={"field": "metrics"}
            )
        col, n = parts[1], min(int(parts[2]), 50)
        self._check_fields([col], list(rs.columns), "metrics")
        params.update(tc=col, tn=n)
        sql = (
            "SELECT v, count(*) AS c FROM ("
            "  SELECT jsonb_array_elements_text(data->:tc) AS v FROM mcp.result_items "
            f"  WHERE {' AND '.join(where)} AND jsonb_typeof(data->:tc) = 'array'"
            "  UNION ALL "
            "  SELECT data->>:tc AS v FROM mcp.result_items "
            f"  WHERE {' AND '.join(where)} AND jsonb_typeof(data->:tc) NOT IN ('array', 'null')"
            ") t GROUP BY v ORDER BY c DESC, v LIMIT :tn"
        )
        rows = (await session.execute(text(sql), params)).all()
        return {
            "summary": f"top {len(rows)} nilai {col} dari {rs.total} baris",
            "cols": [col, "count"],
            "rows": [[r.v, int(r.c)] for r in rows],
        }

    # ------------------------------------------------------------------ export
    async def export(
        self, handle: str, owner: Owner, fmt: Literal["csv", "jsonl", "xlsx"]
    ) -> tuple[Path, int]:
        if self.export_dir is None:
            raise ValidationFailed("export tidak dikonfigurasi")
        self.export_dir.mkdir(parents=True, exist_ok=True)
        async with self.db.session() as session:
            rs = await self.get_set(session, handle, owner)
            columns = list(rs.columns)
            result = await session.stream(
                select(ResultItem.data).where(ResultItem.handle == handle).order_by(ResultItem.idx)
            )
            rows = [r[0] async for r in result]
        stamp = now().strftime("%Y%m%d%H%M%S")
        path = self.export_dir / f"{handle}-{stamp}.{fmt}"
        if fmt == "jsonl":
            path.write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in rows))
        elif fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(columns)
            for r in rows:
                writer.writerow([_cell(r.get(c)) for c in columns])
            path.write_text(buf.getvalue(), encoding="utf-8-sig")
        else:
            from openpyxl import Workbook

            wb = Workbook(write_only=True)
            ws = wb.create_sheet(handle)
            ws.append(columns)
            for r in rows:
                ws.append([_cell(r.get(c)) for c in columns])
            wb.save(path)
        return path, len(rows)


def _cell(value: Any) -> Any:
    if isinstance(value, list | dict):
        return orjson.dumps(value).decode()
    return value


def _num(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    return value
