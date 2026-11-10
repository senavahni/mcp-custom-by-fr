"""doktrin agent per peran + versi doktrin yang dipakai run (MCP-V1-03 injeksi, isi MCP-V1-13)

- `mcp.agent_doctrines`: L2 "doktrin" (V1-K11) per peran (= agent_key: analyst, strategist,
  creative, ...), berversi; tepat satu versi `active` per (workspace, peran). `workspace_id`
  NULL = doktrin global (cadangan bila workspace belum punya doktrin sendiri).
- `mcp.runs.doctrine`: `{id, role, version, tokens}` doktrin yang disuntikkan ke prompt run.

Revision ID: 0006_agent_doctrines
Revises: 0005_market_intel
Create Date: 2026-11-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_agent_doctrines"
down_revision: str | None = "0005_market_intel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE mcp.agent_doctrines (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workspace_id  uuid,
      role          varchar(100) NOT NULL,
      version       integer      NOT NULL CHECK (version >= 1),
      content       text         NOT NULL,
      tokens        integer      NOT NULL CHECK (tokens BETWEEN 1 AND 1500),
      card_ids      uuid[]       NOT NULL DEFAULT '{}',
      status        varchar(16)  NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','active','rejected','superseded')),
      created_by    varchar(100) NOT NULL,
      approved_by   uuid,
      approved_at   timestamptz,
      created_at    timestamptz  NOT NULL DEFAULT now(),
      updated_at    timestamptz  NOT NULL DEFAULT now(),
      CHECK (status <> 'active' OR approved_at IS NOT NULL)
    )
    """,
    "CREATE UNIQUE INDEX agent_doctrines_version_key ON mcp.agent_doctrines "
    "(workspace_id, role, version) NULLS NOT DISTINCT",
    # versi baru perlu approve manusia; hanya satu yang aktif per peran
    "CREATE UNIQUE INDEX agent_doctrines_active_key ON mcp.agent_doctrines "
    "(workspace_id, role) NULLS NOT DISTINCT WHERE status = 'active'",
    "ALTER TABLE mcp.runs ADD COLUMN doctrine jsonb",
]

DOWNGRADE = [
    "ALTER TABLE mcp.runs DROP COLUMN IF EXISTS doctrine",
    "DROP TABLE IF EXISTS mcp.agent_doctrines",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
