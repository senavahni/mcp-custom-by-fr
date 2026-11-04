# Changelog

## 0.5.0-dev — 2026-11-04

### Modul 3 Dashboard — kontrak 1.2 & toolset `intel` (M3_TASK_000, M3_TASK_007)
- Kontrak 1.2 (`schemas/intel.py`) + 50 fixture bersama `contracts/intel-1.2/` (+ `SHA256SUMS`) yang
  juga divalidasi Zod Dashboard; generator `scripts/export_intel_fixtures.py [--dashboard <repo>]`.
- Toolset `intel` (khusus klien `dashboard-intel`, output `machine_output` tanpa kompaksi):
  `intel_agents_list`, `intel_sources_status`, `intel_job_estimate`, `intel_job_create` (idempoten per
  `dashboard_job_id`), `intel_job_get`, `intel_job_cancel`, `intel_items`, `intel_llm_status`.
- Job induk `intel_pipeline`: tahap Kumpulkan menjalankan collector yang ada sebagai job anak
  (`parent_job_id`) di slot induk; tahap Analisis = sentimen + statistik Python + satu sintesis LLM
  (route agent, sampel dibungkus `<untrusted_content>`). Validasi kapabilitas agent per tahap
  (`UNKNOWN_AGENT`/`AGENT_INACTIVE`/`AGENT_NOT_CAPABLE`), scoping `workspace_id` di setiap tool.
- Event `intel.job.*` lewat outbox yang sama: stream per run **atau** per job (`webhook_outbox.job_id`,
  `run_id` nullable), urut `sequence`; 404/409 `JOB_NOT_ACTIVE` dari Dashboard membuang sisa stream.
- Atribusi LLM: header `X-Caller-Agent`/`X-Caller-Task` + kolom `llm_calls.agent_key/job_id/stage`.
- Migrasi `0003_intel`; kode error baru (`AGENT_*`, `SOURCE_UNAVAILABLE`, `LLM_*`, `JOB_NOT_ACTIVE`, …).

### Modul 3 Dashboard — key LLM Gateway per agent (M3_TASK_009)
- `MCP_SECRET_KEY` (opsional, 32 byte base64, wajib berbeda dari pepper & secret Dashboard) +
  `utils/secretbox.py` (AES-256-GCM, AAD = nama credential).
- Tabel `mcp.llm_credentials` (migrasi `0004_llm_credentials`): key `lgw_` terenkripsi per agent + `system`.
- `manage.py gateway-keys gen-secret|provision|list|import|disable`; admin key Gateway hanya dari
  env sementara / prompt tersembunyi, tidak disimpan. `provision` idempoten; `--force` merotasi.
- `gateway_client`: key dipilih per panggilan (agent → system → `LLM_GATEWAY_API_KEY`), klien di-cache
  per key; 401/403 → credential `invalid` + `last_error`, panggilan berikutnya jatuh ke key berikutnya.
- `intel_agents_list` / `intel_llm_status` melaporkan sumber, prefix & status key (tidak pernah key lengkap).
  Tanpa `MCP_SECRET_KEY` perilaku sama seperti sebelumnya (key env).

## 0.4.0-dev — 2026-11-03

### TASK-18 — Tools CRUD database (`db_*`)
- Registry entitas whitelist (`services/db/entities.py`, 14 entitas sesuai DATABASE §5) + model tulis
  Pydantic (`competitors`, `watchlists`, `kb_collections`).
- Query builder aman (`services/db/query.py`): filter terstruktur 12 operator dengan parameter binding dan
  cek tipe, proyeksi field whitelist, cursor keyset, pembatasan workspace (run/klien), soft delete,
  `statement_timeout 5s`, hitung maks 10.000 baris.
- CRUD (`services/db/write.py`): validasi per entitas, optimistic locking `expected_updated_at` →
  `CONFLICT` + `updated_at` terkini, soft delete default, hard delete khusus admin, entitas inti
  Dashboard ditolak dengan petunjuk tool (`task_create`/`task_update`/`tpl_render`).
