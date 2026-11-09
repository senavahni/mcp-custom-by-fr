"""market intelligence v1: wilayah, KBLI+ISIC, perusahaan, kontak, provenance, import (MCP-V1-02)

- Referensi global (tanpa workspace): `regions` (Kemendagri), `industries` (KBLI 2020),
  `industry_isic_map` (ISIC Rev.4) — di-seed dari `services/market_intel/data/*.csv`.
- Data per workspace (V1-K1/K3): analisis produk & segmen, perusahaan + domain + lokasi + kontak,
  kebutuhan, sinyal, provenance per field, snapshot, import batch/baris, target Contact Finder.
- `industries.embedding`: `vector(1024)` + HNSW bila pgvector terpasang; tanpa pgvector `real[]`
  lalu dipromosikan `services.kb.vector.promote_to_pgvector()` saat `manage.py migrate`.
- `product_id` merujuk `public.products` milik Dashboard (DSH-V1-01) tanpa FK lintas schema.

Revision ID: 0005_market_intel
Revises: 0004_llm_credentials
Create Date: 2026-11-09
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0005_market_intel"
down_revision: str | None = "0004_llm_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = "created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()"
REVIEW = (
    "status varchar(16) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','rejected'))"
)

REFERENCE = [
    f"""
    CREATE TABLE mcp.regions (
      code          varchar(5)   PRIMARY KEY CHECK (code ~ '^\\d{{2}}(\\.\\d{{2}})?$'),
      level         varchar(16)  NOT NULL CHECK (level IN ('province','regency_city')),
      parent_code   varchar(5)   REFERENCES mcp.regions(code),
      name          varchar(100) NOT NULL,
      aliases       text[]       NOT NULL DEFAULT '{{}}',
      centroid_lat  numeric(9,6),
      centroid_lng  numeric(9,6),
      {TS},
      CHECK ((level = 'province') = (parent_code IS NULL))
    )
    """,
    "CREATE INDEX regions_parent_idx ON mcp.regions (parent_code)",
    "CREATE INDEX regions_name_trgm_idx ON mcp.regions USING gin (lower(name) gin_trgm_ops)",
    f"""
    CREATE TABLE mcp.industries (
      kbli_code     varchar(5)   PRIMARY KEY CHECK (kbli_code ~ '^([A-U]|\\d{{2,5}})$'),
      parent_code   varchar(5)   REFERENCES mcp.industries(kbli_code),
      level         varchar(16)  NOT NULL
                    CHECK (level IN ('section','division','group','class','subclass')),
      name_id       varchar(300) NOT NULL,
      name_en       varchar(300),
      description   text,
      embedding     {{vtype}},
      embed_model   varchar(64),
      {TS},
      CHECK ((level = 'section') = (parent_code IS NULL))
    )
    """,
    "CREATE INDEX industries_parent_idx ON mcp.industries (parent_code)",
    """
    CREATE TABLE mcp.industry_isic_map (
      kbli_code     varchar(5)   NOT NULL REFERENCES mcp.industries(kbli_code) ON DELETE CASCADE,
      isic_code     varchar(4)   NOT NULL,
      isic_name     varchar(300) NOT NULL,
      match_level   varchar(16)  NOT NULL CHECK (match_level IN ('exact','class','group','division')),
      PRIMARY KEY (kbli_code, isic_code)
    )
    """,
    "CREATE INDEX industry_isic_isic_idx ON mcp.industry_isic_map (isic_code)",
]

ANALYSIS = [
    f"""
    CREATE TABLE mcp.product_analyses (
      id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workspace_id     uuid        NOT NULL,
      product_id       uuid        NOT NULL,
      job_id           varchar(16),
      region_codes     varchar(5)[] NOT NULL DEFAULT '{{}}',
      mode             varchar(8)  NOT NULL DEFAULT 'review' CHECK (mode IN ('auto','review')),
      {REVIEW},
      kbli_candidates  jsonb       NOT NULL DEFAULT '[]',
      summary          text,
      framework_refs   jsonb       NOT NULL DEFAULT '[]',
      llm_tokens       integer     NOT NULL DEFAULT 0,
      usd_llm          numeric(10,6) NOT NULL DEFAULT 0,
      reviewed_by      uuid,
      reviewed_at      timestamptz,
      {TS},
      deleted_at       timestamptz
    )
    """,
    "CREATE INDEX product_analyses_ws_idx ON mcp.product_analyses (workspace_id, product_id, created_at DESC)",
    f"""
    CREATE TABLE mcp.segments (
      id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      analysis_id      uuid        NOT NULL REFERENCES mcp.product_analyses(id) ON DELETE CASCADE,
      workspace_id     uuid        NOT NULL,
      name             varchar(200) NOT NULL,
      kbli_code        varchar(5)  REFERENCES mcp.industries(kbli_code),
      org_type         varchar(16) CHECK (org_type IN ('company','government','education','health','ngo','other')),
      size_band        varchar(16),
      region_codes     varchar(5)[] NOT NULL DEFAULT '{{}}',
      needs            jsonb       NOT NULL DEFAULT '[]',
      rationale        text,
      confidence       real        CHECK (confidence BETWEEN 0 AND 1),
      queries          jsonb       NOT NULL DEFAULT '{{}}',
      {REVIEW},
      framework_refs   jsonb       NOT NULL DEFAULT '[]',
      {TS}
    )
    """,
    "CREATE INDEX segments_analysis_idx ON mcp.segments (analysis_id)",
    "CREATE INDEX segments_ws_kbli_idx ON mcp.segments (workspace_id, kbli_code)",
]

COMPANIES = [
    f"""
    CREATE TABLE mcp.companies (
      id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workspace_id       uuid         NOT NULL,
      name               varchar(300) NOT NULL,
      legal_name         varchar(300),
      org_type           varchar(16)  NOT NULL DEFAULT 'company'
                         CHECK (org_type IN ('company','government','education','health','ngo','other')),
      primary_domain     varchar(253),
      description_quotes jsonb        NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(description_quotes) = 'array'),
      kbli_code          varchar(5)   REFERENCES mcp.industries(kbli_code),
      size_band          varchar(16),
      completeness       real         NOT NULL DEFAULT 0 CHECK (completeness BETWEEN 0 AND 1),
      status             varchar(16)  NOT NULL DEFAULT 'active' CHECK (status IN ('active','stale','merged')),
      merged_into        uuid         REFERENCES mcp.companies(id),
      last_enriched_at   timestamptz,
      origin             varchar(16)  NOT NULL CHECK (origin IN ('discovery','import','manual')),
      extra              jsonb        NOT NULL DEFAULT '{{}}',
      {TS},
      deleted_at         timestamptz,
      CHECK ((status = 'merged') = (merged_into IS NOT NULL)),
      CHECK (merged_into IS DISTINCT FROM id)
    )
    """,
    # satu perusahaan aktif per domain per workspace; instansi tanpa website boleh NULL
    "CREATE UNIQUE INDEX companies_ws_domain_key ON mcp.companies (workspace_id, primary_domain) "
    "WHERE primary_domain IS NOT NULL AND deleted_at IS NULL AND status <> 'merged'",
    "CREATE INDEX companies_ws_kbli_idx ON mcp.companies (workspace_id, kbli_code)",
    "CREATE INDEX companies_ws_status_idx ON mcp.companies (workspace_id, status, updated_at DESC)",
    "CREATE INDEX companies_name_trgm_idx ON mcp.companies USING gin (lower(name) gin_trgm_ops)",
    """
    CREATE TABLE mcp.company_domains (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      company_id    uuid         NOT NULL REFERENCES mcp.companies(id) ON DELETE CASCADE,
      workspace_id  uuid         NOT NULL,
      domain        varchar(253) NOT NULL CHECK (domain = lower(domain)),
      is_primary    boolean      NOT NULL DEFAULT false,
      verification  varchar(8)   NOT NULL DEFAULT 'auto' CHECK (verification IN ('auto','manual')),
      confidence    real         CHECK (confidence BETWEEN 0 AND 1),
      created_at    timestamptz  NOT NULL DEFAULT now(),
      UNIQUE (workspace_id, domain)
    )
    """,
    "CREATE INDEX company_domains_company_idx ON mcp.company_domains (company_id)",
    f"""
    CREATE TABLE mcp.company_locations (
      id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      company_id      uuid         NOT NULL REFERENCES mcp.companies(id) ON DELETE CASCADE,
      workspace_id    uuid         NOT NULL,
      kind            varchar(16)  NOT NULL DEFAULT 'head_office'
                      CHECK (kind IN ('head_office','branch','listing','other')),
      address_raw     text,
      street          varchar(300),
      region_code     varchar(5)   REFERENCES mcp.regions(code),
      postal_code     varchar(10),
      lat             numeric(9,6) CHECK (lat BETWEEN -90 AND 90),
      lng             numeric(9,6) CHECK (lng BETWEEN -180 AND 180),
      geocode_source  varchar(16)  CHECK (geocode_source IN ('jsonld','gmaps','nominatim','import','manual','centroid')),
      precision       varchar(16)  CHECK (precision IN ('rooftop','street','postal_code','city','province')),
      {TS},
      CHECK ((lat IS NULL) = (lng IS NULL))
    )
    """,
    "CREATE INDEX company_locations_company_idx ON mcp.company_locations (company_id)",
    "CREATE INDEX company_locations_region_idx ON mcp.company_locations (region_code)",
    "CREATE INDEX company_locations_latlng_idx ON mcp.company_locations (workspace_id, lat, lng) "
    "WHERE lat IS NOT NULL",
    f"""
    CREATE TABLE mcp.contacts (
      id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      company_id           uuid         NOT NULL REFERENCES mcp.companies(id) ON DELETE CASCADE,
      workspace_id         uuid         NOT NULL,
      kind                 varchar(16)  NOT NULL CHECK (kind IN ('email','phone','whatsapp','social','form')),
      value_normalized     varchar(500) NOT NULL,
      contact_type         varchar(16)  NOT NULL DEFAULT 'generic'
                           CHECK (contact_type IN ('generic','personal','role')),
      person_name          varchar(200),
      job_title            varchar(200),
      source_url           text,
      verification_status  varchar(16)  NOT NULL DEFAULT 'unverified'
                           CHECK (verification_status IN ('unverified','format_ok','mx_ok','invalid')),
      review_status        varchar(16)  NOT NULL DEFAULT 'not_required'
                           CHECK (review_status IN ('not_required','pending','approved','rejected')),
      {TS},
      deleted_at           timestamptz,
      UNIQUE (company_id, kind, value_normalized),
      -- kontak personal wajib melewati review manusia (V1-K6)
      CHECK (contact_type <> 'personal' OR review_status <> 'not_required')
    )
    """,
    "CREATE INDEX contacts_ws_review_idx ON mcp.contacts (workspace_id, review_status) "
    "WHERE deleted_at IS NULL",
    """
    CREATE TABLE mcp.contact_suppressions (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workspace_id  uuid        NOT NULL,
      value_hash    char(64)    NOT NULL,
      reason        varchar(48) NOT NULL,
      created_by    varchar(100) NOT NULL,
      created_at    timestamptz NOT NULL DEFAULT now(),
      UNIQUE (workspace_id, value_hash)
    )
    """,
]

PROVENANCE = [
    f"""
    CREATE TABLE mcp.import_batches (
      id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workspace_id    uuid         NOT NULL,
      upload_id       uuid         NOT NULL,
      job_id          varchar(16),
      filename        varchar(255) NOT NULL,
      sheet           varchar(100),
      rows_total      integer      NOT NULL DEFAULT 0 CHECK (rows_total >= 0),
      rows_ok         integer      NOT NULL DEFAULT 0 CHECK (rows_ok >= 0),
      rows_review     integer      NOT NULL DEFAULT 0 CHECK (rows_review >= 0),
      rows_failed     integer      NOT NULL DEFAULT 0 CHECK (rows_failed >= 0),
      column_mapping  jsonb        NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(column_mapping) = 'array'),
      mode            varchar(16)  NOT NULL DEFAULT 'fill_db' CHECK (mode IN ('fill_db','contact_targets')),
      status          varchar(16)  NOT NULL DEFAULT 'profiling'
                      CHECK (status IN ('profiling','mapping','importing','completed','failed','rolled_back')),
      rolled_back_at  timestamptz,
      {TS},
      CHECK (rows_ok + rows_review + rows_failed <= rows_total)
    )
    """,
    "CREATE INDEX import_batches_ws_idx ON mcp.import_batches (workspace_id, created_at DESC)",
    f"""
    CREATE TABLE mcp.import_rows (
      id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      batch_id    uuid        NOT NULL REFERENCES mcp.import_batches(id) ON DELETE CASCADE,
      row_no      integer     NOT NULL CHECK (row_no >= 1),
      raw         jsonb       NOT NULL,
      normalized  jsonb,
      status      varchar(16) NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','inserted','merged','review','failed')),
      company_id  uuid        REFERENCES mcp.companies(id) ON DELETE SET NULL,
      issues      text[]      NOT NULL DEFAULT '{{}}',
      {TS},
      UNIQUE (batch_id, row_no)
    )
    """,
    "CREATE INDEX import_rows_batch_status_idx ON mcp.import_rows (batch_id, status)",
    """
    CREATE TABLE mcp.source_records (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workspace_id   uuid        NOT NULL,
      entity_type    varchar(32) NOT NULL,
      entity_id      uuid        NOT NULL,
      field          varchar(64) NOT NULL,
      value_hash     char(64)    NOT NULL,
      source_url     text,
      source_kind    varchar(16) NOT NULL
                     CHECK (source_kind IN ('web','gmaps','google_search','nominatim','social','import','manual')),
      method         varchar(32) NOT NULL,
      collected_at   timestamptz NOT NULL DEFAULT now(),
      verified_at    timestamptz,
      reliability    real        CHECK (reliability BETWEEN 0 AND 1),
      job_id         varchar(16),
      import_row_id  uuid        REFERENCES mcp.import_rows(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX source_records_entity_idx ON mcp.source_records (entity_type, entity_id, field)",
    "CREATE INDEX source_records_job_idx ON mcp.source_records (job_id) WHERE job_id IS NOT NULL",
    f"""
    CREATE TABLE mcp.company_needs (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      company_id    uuid        NOT NULL REFERENCES mcp.companies(id) ON DELETE CASCADE,
      workspace_id  uuid        NOT NULL,
      need          text        NOT NULL,
      category      varchar(48),
      evidence      jsonb       NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(evidence) = 'array'),
      confidence    real        CHECK (confidence BETWEEN 0 AND 1),
      method        varchar(8)  NOT NULL CHECK (method IN ('local','llm')),
      {TS}
    )
    """,
    "CREATE INDEX company_needs_company_idx ON mcp.company_needs (company_id)",
    """
    CREATE TABLE mcp.market_signals (
      id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workspace_id      uuid        NOT NULL,
      kind              varchar(24) NOT NULL
                        CHECK (kind IN ('need','complaint','purchase_intent','competitor_mention')),
      subject_type      varchar(16) NOT NULL,
      subject_id        varchar(64) NOT NULL,
      text              text        NOT NULL,
      strength          real        CHECK (strength BETWEEN 0 AND 1),
      observed_at       timestamptz NOT NULL,
      source_record_id  uuid        REFERENCES mcp.source_records(id) ON DELETE SET NULL,
      created_at        timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX market_signals_ws_idx ON mcp.market_signals (workspace_id, kind, observed_at DESC)",
    "CREATE INDEX market_signals_subject_idx ON mcp.market_signals (subject_type, subject_id)",
    """
    CREATE TABLE mcp.company_snapshots (
      id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      company_id       uuid        NOT NULL REFERENCES mcp.companies(id) ON DELETE CASCADE,
      workspace_id     uuid        NOT NULL,
      version          integer     NOT NULL CHECK (version >= 1),
      data             jsonb       NOT NULL,
      reason           varchar(16) NOT NULL
                       CHECK (reason IN ('enrich','merge','import','manual','rollback','refresh')),
      job_id           varchar(16),
      import_batch_id  uuid        REFERENCES mcp.import_batches(id) ON DELETE SET NULL,
      created_at       timestamptz NOT NULL DEFAULT now(),
      UNIQUE (company_id, version)
    )
    """,
    "CREATE INDEX company_snapshots_batch_idx ON mcp.company_snapshots (import_batch_id) "
    "WHERE import_batch_id IS NOT NULL",
    f"""
    CREATE TABLE mcp.contact_finder_targets (
      id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workspace_id     uuid        NOT NULL,
      company_id       uuid        NOT NULL UNIQUE REFERENCES mcp.companies(id) ON DELETE CASCADE,
      import_batch_id  uuid        REFERENCES mcp.import_batches(id) ON DELETE SET NULL,
      priority         smallint    NOT NULL DEFAULT 0,
      hint             jsonb       NOT NULL DEFAULT '{{}}',
      status           varchar(16) NOT NULL DEFAULT 'queued'
                       CHECK (status IN ('queued','found','ambiguous','not_found','needs_hint')),
      candidates       jsonb       NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(candidates) = 'array'),
      attempts         integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
      last_attempt_at  timestamptz,
      {TS}
    )
    """,
    "CREATE INDEX contact_finder_queue_idx ON mcp.contact_finder_targets "
    "(workspace_id, status, priority DESC, created_at)",
    """
    CREATE TABLE mcp.geocode_cache (
      query_hash  char(64)    PRIMARY KEY,
      provider    varchar(16) NOT NULL CHECK (provider IN ('nominatim','gmaps')),
      result      jsonb       NOT NULL,
      fetched_at  timestamptz NOT NULL DEFAULT now(),
      expires_at  timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE mcp.search_cache (
      query_hash  char(64)    PRIMARY KEY,
      provider    varchar(16) NOT NULL CHECK (provider IN ('google_search','google_maps')),
      query       text        NOT NULL,
      result      jsonb       NOT NULL,
      fetched_at  timestamptz NOT NULL DEFAULT now(),
      expires_at  timestamptz NOT NULL
    )
    """,
    "CREATE INDEX search_cache_expires_idx ON mcp.search_cache (expires_at)",
]

TABLES_DOWN = (
    "search_cache",
    "geocode_cache",
    "contact_finder_targets",
    "company_snapshots",
    "market_signals",
    "company_needs",
    "source_records",
    "import_rows",
    "import_batches",
    "contact_suppressions",
    "contacts",
    "company_locations",
    "company_domains",
    "companies",
    "segments",
    "product_analyses",
    "industry_isic_map",
    "industries",
    "regions",
)

# sama dengan services.kb.vector.FIXED_DIM_TABLES (dipakai juga saat promosi real[] -> vector)
HNSW_INDUSTRIES = (
    "CREATE INDEX IF NOT EXISTS industries_embedding_hnsw ON mcp.industries "
    "USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL"
)


def _has_extension(name: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(text("SELECT 1 FROM pg_extension WHERE extname = :n"), {"n": name}).scalar()
    )


def upgrade() -> None:
    if not _has_extension("pg_trgm"):
        # trusted extension (PG13+): cukup hak CREATE di database; bila gagal jalankan bootstrap.sql
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    has_vector = _has_extension("vector")
    vtype = "vector(1024)" if has_vector else "real[]"
    for statement in REFERENCE + ANALYSIS + COMPANIES + PROVENANCE:
        op.execute(statement.replace("{vtype}", vtype))
    if has_vector:
        op.execute(HNSW_INDUSTRIES)

    from mcp_marketing.services.market_intel.reference_data import load_reference_data

    load_reference_data(op.get_bind())


def downgrade() -> None:
    for table in TABLES_DOWN:
        op.execute(f"DROP TABLE IF EXISTS mcp.{table} CASCADE")
