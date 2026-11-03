"""objek KB: embedding_cache & kb_chunks + indeks (DATABASE.md §4.1, §4.8)

Dengan pgvector: kolom `embedding vector` + indeks HNSW parsial per dimensi.
Tanpa pgvector (mode cadangan): kolom `embedding real[]` tanpa HNSW; pencarian vector dilakukan
brute-force di aplikasi. Setelah pgvector dipasang, `manage.py migrate` menjalankan
`services.kb.vector.promote_to_pgvector()` yang mengubah kolom ke `vector` dan membuat indeks.

Revision ID: 0002_kb_vector
Revises: 0001_init
Create Date: 2026-11-03
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0002_kb_vector"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
    CREATE TABLE mcp.embedding_cache (
      text_sha256 char(64)    NOT NULL,
      model       varchar(64) NOT NULL,
      dim         smallint    NOT NULL,
      embedding   {vtype}     NOT NULL,
      created_at  timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (text_sha256, model)
    );
    CREATE TABLE mcp.kb_chunks (
      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      document_id  uuid     NOT NULL REFERENCES mcp.kb_documents(id) ON DELETE CASCADE,
      collection_id uuid    NOT NULL,
      chunk_index  integer  NOT NULL,
      text         text     NOT NULL,
      tokens       integer  NOT NULL,
      embed_dim    smallint NOT NULL,
      embedding    {vtype}  NOT NULL,
      metadata     jsonb    NOT NULL DEFAULT '{{}}',
      text_tsv     tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
      UNIQUE (document_id, chunk_index)
    );
    CREATE INDEX kb_chunks_tsv_idx  ON mcp.kb_chunks USING gin (text_tsv);
    CREATE INDEX kb_chunks_coll_idx ON mcp.kb_chunks (collection_id);
"""

# sama dengan services.kb.vector.HNSW_INDEXES (dipakai saat promosi real[] -> vector)
HNSW_INDEXES = (
    "CREATE INDEX IF NOT EXISTS kb_chunks_hnsw_1536 ON mcp.kb_chunks "
    "USING hnsw ((embedding::vector(1536)) vector_cosine_ops) WHERE embed_dim = 1536",
    "CREATE INDEX IF NOT EXISTS kb_chunks_hnsw_384 ON mcp.kb_chunks "
    "USING hnsw ((embedding::vector(384)) vector_cosine_ops) WHERE embed_dim = 384",
)


def upgrade() -> None:
    bind = op.get_bind()
    has_vector = bind.execute(
        text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
    ).scalar()
    for statement in TABLES.format(vtype="vector" if has_vector else "real[]").split(";"):
        if statement.strip():
            op.execute(statement)
    if has_vector:
        for statement in HNSW_INDEXES:
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mcp.kb_chunks CASCADE")
    op.execute("DROP TABLE IF EXISTS mcp.embedding_cache CASCADE")
