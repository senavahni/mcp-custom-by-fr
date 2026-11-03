# TASK-24 — Observability: Health, Metrics, Laporan Token & Penghematan

| Item | Nilai |
|---|---|
| Milestone | M6 Production |
| Prioritas | P0 (laporan penghematan P1) |
| Estimasi | 3 hari |
| Dependensi | TASK-04, TASK-05, TASK-06, TASK-07 |
| Referensi | PRD FR-01, FR-26, NFR-10 · DATABASE §4.1, §6 |

## Tujuan
Operator dapat mengetahui apakah MCP sehat, berapa token/biaya yang dipakai per tool/klien/agent, dan **berapa token yang dihemat** dibanding baseline — bukti terukur untuk tujuan utama proyek.

## Spesifikasi

### Health
- `GET /health/live` → 200 bila proses hidup.
- `GET /health/ready` → cek DB (`SELECT 1` + ekstensi vector), Gateway (`GET /v1/models` cache 30 s), Dashboard (`GET /api/v1/health` cache 30 s), worker heartbeat (≤ 60 s), outbox backlog; respons `{status: ok|degraded|down, checks{…}}`; `down` hanya bila DB gagal.

### Metrics Prometheus (`/metrics`, opsional basic auth)
- `mcp_tool_calls_total{tool,status}`, `mcp_tool_latency_seconds{tool}` (histogram), `mcp_tool_output_tokens{tool}` (histogram), `mcp_tool_cache_hits_total{tool}`.
- `mcp_llm_tokens_total{purpose,route,type=prompt|completion}`, `mcp_llm_latency_seconds{route}`, `mcp_llm_errors_total{code}`.
- `mcp_jobs{kind,status}` (gauge), `mcp_job_duration_seconds{kind}`, `mcp_runs_active`, `mcp_runs_total{trigger,status}`.
- `mcp_webhook_sent_total{status}`, `mcp_outbox_pending`, `mcp_external_units_total{provider}` (post X/Threads, permintaan Trends).
- `mcp_browser_profiles_locked`.

### Rollup & laporan
- Job `usage_rollup` (tiap 10 menit) agregasi `tool_calls` + `llm_calls` → `usage_daily`.
- Admin API:
  - `GET /admin/usage?since&until&group_by=tool|client|agent|day` → calls, error rate, cache hit, output tokens, llm tokens, cost USD, external units.
  - `GET /admin/savings?since&until` → per tool: `baseline_tokens`, `output_tokens + llm_tokens`, penghematan % (definisi di MCP_SPEC §3 / TASK-05), plus total.
  - `GET /admin/runs/{run_id}` → langkah, tool, token per langkah (debug agent).
- CLI: `manage.py usage-report --month 2026-11 --format md` → laporan markdown untuk manajemen.

### Logging operasional
- Log `warning` bila: tool output dipotong keras, cache miss rate > 80% untuk tool ber-cache (per jam), job gagal berulang, outbox backlog > 50, Gateway `degraded`.
- Korelasi: `request_id` MCP dikirim ke Gateway (`X-Request-Id`) dan Dashboard → bisa ditelusuri lintas 3 sistem.

## Checklist Implementasi
- [ ] Endpoint health live/ready.
- [ ] Semua metrics di atas + registrasi di pipeline/worker/outbox.
- [ ] Rollup + endpoint usage/savings/runs.
- [ ] CLI `usage-report`.
- [ ] Warning operasional.
- [ ] `docs/operations.md#monitoring` (contoh query PromQL & panel Grafana opsional).

## Pengujian
- Ready: DB mati → `down`; Gateway mati → `degraded`.
- Metrics bertambah sesuai skenario tool call (test registry Prometheus).
- Rollup: 1.000 tool_calls sintetis → angka `usage_daily` = agregasi SQL manual.
- Savings: dataset sintetis dengan baseline diketahui → persentase benar.

## Acceptance Criteria
1. Operator dapat menjawab "berapa token & biaya MCP bulan ini, per tool & per agent" dari satu perintah/endpoint.
2. Laporan penghematan tersedia dan dipakai di eval TASK-26.
3. Satu request dapat ditelusuri di log MCP, Gateway, dan Dashboard memakai `request_id` yang sama.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; coverage ≥ 80%.
- [ ] Dokumentasi monitoring terbit.
- [ ] Review disetujui.
