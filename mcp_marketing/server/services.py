"""Container dependensi bersama proses server/worker (dibangun di lifespan, disuntik ke tool)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from mcp_marketing.core.config import Settings
from mcp_marketing.core.database import Database
from mcp_marketing.services.agent.budget import BudgetManager
from mcp_marketing.services.results.cache import ResultCache
from mcp_marketing.services.results.result_store import ResultStore

if TYPE_CHECKING:
    from mcp_marketing.services.agent.runtime import AgentRuntime
    from mcp_marketing.services.dashboard.agent_api import AgentApiClient
    from mcp_marketing.services.dashboard.outbox import Outbox
    from mcp_marketing.services.dashboard.service_api import DashboardServiceApi
    from mcp_marketing.services.llm.gateway_client import GatewayClient
    from mcp_marketing.worker.queue import JobQueue


@dataclass
class Services:
    settings: Settings
    db: Database
    http: httpx.AsyncClient
    results: ResultStore
    cache: ResultCache
    budgets: BudgetManager
    llm: GatewayClient | None = None
    agent_api: AgentApiClient | None = None
    service_api: DashboardServiceApi | None = None
    outbox: Outbox | None = None
    jobs: JobQueue | None = None
    runtime: AgentRuntime | None = None
    extras: dict[str, Any] = field(default_factory=dict)
