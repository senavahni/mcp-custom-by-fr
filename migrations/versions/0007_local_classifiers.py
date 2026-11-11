"""classifier lokal berversi di atas embedding (MCP-V1-04): head sentimen, dst.

`mcp.local_classifiers`: bobot head (JSON), label, metrik evaluasi, ringkasan dataset; tepat satu
versi `active` per nama. Model embedding & dimensi dikunci per versi (vektor beda model tidak
boleh dipakai dengan head ini).

Revision ID: 0007_local_classifiers
Revises: 0006_agent_doctrines
Create Date: 2026-11-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_local_classifiers"
down_revision: str | None = "0006_agent_doctrines"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE mcp.local_classifiers (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      name          varchar(48)  NOT NULL,
      version       integer      NOT NULL CHECK (version >= 1),
      embed_model   varchar(64)  NOT NULL,
      embed_dim     smallint     NOT NULL CHECK (embed_dim > 0),
      labels        text[]       NOT NULL,
      params        jsonb        NOT NULL,
      metrics       jsonb        NOT NULL DEFAULT '{}',
      dataset       jsonb        NOT NULL DEFAULT '{}',
      status        varchar(16)  NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','active','archived')),
      created_at    timestamptz  NOT NULL DEFAULT now(),
      activated_at  timestamptz,
      UNIQUE (name, version)
    )
    """,
    "CREATE UNIQUE INDEX local_classifiers_active_key ON mcp.local_classifiers (name) "
    "WHERE status = 'active'",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mcp.local_classifiers")
