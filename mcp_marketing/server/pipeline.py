"""Pipeline standar setiap tool call — dipakai server MCP DAN agent runtime (in-process).

Urutan: konteks -> scope -> rate limit -> validasi input -> cache -> budget pre-check -> handler
(timeout) -> auto-handle (T2) -> kompaksi (T1) -> simpan cache -> audit async -> hasil.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

import orjson
from pydantic import BaseModel, ValidationError

from mcp_marketing.core import context
from mcp_marketing.core.errors import (
    BudgetExceeded,
    Forbidden,
    NotFound,
    UpstreamTimeout,
    to_tool_error,
    validation_error_from,
)
from mcp_marketing.core.logging import get_logger, redact
from mcp_marketing.core.metrics import TOOL_CALLS, TOOL_LATENCY, TOOL_OUTPUT_TOKENS
from mcp_marketing.models import ToolCall
from mcp_marketing.schemas.common import JobAccepted
from mcp_marketing.server.auth import Authenticator, ClientIdentity
from mcp_marketing.server.registry import ToolContext, ToolSpec, get_tool
from mcp_marketing.server.services import Services
from mcp_marketing.services.results.cache import tool_cache_key
from mcp_marketing.services.results.compact import CONCISE_ITEMS, compact
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.utils.crypto import sha256_hex
from mcp_marketing.utils.text import truncate
from mcp_marketing.utils.tokens import count_tokens

log = get_logger(__name__)

ARGS_PREVIEW_MAX = 2_048
TEXT_SUMMARY_MAX = 300


@dataclass
class ToolOutcome:
    structured: dict[str, Any]
    text: str
    is_error: bool
    status: str  # ok | error | cached | job
    error_code: str | None = None
    output_tokens: int = 0
    duration_ms: int = 0
    llm_tokens: int = 0


def _args_preview(args: dict[str, Any]) -> dict[str, Any]:
    def shorten(v: Any) -> Any:
        if isinstance(v, str):
            return truncate(v, 200)
        if isinstance(v, list):
            return [shorten(x) for x in v[:10]]
        if isinstance(v, dict):
            return {k: shorten(x) for k, x in list(v.items())[:30]}
        return v

    preview = shorten(redact(args))
    if len(orjson.dumps(preview, default=str)) > ARGS_PREVIEW_MAX:
        return {"_truncated": True, "keys": sorted(args)[:50]}
    return preview  # type: ignore[no-any-return]


def _summary_text(out: dict[str, Any]) -> str:
    summary = out.get("summary")
    if isinstance(summary, str) and summary:
        return summary
    return truncate(orjson.dumps(out, default=str).decode(), TEXT_SUMMARY_MAX)


def _dump(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    return orjson.loads(orjson.dumps(value, default=str))  # type: ignore[no-any-return]


class ToolPipeline:
    def __init__(self, services: Services, authenticator: Authenticator | None = None) -> None:
        self.services = services
        self.auth = authenticator
        self._pending: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ publik
    async def call(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        client: ClientIdentity,
        request_id: str | None = None,
        run_id: str | None = None,
        internal: bool = False,
        allowed: frozenset[str] | None = None,
    ) -> ToolOutcome:
        request_id = request_id or uuid.uuid4().hex
        args = dict(arguments or {})
        started = time.perf_counter()
        spec = get_tool(name)
        ctx: ToolContext | None = None
        with context.context_scope(
            request_id=request_id,
            client_id=client.id,
            client_name=client.name,
            tool=name,
            run_id=run_id,
        ):
            try:
                spec = self._authorize(spec, name, client, internal, allowed)
                if not internal and self.auth is not None:
                    self.auth.check_rate(client)
                try:
                    inp = spec.input_model.model_validate(args)
                except ValidationError as exc:
                    raise validation_error_from(exc) from None
                ctx = ToolContext(
                    client=client,
                    request_id=request_id,
                    services=self.services,
                    run_id=run_id,
                    internal=internal,
                )
                outcome = await self._execute(spec, inp, args, ctx)
            except Exception as exc:  # noqa: BLE001 - semua error menjadi ToolError terstruktur
                err = to_tool_error(exc)
                if err.code == "INTERNAL":
                    log.exception("tool.internal_error", error_type=type(exc).__name__)
                else:
                    log.info("tool.error", code=err.code, message=err.message)
                structured = err.to_structured()
                outcome = ToolOutcome(
                    structured=structured,
                    text=err.message,
                    is_error=True,
                    status="error",
                    error_code=err.code,
                    output_tokens=count_tokens(structured),
                    llm_tokens=ctx.llm_tokens if ctx else 0,
                )
            outcome.duration_ms = int((time.perf_counter() - started) * 1000)
            TOOL_CALLS.labels(name, outcome.status).inc()
            TOOL_LATENCY.labels(name).observe(outcome.duration_ms / 1000)
            TOOL_OUTPUT_TOKENS.labels(name).inc(outcome.output_tokens)
            log.info(
                "tool.call",
                status=outcome.status,
                duration_ms=outcome.duration_ms,
                output_tokens=outcome.output_tokens,
            )
            self._audit(name, args, client, request_id, run_id, outcome, ctx)
        return outcome

    async def drain(self) -> None:
        """Tunggu audit yang masih berjalan (shutdown/test)."""
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    # ------------------------------------------------------------------ langkah
    @staticmethod
    def _authorize(
        spec: ToolSpec | None,
        name: str,
        client: ClientIdentity,
        internal: bool,
        allowed: frozenset[str] | None,
    ) -> ToolSpec:
        if spec is None:
            raise NotFound(f"Tool tidak dikenal: {name}")
        if allowed is not None and name not in allowed:
            raise Forbidden(f"Tool {name} tidak diizinkan untuk pemanggil ini")
        if spec.agent_only and not internal:
            raise Forbidden(f"Tool {name} hanya untuk agent runtime")
        if not internal and not client.can_use(spec.toolset, admin_only=spec.admin_only):
            raise Forbidden(f"Toolset '{spec.toolset}' di luar scope klien")
        return spec

    async def _execute(
        self, spec: ToolSpec, inp: BaseModel, args: dict[str, Any], ctx: ToolContext
    ) -> ToolOutcome:
        s = self.services.settings
        response_format = getattr(inp, "response_format", "concise")
        use_cache = (
            bool(spec.cache_ttl_s) and not spec.writes and getattr(inp, "cache", "use") != "bypass"
        )
        cache_key = None
        if use_cache:
            scope = f"{ctx.client.workspace_id or '-'}|{ctx.client.id or ctx.client.name}"
            cache_key = tool_cache_key(spec.name, spec.version, inp.model_dump(mode="json"), scope)
            cached = await self.services.cache.get(cache_key)
            if cached is not None:
                return self._finish(spec, cached, response_format, ctx, status="cached")

        ctx.budget = await self.services.budgets.scope(
            client_id=ctx.client.id,
            daily_limit=ctx.client.daily_token_budget,
            run_id=ctx.run_id,
            tool_limit=s.token.tool_llm_tokens_default,
        )
        for layer in ctx.budget.layers:
            if layer.scope != "tool_call" and layer.remaining == 0:
                raise BudgetExceeded(
                    f"Budget token {layer.scope} habis",
                    details={"used": layer.used, "limit": layer.limit, "scope": layer.scope},
                )

        try:
            raw = await asyncio.wait_for(spec.handler(inp, ctx), timeout=spec.timeout_s)
        except TimeoutError:
            raise UpstreamTimeout(
                f"Tool {spec.name} melebihi {int(spec.timeout_s)} s; gunakan mode job/wait_s",
            ) from None

        if isinstance(raw, JobAccepted):
            ctx.job_id = raw.job_id
            return self._finish(
                spec, _dump(raw), response_format, ctx, status="job", compact_out=False
            )
        out = _dump(raw)
        if spec.machine_output:
            return self._finish(spec, out, response_format, ctx, status="ok", compact_out=False)
        out = await self._auto_handle(spec, out, ctx)
        if cache_key is not None:
            await self.services.cache.set(cache_key, "tool", out, int(spec.cache_ttl_s or 0))
        return self._finish(spec, out, response_format, ctx, status="ok")

    async def _auto_handle(
        self, spec: ToolSpec, out: dict[str, Any], ctx: ToolContext
    ) -> dict[str, Any]:
        """T2: > INLINE_ITEMS_MAX baris di `items` -> pindah ke result store + 5 sampel."""
        items = out.get("items")
        limit = self.services.settings.token.inline_items_max
        if not (isinstance(items, dict) and isinstance(items.get("rows"), list)) or out.get(
            "handle"
        ):
            return out
        rows: list[list[Any]] = items["rows"]
        if len(rows) <= limit:
            return out
        cols: list[str] = list(items.get("cols") or [])
        log.warning(
            "tool.auto_handle",
            rows=len(rows),
            hint="tool sebaiknya membuat handle sendiri (MCP_SPEC §3 T2)",
        )
        rs = await self.services.results.create(
            kind=spec.name,
            columns=cols,
            rows=(dict(zip(cols, r, strict=False)) for r in rows),
            owner=Owner(client_id=ctx.client.id, run_id=ctx.run_id),
            stats=out.get("stats") if isinstance(out.get("stats"), dict) else None,
            source_ref={"tool": spec.name, "auto": True},
        )
        sample_tokens = count_tokens(rows[:CONCISE_ITEMS])
        ctx.baseline_tokens = ctx.baseline_tokens or int(
            sample_tokens / max(1, min(len(rows), CONCISE_ITEMS)) * len(rows)
        )
        return {
            **out,
            "handle": rs.handle,
            "total": max(int(out.get("total") or 0), len(rows)),
            "items": {"cols": cols, "rows": rows[:CONCISE_ITEMS]},
        }

    def _finish(
        self,
        spec: ToolSpec,
        out: dict[str, Any],
        response_format: str,
        ctx: ToolContext,
        *,
        status: str,
        compact_out: bool = True,
    ) -> ToolOutcome:
        s = self.services.settings
        out = dict(out)
        if isinstance(out.get("cost"), dict) and not spec.machine_output:
            out["cost"] = {
                **out["cost"],
                "llm_tokens": 0 if status == "cached" else ctx.llm_tokens,
                "cache_hit": status == "cached",
            }
            if ctx.external_units:
                out["cost"]["external_units"] = ctx.external_units
        before = count_tokens(out)
        if compact_out and not spec.machine_output:
            result = compact(
                out,
                response_format=response_format,
                soft_tokens=s.token.tool_output_soft_tokens,
                hard_tokens=s.token.tool_output_hard_tokens,
                drop_first=spec.drop_first,
            )
            out, after = result.output, result.tokens_after
        else:
            after = before
        if ctx.baseline_tokens is None:
            ctx.baseline_tokens = before
        return ToolOutcome(
            structured=out,
            text=_summary_text(out),
            is_error=False,
            status=status,
            output_tokens=after,
            llm_tokens=ctx.llm_tokens,
        )

    # ------------------------------------------------------------------ audit
    def _audit(
        self,
        name: str,
        args: dict[str, Any],
        client: ClientIdentity,
        request_id: str,
        run_id: str | None,
        outcome: ToolOutcome,
        ctx: ToolContext | None,
    ) -> None:
        row = ToolCall(
            request_id=request_id[:64],
            client_id=uuid.UUID(client.id) if client.id else None,
            run_id=_maybe_uuid(run_id),
            tool=name[:64],
            args_hash=sha256_hex(orjson.dumps(args, option=orjson.OPT_SORT_KEYS, default=str)),
            args_preview=_args_preview(args),
            status=outcome.status,
            error_code=outcome.error_code,
            duration_ms=outcome.duration_ms,
            output_tokens=outcome.output_tokens,
            baseline_tokens=ctx.baseline_tokens if ctx else None,
            llm_tokens=outcome.llm_tokens,
            job_id=ctx.job_id if ctx else None,
        )
        task = asyncio.create_task(self._insert_audit(row))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _insert_audit(self, row: ToolCall) -> None:
        try:
            async with self.services.db.session() as session:
                session.add(row)
        except Exception as exc:  # noqa: BLE001 - audit tidak boleh menggagalkan tool
            log.warning("tool.audit_failed", error=str(exc))


def _maybe_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
