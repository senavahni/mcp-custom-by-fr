"""Helper integrasi: Dashboard palsu (webhook + Agent API) dan pembuat run."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from mcp_marketing.models import Run
from mcp_marketing.server.services import Services
from mcp_marketing.services.dashboard import signer
from mcp_marketing.services.seed import seed

WEBHOOK = "http://dashboard.test/api/v1/webhooks/mcp/events"
AGENT_API = "http://dashboard.test/api/v1/agent-api"


@dataclass
class FakeDashboard:
    """Penerima webhook (verifikasi signature, idempoten per event_id) + Agent API sederhana."""

    secret: str
    responses: list[Callable[[dict[str, Any]], httpx.Response]] = field(default_factory=list)
    received: list[dict[str, Any]] = field(default_factory=list)
    processed: set[str] = field(default_factory=set)
    processed_order: list[dict[str, Any]] = field(default_factory=list)
    context_results: list[dict[str, Any]] = field(default_factory=list)
    agent_api_calls: list[httpx.Request] = field(default_factory=list)
    run_status: dict[str, str] = field(default_factory=dict)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "/agent-api/" in request.url.path:
            return self.agent_api(request)
        body = request.content
        check = signer.verify(
            [self.secret],
            request.headers.get("x-mcp-timestamp"),
            request.headers.get("x-mcp-signature"),
            body,
        )
        if check != "ok":
            return httpx.Response(
                401, json={"error": {"code": "INVALID_SIGNATURE", "message": check}}
            )
        payload = json.loads(body)
        self.received.append(payload)
        if self.run_status.get(payload["run_id"]) in ("cancelled", "completed", "failed"):
            return httpx.Response(409, json={"error": {"code": "RUN_NOT_ACTIVE", "message": "x"}})
        if self.responses:
            return self.responses.pop(0)(payload)
        return self.default(payload)

    def default(self, payload: dict[str, Any]) -> httpx.Response:
        if payload["event_id"] in self.processed:
            return httpx.Response(200, json={"status": "duplicate"})
        self.processed.add(payload["event_id"])
        self.processed_order.append(payload)
        data = payload["data"]
        result: dict[str, Any] = {}
        if payload["event_type"] == "agent.block.appended":
            result = {
                "applied": [
                    {"id": op["block"].get("id") or str(uuid.uuid4()), "version": 1}
                    for op in data["operations"]
                ],
                "conflicts": [],
            }
        elif payload["event_type"] == "agent.subtask.created":
            result = {
                "created": [
                    {"client_ref": s.get("client_ref"), "id": str(uuid.uuid4())}
                    for s in data["subtasks"]
                ]
            }
        elif payload["event_type"] == "agent.comment.added":
            result = {"comment_id": str(uuid.uuid4())}
        elif payload["event_type"] in ("agent.task.completed", "agent.task.failed"):
            self.run_status[payload["run_id"]] = payload["event_type"].rsplit(".", 1)[-1]
        return httpx.Response(200, json={"status": "processed", "result": result})

    def agent_api(self, request: httpx.Request) -> httpx.Response:
        self.agent_api_calls.append(request)
        if request.url.path.endswith("/context/query"):
            return httpx.Response(200, json={"results": self.context_results})
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "x"}})

    def events(self, run_id: str | None = None) -> list[str]:
        return [
            p["event_type"] for p in self.processed_order if run_id is None or p["run_id"] == run_id
        ]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def status(code: int, err: str = "X") -> Callable[[dict[str, Any]], httpx.Response]:
    return lambda _p: httpx.Response(code, json={"error": {"code": err, "message": err}})


async def make_run(services: Services, **over: Any) -> str:
    async with services.db.session() as s:
        await seed(s)
    run_id = str(uuid.uuid4())
    async with services.db.session() as s:
        s.add(
            Run(
                run_id=uuid.UUID(run_id),
                mcp_run_id=f"mr_{uuid.uuid4().hex[:10]}",
                trigger="assignment",
                agent_key="strategist",
                agent_id=uuid.uuid4(),
                project_id=uuid.uuid4(),
                task_id=uuid.uuid4(),
                status="running",
                input={},
                callback={"webhook_url": WEBHOOK, "agent_api_base_url": AGENT_API},
                token_budget=1000,
                **over,
            )
        )
    return run_id
