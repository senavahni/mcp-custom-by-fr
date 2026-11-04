"""toolset intel (kontrak 1.2, Modul 3 Dashboard): intel_jobs, outbox per job, atribusi llm_calls

- `mcp.intel_jobs`: satu baris per job induk `intel_pipeline` (status, tahap, hasil, biaya).
- `mcp.webhook_outbox`: `run_id` boleh NULL + kolom `job_id`; tepat satu terisi; urutan per job.
- `mcp.llm_calls`: `agent_key`, `job_id`, `stage` untuk biaya per agent/tahap (M3_TASK_009).

Revision ID: 0003_intel
Revises: 0002_kb_vector
Create Date: 2026-11-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_intel"
down_revision: str | None = "0002_kb_vector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE mcp.intel_jobs (
      id               varchar(16) PRIMARY KEY REFERENCES mcp.jobs(id) ON DELETE CASCADE,
      dashboard_job_id uuid        NOT NULL UNIQUE,
      workspace_id     uuid        NOT NULL,
      client_id        uuid,
      requested_by     jsonb       NOT NULL DEFAULT '{}',
      spec             jsonb       NOT NULL,
      stage_agents     jsonb       NOT NULL,
      status           varchar(16) NOT NULL DEFAULT 'queued'
                       CHECK (status IN ('queued','collecting','analyzing','indexing','completed',
                                         'failed','cancelling','cancelled')),
      stages           jsonb       NOT NULL DEFAULT '[]',
      event_sequence   integer     NOT NULL DEFAULT 0 CHECK (event_sequence >= 0),
      estimate         jsonb,
      items_handle     varchar(16),
      result           jsonb,
      cost             jsonb       NOT NULL DEFAULT '{}',
      error            jsonb,
      created_at       timestamptz NOT NULL DEFAULT now(),
      updated_at       timestamptz NOT NULL DEFAULT now(),
      finished_at      timestamptz
    )
    """,
    "CREATE INDEX intel_jobs_ws_idx ON mcp.intel_jobs (workspace_id, created_at DESC)",
    "ALTER TABLE mcp.webhook_outbox ALTER COLUMN run_id DROP NOT NULL",
    "ALTER TABLE mcp.webhook_outbox ADD COLUMN job_id varchar(16) "
    "REFERENCES mcp.intel_jobs(id) ON DELETE CASCADE",
    "ALTER TABLE mcp.webhook_outbox ADD CONSTRAINT webhook_outbox_stream_check "
    "CHECK ((run_id IS NULL) <> (job_id IS NULL))",
    "CREATE UNIQUE INDEX webhook_outbox_job_seq_key ON mcp.webhook_outbox (job_id, seq) "
    "WHERE job_id IS NOT NULL",
    "ALTER TABLE mcp.llm_calls ADD COLUMN agent_key varchar(100), "
    "ADD COLUMN job_id varchar(16), ADD COLUMN stage varchar(16)",
    "CREATE INDEX llm_calls_job_idx ON mcp.llm_calls (job_id) WHERE job_id IS NOT NULL",
    "CREATE INDEX llm_calls_agent_idx ON mcp.llm_calls (agent_key, created_at DESC) "
    "WHERE agent_key IS NOT NULL",
]

DOWNGRADE = [
    "DROP INDEX IF EXISTS mcp.llm_calls_agent_idx",
    "DROP INDEX IF EXISTS mcp.llm_calls_job_idx",
    "ALTER TABLE mcp.llm_calls DROP COLUMN IF EXISTS stage, DROP COLUMN IF EXISTS job_id, "
    "DROP COLUMN IF EXISTS agent_key",
    "DELETE FROM mcp.webhook_outbox WHERE job_id IS NOT NULL",
    "ALTER TABLE mcp.webhook_outbox DROP CONSTRAINT IF EXISTS webhook_outbox_stream_check",
    "ALTER TABLE mcp.webhook_outbox DROP COLUMN IF EXISTS job_id",
    "ALTER TABLE mcp.webhook_outbox ALTER COLUMN run_id SET NOT NULL",
    "DROP TABLE IF EXISTS mcp.intel_jobs",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
