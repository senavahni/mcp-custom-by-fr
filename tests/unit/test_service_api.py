from __future__ import annotations

import httpx
import pytest

from mcp_marketing.core.config import get_settings
from mcp_marketing.core.errors import (
    Conflict,
    Forbidden,
    HierarchyDepthExceeded,
    NotFound,
    RateLimited,
    ToolError,
    UpstreamError,
    ValidationFailed,
)
from mcp_marketing.services.dashboard.service_api import DashboardServiceApi


async def test_service_api_sends_contract_headers_and_maps_success() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"X-Contract-Version": "1.1"},
            json={"data": []},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DashboardServiceApi(get_settings(), http)
        result = await client.list_projects("agent-id", "workspace-id", limit=10)

    assert result == {"data": []}
    request = seen[0]
    assert request.headers["authorization"].startswith("Bearer ")
    assert request.headers["x-mcp-agent-id"] == "agent-id"
    assert request.url.path == "/api/v1/mcp-api/workspaces/workspace-id/projects"
    assert request.url.params["limit"] == "10"


@pytest.mark.parametrize(
    ("status", "code", "error_type"),
    [
        (401, "UNAUTHORIZED", UpstreamError),
        (403, "FORBIDDEN", Forbidden),
        (404, "NOT_FOUND", NotFound),
        (409, "CONFLICT", Conflict),
        (422, "HIERARCHY_DEPTH_EXCEEDED", HierarchyDepthExceeded),
        (422, "VALIDATION_ERROR", ValidationFailed),
        (429, "RATE_LIMITED", RateLimited),
    ],
)
def test_service_api_maps_dashboard_errors(
    status: int, code: str, error_type: type[ToolError]
) -> None:
    response = httpx.Response(
        status,
        request=httpx.Request("GET", "http://dashboard.test"),
        json={"error": {"code": code, "message": "failed"}},
    )

    assert isinstance(DashboardServiceApi._error(response), error_type)