- `db_sql_readonly` (toolset `db_admin`, admin): validasi `sqlglot` (satu SELECT/UNION, tanpa DML/CTE
  pengubah data/`FOR UPDATE`/`INTO`, fungsi berbahaya, schema selain `mcp`/`public`, tabel `pg_*`),
  transaksi `READ ONLY` + timeout 5 s, `LIMIT` dipaksa ≤ 1.000.
- Tools: `db_entities`, `db_query`, `db_get`, `db_create`, `db_update`, `db_delete`, `db_sql_readonly`.
- Dependensi baru `sqlglot`. Coverage kini memakai `concurrency = greenlet` (baris setelah `await` pada
  SQLAlchemy async sebelumnya tidak tercatat).

### TASK-19 — Knowledge base / vector DB (`kb_*`)
- Chunker markdown-aware (heading → blok → kalimat, breadcrumb, tabel per baris + header diulang,
  chunk < 30 token digabung), embedder Gateway (`embed-default`) & lokal ONNX (e5-small, prefix
  `query:`/`passage:`), model dikunci per koleksi.
- Ingest idempoten (`text`, `url`, `handle` web_pages/social_posts per hari×kata kunci, `report`,
  `file_text`), sinkron Dashboard inkremental (brief + task, hapus dokumen task terhapus, 0 panggilan
  embedding bila tidak berubah) + pemicu otomatis saat run dimulai (> 1 jam).
- Pencarian `vector`/`keyword`/`hybrid` (RRF k=60), filter metadata, gabung chunk berurutan, potong
  600 karakter di batas kalimat; `pm_query_project_context` menggabungkan Agent API + KB hybrid.
- Tools: `kb_collections`, `kb_collection_create`, `kb_ingest`, `kb_search`, `kb_delete`,
  `kb_sync_dashboard`; CLI `download-models`, `kb-reembed`. Panduan:
  [docs/knowledge-base.md](docs/knowledge-base.md). Set evaluasi `tests/eval/kb/`.
- **Mode cadangan tanpa pgvector**: `0002_kb_vector` kini berjalan juga tanpa ekstensi (kolom
  `real[]`, kosinus di aplikasi); `manage.py migrate` mempromosikan ke `vector` + HNSW setelah
  pgvector dipasang. Cache embedding Gateway dibuat netral tipe (`real[]`).
- Perbaikan saat eval: mode `keyword` sebelumnya meng-AND semua kata pertanyaan (recall@6 = 0);
  kini OR kata bermakna tanpa stopword (recall@6 = 0,67 pada set eval).
- Belum diverifikasi: pgvector/HNSW nyata (p95 50.000 chunk), model ONNX lokal, recall@6 dengan
  embedding Gateway (target ≥ 0,85, jalankan `KB_EVAL_LIVE=1`).
- Mode ringan agent (mention/prompt block) kini hanya mendapat tool baca dari toolset `kb`/`pm_agent`.

### TASK-20 — Analisis sentimen (`sentiment_*`)
- `services/nlp/`: normalisasi + kamus slang ±760 entri + emoji, deteksi bahasa (lingua), model
  lokal ONNX (batch, process pool), eskalasi LLM `fast` format baris `i|label|skor` dengan parser
  toleran & retry sekali, aspek (keyword-spotting + LLM), agregasi (distribusi, net, grup, aspek,
  contoh, perbandingan).
- Cache `sentiment_results` per teks ternormalisasi + model (analisis ulang = 0 token LLM).
- Tools `sentiment_analyze` (job; `method=llm` butuh `confirm_cost` setelah estimasi) dan
  `sentiment_summary` (tanpa LLM, banding handle). Prompt `sentiment_batch.v1`, `aspect_batch.v1`.
