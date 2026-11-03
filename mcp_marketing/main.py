"""Aplikasi ASGI proses `server`: /mcp + health + metrics + admin, agent runtime, outbox sender.

uvicorn mcp_marketing.main:app --host 127.0.0.1 --port 4100
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx

from mcp_marketing.core.config import Settings, get_settings
from mcp_marketing.core.database import Database, set_database
from mcp_marketing.core.logging import configure_logging, get_logger
from mcp_marketing.server.app import build_asgi_app
from mcp_marketing.server.services import Services
from mcp_marketing.services.agent.budget_store import db_budget_manager
from mcp_marketing.services.agent.runtime import AgentRuntime
from mcp_marketing.services.dashboard.agent_api import AgentApiClient
from mcp_marketing.services.dashboard.outbox import Outbox
from mcp_marketing.services.llm.gateway_client import GatewayClient
from mcp_marketing.services.results.cache import ResultCache
from mcp_marketing.services.results.result_store import ResultStore
from mcp_marketing.worker.queue import JobQueue

log = get_logger(__name__)


def build_services(settings: Settings) -> Services:
    db = Database(settings.db.database_url, pool_size=settings.db.db_pool_size)
    http = httpx.AsyncClient(timeout=30)
    cache = ResultCache(db)
    services = Services(
        settings=settings,
        db=db,
        http=http,
        results=ResultStore(
            db,
            ttl_days=settings.token.result_ttl_days,
            timezone=settings.timezone,
            export_dir=settings.storage_dir / "exports",
        ),
        cache=cache,
        budgets=db_budget_manager(db, settings.timezone),
    )
    services.llm = GatewayClient(settings, db=db, cache=cache)
    services.agent_api = AgentApiClient(settings, http)
    services.outbox = Outbox(db, http, settings)
    services.jobs = JobQueue(db, default_timeout_s=settings.worker.job_default_timeout_s)
    return services


async def close_services(services: Services) -> None:
    if services.llm is not None:
        await services.llm.close()
    await services.http.aclose()
    await services.db.dispose()
    set_database(None)


def create_app(settings: Settings | None = None) -> Any:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_dir)
    services = build_services(settings)
    set_database(services.db)
    background: list[asyncio.Task[None]] = []

    @contextlib.asynccontextmanager
    async def lifespan_extra() -> AsyncIterator[None]:
        pipeline = services.extras["pipeline"]
        runtime = AgentRuntime(services, pipeline)
        services.runtime = runtime
        assert services.outbox is not None
        background.append(asyncio.create_task(services.outbox.run_sender(), name="outbox-sender"))
        background.append(asyncio.create_task(runtime.run_supervisor(), name="run-supervisor"))
        log.info(
            "server.started", host=settings.app_host, port=settings.app_port, env=settings.app_env
        )
        try:
            yield
        finally:
            await runtime.shutdown()
            services.outbox.stop()
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await close_services(services)
            log.info("server.stopped")

    app, _, _ = build_asgi_app(services, lifespan_extra=lifespan_extra)
    return app


def __getattr__(name: str) -> Any:
    # `uvicorn mcp_marketing.main:app` -> app dibangun saat diakses (konfigurasi divalidasi)
    if name == "app":
        return create_app()
    raise AttributeError(name)
