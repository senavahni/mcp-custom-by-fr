"""init schema mcp (DATABASE.md §4.1–4.8, tanpa objek pgvector -> 0002_kb_vector)

Revision ID: 0001_init
Revises:
Create Date: 2026-11-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = [
    # ---------- 4.1 klien, audit & pemakaian ----------
    """
    CREATE TABLE mcp.api_clients (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      name                varchar(100) NOT NULL UNIQUE,
      kind                varchar(16)  NOT NULL DEFAULT 'external' CHECK (kind IN ('system','external','agent')),
      key_prefix          varchar(8)   NOT NULL UNIQUE,
      key_hash            char(64)     NOT NULL,
      toolsets            text[]       NOT NULL DEFAULT '{}',
      is_admin            boolean      NOT NULL DEFAULT false,
      dashboard_agent_id  uuid,
      workspace_id        uuid,
      daily_token_budget  integer      CHECK (daily_token_budget IS NULL OR daily_token_budget > 0),
      rpm_limit           integer      NOT NULL DEFAULT 120,
      status              varchar(16)  NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
      expires_at          timestamptz,
      last_used_at        timestamptz,
      created_at          timestamptz  NOT NULL DEFAULT now(),
      updated_at          timestamptz  NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE mcp.tool_calls (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      request_id     varchar(64)  NOT NULL,
      client_id      uuid REFERENCES mcp.api_clients(id),
      run_id         uuid,
      tool           varchar(64)  NOT NULL,
      args_hash      char(64)     NOT NULL,
      args_preview   jsonb        NOT NULL DEFAULT '{}',
      status         varchar(16)  NOT NULL CHECK (status IN ('ok','error','cached','job')),
      error_code     varchar(48),
      duration_ms    integer      NOT NULL,
      output_tokens  integer      NOT NULL DEFAULT 0,
      baseline_tokens integer,
      llm_tokens     integer      NOT NULL DEFAULT 0,
      job_id         varchar(16),
      created_at     timestamptz  NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX tool_calls_created_idx ON mcp.tool_calls (created_at DESC)",
    "CREATE INDEX tool_calls_tool_idx    ON mcp.tool_calls (tool, created_at DESC)",
    "CREATE INDEX tool_calls_run_idx     ON mcp.tool_calls (run_id) WHERE run_id IS NOT NULL",
    """
    CREATE TABLE mcp.llm_calls (
      id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tool_call_id      uuid REFERENCES mcp.tool_calls(id) ON DELETE SET NULL,
      run_id            uuid,
      client_id         uuid,
      purpose           varchar(32)  NOT NULL,
      route             varchar(64)  NOT NULL,
      model_resolved    varchar(128),
      gateway_request_id varchar(64),
      prompt_tokens     integer NOT NULL DEFAULT 0,
      completion_tokens integer NOT NULL DEFAULT 0,
      cached            boolean NOT NULL DEFAULT false,
      cost_usd          numeric(12,6),
      latency_ms        integer NOT NULL,
      status            varchar(16) NOT NULL CHECK (status IN ('ok','error')),
      error_code        varchar(48),
      created_at        timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX llm_calls_created_idx ON mcp.llm_calls (created_at DESC)",
    "CREATE INDEX llm_calls_run_idx     ON mcp.llm_calls (run_id) WHERE run_id IS NOT NULL",
    """
    CREATE TABLE mcp.usage_daily (
      day            date        NOT NULL,
      client_id      uuid        NOT NULL,
      tool           varchar(64) NOT NULL,
      calls          integer     NOT NULL DEFAULT 0,
      errors         integer     NOT NULL DEFAULT 0,
      cache_hits     integer     NOT NULL DEFAULT 0,
      output_tokens  bigint      NOT NULL DEFAULT 0,
      baseline_tokens bigint     NOT NULL DEFAULT 0,
      llm_tokens     bigint      NOT NULL DEFAULT 0,
      cost_usd       numeric(14,6) NOT NULL DEFAULT 0,
      external_units bigint      NOT NULL DEFAULT 0,
      PRIMARY KEY (day, client_id, tool)
    )
    """,
    """
    CREATE TABLE mcp.cache_entries (
      cache_key   char(64)    PRIMARY KEY,
      namespace   varchar(32) NOT NULL,
      value       jsonb       NOT NULL,
      size_bytes  integer     NOT NULL,
      hits        integer     NOT NULL DEFAULT 0,
      expires_at  timestamptz NOT NULL,
      created_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX cache_expires_idx ON mcp.cache_entries (expires_at)",
    # ---------- 4.2 handle & job ----------
    """
    CREATE TABLE mcp.result_sets (
      handle       varchar(16) PRIMARY KEY,
      kind         varchar(32) NOT NULL,
      client_id    uuid,
      run_id       uuid,
      job_id       varchar(16),
      columns      text[]      NOT NULL,
      total        integer     NOT NULL DEFAULT 0,
      stats        jsonb       NOT NULL DEFAULT '{}',
      source_ref   jsonb       NOT NULL DEFAULT '{}',
      expires_at   timestamptz NOT NULL,
      created_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE mcp.result_items (
      handle     varchar(16) NOT NULL REFERENCES mcp.result_sets(handle) ON DELETE CASCADE,
      idx        integer     NOT NULL,
      ref_id     uuid,
      data       jsonb       NOT NULL,
      sort_ts    timestamptz,
      sort_num   double precision,
      PRIMARY KEY (handle, idx)
    )
    """,
    "CREATE INDEX result_items_ts_idx  ON mcp.result_items (handle, sort_ts)",
    "CREATE INDEX result_items_num_idx ON mcp.result_items (handle, sort_num DESC)",
    """
    CREATE TABLE mcp.jobs (
      id             varchar(16) PRIMARY KEY,
      kind           varchar(32) NOT NULL,
      status         varchar(16) NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
      priority       smallint    NOT NULL DEFAULT 5,
      params         jsonb       NOT NULL,
      client_id      uuid,
      run_id         uuid,
      parent_job_id  varchar(16) REFERENCES mcp.jobs(id),
      browser_profile_id uuid,
      progress_done  integer     NOT NULL DEFAULT 0,
      progress_total integer,
      checkpoint     jsonb       NOT NULL DEFAULT '{}',
      result_handle  varchar(16),
      result         jsonb,
      error          jsonb,
      attempts       smallint    NOT NULL DEFAULT 0,
      max_attempts   smallint    NOT NULL DEFAULT 3,
      run_after      timestamptz NOT NULL DEFAULT now(),
      locked_by      varchar(64),
      locked_at      timestamptz,
      heartbeat_at   timestamptz,
      timeout_s      integer     NOT NULL DEFAULT 1800,
      cancel_requested boolean   NOT NULL DEFAULT false,
      started_at     timestamptz,
      finished_at    timestamptz,
      created_at     timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX jobs_pick_idx ON mcp.jobs (priority, run_after) WHERE status = 'queued'",
    "CREATE INDEX jobs_run_idx  ON mcp.jobs (run_id) WHERE run_id IS NOT NULL",
    # tambahan implementasi: dedupe job identik & pencarian turunan (pembatalan berantai)
    "ALTER TABLE mcp.jobs ADD COLUMN dedupe_key char(64)",
    "ALTER TABLE mcp.jobs ADD COLUMN estimate jsonb",
    "ALTER TABLE mcp.jobs ADD COLUMN message varchar(300)",
    "CREATE INDEX jobs_dedupe_idx ON mcp.jobs (dedupe_key) WHERE status IN ('queued','running')",
    "CREATE INDEX jobs_parent_idx ON mcp.jobs (parent_job_id) WHERE parent_job_id IS NOT NULL",
    """
    CREATE TABLE mcp.schedules (
      id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      name        varchar(100) NOT NULL UNIQUE,
      cron        varchar(64)  NOT NULL,
      job_kind    varchar(32)  NOT NULL,
      params      jsonb        NOT NULL DEFAULT '{}',
      is_enabled  boolean      NOT NULL DEFAULT true,
      last_run_at timestamptz,
      next_run_at timestamptz
    )
    """,
    # ---------- 4.3 agent run & outbox ----------
    """
    CREATE TABLE mcp.agent_profiles (
      agent_key      varchar(100) PRIMARY KEY,
      display_name   varchar(100) NOT NULL,
      system_prompt  text         NOT NULL,
      route          varchar(64)  NOT NULL DEFAULT 'smart',
      toolsets       text[]       NOT NULL,
      tool_denylist  text[]       NOT NULL DEFAULT '{}',
      run_token_budget integer    NOT NULL DEFAULT 120000,
      max_steps      smallint     NOT NULL DEFAULT 25,
      is_active      boolean      NOT NULL DEFAULT true,
      version        integer      NOT NULL DEFAULT 1,
      updated_at     timestamptz  NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE mcp.runs (
      run_id          uuid PRIMARY KEY,
      mcp_run_id      varchar(32) NOT NULL UNIQUE,
      trigger         varchar(16) NOT NULL CHECK (trigger IN ('assignment','mention','prompt_block')),
      agent_key       varchar(100) NOT NULL REFERENCES mcp.agent_profiles(agent_key),
      agent_id        uuid        NOT NULL,
      workspace_id    uuid,
      project_id      uuid        NOT NULL,
      task_id         uuid,
      status          varchar(24) NOT NULL DEFAULT 'accepted'
                      CHECK (status IN ('accepted','running','completed','failed','cancelled','cancelled_by_dashboard')),
      input           jsonb       NOT NULL,
      callback        jsonb       NOT NULL,
      tokens_used     integer     NOT NULL DEFAULT 0,
      token_budget    integer     NOT NULL,
      steps           smallint    NOT NULL DEFAULT 0,
      cancel_requested boolean    NOT NULL DEFAULT false,
      cancel_reason   varchar(32),
      error           jsonb,
      summary         text,
      subtask_refs    jsonb       NOT NULL DEFAULT '{}',
      last_event_at   timestamptz,
      started_at      timestamptz,
      finished_at     timestamptz,
      created_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX runs_status_idx ON mcp.runs (status) WHERE status IN ('accepted','running')",
    "CREATE INDEX runs_task_mention_idx ON mcp.runs (task_id, created_at) WHERE trigger = 'mention'",
    """
    CREATE TABLE mcp.run_steps (
      run_id      uuid     NOT NULL REFERENCES mcp.runs(run_id) ON DELETE CASCADE,
      step        smallint NOT NULL,
      role        varchar(16) NOT NULL,
      tool        varchar(64),
      content     jsonb    NOT NULL,
      tokens      integer  NOT NULL DEFAULT 0,
      created_at  timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (run_id, step)
    )
    """,
    """
    CREATE TABLE mcp.webhook_outbox (
      id              bigserial PRIMARY KEY,
      event_id        uuid        NOT NULL UNIQUE,
      run_id          uuid        NOT NULL REFERENCES mcp.runs(run_id),
      seq             integer     NOT NULL,
      event_type      varchar(48) NOT NULL,
      payload         jsonb       NOT NULL,
      status          varchar(16) NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','sending','delivered','duplicate','failed','discarded')),
      attempts        smallint    NOT NULL DEFAULT 0,
      next_attempt_at timestamptz NOT NULL DEFAULT now(),
      first_attempt_at timestamptz,
      last_http_status smallint,
      last_error      text,
      response        jsonb,
      delivered_at    timestamptz,
      created_at      timestamptz NOT NULL DEFAULT now(),
      UNIQUE (run_id, seq)
    )
    """,
    "CREATE INDEX outbox_pending_idx ON mcp.webhook_outbox (next_attempt_at) WHERE status IN ('pending','sending')",
    # ---------- 4.4 template ----------
    """
    CREATE TABLE mcp.templates (
      key          varchar(64) PRIMARY KEY,
      name         varchar(150) NOT NULL,
      category     varchar(32)  NOT NULL,
      description  text,
      origin       varchar(16)  NOT NULL DEFAULT 'seed' CHECK (origin IN ('seed','generated','custom')),
      current_version integer   NOT NULL DEFAULT 1,
      workspace_id uuid,
      is_active    boolean      NOT NULL DEFAULT true,
      created_by_client uuid,
      created_at   timestamptz  NOT NULL DEFAULT now(),
      updated_at   timestamptz  NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE mcp.template_versions (
      template_key varchar(64) NOT NULL REFERENCES mcp.templates(key) ON DELETE CASCADE,
      version      integer     NOT NULL,
      variables    jsonb       NOT NULL DEFAULT '[]',
      blocks       jsonb       NOT NULL,
      subtasks     jsonb       NOT NULL DEFAULT '[]',
      outline      jsonb       NOT NULL,
      created_at   timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (template_key, version)
    )
    """,
    # ---------- 4.5 browser & web ----------
    """
    CREATE TABLE mcp.browser_profiles (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      name           varchar(64)  NOT NULL UNIQUE,
      purpose        varchar(200),
      user_data_dir  text         NOT NULL,
      profile_directory varchar(64) NOT NULL DEFAULT 'Default',
      user_agent     text,
      window_size    varchar(16)  NOT NULL DEFAULT '1366,900',
      proxy_url_env  varchar(64),
      headless       boolean      NOT NULL DEFAULT false,
      allowed_sites  text[]       NOT NULL DEFAULT '{}',
      login_status   jsonb        NOT NULL DEFAULT '{}',
      locked_by_job  varchar(16),
      locked_at      timestamptz,
      last_used_at   timestamptz,
      is_active      boolean      NOT NULL DEFAULT true,
      created_at     timestamptz  NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE mcp.web_pages (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      job_id        varchar(16) REFERENCES mcp.jobs(id) ON DELETE SET NULL,
      url           text        NOT NULL,
      final_url     text,
      domain        varchar(255) NOT NULL,
      status        varchar(24) NOT NULL,
      http_status   smallint,
      title         text,
      published_at  timestamptz,
      content_md    text,
      extracted     jsonb,
      content_hash  char(64),
      chars         integer     NOT NULL DEFAULT 0,
      fetched_at    timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX web_pages_job_idx ON mcp.web_pages (job_id)",
    "CREATE INDEX web_pages_url_idx ON mcp.web_pages (url, fetched_at DESC)",
    # ---------- 4.6 sosial & trends ----------
    """
    CREATE TABLE mcp.social_posts (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      source         varchar(16)  NOT NULL CHECK (source IN ('x','threads')),
      external_id    varchar(64)  NOT NULL,
      url            text,
      author_handle  varchar(100),
      author_id      varchar(64),
      author_followers integer,
      text           text         NOT NULL,
      lang           varchar(8),
      posted_at      timestamptz  NOT NULL,
      is_reply       boolean      NOT NULL DEFAULT false,
      is_repost      boolean      NOT NULL DEFAULT false,
      likes          integer      NOT NULL DEFAULT 0,
      replies        integer      NOT NULL DEFAULT 0,
      reposts        integer      NOT NULL DEFAULT 0,
      quotes         integer      NOT NULL DEFAULT 0,
      views          bigint,
      hashtags       text[]       NOT NULL DEFAULT '{}',
      mentions       text[]       NOT NULL DEFAULT '{}',
      media_type     varchar(16),
      provider       varchar(16)  NOT NULL,
      raw            jsonb,
      first_seen_job varchar(16),
      collected_at   timestamptz  NOT NULL DEFAULT now(),
      text_tsv       tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
      UNIQUE (source, external_id)
    )
    """,
    "CREATE INDEX social_posts_time_idx ON mcp.social_posts (source, posted_at DESC)",
    "CREATE INDEX social_posts_tsv_idx  ON mcp.social_posts USING gin (text_tsv)",
    "CREATE INDEX social_posts_tags_idx ON mcp.social_posts USING gin (hashtags)",
    """
    CREATE TABLE mcp.job_posts (
      job_id   varchar(16) NOT NULL REFERENCES mcp.jobs(id) ON DELETE CASCADE,
      post_id  uuid        NOT NULL REFERENCES mcp.social_posts(id) ON DELETE CASCADE,
      matched_keywords text[] NOT NULL DEFAULT '{}',
      PRIMARY KEY (job_id, post_id)
    )
    """,
    """
    CREATE TABLE mcp.trend_queries (
      id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      kind        varchar(16) NOT NULL CHECK (kind IN ('interest','region','related')),
      keywords    text[]      NOT NULL,
      geo         varchar(16) NOT NULL DEFAULT 'ID',
      start_date  date        NOT NULL,
      end_date    date        NOT NULL,
      category    integer     NOT NULL DEFAULT 0,
      gprop       varchar(16) NOT NULL DEFAULT 'web',
      resolution  varchar(8),
      provider    varchar(16) NOT NULL,
      params_hash char(64)    NOT NULL,
      fetched_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX trend_queries_hash_idx ON mcp.trend_queries (params_hash, fetched_at DESC)",
    """
    CREATE TABLE mcp.trend_points (
      query_id   uuid        NOT NULL REFERENCES mcp.trend_queries(id) ON DELETE CASCADE,
      keyword    text        NOT NULL,
      bucket     date,
      region     varchar(100),
      value      smallint    NOT NULL CHECK (value BETWEEN 0 AND 100),
      is_partial boolean     NOT NULL DEFAULT false
    )
    """,
    "CREATE INDEX trend_points_q_idx ON mcp.trend_points (query_id, keyword, bucket)",
    """
    CREATE TABLE mcp.trend_related (
      query_id   uuid        NOT NULL REFERENCES mcp.trend_queries(id) ON DELETE CASCADE,
      keyword    text        NOT NULL,
      kind       varchar(8)  NOT NULL CHECK (kind IN ('query','topic')),
      rank_type  varchar(8)  NOT NULL CHECK (rank_type IN ('top','rising')),
      term       text        NOT NULL,
      value      integer,
      position   smallint    NOT NULL
    )
    """,
    # ---------- 4.7 NLP, laporan, data master ----------
    """
    CREATE TABLE mcp.sentiment_results (
      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      target_type  varchar(16) NOT NULL CHECK (target_type IN ('social_post','web_page','text')),
      target_id    uuid,
      text_sha256  char(64)    NOT NULL,
      method       varchar(16) NOT NULL CHECK (method IN ('model','llm','hybrid')),
      model        varchar(100) NOT NULL,
      label        varchar(8)  NOT NULL CHECK (label IN ('positive','neutral','negative')),
      score        real        NOT NULL,
      aspects      jsonb       NOT NULL DEFAULT '[]',
      lang         varchar(8),
      created_at   timestamptz NOT NULL DEFAULT now(),
      UNIQUE (text_sha256, model)
    )
    """,
    "CREATE INDEX sentiment_target_idx ON mcp.sentiment_results (target_type, target_id)",
    """
    CREATE TABLE mcp.analysis_reports (
      id            varchar(16) PRIMARY KEY,
      kind          varchar(24) NOT NULL CHECK (kind IN ('sentiment','market','campaign_plan','campaign_eval','digital_strategy')),
      title         varchar(200) NOT NULL,
      subject       text,
      params        jsonb       NOT NULL,
      input_handles text[]      NOT NULL DEFAULT '{}',
      key_numbers   jsonb       NOT NULL DEFAULT '{}',
      summary       text        NOT NULL,
      content       jsonb       NOT NULL,
      language      varchar(8)  NOT NULL DEFAULT 'id',
      llm_tokens    integer     NOT NULL DEFAULT 0,
      workspace_id  uuid,
      project_id    uuid,
      task_id       uuid,
      run_id        uuid,
      client_id     uuid,
      published_to  jsonb       NOT NULL DEFAULT '[]',
      created_at    timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX reports_project_idx ON mcp.analysis_reports (project_id, created_at DESC)",
    """
    CREATE TABLE mcp.competitors (
      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workspace_id uuid,
      name         varchar(150) NOT NULL,
      website      text,
      social       jsonb        NOT NULL DEFAULT '{}',
      keywords     text[]       NOT NULL DEFAULT '{}',
      notes        text,
      created_at   timestamptz  NOT NULL DEFAULT now(),
      updated_at   timestamptz  NOT NULL DEFAULT now(),
      deleted_at   timestamptz,
      UNIQUE (workspace_id, name)
    )
    """,
    """
    CREATE TABLE mcp.keyword_watchlists (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      workspace_id  uuid,
      name          varchar(150) NOT NULL,
      competitor_id uuid REFERENCES mcp.competitors(id) ON DELETE SET NULL,
      keywords      text[]       NOT NULL,
      sources       text[]       NOT NULL DEFAULT '{x,threads,trends}',
      schedule_id   uuid REFERENCES mcp.schedules(id) ON DELETE SET NULL,
      created_at    timestamptz  NOT NULL DEFAULT now(),
      updated_at    timestamptz  NOT NULL DEFAULT now(),
      deleted_at    timestamptz
    )
    """,
    # ---------- 4.8 KB (bagian tanpa kolom vector) ----------
    """
    CREATE TABLE mcp.kb_collections (
      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      name         varchar(150) NOT NULL,
      description  text,
      workspace_id uuid,
      project_id   uuid,
      embed_model  varchar(64) NOT NULL,
      embed_dim    smallint    NOT NULL CHECK (embed_dim IN (384, 768, 1024, 1536)),
      created_at   timestamptz NOT NULL DEFAULT now(),
      UNIQUE (workspace_id, name)
    )
    """,
    """
    CREATE TABLE mcp.kb_documents (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      collection_id uuid NOT NULL REFERENCES mcp.kb_collections(id) ON DELETE CASCADE,
      source_type   varchar(24) NOT NULL,
      source_ref    text        NOT NULL,
      source_version integer,
      title         text,
      metadata      jsonb       NOT NULL DEFAULT '{}',
      content_hash  char(64)    NOT NULL,
      created_at    timestamptz NOT NULL DEFAULT now(),
      updated_at    timestamptz NOT NULL DEFAULT now(),
      UNIQUE (collection_id, source_type, source_ref)
    )
    """,
    # NOTIFY saat job selesai (job_wait memakai LISTEN mcp_job_done)
    """
    CREATE FUNCTION mcp.notify_job_done() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.status IN ('succeeded','failed','cancelled') AND NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM pg_notify('mcp_job_done', NEW.id);
      END IF;
      RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER jobs_notify_done AFTER UPDATE OF status ON mcp.jobs
      FOR EACH ROW EXECUTE FUNCTION mcp.notify_job_done()
    """,
]

TABLES = [
    "kb_documents", "kb_collections", "keyword_watchlists", "competitors", "analysis_reports",
    "sentiment_results", "trend_related", "trend_points", "trend_queries", "job_posts",
    "social_posts", "web_pages", "browser_profiles", "template_versions", "templates",
    "webhook_outbox", "run_steps", "runs", "agent_profiles", "schedules", "jobs", "result_items",
    "result_sets", "cache_entries", "usage_daily", "llm_calls", "tool_calls", "api_clients",
]


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS jobs_notify_done ON mcp.jobs")
    op.execute("DROP FUNCTION IF EXISTS mcp.notify_job_done()")
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS mcp.{table} CASCADE")
