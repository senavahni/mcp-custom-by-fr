"""`POST /uploads` (MCP-V1-11, V1-K9): Dashboard stream-proxy file ke MCP.

    POST /uploads?workspace_id=<uuid>&purpose=book|brand|article|import
    Authorization: Bearer <key klien Dashboard>   X-Filename: <nama asli, opsional>
    body = byte file mentah (bukan multipart) -> distream ke disk, tidak ditampung di memori.

Scope: purpose buku/brand/artikel butuh toolset `kn`, import butuh `ci`; workspace harus sama
dengan workspace klien (kecuali admin). Respons 201 (baru) / 200 (duplikat sha256 di workspace).
Error: 413 UPLOAD_TOO_LARGE, 415 UNSUPPORTED_FILE, 400 VALIDATION_ERROR, 403 FORBIDDEN.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_marketing.core.errors import Forbidden, ToolError, UploadTooLarge, ValidationFailed
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models.knowledge import Upload
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.services.knowledge.uploads import (
    PURPOSE_KINDS,
    StoredUpload,
    max_bytes,
    store_stream,
)

log = get_logger(__name__)

UPLOAD_PATH = "/uploads"
FILENAME_MAX = 255
REQUIRED_TOOLSET = {"book": "kn", "brand": "kn", "article": "kn", "import": "ci"}
HTTP_STATUS = {
    "UPLOAD_TOO_LARGE": 413,
    "UNSUPPORTED_FILE": 415,
    "VALIDATION_ERROR": 400,
    "FORBIDDEN": 403,
}


def _error(exc: ToolError) -> JSONResponse:
    return JSONResponse({"error": exc.to_structured()}, status_code=HTTP_STATUS.get(exc.code, 500))


def check_scope(client: ClientIdentity, workspace_id: uuid.UUID, purpose: str) -> None:
    toolset = REQUIRED_TOOLSET[purpose]
    if toolset not in client.toolsets and not client.is_admin:
        raise Forbidden(f"Klien tidak punya toolset '{toolset}' untuk upload {purpose}")
    if client.is_admin:
        return
    if client.workspace_id is None or uuid.UUID(client.workspace_id) != workspace_id:
        raise Forbidden("Workspace upload berbeda dengan workspace klien")


def parse_params(request: Request) -> tuple[uuid.UUID, str, str | None]:
    try:
        workspace_id = uuid.UUID(request.query_params.get("workspace_id", ""))
    except ValueError:
        raise ValidationFailed("workspace_id wajib (UUID)") from None
    purpose = request.query_params.get("purpose", "")
    if purpose not in PURPOSE_KINDS:
        raise ValidationFailed(f"purpose harus salah satu: {', '.join(PURPOSE_KINDS)}")
    raw_name = request.headers.get("x-filename") or request.query_params.get("filename")
    name = " ".join(raw_name.split())[:FILENAME_MAX] if raw_name else None
    return workspace_id, purpose, name or None


async def save_upload(
    services: Any,
    client: ClientIdentity,
    workspace_id: uuid.UUID,
    purpose: str,
    filename: str | None,
    stored: StoredUpload,
) -> tuple[Upload, bool]:
    """Catat upload; sha256 sama di workspace yang sama -> baris lama (duplicate=True)."""
    async with services.db.session() as session:
        inserted = await session.scalar(
            insert(Upload)
            .values(
                workspace_id=workspace_id,
                sha256=stored.sha256,
                kind=stored.kind,
                purpose=purpose,
                size_bytes=stored.size_bytes,
                path=str(stored.path),
                original_name=filename,
                pages=stored.pages,
                uploaded_by=client.name[:100],
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "sha256"])
            .returning(Upload.id)
        )
        row = await session.scalar(
            select(Upload).where(
                Upload.workspace_id == workspace_id, Upload.sha256 == stored.sha256
            )
        )
    assert row is not None
    return row, inserted is None


def build_upload_routes(services: Any) -> list[Route]:
    async def upload(request: Request) -> Response:
        client: ClientIdentity = request.scope["state"]["client"]
        try:
            workspace_id, purpose, filename = parse_params(request)
            check_scope(client, workspace_id, purpose)
            length = request.headers.get("content-length")
            limit = max(max_bytes(k, services.settings.upload) for k in PURPOSE_KINDS[purpose])
            if length and length.isdigit() and int(length) > limit:
                raise UploadTooLarge(f"File melebihi batas {limit // (1024 * 1024)} MB")
            stored = await store_stream(
                request.stream(),
                storage_dir=services.settings.storage_dir,
                workspace_id=workspace_id,
                purpose=purpose,  # type: ignore[arg-type]
                filename=filename,
                settings=services.settings.upload,
            )
            row, duplicate = await save_upload(
                services, client, workspace_id, purpose, filename, stored
            )
        except ToolError as exc:
            log.info("upload.rejected", code=exc.code, client=client.name)
            return _error(exc)
        log.info("upload.stored", kind=row.kind, size=row.size_bytes, duplicate=duplicate)
        return JSONResponse(
            {
                "upload_id": str(row.id),
                "sha256": row.sha256.strip(),
                "kind": row.kind,
                "purpose": row.purpose,
                "size_bytes": row.size_bytes,
                "pages": row.pages,
                "duplicate": duplicate,
            },
            status_code=200 if duplicate else 201,
        )

    return [Route(UPLOAD_PATH, upload, methods=["POST"])]
