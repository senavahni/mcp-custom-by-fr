"""`db_sql_readonly`: validasi SQL (sqlglot) + eksekusi dalam transaksi READ ONLY ber-timeout.

Lapisan pertahanan: (1) parser menolak apa pun selain satu SELECT/UNION tanpa DML, lock, INTO,
fungsi berbahaya, atau tabel di luar schema `mcp`/`public`; (2) transaksi `READ ONLY` +
`statement_timeout`; (3) grant DB role `mcp_app` (tidak punya hak tulis ke `public`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlglot import exp
from sqlglot.errors import SqlglotError

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.services.db.query import STATEMENT_TIMEOUT, _cell

MAX_LIMIT = 1_000
MAX_SQL_CHARS = 10_000
ALLOWED_SCHEMAS = frozenset({"mcp", "public"})
DENIED_FUNC_PREFIXES = (
    "pg_read",
    "pg_ls_",
    "pg_stat_file",
    "pg_sleep",
    "pg_terminate",
    "pg_cancel",
    "pg_reload",
    "pg_rotate",
    "pg_file",
    "pg_logdir",
    "pg_advisory",
    "dblink",
    "lo_",
    "set_config",
    "query_to_xml",
    "cursor_to_xml",
    "table_to_xml",
    "database_to_xml",
    "schema_to_xml",
    "txid_",
    "nextval",
    "setval",
)
DENIED_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Into,
    exp.Lock,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
)
_PARAM_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")


@dataclass
class SqlResult:
    cols: list[str]
    rows: list[list[Any]]
    truncated: bool


def _reject(msg: str) -> ValidationFailed:
    return ValidationFailed(f"SQL ditolak: {msg}", details={"field": "sql"})


def _func_name(node: exp.Func) -> str:
    return (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()


def validate_sql(sql: str) -> str:
    """Kembalikan SQL yang sudah dibersihkan dari `;` penutup; lempar `ValidationFailed`."""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned or len(cleaned) > MAX_SQL_CHARS:
        raise _reject(f"kosong atau lebih dari {MAX_SQL_CHARS} karakter")
    try:
        statements = [s for s in sqlglot.parse(cleaned, read="postgres") if s is not None]
    except SqlglotError as exc:
        raise _reject(f"tidak dapat di-parse ({type(exc).__name__})") from None
    if len(statements) != 1:
        raise _reject("hanya satu statement")
    stmt = statements[0]
    if not isinstance(stmt, exp.Select | exp.SetOperation):
        raise _reject("hanya SELECT / WITH … SELECT")
    ctes = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}
    for node in stmt.walk():
        if isinstance(node, DENIED_NODES):
            raise _reject(f"mengandung {type(node).__name__.upper()}")
        if isinstance(node, exp.Func):
            name = _func_name(node)
            if name.startswith(DENIED_FUNC_PREFIXES):
                raise _reject(f"fungsi {name} tidak diizinkan")
        if isinstance(node, exp.Table) and node.name:
            schema = (node.db or "").lower()
            table = node.name.lower()
            if not schema and table in ctes:
                continue
            if schema and schema not in ALLOWED_SCHEMAS:
                raise _reject(f"schema {schema} tidak diizinkan")
            if not schema and table.startswith("pg_"):
                raise _reject(f"tabel sistem {table} tidak diizinkan")
    return cleaned


def _check_params(params: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if not _PARAM_RE.match(key):
            raise ValidationFailed(
                f"nama parameter tidak valid: {key}", details={"field": "params"}
            )
        if not isinstance(value, str | int | float | bool) and value is not None:
            raise ValidationFailed(f"parameter {key}: hanya skalar", details={"field": "params"})
        out[key] = value
    return out


async def execute_readonly(
    session: AsyncSession, sql: str, params: dict[str, Any] | None, limit: int
) -> SqlResult:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValidationFailed(f"limit 1–{MAX_LIMIT}", details={"field": "limit"})
    cleaned = validate_sql(sql)
    bound = _check_params(params)
    # baris baru sebelum ")" agar komentar `--` di akhir SQL tidak memakan kurung penutup
    wrapped = f"SELECT * FROM (\n{cleaned}\n) AS _q LIMIT {limit + 1}"
    await session.execute(text("SET TRANSACTION READ ONLY"))
    await session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
    result = await session.execute(text(wrapped), bound)
    cols = list(result.keys())
    raw = result.all()
    return SqlResult(
        cols=cols,
        rows=[[_cell(v) for v in r] for r in raw[:limit]],
        truncated=len(raw) > limit,
    )
