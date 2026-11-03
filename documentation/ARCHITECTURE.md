# Architecture — MCP Marketing

## 1. Konteks Sistem

```mermaid
flowchart LR
  subgraph Klien
    DB[Dashboard Backend NestJS]
    CD[Claude Desktop / Cursor / Cline]
  end
  subgraph MCP[MCP Marketing :4100]
    S[MCP Server /mcp]
    R[Agent Runtime]
    W[Worker job queue]
    O[Outbox sender]
  end
  subgraph Data
    PG[(PostgreSQL marketing_dashboard\npublic=Prisma, mcp=Alembic+pgvector)]
  end
  GW[LLM Gateway :8000]
  EXT[X API / Threads API / Google Trends / Web]
  CH[Chrome + chromedriver\nprofil terdaftar]

  DB -- tools/call pm_* --> S
  CD -- tools/call --> S
  S --> R
  S --> W
  R --> GW
  W --> GW
  W --> CH --> EXT
  W --> EXT
  S & R & W & O --> PG
  O -- webhook HMAC --> DB
  R -- Agent API / Service API --> DB
```

## 2. Tech Stack

Versi Python mengikuti LLM Gateway (`pyproject.toml`: `requires-python = ">=3.11,<3.13"`, conda env Python 3.11).

| Lapisan | Pilihan | Catatan |
|---|---|---|
| Bahasa | **Python 3.11** | conda env `mcp-marketing` |
| MCP | `mcp` (SDK resmi, `FastMCP`), transport Streamable HTTP | Pin versi stable terbaru di TASK-01; output memakai `structuredContent` |
| ASGI | Starlette (bawaan SDK) + `uvicorn[standard]` | Satu proses: MCP + health + metrics + admin JSON |
| Validasi | `pydantic>=2.7`, `pydantic-settings>=2.6` | Model I/O tool → JSON Schema |
| DB | `sqlalchemy[asyncio]>=2.0`, `asyncpg`, `alembic`, `pgvector` | Sama dengan Gateway |
| HTTP | `httpx`, `tenacity` | Klien Dashboard, X, Threads |
| LLM | `openai>=1.40` (base_url = Gateway) | Tidak memanggil provider langsung |
| Token | `tiktoken` | Estimasi token untuk budget & pemotongan |
| Log/metrics | `structlog`, `orjson`, `prometheus-client` | Format log sama dengan Gateway |
| Browser | `selenium>=4.20` + **chromedriver eksplisit** (`CHROMEDRIVER_PATH`) + Chrome profil (`--user-data-dir`, `--profile-directory`) | Selenium Manager dimatikan agar versi terkontrol |
| HTML → teks | `trafilatura` / `selectolax`, `markdownify` | Ekstraksi konten utama (hemat token) |
| Trends | library unofficial terpilih di spike (D8) + `pandas` | Adapter |
| Sentimen | `onnxruntime`, `tokenizers` (model IndoBERT/IndoRoBERTa ONNX) | CPU, tanpa PyTorch di runtime |
| Data | `pandas`, `numpy`, `scipy` (statistik tren) | Agregasi server-side |
| Test | `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy` | Sama dengan Gateway |

## 3. Struktur Repository

```
mcp-marketing/
├── pyproject.toml / requirements.txt / requirements.lock.txt / requirements-browser.txt / requirements-ml.txt
├── .env.example  alembic.ini  CHANGELOG.md  README.md
├── docs/                         # dokumen ini
├── migrations/                   # Alembic (schema mcp)
├── mcp_marketing/
│   ├── main.py                   # app ASGI: /mcp, /health/*, /metrics, /admin/*
│   ├── core/                     # config, logging, db, lifespan, errors, security, context
│   ├── models/                   # SQLAlchemy ORM (schema mcp) + reflected read models (public)
│   ├── schemas/                  # Pydantic: kontrak Dashboard (contract.py), block (blocks.py), tools I/O
│   ├── server/                   # FastMCP factory, registry toolset, middleware (auth, audit, budget, cache)
│   ├── tools/                    # satu modul per toolset: pm.py, tpl.py, task.py, web.py, social_x.py,
│   │                             #   threads.py, trends.py, db.py, kb.py, sentiment.py, market.py,
│   │                             #   campaign.py, dm.py, data.py, job.py, toolbox.py
│   ├── services/                 # logika bisnis (dipakai tools & agent runtime)
│   │   ├── llm/                  # gateway_client.py, route_policy.py, prompts/
│   │   ├── dashboard/            # agent_api.py, service_api.py, outbox.py, signer.py
│   │   ├── agent/                # runtime.py, profiles.py, memory.py, budget.py
│   │   ├── results/              # result_store.py (handle), compact.py, cache.py
│   │   ├── browser/              # profiles.py, driver_pool.py, extract.py, ssrf.py
│   │   ├── collectors/           # x_api.py, x_browser.py, threads_api.py, threads_browser.py,
│   │   │                         #   trends_unofficial.py, trends_browser.py, trends_official.py
│   │   ├── kb/                   # chunker.py, embedder.py, search.py, dashboard_sync.py
│   │   ├── nlp/                  # sentiment_model.py, sentiment_llm.py, lang.py, normalize.py
│   │   ├── analysis/             # market.py, campaign.py, digital.py, frameworks/
│   │   └── templates/            # library.py, renderer.py, validators.py, seeds/*.yaml
│   ├── worker/                   # queue.py, runner.py, handlers/*.py, scheduler.py
│   └── utils/                    # time.py, ids.py, text.py, tokens.py, crypto.py
├── scripts/                      # *.bat Windows + manage.py, run_server.py, run_worker.py, healthcheck.py
├── browser_profiles/             # (gitignored) user-data-dir per profil
├── models/                       # (gitignored) model ONNX sentimen / embedding lokal
└── tests/                        # unit/, integration/, contract/, eval/
```

