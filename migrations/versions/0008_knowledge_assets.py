"""upload file + aset pengetahuan (buku/brand) + stream event entitas di outbox (MCP-V1-11)

- `mcp.uploads`: file dari `POST /uploads` (dedupe sha256 per workspace; nama asli hanya
  metadata, tidak pernah dipakai sebagai path).
- `mcp.knowledge_assets` + `mcp.knowledge_pages`: metadata aset dan teks per halaman
  (`needs_ocr` untuk halaman scan; OCR = P2).
- `mcp.webhook_outbox.stream`: aliran ketiga (selain run_id/job_id) untuk event entitas
  kontrak 1.3 (`kn.asset.status`, `ci.import.status`, ...), mis. `asset:<id>`.

Revision ID: 0008_knowledge_assets
Revises: 0007_local_classifiers
Create Date: 2026-11-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_knowledge_assets"
down_revision: str | None = "0007_local_classifiers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE TABLE mcp.uploads (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workspace_id   uuid         NOT NULL,
      sha256         char(64)     NOT NULL,
      kind           varchar(8)   NOT NULL CHECK (kind IN ('pdf','xlsx','csv')),
      purpose        varchar(16)  NOT NULL CHECK (purpose IN ('book','brand','article','import')),
      size_bytes     bigint       NOT NULL CHECK (size_bytes > 0),
      path           text         NOT NULL,
      original_name  varchar(255),
      pages          integer      CHECK (pages IS NULL OR pages > 0),
      uploaded_by    varchar(100),
      created_at     timestamptz  NOT NULL DEFAULT now(),
      UNIQUE (workspace_id, sha256)
    )
    """,
    """
    CREATE TABLE mcp.knowledge_assets (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workspace_id   uuid         NOT NULL,
      upload_id      uuid         NOT NULL REFERENCES mcp.uploads(id),
      kind           varchar(16)  NOT NULL CHECK (kind IN ('book','brand','article')),
      title          varchar(300) NOT NULL,
      authors        text[]       NOT NULL DEFAULT '{}',
      edition        varchar(50),
      year           smallint     CHECK (year IS NULL OR year BETWEEN 1800 AND 2100),
      isbn           varchar(20),
      language       varchar(8)   NOT NULL DEFAULT 'id',
      license_note   text,
      sha256         char(64)     NOT NULL,
      pages          integer,
      status         varchar(16)  NOT NULL DEFAULT 'registered'
                     CHECK (status IN ('registered','extracting','embedding','carding','ready','failed')),
      stats          jsonb        NOT NULL DEFAULT '{}',
      collection_id  uuid,
      error          jsonb,
      created_at     timestamptz  NOT NULL DEFAULT now(),
      updated_at     timestamptz  NOT NULL DEFAULT now(),
      deleted_at     timestamptz
    )
    """,
    "CREATE UNIQUE INDEX knowledge_assets_ws_sha_key ON mcp.knowledge_assets "
    "(workspace_id, sha256) WHERE deleted_at IS NULL",
    "CREATE INDEX knowledge_assets_ws_idx ON mcp.knowledge_assets (workspace_id, created_at DESC)",
    """
    CREATE TABLE mcp.knowledge_pages (
      asset_id   uuid     NOT NULL REFERENCES mcp.knowledge_assets(id) ON DELETE CASCADE,
      page_no    integer  NOT NULL CHECK (page_no >= 1),
      text       text     NOT NULL,
      needs_ocr  boolean  NOT NULL DEFAULT false,
      chapter    varchar(300),
      section    varchar(300),
      PRIMARY KEY (asset_id, page_no)
    )
    """,
    "ALTER TABLE mcp.webhook_outbox ADD COLUMN stream varchar(80)",
    "ALTER TABLE mcp.webhook_outbox DROP CONSTRAINT webhook_outbox_stream_check",
    "ALTER TABLE mcp.webhook_outbox ADD CONSTRAINT webhook_outbox_stream_check "
    "CHECK (num_nonnulls(run_id, job_id, stream) = 1)",
    "CREATE UNIQUE INDEX webhook_outbox_entity_seq_key ON mcp.webhook_outbox (stream, seq) "
    "WHERE stream IS NOT NULL",
]

DOWNGRADE = [
    "DELETE FROM mcp.webhook_outbox WHERE stream IS NOT NULL",
    "DROP INDEX IF EXISTS mcp.webhook_outbox_entity_seq_key",
    "ALTER TABLE mcp.webhook_outbox DROP CONSTRAINT IF EXISTS webhook_outbox_stream_check",
    "ALTER TABLE mcp.webhook_outbox ADD CONSTRAINT webhook_outbox_stream_check "
    "CHECK ((run_id IS NULL) <> (job_id IS NULL))",
    "ALTER TABLE mcp.webhook_outbox DROP COLUMN IF EXISTS stream",
    "DROP TABLE IF EXISTS mcp.knowledge_pages",
    "DROP TABLE IF EXISTS mcp.knowledge_assets",
    "DROP TABLE IF EXISTS mcp.uploads",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
