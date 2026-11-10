"""Injeksi doktrin per peran ke prompt agent (MCP-V1-03; isi doktrin dari MCP-V1-13, V1-K11).

Saat run dimulai: doktrin `active` untuk peran (= agent_key) di workspace run — cadangan doktrin
global (`workspace_id` NULL) — disisipkan SETELAH prompt peran dan SEBELUM aturan umum, jatah
±1.500 token. Urutan prompt dibuat stabil (peran -> doktrin -> aturan -> format) agar awalan yang
sama per (agent, versi doktrin) bisa di-cache provider (prompt caching otomatis berbasis awalan).
Versi doktrin dicatat di `runs.doctrine`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import AgentDoctrine
from mcp_marketing.services.agent.profiles import PROMPT_VERSION
from mcp_marketing.services.llm import prompts
from mcp_marketing.utils.tokens import count_tokens, truncate_to_tokens

log = get_logger(__name__)

DOCTRINE_MAX_TOKENS = 1_500
DOCTRINE_HEADER = (
    "Doktrin peran (prinsip & framework dari buku/brand yang disetujui; ikuti bila relevan, "
    "sebutkan framework yang dipakai):"
)


@dataclass(frozen=True)
class ActiveDoctrine:
    id: uuid.UUID
    role: str
    version: int
    content: str
    tokens: int

    def run_record(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "role": self.role,
            "version": self.version,
            "tokens": self.tokens,
        }


async def load_active_doctrine(
    session: AsyncSession, workspace_id: uuid.UUID | None, role: str
) -> ActiveDoctrine | None:
    """Doktrin aktif peran: milik workspace lebih diutamakan daripada doktrin global."""
    scope: ColumnElement[bool] = AgentDoctrine.workspace_id.is_(None)
    if workspace_id is not None:
        scope = or_(AgentDoctrine.workspace_id == workspace_id, scope)
    rows = (
        await session.scalars(
            select(AgentDoctrine).where(
                AgentDoctrine.role == role, AgentDoctrine.status == "active", scope
            )
        )
    ).all()
    if not rows:
        return None
    row = min(rows, key=lambda r: r.workspace_id is None)  # workspace dulu, lalu global
    content = row.content.strip()
    tokens = count_tokens(content)
    if tokens > DOCTRINE_MAX_TOKENS:
        log.warning("doctrine.truncated", role=role, version=row.version, tokens=tokens)
        content = truncate_to_tokens(content, DOCTRINE_MAX_TOKENS)
        tokens = count_tokens(content)
    return ActiveDoctrine(row.id, row.role, row.version, content, tokens)


def compose_with_doctrine(system_prompt: str, doctrine: ActiveDoctrine | None) -> str:
    """`peran + doktrin + aturan umum`; tanpa doktrin prompt tidak berubah."""
    if doctrine is None:
        return system_prompt
    rules = prompts.load("agent_rules", PROMPT_VERSION).strip()
    block = (
        f"{DOCTRINE_HEADER}\n"
        f'<doctrine role="{doctrine.role}" version="{doctrine.version}">\n'
        f"{doctrine.content}\n</doctrine>"
    )
    text = system_prompt.strip()
    if text.endswith(rules):  # profil standar = peran + aturan umum (profiles/profile_admin)
        role = text[: -len(rules)].strip()
        return f"{role}\n\n{block}\n\n{rules}"
    return f"{text}\n\n{block}"
