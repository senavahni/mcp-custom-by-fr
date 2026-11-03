"""Test double bersama: Gateway palsu (transport httpx2 untuk SDK openai 3.x) & LLM berskrip."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
from pydantic import BaseModel

from mcp_marketing.core.errors import UpstreamError
from mcp_marketing.services.llm.gateway_client import ChatResult, ToolCallRequest
from mcp_marketing.utils.tokens import count_tokens


def reply(
    *calls: tuple[str, dict[str, Any]], content: str | None = None, tokens: int = 100
) -> ChatResult:
    """ChatResult berisi tool call berurutan, mis. reply(("finish", {"summary": "ok"}))."""
    reqs = [
        ToolCallRequest(
            id=f"call_{i}_{name}", name=name, arguments=args, raw_arguments=json.dumps(args)
        )
        for i, (name, args) in enumerate(calls)
    ]
    return ChatResult(
        content=content,
        tool_calls=reqs,
        finish_reason="tool_calls" if reqs else "stop",
        prompt_tokens=tokens,
        completion_tokens=tokens // 4,
        model_resolved="fake/model",
        route="smart",
    )


@dataclass
class ScriptedLLM:
    """Pengganti GatewayClient: balasan `agent_loop` diambil berurutan dari `script`.

    Item script boleh ChatResult, Exception (dilempar), atau asyncio.Event (menunggu sampai di-set,
    untuk mensimulasikan panggilan LLM yang lama). Budget di-reserve/commit seperti klien asli.
    """

    script: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    json_reply: Any = None
    compact_text: str = "Ringkasan: data h_0123456789 sudah dianalisis."

    async def chat(
        self,
        purpose: str,
        messages: list[dict[str, Any]],
        *,
        tools: Any = None,
        budget: Any = None,
        **kw: Any,
    ) -> ChatResult:
        self.calls.append(
            {
                "purpose": purpose,
                "messages": copy.deepcopy(messages),
                "tools": [t["function"]["name"] for t in tools or []],
                **kw,
            }
        )
        if purpose == "compact":
            result = ChatResult(
                content=self.compact_text, prompt_tokens=200, completion_tokens=40, route="fast"
            )
        else:
            item = (
                self.script.pop(0)
                if self.script
                else reply(("finish", {"summary": "selesai otomatis"}))
            )
            if isinstance(item, asyncio.Event):
                await item.wait()
                item = (
                    self.script.pop(0) if self.script else reply(("finish", {"summary": "lanjut"}))
                )
            if isinstance(item, BaseException):
                raise item
            result = item
        if budget is not None:
            reservation = await budget.reserve(count_tokens(messages) + 500)
            await budget.commit(reservation, result.total_tokens)
        return result

    async def chat_json(
        self, purpose: str, messages: list[dict[str, Any]], schema: type[BaseModel], **kw: Any
    ) -> Any:
        self.calls.append({"purpose": purpose, "messages": copy.deepcopy(messages)})
        if isinstance(self.json_reply, BaseException):
            raise self.json_reply
        if self.json_reply is None:
            raise UpstreamError("tidak ada balasan json")
        return schema.model_validate(self.json_reply), ChatResult(
            content="{}", prompt_tokens=80, completion_tokens=20
        )

    def agent_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["purpose"] == "agent_loop"]


Reply = httpx2.Response | Callable[[httpx2.Request], httpx2.Response]


def completion(
    content: str | None = "halo",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    prompt: int = 10,
    completion_tokens: int = 5,
    finish: str = "stop",
    model: str = "openai/gpt-test",
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt + completion_tokens,
        },
    }


def tool_call(call_id: str, name: str, args: dict[str, Any] | str) -> dict[str, Any]:
    raw = args if isinstance(args, str) else json.dumps(args)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}


def ok(body: dict[str, Any], headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(200, json=body, headers=headers or {})


def err(status: int, code: str, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        status, json={"error": {"message": code, "type": "x", "code": code}}, headers=headers or {}
    )


@dataclass
class FakeGateway:
    """Antrian balasan per path; balasan terakhir diulang bila antrian habis."""

    replies: dict[str, list[Reply]] = field(default_factory=dict)
    calls: list[httpx2.Request] = field(default_factory=list)

    def on(self, path: str, *replies: Reply) -> FakeGateway:
        self.replies.setdefault(path, []).extend(replies)
        return self

    def calls_to(self, path: str) -> list[httpx2.Request]:
        return [c for c in self.calls if c.url.path.endswith(path)]

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [json.loads(c.content) for c in self.calls_to(path)]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(request)
        for path, queue in self.replies.items():
            if request.url.path.endswith(path) and queue:
                reply = queue.pop(0) if len(queue) > 1 else queue[0]
                return reply(request) if callable(reply) else reply
        return httpx2.Response(
            404, json={"error": {"code": "not_mocked", "message": request.url.path}}
        )

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))
