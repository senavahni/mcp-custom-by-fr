# Daftar Task — MCP Marketing

Setiap task memiliki: tujuan, spesifikasi, checklist implementasi, pengujian, **Acceptance Criteria**, dan **Definition of Done**. DoD global berlaku tambahan untuk semua task ([README §5](../README.md#5-definition-of-done-global-berlaku-untuk-semua-task)).

| # | Task | Milestone | Prioritas | Estimasi | Dependensi |
|---|---|---|---|---|---|
| 01 | [Setup environment & struktur repo](TASK-01-setup-environment.md) | M0 Fondasi | P0 | 1 hari | — |
| 02 | [Config, logging, error model, request context](TASK-02-config-core-logging.md) | M0 Fondasi | P0 | 2 hari | 01 |
| 03 | [Database, schema `mcp`, pgvector & migrasi](TASK-03-database-migrations.md) | M0 Fondasi | P0 | 3 hari | 02 |
| 04 | [MCP server core: transport, auth, toolset registry](TASK-04-mcp-server-core.md) | M1 Platform | P0 | 3 hari | 02, 03 |
| 05 | [Token-efficiency layer: handle, kompaksi, cache, budget](TASK-05-token-efficiency-layer.md) | M1 Platform | P0 | 4 hari | 04 |
| 06 | [Klien LLM Gateway & route policy](TASK-06-llm-gateway-client.md) | M1 Platform | P0 | 2 hari | 02, (05) |
| 07 | [Job queue, worker & scheduler](TASK-07-job-queue-worker.md) | M1 Platform | P0 | 3 hari | 03, 05 |
| 08 | [Dashboard client: schema kontrak, Agent API, webhook outbox](TASK-08-dashboard-client-outbox.md) | M1 Platform | P0 | 4 hari | 03, 05 |
| 09 | [Tool kontrak Dashboard `pm_*` & registry run](TASK-09-pm-contract-tools.md) | M2 Agent & Dashboard | P0 | 3 hari | 04, 06, 07, 08 |
| 10 | [Agent runtime (loop tool-calling)](TASK-10-agent-runtime.md) | M2 Agent & Dashboard | P0 | 6 hari | 05, 06, 08, 09 |
| 11 | [Ekstensi Dashboard: Service API (contract 1.1)](TASK-11-dashboard-service-api.md) — *repo Dashboard* | M2 Agent & Dashboard | P0 | 5 hari | Modul 1 |
| 12 | [Tools template Notion `tpl_*`](TASK-12-notion-template-tools.md) | M2 Agent & Dashboard | P0 | 4 hari | 05, 06, 08, (11) |
| 13 | [Tools task & subtask `task_*`](TASK-13-task-subtask-tools.md) | M2 Agent & Dashboard | P0 | 3 hari | 05, 08, 11 |
| 14 | [Profil browser & web scraping Selenium](TASK-14-browser-profile-selenium-scraping.md) | M3 Data Collection | P0 | 5 hari | 05, 06, 07 |
| 15 | [Crawling X/Twitter (kata kunci & periode)](TASK-15-x-twitter-crawling.md) | M3 Data Collection | P0 | 4 hari | 07, 14 |
| 16 | [Crawling Threads (kata kunci & periode)](TASK-16-threads-crawling.md) | M3 Data Collection | P0 | 3 hari | 14, 15 |
| 17 | [Crawling Google Trends (kata kunci & periode)](TASK-17-google-trends-crawling.md) | M3 Data Collection | P0 | 3 hari | 05, 07, 14 |
| 18 | [Tools akses database Dashboard (CRUD)](TASK-18-database-crud-tools.md) | M4 Data Access | P0 | 4 hari | 03, 05, (11) |
| 19 | [Vector DB (pgvector) & tools `kb_*`](TASK-19-vectordb-tools.md) | M4 Data Access | P0 | 5 hari | 03, 06, 07 |
| 20 | [Analisis sentimen](TASK-20-sentiment-analysis.md) | M5 Analysis | P0 | 4 hari | 06, 07, 15, 16 |
| 21 | [Analisis pasar & laporan](TASK-21-market-analysis.md) | M5 Analysis | P0 | 4 hari | 14–17, 19, 20 |
| 22 | [Analisis strategi campaign](TASK-22-campaign-strategy-analysis.md) | M5 Analysis | P0 | 3 hari | 20, 21 |
| 23 | [Analisis strategi digital marketing](TASK-23-digital-marketing-strategy.md) | M5 Analysis | P0 | 4 hari | 12, 13, 21, 22 |
| 24 | [Observability & laporan token](TASK-24-observability-usage.md) | M6 Production | P0 | 3 hari | 04–07 |
| 25 | [Security hardening & kepatuhan data](TASK-25-security-hardening.md) | M6 Production | P0 | 3 hari | 04–23 |
| 26 | [Testing, QA, contract test & eval token](TASK-26-testing-qa-eval.md) | M6 Production | P0 | 5 hari | 01–25 |
| 27 | [Deployment, operasional & integrasi klien](TASK-27-deployment-client-integration.md) | M6 Production | P0 | 3 hari | 24, 25, 26 |

**Total estimasi:** ± 96 person-days + 3 pd spike Sprint 0 (termasuk 5 pd di repo Dashboard untuk TASK-11).

## Pemetaan kebutuhan → task

| Kebutuhan dari brief | Task |
|---|---|
| Hemat token LLM lokal & API eksternal | 05, 06, 10, 24, 26 |
| Terhubung ke LLM Gateway | 06 |
| Terhubung ke Marketing Dashboard (agentic AI) | 08, 09, 10, 11 |
| Generate template Notion seperti Modul 1 | 12 |
| Mengatur task & subtask | 13 (+ 11) |
| Scraping website (Selenium + chromedriver + profil jelas) | 14 |
| Crawling X/Twitter per periode & kata kunci | 15 |
| Crawling Threads per periode & kata kunci | 16 |
| Crawling Google Trends per periode & kata kunci | 17 |
| CRUD database Dashboard | 18 |
| Akses vector DB | 19 |
| Analisis sentimen | 20 |
| Analisis pasar | 21 |
| Analisis strategi campaign | 22 |
| Analisis strategi digital marketing | 23 |