## 4. Proses Runtime

| Proses | Perintah | Fungsi |
|---|---|---|
| `server` | `scripts\start-server.bat` → `uvicorn mcp_marketing.main:app --port 4100` | MCP endpoint, agent runtime (asyncio task), outbox sender |
| `worker` | `scripts\start-worker.bat` → `python scripts\run_worker.py` | Job crawl/scrape/ingest/analisis besar; satu-satunya proses yang membuka Chrome |
| `scheduler` | di dalam worker | Job berkala: retensi, sinkron vector Dashboard, refresh trends terjadwal |

Agent runtime berjalan di proses `server` (I/O-bound). Pekerjaan berat CPU/browser selalu di `worker` agar tool call Dashboard tetap ≤ 15 s.

## 5. Alur Utama

### 5.1 Dispatch task dari Dashboard (contract 1.0)

```mermaid
sequenceDiagram
  autonumber
  participant D as Dashboard
  participant M as MCP Server
  participant A as Agent Runtime
  participant G as LLM Gateway
  participant W as Worker
  D->>M: tools/call pm_dispatch_task_to_agent
  M->>M: simpan mcp.runs (status accepted), outbox started
  M-->>D: {accepted:true, mcp_run_id}
  M->>A: jalankan run (background)
  A->>D: webhook agent.task.started / status.changed (via outbox)
  A->>G: chat.completions (route sesuai profil, tools = toolset agent)
  G-->>A: tool_calls: x_search(...)
  A->>W: enqueue job crawl → handle
  W-->>A: job selesai: handle + ringkasan (≤ 1.500 token)
  A->>G: lanjut reasoning dengan ringkasan
  A->>D: webhook block.appended / subtask.created / comment.added
  A->>D: webhook agent.task.completed
```

### 5.2 Klien eksternal (Claude Desktop)

Klien terhubung ke `http://127.0.0.1:4100/mcp` dengan header `Authorization: Bearer mcpk_…`. Toolset yang terlihat mengikuti scope key. Tulis ke Dashboard memakai Service API dengan aktor = agent yang dipetakan pada key tersebut (`api_clients.dashboard_agent_id`).

## 6. Konvensi

- **Nama tool**: `<toolset>_<verb>[_<obj>]`, snake_case, kecuali tool kontrak (`pm_*`) yang namanya ditetapkan Dashboard.
- **Output tool**: selalu objek JSON (`structuredContent`), ditambah satu `TextContent` ringkas (≤ 3 kalimat) untuk klien yang tidak membaca structured content.
- **Error**: `isError: true`, `structuredContent = {code, message, retryable, details?}`. Kode baku: `VALIDATION_ERROR`, `NOT_FOUND`, `FORBIDDEN`, `RATE_LIMITED`, `BUDGET_EXCEEDED`, `UPSTREAM_ERROR`, `UPSTREAM_TIMEOUT`, `JOB_FAILED`, `CONFLICT`, `UNSUPPORTED_CONTRACT_VERSION`, `RUN_NOT_ACTIVE`, `PROFILE_BUSY`, `LOGIN_REQUIRED`.
- **Waktu**: input periode menerima `YYYY-MM-DD` atau ISO-8601; ditafsirkan di `Asia/Jakarta` bila tanpa offset; disimpan UTC.
- **ID**: UUID v4 untuk entitas; handle `h_<base32 10>`; job `j_<base32 10>`; report `r_<base32 10>`.

## 7. Port & Endpoint

| Komponen | Alamat |
|---|---|
| MCP Marketing | `http://127.0.0.1:4100/mcp` (Streamable HTTP), `/health/live`, `/health/ready`, `/metrics`, `/admin/*` (key admin) |
| LLM Gateway | `http://127.0.0.1:8000/v1` |
| Dashboard backend | `http://localhost:3000/api/v1` (dev) / `http://localhost:8088/api/v1` (docker nginx) |
| PostgreSQL | `127.0.0.1:5432`, database `marketing_dashboard` |

## 8. Environment Variables

