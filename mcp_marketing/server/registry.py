"""Registry tool: `ToolSpec` + dekorator `@tool`, konteks eksekusi, dan JSON Schema ringkas."""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from mcp_marketing.schemas.common import JobAccepted

if TYPE_CHECKING:
    from mcp_marketing.core.config import Settings
    from mcp_marketing.server.auth import ClientIdentity
    from mcp_marketing.server.services import Services
    from mcp_marketing.services.agent.budget import BudgetScope

MAX_DESCRIPTION = 300
MAX_SUMMARY = 80

Handler = Callable[[Any, "ToolContext"], Awaitable[BaseModel | dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    toolset: str
    summary: str  # <= 80 karakter, untuk toolbox_list
    description: str  # <= 300 karakter
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Handler
    cache_ttl_s: int | None = None
    default_fields: tuple[str, ...] = ()
    admin_only: bool = False
    writes: bool = False
    returns_job: bool = False
    version: int = 1
    timeout_s: float = 25.0
    drop_first: tuple[str, ...] = ()
    # tool yang hanya untuk agent runtime (run_id disuntik) tidak tampil ke klien eksternal
    agent_only: bool = False
    dev_only: bool = False

    def input_schema(self) -> dict[str, Any]:
        return compact_schema(self.input_model.model_json_schema())

    def output_schema(self) -> dict[str, Any]:
        schema = compact_schema(self.output_model.model_json_schema())
        if not self.returns_job:
            return schema
        job = compact_schema(JobAccepted.model_json_schema())
        defs = {**schema.pop("$defs", {}), **job.pop("$defs", {})}
        out: dict[str, Any] = {"type": "object", "anyOf": [schema, job]}
        if defs:
            out["$defs"] = defs
        return out


@dataclass
class ToolContext:
    client: ClientIdentity
    request_id: str
    services: Services
    run_id: str | None = None
    budget: BudgetScope | None = None
    internal: bool = False  # dipanggil agent runtime (bukan klien MCP)
    llm_tokens: int = 0
    external_units: int = 0
    baseline_tokens: int | None = None
    job_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def settings(self) -> Settings:
        return self.services.settings


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    if spec.name in _REGISTRY:
        raise ValueError(f"tool ganda: {spec.name}")
    if len(spec.description) > MAX_DESCRIPTION:
        raise ValueError(f"deskripsi {spec.name} > {MAX_DESCRIPTION} karakter")
    if len(spec.summary) > MAX_SUMMARY:
        raise ValueError(f"summary {spec.name} > {MAX_SUMMARY} karakter")
    _REGISTRY[spec.name] = spec
    return spec


def tool(
    *,
    name: str,
    toolset: str,
    summary: str,
    description: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    **options: Any,
) -> Callable[[Handler], Handler]:
    """Dekorator: daftarkan handler `async def h(inp, ctx)` sebagai tool MCP."""

    def decorator(fn: Handler) -> Handler:
        register(
            ToolSpec(
                name=name,
                toolset=toolset,
                summary=summary,
                description=description,
                input_model=input_model,
                output_model=output_model,
                handler=fn,
                **options,
            )
        )
        return fn

    return decorator


def get_tool(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def all_tools() -> list[ToolSpec]:
    return list(_REGISTRY.values())


def unregister(name: str) -> None:
    """Untuk test."""
    _REGISTRY.pop(name, None)


def compact_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Buang `title` otomatis Pydantic (hemat token di tools/list); nama properti tetap utuh."""

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if key == "title" and isinstance(value, str):
                    continue
                if key in ("properties", "$defs") and isinstance(value, dict):
                    out[key] = {k: strip(v) for k, v in value.items()}
                else:
                    out[key] = strip(value)
            return out
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    result = strip(copy.deepcopy(schema))
    result.setdefault("type", "object")
    return result  # type: ignore[no-any-return]
