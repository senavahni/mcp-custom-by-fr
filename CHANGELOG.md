# Changelog

## 0.1.0-dev — 2026-11-03

TASK-01 s/d TASK-10 (milestone M0, M1, sebagian M2).

### Ditambahkan
- **TASK-01** Struktur repo, `pyproject.toml` (ruff/mypy/pytest identik Gateway), requirements terpisah
  (`requirements*.txt`, `requirements.lock.txt`), skrip Windows (`env.bat`, `conda-run.bat`, `check.bat`,
  `test.bat`, `migrate.bat`, `start-*.bat`, `stop-all.bat`, `db-bootstrap.bat`), `secret_scan.py`.
- **TASK-02** `Settings` tervalidasi per grup, log JSON (structlog+orjson) ke stdout + file harian (14 file)
  dengan redaksi secret, context vars (`request_id`, `client_id`, `run_id`, `job_id`, `tool`), model error
  tunggal `ToolError`, utils (`ids`, `time`, `tokens`, `text`, `crypto`).
- **TASK-03** `bootstrap.sql` (role, schema `mcp`, grant berkolom ke `public`), Alembic async (hanya schema
  `mcp`), revisi `0001_init` + `0002_kb_vector`, ORM semua tabel + model read-only Dashboard, `manage.py`
  (`migrate`, `seed`, `db-check`, klien, `job-retry`).
- **TASK-04** Server MCP Streamable HTTP `:4100/mcp`, auth Bearer (Dashboard key / `mcpk_…`), rate limit
  per klien, registry `@tool` + visibilitas toolset (`?toolsets=`), pipeline (validasi → scope → cache →
  budget → handler 25 s → auto-handle → kompaksi → audit `tool_calls`), `toolbox_list`, admin `/admin/*`.
- **TASK-05** Result store (handle, cursor keyset, filter, agregasi SQL, export CSV/JSONL/XLSX bertanda
  tangan), kompaksi output (soft 1.500 / hard 4.000 token), cache tool & LLM, budget 3 lapis + kuota
  eksternal, `data_get`/`data_aggregate`/`data_export`.
- **TASK-06** `GatewayClient` (chat, chat_json + repair, embed batch 64), route policy per purpose, retry
  sesuai tabel error Gateway, pencatatan `llm_calls`, prompt library berversi, `smoke_gateway.py`.
- **TASK-07** Job queue PostgreSQL (`SKIP LOCKED`, dedupe, heartbeat, reaper, checkpoint), worker (slot,
  timeout, retry, cancel berantai), scheduler cron exactly-once per slot, handler `cache_cleanup`,
  `retention`, `usage_rollup`, tool `job_status`/`job_wait` (LISTEN/NOTIFY)/`job_cancel`/`job_list`.
- **TASK-08** Port schema Zod kontrak & blok ke Pydantic, signer HMAC setara Dashboard, klien Agent API
  (rate limit 4 rps/run, pemangkasan), webhook outbox (berurutan per run, backoff, discard pada 409),
  chunker, fixture kontrak + validator Zod (`tests/contract/validate_with_zod.cjs`).
- **TASK-09** Tool `pm_dispatch_task_to_agent`, `pm_request_agent_estimation`, `pm_notify_agent_mention`,
  `pm_execute_prompt_block`, `pm_cancel_agent_run`; registry run (idempoten, batas 10 mention/jam),
  estimasi heuristik + LLM `fast` (dibatasi 0,5×–2×, hari kerja).
- **TASK-10** Agent runtime (loop tool-calling, eksekusi paralel ≤ 3 / tool tulis berurutan, mode ringan,
  kompaksi riwayat, resume dari `run_steps`, supervisor timeout/restart), tool virtual (`report_progress`,
  `write_blocks`, `create_subtasks`, `post_comment`, `finish`, `fail`), `pm_query_project_context`,
  `md_to_blocks`, 3 profil agent seed (`strategist`, `researcher`, `analyst`).

### Hasil spike (TASK-01)
- **SDK `mcp` 2.3.0** (rilis stable terbaru saat dikerjakan; mendukung protokol handshake lama yang dipakai
  SDK TS Dashboard dan era 2026-07-28). `FastMCP` 1.x sudah diganti `MCPServer` yang membangun schema dari
  signature fungsi, sehingga dipakai **server low-level** `Server(on_list_tools=…, on_call_tool=…)` +
  `StreamableHTTPSessionManager(stateless=True, json_response=True)`. `structuredContent` + `outputSchema`
  berjalan; klien SDK memvalidasi output schema hanya bila `isError=false`. Identitas klien dibaca dari
  `ctx.request.scope["state"]`. Diverifikasi dengan klien resmi mode `legacy` dan `auto`.
- **SDK `openai` 3.x memakai `httpx2`** (bukan `httpx`): mock test memakai `httpx2.MockTransport`.

### Catatan implementasi / deviasi dari dokumen
- **pgvector** belum terpasang di PostgreSQL lokal (18.6, tanpa compiler/Docker untuk build). Objek vector
  (`embedding_cache`, `kb_chunks`) dipindah ke revisi **`0002_kb_vector`**; `manage.py migrate` otomatis
  berhenti di `0001_init` bila ekstensi belum ada. Tidak berdampak pada TASK-01..10.
- **`X-Request-Id` ke Gateway dibuat unik per panggilan LLM** karena Gateway memakainya sebagai primary key
  `requests.id`; korelasi ke request MCP lewat header `X-Correlation-Id` dan parameter `user`.
- **Kunci cache tool** menyertakan `limit`/`cursor` (mengubah hasil) dan dicakup per workspace **dan** klien
  (handle hasil hanya bisa dibaca pembuatnya).
- `mcp.jobs` mendapat kolom tambahan `dedupe_key`, `estimate`, `message` + trigger NOTIFY `mcp_job_done`;
  `mcp.runs` mendapat indeks `runs_task_mention_idx` (batas mention per jam).
- Tool virtual `write_blocks` menerima **markdown** (dikonversi `md_to_blocks`, lebih hemat token daripada
  JSON blok) + `updates[]` opsional.
- Kegagalan run tidak menambah komentar dari MCP: Dashboard sudah membuat komentar sistem saat menerima
  `agent.task.failed`.
- Env baru: `PUBLIC_BASE_URL`, `STORAGE_DIR`, `ENABLE_DEV_TOOLS`, `TOOL_LLM_TOKENS_DEFAULT`,
  `TOOL_LLM_TOKENS_DEEP`, `AUDIT_RETENTION_DAYS`, `RAW_RETENTION_DAYS`, `SOCIAL_RETENTION_DAYS`.

### Belum diverifikasi (butuh sistem nyata)
- E2E dengan Dashboard lokal & Gateway nyata (UC-01/03/04, `-m live`/`contract`), lihat
  [documentation/contract-report.md](documentation/contract-report.md).
