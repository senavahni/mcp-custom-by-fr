"""Klien tunggal LLM Gateway (OpenAI-compatible): chat, chat_json, embed.

SATU-SATUNYA modul yang mengimpor `openai` (diuji). Retry ditangani sendiri sesuai tabel TASK-06;
setiap panggilan: budget.reserve -> panggil -> budget.commit -> catat `mcp.llm_calls` (async).

Header: `X-Request-Id` dibuat UNIK per panggilan LLM (Gateway memakainya sebagai primary key
`requests.id`); korelasi ke request MCP lewat `X-Correlation-Id` dan parameter `user`.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx2
import openai
import orjson
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from mcp_marketing.core import context
from mcp_marketing.core.config import Settings
from mcp_marketing.core.database import Database
from mcp_marketing.core.errors import (
    BudgetExceeded,
    RateLimited,
    ToolError,
    UpstreamError,
    UpstreamTimeout,
)
from mcp_marketing.core.logging import get_logger
from mcp_marketing.core.metrics import LLM_TOKENS
from mcp_marketing.models import LlmCall
from mcp_marketing.services.agent.budget import BudgetScope
from mcp_marketing.services.llm import prompts
from mcp_marketing.services.llm.route_policy import resolve
from mcp_marketing.services.results.cache import ResultCache, llm_cache_key
from mcp_marketing.utils.crypto import sha256_hex
from mcp_marketing.utils.text import truncate
from mcp_marketing.utils.tokens import count_tokens, truncate_to_tokens

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

MAX_RATE_RETRIES = 2
UPSTREAM_BACKOFF_S = (1.0, 3.0)
RETRY_AFTER_CAP_S = 30.0
TIMEOUT_TOKEN_FACTOR = 0.7
EMBED_BATCH = 64
EMBED_MAX_TOKENS = 8_000
REPAIR_PREVIOUS_CHARS = 2_000
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str
    parse_error: str | None = None


@dataclass
class ChatResult:
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_resolved: str | None = None
    gateway_request_id: str | None = None
    latency_ms: int = 0
    cached: bool = False
    route: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_cache(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "finish_reason": self.finish_reason,
            "model_resolved": self.model_resolved,
        }


def _error_code(exc: openai.APIStatusError) -> str:
    body = exc.body if isinstance(exc.body, dict) else {}
    err = body.get("error", body) if isinstance(body, dict) else {}
    code = err.get("code") if isinstance(err, dict) else None
    return str(code or "")


def _retry_after(exc: openai.APIStatusError) -> float:
    raw = exc.response.headers.get("retry-after") if exc.response is not None else None
    try:
        return min(RETRY_AFTER_CAP_S, max(0.0, float(raw))) if raw else 1.0
    except ValueError:
        return 1.0


def _parse_tool_calls(message: Any) -> list[ToolCallRequest]:
    calls = []
    for tc in getattr(message, "tool_calls", None) or []:
        fn = getattr(tc, "function", None)
        if fn is None:
            continue
        raw = fn.arguments or "{}"
        try:
            args = orjson.loads(raw) if raw.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("argumen bukan objek JSON")
            calls.append(ToolCallRequest(id=tc.id, name=fn.name, arguments=args, raw_arguments=raw))
        except ValueError as exc:
            calls.append(
                ToolCallRequest(
                    id=tc.id,
                    name=fn.name,
                    arguments={},
                    raw_arguments=raw,
                    parse_error=str(exc)[:200],
                )
            )
    return calls


def parse_json_text(text: str) -> Any:
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    if not cleaned.startswith(("{", "[")):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            cleaned = cleaned[start : end + 1]
    return orjson.loads(cleaned)


class GatewayClient:
    def __init__(
        self,
        settings: Settings,
        *,
        db: Database | None = None,
        cache: ResultCache | None = None,
        http_client: httpx2.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        llm = settings.llm
        self.client = AsyncOpenAI(
            base_url=llm.llm_gateway_url,
            api_key=llm.llm_gateway_api_key.get_secret_value(),
            timeout=llm.llm_timeout_s,
            max_retries=0,
            http_client=http_client,
        )
        self.db = db
        self.cache = cache
        self._sleep = sleep
        self._pending: set[asyncio.Task[None]] = set()
        self._embed_cache_ok: bool | None = None

    # ------------------------------------------------------------------ chat
    async def chat(
        self,
        purpose: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        json_mode: bool | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        route_override: str | None = None,
        budget: BudgetScope | None = None,
        prompt_version: str | None = None,
    ) -> ChatResult:
        route, cfg = resolve(purpose, self.settings.llm, route_override=route_override)
        max_tokens = max_tokens or cfg.max_tokens
        temperature = cfg.temperature if temperature is None else temperature
        use_json = cfg.json if json_mode is None else json_mode
        params: dict[str, Any] = {"temperature": temperature, "max_tokens": max_tokens}
        if use_json and not tools:
            params["response_format"] = {"type": "json_object"}
        if tools:
            params["tools"] = tools
            if tool_choice:
                params["tool_choice"] = tool_choice
        purpose_tag = prompts.tag(purpose, prompt_version) if prompt_version else purpose

        cache_key = None
        if self.cache is not None and temperature == 0 and not tools:
            cache_key = llm_cache_key(route, messages, params)
            cached = await self.cache.get(cache_key)
            if cached is not None:
                result = ChatResult(
                    content=cached.get("content"),
                    finish_reason=cached.get("finish_reason"),
                    model_resolved=cached.get("model_resolved"),
                    cached=True,
                    route=route,
                )
                self._record(purpose_tag, route, result, "ok", None)
                return result

        estimate = count_tokens(messages) + (count_tokens(tools) if tools else 0) + max_tokens
        reservation = await budget.reserve(estimate) if budget is not None else None
        try:
            result = await self._call_with_retry(route, messages, params, purpose_tag)
        except ToolError as err:
            if reservation is not None and budget is not None:
                await budget.release(reservation)
            self._record(
                purpose_tag, route, ChatResult(content=None, route=route), "error", err.code
            )
            raise
        except BaseException:
            # cancel/timeout pemanggil atau error tak terpetakan: reservasi wajib dilepas
            if reservation is not None and budget is not None:
                await budget.release(reservation)
            raise
        if reservation is not None and budget is not None:
            await budget.commit(reservation, result.total_tokens)
        self._record(purpose_tag, route, result, "ok", None)
        if cache_key is not None and self.cache is not None and result.finish_reason != "length":
            await self.cache.set(
                cache_key, "llm", result.to_cache(), self.settings.llm.llm_cache_ttl_s
            )
        return result

    async def _call_with_retry(
        self, route: str, messages: list[dict[str, Any]], params: dict[str, Any], purpose: str
    ) -> ChatResult:
        rate_retries = 0
        upstream_retries = 0
        timeout_retried = False
        while True:
            try:
                return await self._call_once(route, messages, params)
            except openai.RateLimitError as exc:
                code = _error_code(exc)
                if code == "quota_exceeded":
                    raise BudgetExceeded(
                        "Kuota project di LLM Gateway habis", details={"scope": "gateway"}
                    ) from None
                if rate_retries >= MAX_RATE_RETRIES:
                    raise RateLimited(
                        "LLM Gateway membatasi laju request",
                        details={"reason": code or "rate_limited"},
                    ) from None
                rate_retries += 1
                await self._sleep(_retry_after(exc))
            except (
                openai.APITimeoutError,
                openai.APIStatusError,
                openai.APIConnectionError,
            ) as exc:
                status = getattr(exc, "status_code", None)
                if isinstance(exc, openai.APITimeoutError) or status == 504:
                    if timeout_retried:
                        raise UpstreamTimeout(
                            "LLM Gateway timeout", details={"route": route}
                        ) from None
                    timeout_retried = True
                    params = {
                        **params,
                        "max_tokens": max(64, int(params["max_tokens"] * TIMEOUT_TOKEN_FACTOR)),
                    }
                    continue
                if isinstance(exc, openai.APIConnectionError) or (
                    status is not None and status >= 500
                ):
                    if upstream_retries >= len(UPSTREAM_BACKOFF_S):
                        reason = (
                            _error_code(exc)
                            if isinstance(exc, openai.APIStatusError)
                            else "connection"
                        )
                        raise UpstreamError(
                            "LLM Gateway/provider gagal",
                            retryable=True,
                            details={"reason": reason or "upstream_error"},
                        ) from None
                    await self._sleep(UPSTREAM_BACKOFF_S[upstream_retries])
                    upstream_retries += 1
                    continue
                if status in (401, 403):
                    log.critical("llm.gateway_auth_failed", status=status, purpose=purpose)
                    raise UpstreamError(
                        "API key LLM Gateway ditolak (cek LLM_GATEWAY_API_KEY)",
                        retryable=False,
                        details={"status": status},
                    ) from None
                assert isinstance(exc, openai.APIStatusError)
                raise UpstreamError(
                    f"Request LLM ditolak Gateway ({status}): {truncate(str(exc.message), 200)}",
                    retryable=False,
                    details={"status": status, "reason": _error_code(exc)},
                ) from None

    async def _call_once(
        self, route: str, messages: list[dict[str, Any]], params: dict[str, Any]
    ) -> ChatResult:
        request_id = str(uuid.uuid4())
        headers = {"X-Request-Id": request_id}
        correlation = context.get("request_id")
        if correlation:
            headers["X-Correlation-Id"] = correlation
        user = f"{context.get('client_name') or '-'}:{context.get('run_id') or '-'}"
        started = time.perf_counter()
        raw = await self.client.chat.completions.with_raw_response.create(
            model=route,
            messages=messages,  # type: ignore[arg-type]
            user=user,
            extra_headers=headers,
            **params,  # type: ignore[arg-type]
        )
        resp = raw.parse()
        latency = int((time.perf_counter() - started) * 1000)
        choice = resp.choices[0] if resp.choices else None
        message = choice.message if choice else None
        usage = resp.usage
        return ChatResult(
            content=getattr(message, "content", None),
            tool_calls=_parse_tool_calls(message) if message else [],
            finish_reason=getattr(choice, "finish_reason", None),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model_resolved=resp.model,
            gateway_request_id=raw.headers.get("x-request-id") or request_id,
            latency_ms=latency,
            route=route,
        )

    # ------------------------------------------------------------------ JSON
    async def chat_json(
        self,
        purpose: str,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        repair_attempts: int = 1,
        budget: BudgetScope | None = None,
        prompt_version: str | None = None,
        **kwargs: Any,
    ) -> tuple[T, ChatResult]:
        result = await self.chat(
            purpose,
            messages,
            json_mode=True,
            budget=budget,
            prompt_version=prompt_version,
            **kwargs,
        )
        total = ChatResult(
            content=result.content,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            model_resolved=result.model_resolved,
            gateway_request_id=result.gateway_request_id,
            latency_ms=result.latency_ms,
            cached=result.cached,
            route=result.route,
            finish_reason=result.finish_reason,
        )
        previous = result.content or ""
        for attempt in range(repair_attempts + 1):
            try:
                return schema.model_validate(parse_json_text(previous)), total
            except (ValueError, ValidationError) as exc:
                error = _short_error(exc)
                if attempt >= repair_attempts:
                    raise UpstreamError(
                        "Output LLM bukan JSON valid",
                        retryable=False,
                        details={"reason": "invalid_json", "error": error},
                    ) from None
                log.info("llm.json_repair", purpose=purpose, error=error)
                repair = prompts.render(
                    "json_repair",
                    error=error,
                    previous=truncate(previous, REPAIR_PREVIOUS_CHARS),
                    schema=orjson.dumps(schema.model_json_schema()).decode(),
                )
                fixed = await self.chat(
                    "repair",
                    [{"role": "user", "content": repair}],
                    budget=budget,
                    prompt_version="v1",
                )
                total.prompt_tokens += fixed.prompt_tokens
                total.completion_tokens += fixed.completion_tokens
                previous = fixed.content or ""
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ embed
    async def embed(
        self, texts: list[str], *, model: str | None = None, budget: BudgetScope | None = None
    ) -> list[list[float]]:
        route = model or self.settings.llm.llm_route_embed
        prepared = []
        for text in texts:
            if count_tokens(text) > EMBED_MAX_TOKENS:
                log.warning("llm.embed_truncated", max_tokens=EMBED_MAX_TOKENS)
                text = truncate_to_tokens(text, EMBED_MAX_TOKENS)
            prepared.append(text)
        cached = await self._embed_cache_get(prepared, route)
        missing = [i for i, vec in enumerate(cached) if vec is None]
        for start in range(0, len(missing), EMBED_BATCH):
            idxs = missing[start : start + EMBED_BATCH]
            batch = [prepared[i] for i in idxs]
            estimate = count_tokens(batch)
            reservation = await budget.reserve(estimate) if budget is not None else None
            started = time.perf_counter()
            try:
                resp = await self.client.embeddings.create(
                    model=route, input=batch, extra_headers={"X-Request-Id": str(uuid.uuid4())}
                )
            except openai.APIError as exc:
                if reservation is not None and budget is not None:
                    await budget.release(reservation)
                raise UpstreamError(
                    "Embedding Gateway gagal", details={"reason": type(exc).__name__}
                ) from None
            except BaseException:
                if reservation is not None and budget is not None:
                    await budget.release(reservation)
                raise
            used = getattr(resp.usage, "prompt_tokens", 0) or estimate
            if reservation is not None and budget is not None:
                await budget.commit(reservation, used)
            self._record(
                "embed",
                route,
                ChatResult(
                    content=None,
                    prompt_tokens=used,
                    route=route,
                    model_resolved=resp.model,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                ),
                "ok",
                None,
            )
            vectors = [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
            for i, vec in zip(idxs, vectors, strict=True):
                cached[i] = vec
            await self._embed_cache_put([prepared[i] for i in idxs], vectors, route)
        return [vec for vec in cached if vec is not None]

    async def _embed_cache_available(self) -> bool:
        if self._embed_cache_ok is None:
            if self.db is None:
                self._embed_cache_ok = False
            else:
                from sqlalchemy import text as sql

                async with self.db.engine.connect() as conn:
                    self._embed_cache_ok = bool(
                        (
                            await conn.execute(sql("SELECT to_regclass('mcp.embedding_cache')"))
                        ).scalar()
                    )
        return self._embed_cache_ok

    async def _embed_cache_get(self, texts: list[str], model: str) -> list[list[float] | None]:
        out: list[list[float] | None] = [None] * len(texts)
        if not texts or not await self._embed_cache_available():
            return out
        from sqlalchemy import text as sql

        hashes = [sha256_hex(t) for t in texts]
        assert self.db is not None
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    sql(
                        # ::real[] berlaku untuk kolom vector (pgvector) maupun real[] (cadangan)
                        "SELECT text_sha256, embedding::real[] AS e FROM mcp.embedding_cache "
                        "WHERE model = :m AND text_sha256 = ANY(:h)"
                    ),
                    {"m": model, "h": hashes},
                )
            ).all()
        found = {r.text_sha256.strip(): [float(x) for x in r.e] for r in rows}
        return [found.get(h) for h in hashes]

    async def _embed_cache_put(
        self, texts: list[str], vectors: list[list[float]], model: str
    ) -> None:
        if not await self._embed_cache_available():
            return
        from sqlalchemy import text as sql

        assert self.db is not None
        async with self.db.session() as session:
            for text, vec in zip(texts, vectors, strict=True):
                await session.execute(
                    sql(
                        "INSERT INTO mcp.embedding_cache (text_sha256, model, dim, embedding) "
                        "VALUES (:h, :m, :d, CAST(:e AS real[])) ON CONFLICT DO NOTHING"
                    ),
                    {
                        "h": sha256_hex(text),
                        "m": model,
                        "d": len(vec),
                        "e": [float(x) for x in vec],
                    },
                )

    # ------------------------------------------------------------------ audit
    def _record(
        self, purpose: str, route: str, result: ChatResult, status: str, error_code: str | None
    ) -> None:
        LLM_TOKENS.labels(purpose, "prompt").inc(result.prompt_tokens)
        LLM_TOKENS.labels(purpose, "completion").inc(result.completion_tokens)
        if self.db is None:
            return
        row = LlmCall(
            run_id=_uuid_or_none(context.get("run_id")),
            client_id=_uuid_or_none(context.get("client_id")),
            purpose=purpose[:32],
            route=route[:64],
            model_resolved=(result.model_resolved or None) and result.model_resolved[:128],
            gateway_request_id=(result.gateway_request_id or None)
            and result.gateway_request_id[:64],
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cached=result.cached,
            latency_ms=result.latency_ms,
            status=status,
            error_code=error_code,
        )
        task = asyncio.create_task(self._insert(row))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _insert(self, row: LlmCall) -> None:
        assert self.db is not None
        try:
            async with self.db.session() as session:
                session.add(row)
        except Exception as exc:  # noqa: BLE001 - pencatatan tidak boleh menggagalkan panggilan
            log.warning("llm.record_failed", error=str(exc))

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def list_models(self) -> list[str]:
        page = await self.client.models.list()
        return [m.id for m in page.data]

    async def close(self) -> None:
        await self.drain()
        await self.client.close()


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]]
        return "; ".join(parts)
    return truncate(str(exc), 200)


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None
