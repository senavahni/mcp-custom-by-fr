"""Alembic env (async). Hanya mengelola objek di schema `mcp`; schema `public` milik Prisma."""

import asyncio
from typing import Any

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from mcp_marketing.models import Base

config = context.config
target_metadata = Base.metadata
SCHEMA = "mcp"


def _url() -> str:
    # urutan: Config.attributes["url"] (test/manage.py) -> -x url=... -> MIGRATION_DATABASE_URL
    url = config.attributes.get("url") or context.get_x_argument(as_dictionary=True).get("url")
    if url:
        return str(url)
    from mcp_marketing.core.config import get_settings

    return get_settings().db.migration_database_url


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    # tabel public (Prisma) tidak pernah di-autogenerate atau di-drop
    if type_ == "table":
        return getattr(obj, "schema", None) == SCHEMA
    table = getattr(obj, "table", None)
    return table is None or getattr(table, "schema", None) == SCHEMA


def _configure(connection: Connection | None = None, **kw: Any) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
        include_object=include_object,
        transaction_per_migration=True,
        compare_type=True,
        **kw,
    )


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    exists = connection.execute(
        text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": SCHEMA}
    ).scalar()
    if not exists:
        connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    # akhiri transaksi autobegin; bila tidak, Alembic menganggap transaksi milik pemanggil dan
    # tidak pernah meng-commit migrasi
    connection.commit()
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