```dotenv
# ---------- Server ----------
APP_ENV=dev
APP_HOST=127.0.0.1
APP_PORT=4100
LOG_LEVEL=INFO
LOG_DIR=./logs
TIMEZONE=Asia/Jakarta
# minimal 32 karakter; hash API key klien
MCP_KEY_PEPPER=CHANGE_ME_LONG_RANDOM
# toolset default untuk key baru (dipisah koma)
DEFAULT_TOOLSETS=toolbox,data,job
# URL publik server (link data_export), folder export, tool dev toolbox_ping
PUBLIC_BASE_URL=http://127.0.0.1:4100
STORAGE_DIR=./storage
ENABLE_DEV_TOOLS=false

# ---------- Database ----------
DATABASE_URL=postgresql+asyncpg://mcp_app:CHANGE_ME@127.0.0.1:5432/marketing_dashboard
DB_SCHEMA=mcp
DB_POOL_SIZE=10
# role migrasi (pemilik schema mcp); dipakai hanya oleh alembic
MIGRATION_DATABASE_URL=postgresql+asyncpg://mcp_owner:CHANGE_ME@127.0.0.1:5432/marketing_dashboard

# ---------- LLM Gateway ----------
LLM_GATEWAY_URL=http://127.0.0.1:8000/v1
LLM_GATEWAY_API_KEY=lgw_CHANGE_ME
LLM_ROUTE_FAST=fast
LLM_ROUTE_SMART=smart
LLM_ROUTE_EMBED=embed-default
LLM_TIMEOUT_S=120
LLM_CACHE_TTL_S=86400

# ---------- Dashboard (contract 1.x) ----------
DASHBOARD_API_URL=http://localhost:3000/api/v1
MCP_SERVER_API_KEY=CHANGE_ME_32B          # Dashboard -> MCP (sama dengan .env Dashboard)
MCP_WEBHOOK_SECRET=CHANGE_ME_32B          # MCP -> Dashboard HMAC
MCP_SERVICE_TOKEN=CHANGE_ME_32B           # MCP -> Agent API / Service API
CONTRACT_VERSION=1.0
WEBHOOK_MAX_RETRY_MINUTES=30
AGENT_RUN_TIMEOUT_MINUTES=110             # < 120 menit timeout Dashboard

# ---------- Agent ----------
AGENT_DEFAULT_RUN_TOKEN_BUDGET=120000
AGENT_MAX_STEPS=25
AGENT_HISTORY_COMPACT_AT_TOKENS=12000

# ---------- Token efficiency ----------
TOOL_OUTPUT_SOFT_TOKENS=1500
TOOL_OUTPUT_HARD_TOKENS=4000
INLINE_ITEMS_MAX=20
RESULT_TTL_DAYS=30
# maks token LLM internal per tool call (analisis deep)
TOOL_LLM_TOKENS_DEFAULT=20000
TOOL_LLM_TOKENS_DEEP=60000

# ---------- Retensi ----------
AUDIT_RETENTION_DAYS=180
RAW_RETENTION_DAYS=14
SOCIAL_RETENTION_DAYS=180

# ---------- Worker ----------
WORKER_CONCURRENCY=2
WORKER_POLL_INTERVAL_MS=500
JOB_DEFAULT_TIMEOUT_S=1800

# ---------- Browser / Selenium ----------
CHROME_BINARY=C:\Program Files\Google\Chrome\Application\chrome.exe
CHROMEDRIVER_PATH=.\bin\chromedriver.exe
BROWSER_PROFILES_DIR=.\browser_profiles
BROWSER_HEADLESS=false
BROWSER_PAGE_TIMEOUT_S=45
SCRAPE_RESPECT_ROBOTS=true
SCRAPE_PER_DOMAIN_RPM=20
SCRAPE_ALLOW_PRIVATE_NETWORK=false

# ---------- X / Threads / Trends ----------
X_BEARER_TOKEN=
X_MAX_READS_PER_DAY=2000
THREADS_ACCESS_TOKEN=
THREADS_USER_ID=
TRENDS_PROVIDER=unofficial
TRENDS_DEFAULT_GEO=ID
TRENDS_CACHE_TTL_S=86400

# ---------- NLP ----------
SENTIMENT_MODEL_DIR=./models/sentiment-id
SENTIMENT_LLM_THRESHOLD=0.60
EMBED_PROVIDER=gateway                    # gateway | local
EMBED_DIM=1536
LOCAL_EMBED_MODEL_DIR=./models/embed-local
```

Semua variabel wajib divalidasi saat startup (TASK-02); nilai `CHANGE_ME` di `APP_ENV=prod` membuat server gagal start.

## 9. Menjalankan Lokal (ringkas)

```bat
conda create -n mcp-marketing python=3.11 -y && conda activate mcp-marketing
pip install -r requirements.txt -r requirements-browser.txt -r requirements-ml.txt
copy .env.example .env   & rem isi CHANGE_ME
scripts\db-bootstrap.bat & rem sekali, sebagai superuser: role, schema mcp, extension vector, grant
scripts\migrate.bat
scripts\conda-run.bat python scripts\manage.py seed
scripts\conda-run.bat python scripts\manage.py create-client --name dashboard --toolsets pm
scripts\start-all.bat    & rem server + worker
```

Prasyarat: LLM Gateway berjalan dan sudah ada API key project `mcp-marketing` (`manage.py create-project/create-key` di Gateway); Dashboard `.env` memakai `MCP_SERVER_URL=http://localhost:4100/mcp` dan secret yang sama.
