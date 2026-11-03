"""Engine & session async (role `mcp_app`, search_path mcp,public)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

APP_NAME = "mcp-marketing"


class Database:
    def __init__(self, url: str, *, pool_size: int = 10, app_name: str = APP_NAME) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=pool_size,
            pool_pre_ping=True,
            connect_args={
                "server_settings": {"application_name": app_name, "search_path": "mcp,public"}
            },
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Session dengan commit otomatis di akhir blok, rollback bila error."""
        async with self.sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def health(self) -> dict[str, Any]:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            vector = (
                await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
            ).scalar() is not None
        return {"db": True, "vector": vector}

    async def dispose(self) -> None:
        await self.engine.dispose()


_db: Database | None = None


def set_database(db: Database | None) -> None:
    global _db
    _db = db


def get_database() -> Database:
    if _db is None:
        raise RuntimeError("Database belum diinisialisasi (lifespan server/worker)")
    return _db


def asyncpg_dsn(url: str) -> str:
    """URL SQLAlchemy -> DSN asyncpg murni (untuk LISTEN/NOTIFY)."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)
