"""Endpoint admin JSON (`/admin/*`, key `is_admin`) + unduhan export bertanda tangan."""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from mcp_marketing.core.errors import ToolError, validation_error_from
from mcp_marketing.server.auth import Authenticator
from mcp_marketing.server.services import Services
from mcp_marketing.services.clients import (
    client_summary,
    create_client,
    list_clients,
    revoke_client,
)
from mcp_marketing.utils import crypto

EXPORT_URL_TTL_S = 3_600


class CreateClientBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    toolsets: list[str] = Field(default_factory=list)
    kind: str = Field(default="external", pattern="^(external|agent)$")
    is_admin: bool = False
    workspace_id: uuid.UUID | None = None
    dashboard_agent_id: uuid.UUID | None = None
    rpm_limit: int = Field(default=120, ge=1, le=10_000)
    daily_token_budget: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None


def _signing_key(services: Services) -> str:
    return "export|" + services.settings.mcp_key_pepper.get_secret_value()


def sign_export(services: Services, filename: str, ttl_s: int = EXPORT_URL_TTL_S) -> str:
    exp = int(time.time()) + ttl_s
    sig = crypto.hmac_sha256_hex(_signing_key(services), f"{filename}|{exp}")
    base = services.settings.public_base_url.rstrip("/")
    return f"{base}/admin/exports/{filename}?exp={exp}&sig={sig}"


def _error(err: ToolError, status: int) -> JSONResponse:
    return JSONResponse({"error": err.to_structured()}, status_code=status)


def build_admin_routes(services: Services, authenticator: Authenticator) -> list[Route]:
    async def get_clients(request: Request) -> Response:
        async with services.db.session() as session:
            rows = await list_clients(session)
        return JSONResponse({"clients": [client_summary(c) for c in rows]})

    async def post_client(request: Request) -> Response:
        try:
            body = CreateClientBody.model_validate(await request.json())
        except ValidationError as exc:
            return _error(validation_error_from(exc), 422)
        try:
            async with services.db.session() as session:
                created = await create_client(
                    session,
                    pepper=services.settings.mcp_key_pepper.get_secret_value(),
                    name=body.name,
                    toolsets=body.toolsets or services.settings.default_toolset_list,
                    kind=body.kind,
                    is_admin=body.is_admin,
                    workspace_id=body.workspace_id,
                    dashboard_agent_id=body.dashboard_agent_id,
                    rpm_limit=body.rpm_limit,
                    daily_token_budget=body.daily_token_budget,
                    expires_at=body.expires_at,
                )
                summary = client_summary(created.client)
        except ToolError as err:
            return _error(err, 409 if err.code == "CONFLICT" else 422)
        return JSONResponse({"client": summary, "api_key": created.key}, status_code=201)

    async def post_revoke(request: Request) -> Response:
        try:
            async with services.db.session() as session:
                client = await revoke_client(session, request.path_params["client_id"])
                summary = client_summary(client)
        except ToolError as err:
            return _error(err, 404)
        authenticator.invalidate(summary["key_prefix"])
        return JSONResponse({"client": summary})

    async def get_export(request: Request) -> Response:
        filename = request.path_params["filename"]
        exp = request.query_params.get("exp", "")
        sig = request.query_params.get("sig", "")
        expected = crypto.hmac_sha256_hex(_signing_key(services), f"{filename}|{exp}")
        if (
            not (exp.isascii() and exp.isdigit())
            or int(exp) < time.time()
            or not crypto.constant_time_eq(sig, expected)
        ):
            return JSONResponse({"error": {"code": "FORBIDDEN"}}, status_code=403)
        export_dir = (services.settings.storage_dir / "exports").resolve()
        path = (export_dir / filename).resolve()
        if path.parent != export_dir or not path.is_file():
            return JSONResponse({"error": {"code": "NOT_FOUND"}}, status_code=404)
        return FileResponse(Path(path), filename=filename)

    return [
        Route("/admin/clients", get_clients, methods=["GET"]),
        Route("/admin/clients", post_client, methods=["POST"]),
        Route("/admin/clients/{client_id}/revoke", post_revoke, methods=["POST"]),
        Route("/admin/exports/{filename}", get_export, methods=["GET"]),
    ]


def health_payload(ok: bool, **details: Any) -> dict[str, Any]:
    return {"status": "ok" if ok else "degraded", **details}
