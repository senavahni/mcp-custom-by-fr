-- Bootstrap schema mcp (DATABASE.md §3). Jalankan SEKALI sebagai superuser pada database Dashboard
-- yang sudah termigrasi Prisma. Idempoten: aman dijalankan ulang (password role diperbarui).
--   psql -v owner_pw=... -v app_pw=... -d marketing_dashboard -f scripts/sql/bootstrap.sql
-- Opsional: -v owner_role=... -v app_role=... (default mcp_owner / mcp_app; test memakai *_test)
\set ON_ERROR_STOP on
\if :{?owner_role}
\else
  \set owner_role mcp_owner
\endif
\if :{?app_role}
\else
  \set app_role mcp_app
\endif

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- pgvector opsional saat bootstrap: revisi 0002_kb_vector butuh ekstensi ini (TASK-19)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
    CREATE EXTENSION IF NOT EXISTS vector;
  ELSE
    RAISE NOTICE 'pgvector belum terpasang di server ini; revisi 0002_kb_vector akan dilewati';
  END IF;
END $$;

SELECT format('CREATE ROLE %I LOGIN', :'owner_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner_role') \gexec
SELECT format('CREATE ROLE %I LOGIN', :'app_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role') \gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'owner_role', :'owner_pw') \gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'app_role', :'app_pw') \gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I, %I', current_database(), :'owner_role', :'app_role') \gexec

CREATE SCHEMA IF NOT EXISTS mcp AUTHORIZATION :"owner_role";
GRANT USAGE ON SCHEMA mcp TO :"app_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA mcp
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA mcp
  GRANT USAGE, SELECT ON SEQUENCES TO :"app_role";

-- Akses baca ke data Dashboard (schema public), berkolom
GRANT USAGE ON SCHEMA public TO :"app_role";
GRANT SELECT (id, username, display_name, avatar_url, is_active) ON public.users TO :"app_role";
GRANT SELECT ON public.workspaces, public.workspace_members, public.agents, public.projects,
               public.tasks, public.document_blocks, public.comments, public.activity_logs,
               public.agent_runs TO :"app_role";
-- TIDAK diberikan: refresh_tokens, notifications, mcp_webhook_events, users.email, users.password_hash

ALTER ROLE :"app_role" IN DATABASE :"DBNAME" SET search_path = mcp, public;
ALTER ROLE :"app_role" SET statement_timeout = '30s';
ALTER ROLE :"app_role" SET idle_in_transaction_session_timeout = '60s';
