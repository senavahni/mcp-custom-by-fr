"""Sandboxed YAML template rendering to validated Dashboard block payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.schemas.blocks import validate_block_content
from mcp_marketing.services.templates.md_to_blocks import markdown_to_blocks
from mcp_marketing.utils.time import add_workdays


class _NoAttributesEnvironment(SandboxedEnvironment):
    def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
        return False

    def is_safe_callable(self, obj: Any) -> bool:
        return False


_ENV = _NoAttributesEnvironment(undefined=StrictUndefined, autoescape=False)
_ENV.globals.clear()
_ENV.filters.update(
    join=lambda value, separator=", ": separator.join(str(item) for item in value),
    date_add=lambda value, days: (
        date.fromisoformat(str(value)) + timedelta(days=int(days))
    ).isoformat(),
    upper=str.upper,
    title=str.title,
)


@dataclass(frozen=True)
class RenderedTemplate:
    blocks: list[dict[str, Any]]
    subtasks: list[dict[str, Any]]
    outline: dict[str, Any]


def _render_text(value: str, variables: dict[str, Any]) -> str:
    try:
        return _ENV.from_string(value).render(variables)
    except Exception as exc:
        raise ValidationFailed(f"Template tidak dapat dirender: {type(exc).__name__}") from None


def _variables(template: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    specs = template.get("variables", [])
    if not isinstance(specs, list):
        raise ValidationFailed("Definisi variables template tidak valid")
    for spec in specs:
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
            raise ValidationFailed("Definisi variable template tidak valid")
        name, kind = spec["name"], spec.get("type", "string")
        value = supplied.get(name, spec.get("default"))
        if value is None:
            if spec.get("required", False):
                raise ValidationFailed(f"Variabel wajib belum diisi: {name}")
            continue
        if kind == "string" and not isinstance(value, str):
            raise ValidationFailed(f"Variabel {name} harus berupa teks")
        if kind == "date":
            try:
                date.fromisoformat(str(value))
            except ValueError:
                raise ValidationFailed(f"Variabel {name} harus berupa tanggal YYYY-MM-DD") from None
            value = str(value)
        if kind == "list" and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            raise ValidationFailed(f"Variabel {name} harus berupa daftar teks")
        if kind not in {"string", "date", "list"}:
            raise ValidationFailed(f"Tipe variabel tidak didukung: {kind}")
        result[name] = value
    extra = set(supplied) - {str(spec["name"]) for spec in specs}
    if extra:
        raise ValidationFailed(f"Variabel tidak dikenal: {sorted(extra)[0]}")
    return result


def _render_blocks(markdown: str, label: str) -> list[dict[str, Any]]:
    try:
        blocks = markdown_to_blocks(markdown)
        for block in blocks:
            validate_block_content(block["block_type"], block["content_json"])
        return blocks
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationFailed(f"{label}: {exc}") from None


def _workday_offset(start: date, offset_days: int) -> date:
    if offset_days < 0:
        raise ValidationFailed("offset_days subtask tidak boleh negatif")
    return add_workdays(start, offset_days + 1)


def render(
    template: dict[str, Any],
    variables: dict[str, Any],
    base_date: date,
    *,
    target_depth: int = 0,
) -> RenderedTemplate:
    values = _variables(template, variables)
    body = template.get("blocks", {})
    markdown = body.get("md") if isinstance(body, dict) else None
    if not isinstance(markdown, str):
        raise ValidationFailed("Template blocks.md wajib berupa markdown")
    blocks = _render_blocks(_render_text(markdown, values), "blocks")
    subtasks: list[dict[str, Any]] = []

    def visit(items: Any, parent_ref: str | None, depth: int) -> None:
        if not isinstance(items, list):
            raise ValidationFailed("Template subtasks harus berupa daftar")
        if target_depth + depth > 3 and items:
            raise ValidationFailed("Template melebihi kedalaman Dashboard maksimum (3)")
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValidationFailed(f"Subtask template ke-{index} tidak valid")
            ref = item.get("client_ref")
            title = item.get("title")
            if not isinstance(ref, str) or not ref or not isinstance(title, str):
                raise ValidationFailed(f"Subtask template ke-{index} perlu client_ref dan title")
            if any(existing["client_ref"] == ref for existing in subtasks):
                raise ValidationFailed(f"client_ref subtask duplikat: {ref}")
            offset = item.get("offset_days", 0)
            duration = item.get("duration_days", 1)
            if (
                not isinstance(offset, int)
                or offset < 0
                or not isinstance(duration, int)
                or duration < 1
            ):
                raise ValidationFailed(f"Jadwal subtask {ref} tidak valid")
            start = _workday_offset(base_date, offset)
            end = add_workdays(start, duration)
            markdown_value = item.get("md", "")
            initial_blocks = _render_blocks(
                _render_text(markdown_value, values), f"subtask {ref}"
            ) if markdown_value else []
            subtasks.append(
                {
                    "client_ref": ref,
                    "parent_client_ref": parent_ref,
                    "title": _render_text(title, values),
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "tags": item.get("tags", []),
                    "initial_blocks": initial_blocks,
                    "offset_days": offset,
                    "duration_days": duration,
                    "depends_on": item.get("depends_on", []),
                }
            )
            visit(item.get("children", []), ref, depth + 1)

    visit(template.get("subtasks", []), None, 1)
    sections = []
    for index, block in enumerate(blocks):
        if block["block_type"] != "heading":
            continue
        end = next(
            (
                later
                for later in range(index + 1, len(blocks))
                if blocks[later]["block_type"] == "heading"
            ),
            len(blocks),
        )
        sections.append(
            {
                "heading": "".join(
                    part.get("text", "") for part in block["content_json"].get("content", [])
                ),
                "block_count": end - index - 1,
            }
        )
    outline = {
        "key": template.get("key"),
        "name": template.get("name"),
        "sections": sections,
        "subtasks": [
            {
                "client_ref": item["client_ref"],
                "title": item["title"],
                "offset_days": item["offset_days"],
                "duration_days": item["duration_days"],
            }
            for item in subtasks
        ],
    }
    return RenderedTemplate(blocks=blocks, subtasks=subtasks, outline=outline)
