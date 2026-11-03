# MCP Marketing

Server **Model Context Protocol** (Python 3.11) yang menghubungkan **LLM Gateway** (`llm-genai`) dan
**Marketing Dashboard** (`marketingdashboardjtp`) menjadi agentic AI yang hemat token.

- Dokumen produk & desain: [documentation/](documentation/README.md) (PRD, arsitektur, MCP_SPEC, DATABASE, 27 task).
- Status: **TASK-01 – TASK-10 selesai** (fondasi, server MCP, token-efficiency layer, klien Gateway,
  job queue, kontrak Dashboard, tool `pm_*`, agent runtime). Lihat [CHANGELOG.md](CHANGELOG.md).

## Prasyarat

| Komponen | Keterangan |
|---|---|
| Python 3.11 (conda) | env `D:\Data.Fahrul\.conda\envs\mcp` (sama dengan versi LLM Gateway) |
| PostgreSQL ≥ 16 | database Dashboard `marketing_dashboard` (sudah termigrasi Prisma) |
| pgvector | **opsional untuk TASK-01..10**; wajib mulai TASK-19 (vector DB) |
| LLM Gateway | `http://127.0.0.1:8000/v1` + API key project `mcp-marketing` |
| Dashboard backend | `http://localhost:3000/api/v1`, `.env` berisi secret MCP yang sama |

## Instalasi

```bat
rem 1. dependensi (env conda "mcp" sudah dibuat dengan Python 3.11)
D:\Data.Fahrul\.conda\envs\mcp\python.exe -m pip install -r requirements.txt
rem    (TASK-14+ / TASK-19+) -r requirements-browser.txt -r requirements-ml.txt

rem 2. konfigurasi: salin lalu isi semua nilai CHANGE_ME
copy .env.example .env
```

Tiga secret Dashboard di `.env` **harus sama** dengan `.env` Dashboard: `MCP_SERVER_API_KEY`,
`MCP_WEBHOOK_SECRET`, `MCP_SERVICE_TOKEN` (ketiganya berbeda satu sama lain, ≥ 32 karakter).
Di Dashboard: `MCP_SERVER_URL=http://localhost:4100/mcp`.

```bat
rem 3. database (sekali, sebagai superuser postgres) -> role mcp_owner/mcp_app, schema mcp, grant
scripts\db-bootstrap.bat marketing_dashboard postgres
rem    isi DATABASE_URL (mcp_app) & MIGRATION_DATABASE_URL (mcp_owner) di .env, lalu:
scripts\migrate.bat
scripts\conda-run.bat python scripts\manage.py seed
scripts\conda-run.bat python scripts\manage.py db-check

rem 4. klien MCP lain (Claude Desktop, Cursor, …) — key tampil sekali
scripts\conda-run.bat python scripts\manage.py create-client --name claude-desktop --toolsets toolbox,data,job

rem 5. jalankan server (:4100/mcp) + worker
scripts\start-all.bat
```

Cek Gateway: `scripts\conda-run.bat python scripts\smoke_gateway.py`.

## Pengembangan

| Perintah | Fungsi |
|---|---|
| `scripts\check.bat` | ruff, format, mypy, unit test, secret scan |
| `scripts\test.bat -m integration` | test integrasi PostgreSQL (butuh `TEST_ADMIN_DATABASE_URL` superuser; membuat ulang DB `marketing_dashboard_test` dan role `mcp_owner_test`/`mcp_app_test`) |
| `scripts\test.bat -m contract` | fixture kontrak divalidasi dengan Zod asli Dashboard (butuh `node` + build `dist/` Dashboard) |
| `python scripts\export_contract_fixtures.py` | bangkitkan ulang fixture kontrak |

Panduan lanjutan: [operasional](documentation/operations.md) · [membuat tool hemat token](documentation/dev-guide-tools.md)
· [pemakaian LLM](documentation/llm-usage.md) · [agent](documentation/agents.md) · [laporan kontrak](documentation/contract-report.md).
