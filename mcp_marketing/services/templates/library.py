"""Versioned template storage and YAML seed loading."""

from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_marketing.core.errors import NotFound, ValidationFailed
from mcp_marketing.models.templates import Template, TemplateVersion

SEED_DIR = Path(__file__).with_name("seeds")
LIST_CACHE_TTL_S = 300
_list_cache: dict[
    tuple[str | None, str | None, str | None], tuple[float, list[dict[str, Any]]]
] = {}


@lru_cache(maxsize=128)
def _load_seed(path: str, modified_ns: int) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as file:
        template = yaml.safe_load(file)
    if not isinstance(template, dict) or not isinstance(template.get("key"), str):
        raise ValidationFailed(f"Seed template tidak valid: {Path(path).name}")
    return template


def load_seed(path: Path) -> dict[str, Any]:
    return _load_seed(str(path), path.stat().st_mtime_ns)


async def seed_templates(session: AsyncSession) -> int:
    seeded = 0
    for path in sorted(SEED_DIR.glob("*.yaml")):
        template = load_seed(path)
        row = await session.get(Template, template["key"])
        if row is not None:
            continue
        session.add(
            Template(
                key=template["key"],
                name=template["name"],
                category=template["category"],
                description=template.get("description"),
                origin="seed",
                current_version=1,
                workspace_id=None,
                is_active=True,
            )
        )
        session.add(
            TemplateVersion(
                template_key=template["key"],
                version=1,
                variables=template.get("variables", []),
                blocks=template.get("blocks", {}),
                subtasks=template.get("subtasks", []),
                outline=_seed_outline(template),
            )
        )
        seeded += 1
    await session.flush()
    _list_cache.clear()
    return seeded


async def list_templates(
    session: AsyncSession,
    category: str | None = None,
    workspace_id: str | None = None,
    client_id: str | None = None,
) -> list[dict[str, Any]]:
    cache_key = (category, workspace_id, client_id)
    cached = _list_cache.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < LIST_CACHE_TTL_S:
        return [dict(item) for item in cached[1]]
    scope = (
        Template.workspace_id == workspace_id
        if workspace_id
        else Template.created_by_client == client_id
        if client_id
        else false()
    )
    query = select(Template).where(
        Template.is_active.is_(True),
        or_(Template.origin == "seed", scope),
    )
    if category:
        query = query.where(Template.category == category)
    rows = list((await session.scalars(query.order_by(Template.category, Template.name))).all())
    items = [
        {
            "key": row.key,
            "name": row.name,
            "category": row.category,
            "description": row.description,
            "origin": row.origin,
            "current_version": row.current_version,
            "workspace_id": row.workspace_id,
            "is_active": row.is_active,
            "created_by_client": row.created_by_client,
        }
        for row in rows
    ]
    _list_cache[cache_key] = (time.monotonic(), [dict(item) for item in items])
    return [dict(item) for item in items]


async def get_template(
    session: AsyncSession,
    key: str,
    workspace_id: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    scope = (
        Template.workspace_id == workspace_id
        if workspace_id
        else Template.created_by_client == client_id
        if client_id
        else false()
    )
    row = await session.scalar(
        select(Template).where(
            Template.key == key,
            Template.is_active.is_(True),
            (Template.origin == "seed") | scope,
        )
    )
    if row is None:
        raise NotFound(f"Template tidak ditemukan: {key}")
    version = await session.get(TemplateVersion, (row.key, row.current_version))
    if version is None:
        raise NotFound(f"Versi template tidak ditemukan: {key}")
    return {
        "key": row.key,
        "name": row.name,
        "category": row.category,
        "description": row.description,
        "origin": row.origin,
        "variables": version.variables,
        "blocks": version.blocks,
        "subtasks": version.subtasks,
        "outline": version.outline,
        "version": version.version,
    }


async def save_template(
    session: AsyncSession,
    template: dict[str, Any],
    *,
    client_id: str | None,
    workspace_id: str | None,
    origin: str = "client",
) -> int:
    key = template.get("key")
    if not isinstance(key, str) or not key or len(key) > 64:
        raise ValidationFailed("Template key wajib diisi (maksimal 64 karakter)")
    row = await session.get(Template, key)
    if row and (
        row.workspace_id != workspace_id
        or (workspace_id is None and (client_id is None or row.created_by_client != client_id))
        or row.origin == "seed"
    ):
        raise ValidationFailed("Template global/seed tidak dapat ditimpa; gunakan key baru")
    if row is None and workspace_id is None and client_id is None:
        raise ValidationFailed("Klien tanpa workspace perlu id untuk menyimpan template privat")
    version = row.current_version + 1 if row else 1
    if row is None:
        row = Template(
            key=key,
            name=template["name"],
            category=template.get("category", "custom"),
            description=template.get("description"),
            origin=origin,
            current_version=version,
            workspace_id=workspace_id,
            is_active=True,
            created_by_client=client_id,
        )
        session.add(row)
    else:
        row.name = template["name"]
        row.category = template.get("category", row.category)
        row.description = template.get("description")
        row.current_version = version
    session.add(
        TemplateVersion(
            template_key=key,
            version=version,
            variables=template.get("variables", []),
            blocks=template.get("blocks", {}),
            subtasks=template.get("subtasks", []),
            outline=template.get("outline", {}),
        )
    )
    await session.flush()
    _list_cache.clear()
    return version


def _seed_outline(template: dict[str, Any]) -> dict[str, Any]:
    markdown = template.get("blocks", {}).get("md", "")
    lines = markdown.splitlines() if isinstance(markdown, str) else []
    headings = [
        (index, line.lstrip("#").strip())
        for index, line in enumerate(lines)
        if line.startswith("#")
    ]
    sections = []
    for position, (line_index, heading) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        sections.append(
            {
                "heading": heading,
                "block_count": sum(bool(line.strip()) for line in lines[line_index + 1 : end]),
            }
        )
    return {
        "key": template["key"],
        "name": template["name"],
        "sections": sections,
        "subtasks": [
            {
                "client_ref": task.get("client_ref"),
                "title": task.get("title"),
                "offset_days": task.get("offset_days", 0),
                "duration_days": task.get("duration_days", 1),
            }
            for task in template.get("subtasks", [])
        ],
    }
