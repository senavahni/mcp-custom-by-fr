"""Middleware ASGI murni: request id, batas body, autentikasi Bearer."""

from __future__ import annotations

import re
import uuid
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_marketing.core import context
from mcp_marketing.server.auth import Authenticator

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
MAX_BODY_BYTES = 2 * 1024 * 1024
PROTECTED_PREFIXES = ("/mcp", "/admin", "/uploads")
SIGNED_PREFIXES = ("/admin/exports/",)
# upload file besar (MCP-V1-11): batas ukuran ditegakkan route sendiri per jenis file
BODY_LIMIT_EXEMPT = ("/uploads",)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")  # type: ignore[no-any-return]
    return None


def _state(scope: Scope) -> dict[str, Any]:
    return scope.setdefault("state", {})  # type: ignore[no-any-return]


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = _header(scope, b"x-request-id")
        request_id = incoming if incoming and REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        _state(scope)["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        with context.context_scope(request_id=request_id):
            await self.app(scope, receive, send_with_id)


class BodyLimitError(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "") in BODY_LIMIT_EXEMPT:
            await self.app(scope, receive, send)
            return
        length = _header(scope, b"content-length")
        if length and length.isdigit() and int(length) > self.max_bytes:
            await _too_large(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyLimitError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except BodyLimitError:
            await _too_large(scope, receive, send)


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse({"error": {"code": "PAYLOAD_TOO_LARGE"}}, status_code=413)
    await response(scope, receive, send)


class AuthMiddleware:
    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        self.app = app
        self.auth = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope.get("path", "")
        if (
            scope["type"] != "http"
            or not path.startswith(PROTECTED_PREFIXES)
            or path.startswith(SIGNED_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return
        client = await self.auth.authenticate(_header(scope, b"authorization"))
        if client is None:
            response = JSONResponse(
                {"error": {"code": "UNAUTHORIZED"}},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        if path.startswith("/admin") and not client.is_admin:
            await JSONResponse({"error": {"code": "FORBIDDEN"}}, status_code=403)(
                scope, receive, send
            )
            return
        _state(scope)["client"] = client
        with context.context_scope(client_id=client.id, client_name=client.name):
            await self.app(scope, receive, send)
