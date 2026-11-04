"""key LLM Gateway per agent (M3_TASK_009): mcp.llm_credentials

Satu baris per agent (`name = agent:<key>`) + satu baris `system`; key disimpan terenkripsi
AES-256-GCM dengan MCP_SECRET_KEY (hanya prefix yang pernah keluar dari MCP).

Revision ID: 0004_llm_credentials
Revises: 0003_intel
Create Date: 2026-11-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_llm_credentials"
down_revision: str | None = "0003_intel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mcp.llm_credentials (
          id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          name            varchar(110) NOT NULL UNIQUE
                          CHECK (name = 'system' OR name = 'agent:' || agent_key),
          agent_key       varchar(100),
          gateway_key_id  uuid,
          key_prefix      varchar(16)  NOT NULL,
          key_enc         text         NOT NULL,
          status          varchar(16)  NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active','revoked','invalid')),
          last_error      varchar(300),
          last_checked_at timestamptz,
          created_at      timestamptz  NOT NULL DEFAULT now(),
          rotated_at      timestamptz
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mcp.llm_credentials")
