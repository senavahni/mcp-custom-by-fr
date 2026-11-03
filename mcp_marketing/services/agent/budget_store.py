"""Sumber data budget dari DB: pemakaian harian klien (`llm_calls`) dan budget run (`runs`)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from mcp_marketing.core.database import Database
from mcp_marketing.models import LlmCall, Run
from mcp_marketing.services.agent.budget import BudgetManager


def db_budget_manager(db: Database, timezone: str = "UTC") -> BudgetManager:
    async def client_used(client_id: str, day: date) -> int:
        start = datetime.combine(day, time.min, tzinfo=ZoneInfo(timezone))
        async with db.session() as session:
            used = await session.scalar(
                select(
                    func.coalesce(func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens), 0)
                ).where(LlmCall.client_id == uuid.UUID(client_id), LlmCall.created_at >= start)
            )
        return int(used or 0)

    async def run_loader(run_id: str) -> tuple[int, int] | None:
        try:
            key = uuid.UUID(run_id)
        except ValueError:
            return None
        async with db.session() as session:
            row = (
                await session.execute(
                    select(Run.tokens_used, Run.token_budget).where(Run.run_id == key)
                )
            ).first()
        return (int(row.tokens_used), int(row.token_budget)) if row else None

    async def run_persister(run_id: str, used: int) -> None:
        async with db.session() as session:
            await session.execute(
                update(Run).where(Run.run_id == uuid.UUID(run_id)).values(tokens_used=used)
            )

    return BudgetManager(
        client_used_loader=client_used, run_loader=run_loader, run_persister=run_persister
    )
