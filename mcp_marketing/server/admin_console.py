"""Endpoint admin tambahan untuk Admin Console (`/admin/*`, key klien `is_admin`).

Kredensial yang dikelola: klien MCP (`mcpk_`, rotasi) dan key LLM Gateway per agent (impor,
nonaktifkan, provisioning lewat Admin API Gateway). Respons tidak pernah memuat key lengkap kecuali
key baru yang memang harus ditampilkan sekali. Admin key Gateway untuk provisioning hanya dipakai
selama request berjalan, tidak disimpan maupun dicatat di log.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_marketing.core.errors import (
    Conflict,
    NotFound,
    ToolError,
    UpstreamError,
    ValidationFailed,
    validation_error_from,
)
from mcp_marketing.models import AgentProfile, LlmCredential
from mcp_marketing.server.auth import Authenticator
from mcp_marketing.server.services import Services
from mcp_marketing.services.agent.profile_admin import (
    AGENT_TOOLSETS,
    AgentCreate,
    AgentUpdate,
    create_profile,
    list_profiles,
    update_profile,
)
from mcp_marketing.services.clients import client_summary, rotate_client
from mcp_marketing.services.env_status import env_vars, gateway_key_state, secret_key_state
from mcp_marketing.services.llm.credentials import (
    SYSTEM,
    credential_name,
    key_info,
    list_credentials,
    save_credential,
)
from mcp_marketing.services.llm.gateway_admin import (
    DEFAULT_PROJECT,
    GatewayAdmin,
    admin_base_url,
    provision,
)
from mcp_marketing.tools import TOOL_MODULES

STATUS_BY_ERROR = {"NOT_FOUND": 404, "CONFLICT": 409, "UPSTREAM_ERROR": 502}


class AgentBody(BaseModel):
    agent: str = Field(min_length=1, max_length=100)


class ImportBody(AgentBody):
    key: str = Field(min_length=1, max_length=200)


class ProvisionBody(BaseModel):
    admin_key: str = Field(min_length=1, max_length=200)
    agents: list[str] | None = None
    daily_usd: float | None = Field(default=None, ge=0)
    monthly_usd: float | None = Field(default=None, ge=0)
    rpm: int | None = Field(default=None, ge=1, le=10_000)
    force: bool = False


def _error(err: ToolError) -> JSONResponse:
    return JSONResponse(
        {"error": err.to_structured()}, status_code=STATUS_BY_ERROR.get(err.code, 422)
    )


def credential_dto(c: LlmCredential) -> dict[str, Any]:
    return {
        "name": c.name,
        "agent_key": c.agent_key,
        "key_prefix": c.key_prefix,
        "status": c.status,
        "last_error": c.last_error,
        "last_checked_at": c.last_checked_at.isoformat() if c.last_checked_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "rotated_at": c.rotated_at.isoformat() if c.rotated_at else None,
    }


def build_console_admin_routes(services: Services, authenticator: Authenticator) -> list[Route]:
    def secret_key() -> str:
        key = services.settings.mcp_secret_key
        if key is None:
            raise Conflict(
                "MCP_SECRET_KEY belum diisi di .env MCP. Buat nilainya di menu File .env, "
                "tempel ke .env, lalu restart MCP."
            )
        return key.get_secret_value()

    def gateway_admin_url() -> str:
        return admin_base_url(services.settings.llm.llm_gateway_url)

    def invalidate_credentials() -> None:
        store = getattr(services.llm, "credentials", None)
        if store is not None:
            store.invalidate()

    async def agent_keys() -> list[dict[str, Any]]:
        async with services.db.session() as session:
            rows = (
                await session.scalars(select(AgentProfile).order_by(AgentProfile.agent_key))
            ).all()
        return [
            {"agent_key": r.agent_key, "display_name": r.display_name, "is_active": r.is_active}
            for r in rows
        ]

    async def resolve_agent(raw: str) -> str | None:
        if raw == SYSTEM:
            return None
        if raw not in {a["agent_key"] for a in await agent_keys()}:
            raise ValidationFailed(f"Agent tidak dikenal: {raw}")
        return raw

    async def parse(request: Request, model: type[BaseModel]) -> Any:
        try:
            return model.model_validate(await request.json())
        except ValidationError as exc:
            raise validation_error_from(exc) from None
        except ValueError:
            raise ValidationFailed("Body harus JSON") from None

    async def get_meta(request: Request) -> Response:
        return JSONResponse(
            {
                "toolsets": list(TOOL_MODULES),
                "default_toolsets": services.settings.default_toolset_list,
                "agents": await agent_keys(),
                "gateway_admin_url": gateway_admin_url(),
                "gateway_project": DEFAULT_PROJECT,
            }
        )

    async def get_env(request: Request) -> Response:
        return JSONResponse({"vars": env_vars(services.settings)})

    async def post_rotate(request: Request) -> Response:
        try:
            async with services.db.session() as session:
                created = await rotate_client(
                    session,
                    request.path_params["client_id"],
                    pepper=services.settings.mcp_key_pepper.get_secret_value(),
                )
                summary = client_summary(created.client)
        except ToolError as err:
            return _error(err)
        authenticator.invalidate()  # prefix lama ikut hilang dari cache
        return JSONResponse({"client": summary, "api_key": created.key})

    async def get_credentials(request: Request) -> Response:
        enabled = services.settings.mcp_secret_key is not None
        async with services.db.session() as session:
            rows = await list_credentials(session)
        by_name = {c.name: c for c in rows}
        agents = await agent_keys()
        effective = []
        for a in agents:
            source, cred = key_info(by_name, a["agent_key"], enabled=enabled)
            used = (
                by_name.get(SYSTEM) if source == "system" else cred if source == "agent" else None
            )
            effective.append(
                {**a, "source": source, "key_prefix": used.key_prefix if used else None}
            )
        return JSONResponse(
            {
                "enabled": enabled,
                "secret_key_state": secret_key_state(services.settings.mcp_secret_key),
                "env_key_state": gateway_key_state(services.settings.llm.llm_gateway_api_key),
                "credentials": [credential_dto(c) for c in rows],
                "effective": effective,
            }
        )

    async def post_import(request: Request) -> Response:
        try:
            body = await parse(request, ImportBody)
            agent = await resolve_agent(body.agent)
            secret = secret_key()
            async with services.db.session() as session:
                row = await save_credential(
                    session, secret, agent_key=agent, full_key=body.key, gateway_key_id=None
                )
                dto = credential_dto(row)
        except ToolError as err:
            return _error(err)
        invalidate_credentials()
        return JSONResponse({"credential": dto})

    async def post_disable(request: Request) -> Response:
        try:
            body = await parse(request, AgentBody)
            agent = await resolve_agent(body.agent)
            async with services.db.session() as session:
                res = await session.execute(
                    update(LlmCredential)
                    .where(LlmCredential.name == credential_name(agent))
                    .values(status="revoked", last_error="dinonaktifkan dari Admin Console")
                    .returning(LlmCredential.id)
                )
                if res.first() is None:
                    raise NotFound(f"Belum ada key untuk {body.agent}")
        except ToolError as err:
            return _error(err)
        invalidate_credentials()
        return JSONResponse({"disabled": credential_name(agent)})

    async def post_provision(request: Request) -> Response:
        try:
            body = await parse(request, ProvisionBody)
            secret = secret_key()
            known = [a["agent_key"] for a in await agent_keys() if a["is_active"]]
            agents = body.agents if body.agents is not None else known
            unknown = sorted(set(agents) - set(known))
            if unknown:
                raise ValidationFailed(f"Agent tidak dikenal/nonaktif: {', '.join(unknown)}")
            admin = GatewayAdmin(services.http, gateway_admin_url(), body.admin_key.strip())
            report = await provision(
                services.db,
                secret,
                admin,
                agents=agents,
                rpm=body.rpm,
                force=body.force,
                daily_usd=body.daily_usd,
                monthly_usd=body.monthly_usd,
            )
        except httpx.HTTPError:
            return _error(
                UpstreamError(
                    f"LLM Gateway tidak bisa dihubungi di {gateway_admin_url()}"
                    " — pastikan Gateway berjalan.",
                    retryable=True,
                )
            )
        except UpstreamError as err:
            status = (err.details or {}).get("status")
            if status in (401, 403):
                err = UpstreamError(
                    "Admin key Gateway ditolak (bukan admin / sudah dicabut).", retryable=False
                )
            return _error(err)
        except ToolError as err:
            return _error(err)
        invalidate_credentials()
        return JSONResponse({"report": report})

    async def get_llm_models(request: Request) -> Response:
        """Pilihan route/model untuk form agent, dari Gateway (sesuai izin key agent/system)."""
        try:
            agent = await resolve_agent(request.query_params.get("agent") or SYSTEM)
            if services.llm is None:
                raise UpstreamError("Klien LLM Gateway belum aktif di MCP", retryable=True)
            models = await services.llm.model_catalog(agent)
        except ToolError as err:  # termasuk UpstreamError (Gateway mati / key ditolak)
            return _error(err)
        return JSONResponse({"models": models, "agent": agent})

    async def get_agents(request: Request) -> Response:
        async with services.db.session() as session:
            agents = await list_profiles(session)
        return JSONResponse({"agents": agents, "toolsets": list(AGENT_TOOLSETS)})

    async def post_agent(request: Request) -> Response:
        try:
            body = await parse(request, AgentCreate)
            async with services.db.session() as session:
                agent = await create_profile(session, body)
        except ToolError as err:
            return _error(err)
        return JSONResponse({"agent": agent}, status_code=201)

    async def patch_agent(request: Request) -> Response:
        try:
            body = await parse(request, AgentUpdate)
            async with services.db.session() as session:
                agent = await update_profile(session, request.path_params["agent_key"], body)
        except ToolError as err:
            return _error(err)
        return JSONResponse({"agent": agent})

    return [
        Route("/admin/llm-models", get_llm_models, methods=["GET"]),
        Route("/admin/agents", get_agents, methods=["GET"]),
        Route("/admin/agents", post_agent, methods=["POST"]),
        Route("/admin/agents/{agent_key}", patch_agent, methods=["PATCH"]),
        Route("/admin/meta", get_meta, methods=["GET"]),
        Route("/admin/env-status", get_env, methods=["GET"]),
        Route("/admin/clients/{client_id}/rotate", post_rotate, methods=["POST"]),
        Route("/admin/llm-credentials", get_credentials, methods=["GET"]),
        Route("/admin/llm-credentials/import", post_import, methods=["POST"]),
        Route("/admin/llm-credentials/disable", post_disable, methods=["POST"]),
        Route("/admin/llm-credentials/provision", post_provision, methods=["POST"]),
    ]
