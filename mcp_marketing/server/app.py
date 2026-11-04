"""Rakit app ASGI: /mcp (Streamable HTTP) + /health/* + /metrics + /admin/* + /console."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable

from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from mcp_marketing.core.logging import get_logger
from mcp_marketing.server.admin import build_admin_routes, health_payload
from mcp_marketing.server.admin_console import build_console_admin_routes
from mcp_marketing.server.auth import Authenticator
from mcp_marketing.server.console import build_console_routes
from mcp_marketing.server.mcp_server import build_mcp_server
from mcp_marketing.server.middleware import (
    AuthMiddleware,
    BodyLimitMiddleware,
    RequestContextMiddleware,
)
from mcp_marketing.server.pipeline import ToolPipeline
from mcp_marketing.server.services import Services
from mcp_marketing.tools import load_all

log = get_logger(__name__)


def build_asgi_app(
    services: Services,
    *,
    lifespan_extra: Callable[[], contextlib.AbstractAsyncContextManager[None]] | None = None,
) -> tuple[ASGIApp, ToolPipeline, Authenticator]:
    load_all()
    authenticator = Authenticator(services.settings, services.db)
    pipeline = ToolPipeline(services, authenticator)
    services.extras["pipeline"] = pipeline
    server = build_mcp_server(pipeline)
    manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)

    async def live(request: Request) -> Response:
        return JSONResponse(health_payload(True))

    async def ready(request: Request) -> Response:
        try:
            db = await services.db.health()
        except Exception as exc:  # noqa: BLE001 - laporkan sebagai tidak siap
            return JSONResponse(health_payload(False, db=False, error=type(exc).__name__), 503)
        return JSONResponse(health_payload(True, **db))

    async def metrics(request: Request) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            if lifespan_extra is None:
                yield
            else:
                async with lifespan_extra():
                    yield
        await pipeline.drain()

    app = Starlette(
        routes=[
            Route("/health/live", live, methods=["GET"]),
            Route("/health/ready", ready, methods=["GET"]),
            Route("/metrics", metrics, methods=["GET"]),
            *build_admin_routes(services, authenticator),
            *build_console_admin_routes(services, authenticator),
            *build_console_routes(),
            Mount("/mcp", app=StreamableHTTPASGIApp(manager)),
        ],
        lifespan=lifespan,
    )
    wrapped: ASGIApp = AuthMiddleware(app, authenticator)
    wrapped = BodyLimitMiddleware(wrapped)
    wrapped = RequestContextMiddleware(wrapped)
    return wrapped, pipeline, authenticator