- `download-models --sentiment-repo <repo>` untuk model ONNX sentimen; dependensi runtime
  `onnxruntime`, `tokenizers`, `huggingface-hub`, `lingua-language-detector` (requirements-ml).
- **Tertunda**: spike/pemilihan model, set uji 300 teks berlabel, F1 & token per 1.000 teks — lihat
  [docs/eval/sentiment.md](docs/eval/sentiment.md). Tanpa model, `auto` memakai LLM untuk semua teks.

## 0.3.0-dev — 2026-11-03

TASK-14 s/d TASK-17 (milestone M3 Data Collection).

### Ditambahkan
- **TASK-14** Profil browser terdaftar + lock eksklusif per job (`PROFILE_BUSY` setelah 60 s, lock basi
  3 jam), driver Selenium dengan chromedriver eksplisit (cek versi Chrome saat worker start →
  `browser.not_ready`), guard SSRF (cek ulang setelah redirect), robots.txt (protego) + rate limit per
  domain, ekstraksi markdown/text/selectors/links (trafilatura → markdownify), deteksi CAPTCHA.
  Tool `browser_profile_list`, `browser_profile_check` (admin), `web_scrape`, `web_crawl` (BFS, pola
  include/exclude, filter tanggal terbit), `web_extract` (LLM `extract`, konten `<untrusted_content>`).
  CLI `profile-add/open/check/list/disable`, `scripts\update-chromedriver.bat` (Chrome for Testing).
  Panduan: [docs/browser-profiles.md](docs/browser-profiles.md).
- **TASK-15** `x_search`: provider `x_api` (recent search 7 hari, tunggu reset rate limit, kuota
  `X_MAX_READS_PER_DAY`, estimasi biaya) / `browser` (per hari, checkpoint per hari, hasil parsial saat
  logout), aturan `provider=auto`, upsert `social_posts` + `job_posts` (tanpa duplikasi saat resume).
- **TASK-16** `threads_search`: Threads Keyword Search API (cursor, retry 5xx → fallback browser, token
  kedaluwarsa → `LOGIN_REQUIRED`) + mode browser; `social_stats` (group by day/keyword/author/source);
  CLI `threads-token-refresh` (menulis token baru ke `.env`).
- **TASK-17** `trends_interest`, `trends_by_region`, `trends_related`: cache DB 24 jam (sinkron bila
  cache segar), backoff 429 30/60/120 s + fallback provider, anchor scaling > 5 kata kunci, statistik tanpa
  LLM (`trend_stats`), parser CSV unduhan (label EN/ID, `<1`). Panduan:
  [docs/collectors.md](docs/collectors.md).
- Job `retention` kini juga menghapus `social_posts` & `web_pages` > `SOCIAL_RETENTION_DAYS` (DATABASE §6).

### Hasil spike D8 (Google Trends)
- `pytrends` diarsip, `trendspy` tidak rilis sejak 2024-12 → ditolak. `pytrends-modern` lolos kriteria
  tetapi belum dipasang. Dipakai **klien internal** ke endpoint web Trends (`unofficial`) dan request yang
  sama di tab Chrome (`browser`); `official` masih stub. Uji live 20 query belum dijalankan.

### Catatan implementasi
- `selectolax` 1.0 menghapus backend Modest → parser memakai `selectolax.lexbor`.
- Env baru: `X_PRICE_PER_READ_USD`, `X_API_BASE_URL`, `THREADS_API_BASE_URL`, `TRENDS_MIN_INTERVAL_S`,
  `TRENDS_BASE_URL`, `TRENDS_OFFICIAL_API_KEY`.
- Seed menambah profil browser `default` (headless, tanpa login).
- Tidak ada perubahan di repo Dashboard untuk TASK-14..17.

### Belum diverifikasi (butuh sistem nyata)
- Chrome + chromedriver nyata (chromedriver belum diunduh di mesin dev), login X/Threads, X API & Threads
  API dengan token asli, dan uji live Google Trends. Seluruh jalur diuji dengan fixture/mock.

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
