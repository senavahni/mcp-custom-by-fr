"""CRUD entitas `rw` milik MCP: validasi Pydantic, optimistic locking, soft delete.

Hanya tabel schema `mcp`; entitas Dashboard (`public`) ditolak dengan petunjuk tool yang benar (D4).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, func, insert, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_marketing.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from mcp_marketing.services.db.entities import EntitySpec
from mcp_marketing.services.db.query import (
    WorkspaceScope,
    coerce,
    get_one,
    scope_conditions,
    set_timeout,
)


def ensure_writable(spec: EntitySpec, *, deleting: bool = False) -> None:
    if spec.access == "rw" or (deleting and spec.access == "rd"):
        if spec.schema != "mcp":  # pertahanan berlapis: tidak pernah menulis ke public
            raise Forbidden("Penulisan ke schema public tidak diizinkan")
        return
    hint = f"; gunakan {spec.redirect_write}" if spec.redirect_write else ""
    raise Forbidden(f"Entitas '{spec.name}' read-only{hint}", details={"entity": spec.name})


def _validate(spec: EntitySpec, data: dict[str, Any]) -> dict[str, Any]:
    assert spec.write_model is not None
    try:
        model = spec.write_model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first.get("loc", ())) or "data"
        raise ValidationFailed(
            f"{field}: {first.get('msg')}", details={"field": field, "errors": len(exc.errors())}
        ) from None
    return model.model_dump(mode="python")


def _integrity(exc: IntegrityError) -> Exception:
    text = str(exc.orig)
    if "unique" in text.lower() or "duplicate" in text.lower():
        return Conflict("Data dengan nama yang sama sudah ada di workspace ini")
    if "foreign key" in text.lower():
        return ValidationFailed("Referensi id tidak ditemukan (foreign key)")
    return ValidationFailed("Data melanggar constraint database")


async def create(
    session: AsyncSession,
    spec: EntitySpec,
    scope: WorkspaceScope,
    data: dict[str, Any],
    *,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_writable(spec)
    values = {**_validate(spec, data), **(defaults or {})}
    if "workspace_id" in spec.fields:
        values["workspace_id"] = scope.workspace_id
    table = spec.source()
    await set_timeout(session)
    try:
        row = (
            await session.execute(
                insert(table).values(**values).returning(table.c[spec.id_field])  # type: ignore[arg-type]
            )
        ).one()
    except IntegrityError as exc:
        raise _integrity(exc) from None
    return {"id": str(row[0])}


async def update_row(
    session: AsyncSession,
    spec: EntitySpec,
    scope: WorkspaceScope,
    ident: Any,
    patch: dict[str, Any],
    *,
    expected_updated_at: datetime | None = None,
) -> dict[str, Any]:
    ensure_writable(spec)
    if not patch:
        raise ValidationFailed("patch kosong", details={"field": "patch"})
    assert spec.write_model is not None
    unknown = set(patch) - set(spec.write_model.model_fields)
    if unknown:
        raise ValidationFailed(f"field tidak dapat diubah: {sorted(unknown)}")
    if expected_updated_at is not None and spec.updated_col is None:
        raise ValidationFailed(f"Entitas '{spec.name}' tidak mendukung expected_updated_at")
    current = await get_one(session, spec, scope, ident, list(spec.write_model.model_fields))
    if current is None:
        raise NotFound(f"{spec.name} {ident} tidak ditemukan")
    merged = _validate(spec, {**current, **patch})
    values = {k: merged[k] for k in patch}
    table = spec.source()
    cols = table.c
    conds = [
        *scope_conditions(spec, cols, scope),
        cols[spec.id_field] == coerce(spec, spec.id_field, ident),
    ]
    returning = [cols[spec.id_field]]
    if spec.updated_col:
        values[spec.updated_col] = func.now()
        returning.append(cols[spec.updated_col])
        if expected_updated_at is not None:
            conds.append(cols[spec.updated_col] == expected_updated_at)
    try:
        row = (
            await session.execute(
                update(table).where(*conds).values(**values).returning(*returning)  # type: ignore[arg-type]
            )
        ).first()
    except IntegrityError as exc:
        raise _integrity(exc) from None
    if row is None:
        latest = await get_one(session, spec, scope, ident, [spec.updated_col or spec.id_field])
        if latest is None:
            raise NotFound(f"{spec.name} {ident} tidak ditemukan")
        raise Conflict(
            "Data sudah diubah pihak lain; ambil ulang lalu coba lagi",
            details={"updated_at": latest.get(spec.updated_col or "")},
        )
    out: dict[str, Any] = {"id": str(row[0]), "updated_fields": sorted(patch)}
    if spec.updated_col:
        out["updated_at"] = row[1].isoformat()
    return out


async def delete_row(
    session: AsyncSession,
    spec: EntitySpec,
    scope: WorkspaceScope,
    ident: Any,
    *,
    hard: bool = False,
) -> dict[str, Any]:
    ensure_writable(spec, deleting=True)
    table = spec.source()
    cols = table.c
    conds = [
        *scope_conditions(spec, cols, scope),
        cols[spec.id_field] == coerce(spec, spec.id_field, ident),
    ]
    await set_timeout(session)
    if spec.soft_delete and not hard:
        stmt: Any = update(table).where(*conds).values(deleted_at=func.now())  # type: ignore[arg-type]
        mode = "soft"
    else:
        if spec.soft_delete and not scope.is_admin:
            raise Forbidden("hard delete hanya untuk klien admin")
        stmt = delete(table).where(*conds)  # type: ignore[arg-type]
        mode = "hard"
    result = await session.execute(stmt.returning(cols[spec.id_field]))
    if result.first() is None:
        raise NotFound(f"{spec.name} {ident} tidak ditemukan")
    return {"deleted": True, "mode": mode}
