# TASK-09 — Tool Kontrak Dashboard (`pm_*`) & Registry Run

| Item | Nilai |
|---|---|
| Milestone | M2 Agent & Dashboard |
| Prioritas | P0 |
| Estimasi | 3 hari |
| Dependensi | TASK-04, TASK-06, TASK-07, TASK-08 |
| Referensi | `M1_MCP_INTEGRATION.md` §3.1–3.5, §4 · MCP_SPEC §4.2 · USE_CASES UC-01–UC-05 |

## Tujuan
Mengimplementasikan 5 tool yang dipanggil Dashboard persis sesuai kontrak 1.0, termasuk idempotensi, batas waktu respons, dan siklus hidup run di sisi MCP — menggantikan Mock MCP Dashboard (M1_TASK_009).

## Spesifikasi

### Umum untuk semua `pm_*`
- Hanya klien `dashboard` (toolset `pm`).
- `contract_version` major ≠ 1 → `isError`, `UNSUPPORTED_CONTRACT_VERSION`, `retryable:false`.
- Output selalu `structuredContent` sesuai output schema; error `isError:true` + `{code, message, retryable}`.

### Run registry (`services/agent/runs.py`)
- `accept(run_input) -> mcp_run_id`: upsert `mcp.runs` by `run_id` (idempoten). Bila sudah ada dan status `accepted/running` → kembalikan `mcp_run_id` lama, tidak menjalankan ulang. Bila `completed/failed/cancelled` → `isError CONFLICT retryable:false`.
- Menyimpan `input` terpangkas (context_blocks → teks + outline; maks 100 KB), `callback`, `token_budget` dari profil agent.
- Status: `accepted → running → completed | failed | cancelled | cancelled_by_dashboard`.

### 3.1 `pm_dispatch_task_to_agent`
1. Validasi input; cek `agent_key` di `agent_profiles` aktif → bila tidak: `UNKNOWN_AGENT`, `retryable:false`.
2. `runs.accept(...)`; emit outbox `agent.task.started` (seq 1) **sebelum** membalas.
3. Jadwalkan runtime (`asyncio.create_task(runtime.start(run_id))`, TASK-10).
4. Balas `{accepted:true, mcp_run_id}` — target p95 < 1 s, maks 15 s.

### 3.2 `pm_request_agent_estimation`
1. Heuristik (`services/agent/estimation.py`): basis 1 hari; +0,5 hari per subtask yang ada; +1 hari per 1.500 karakter deskripsi; kata kunci (riset/analisis +1, kalender/konten mingguan ×jumlah minggu, laporan +1); hanya hari kerja Senin–Jumat dari `earliest_start_date` (zona `timezone` input).
2. LLM `fast` (`chat_json`, prompt `estimate.v1`, input: judul, deskripsi ≤ 1.500 char, subtask judul, heuristik) → `{estimated_days, confidence, message(≤ 200 char, Bahasa Indonesia)}`; dibatasi 0,5×–2× heuristik.
3. Hitung `start_date = earliest_start_date` (atau hari kerja berikutnya), `end_date` = + `estimated_days` hari kerja − 1.
4. Batas waktu internal 20 s; LLM gagal → hasil heuristik `confidence ≤ 0.4`, `message` generik.
5. Tidak mengubah apa pun di Dashboard (Dashboard yang menerapkan).

### 3.3 `pm_notify_agent_mention`
- Seperti dispatch dengan `trigger=mention`; menyimpan `comment` + `thread` (≤ 10 komentar terakhir, masing-masing ≤ 1.000 char). Rate limit kontrak (10 mention/task/jam) ditegakkan Dashboard; MCP tetap menolak run mention ke-11 per task/jam dengan `RATE_LIMITED retryable:true`.

### 3.4 `pm_execute_prompt_block`
- `trigger=prompt_block`; simpan `prompt_block{id, prompt, version}`; runtime memakai mode ringan (TASK-10).

### 3.5 `pm_cancel_agent_run`
- Set `runs.cancel_requested=true`, `cancel_reason`; `jobs.cancel_run_jobs(run_id)`; outbox pending run → `discarded`; status run `cancelled`.
- Run tak dikenal → tetap `{cancelled:true}`. Balas ≤ 2 s.

### Supervisor
- Background task tiap 60 s: run `running` tanpa langkah/event > `AGENT_RUN_TIMEOUT_MINUTES` → runtime dihentikan, emit `agent.task.failed{error_code:"RUN_TIMEOUT", retryable:true}`.
- Saat server start: run `accepted/running` yang tidak memiliki task asyncio → dilanjutkan (resume dari `run_steps`) atau gagal `SERVER_RESTARTED retryable:true` bila resume tidak memungkinkan.

## Checklist Implementasi
- [ ] Run registry + idempotensi.
- [ ] 5 tool + validasi versi kontrak.
- [ ] Estimasi heuristik + prompt `estimate.v1` + fallback.
- [ ] Supervisor timeout & resume saat restart.
- [ ] Metrics: `mcp_runs_total{trigger,status}`, `mcp_tool_latency_seconds{tool}`.

## Pengujian
- Unit estimasi: kalender kerja (task mulai Jumat 3 hari → Jumat, Senin, Selasa), batas 0,5×–2×, fallback LLM gagal.
- Dispatch dua kali `run_id` sama → satu run, `mcp_run_id` sama.
- `agent_key` tidak dikenal → `isError`, `UNKNOWN_AGENT`.
- Cancel selama job crawl berjalan → job `cancelled`, tidak ada webhook setelahnya.
- Contract E2E dengan Dashboard lokal (UC-01, UC-02, UC-03, UC-05): status task, badge agent, komentar, subtask sesuai kontrak.

## Acceptance Criteria
1. Dashboard berjalan dengan `MCP_SERVER_URL` → MCP Marketing; UC2–UC4 Modul 1 lulus E2E tanpa Mock MCP.
2. p95 respons `pm_dispatch_task_to_agent` < 1 s; `pm_request_agent_estimation` < 20 s.
3. Estimasi tidak pernah melanggar `start_date ≥ earliest_start_date` dan `end_date ≥ start_date` (property test 1.000 kasus).
4. Restart server di tengah run tidak meninggalkan run "menggantung" (selesai/dilanjutkan/gagal eksplisit).

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi.
- [ ] Contract test lulus terhadap Dashboard lokal; hasil dicatat di `docs/contract-report.md`.
- [ ] Status M1_TASK_009 di README Dashboard dapat diperbarui ("digantikan MCP Marketing").
- [ ] Review tim Dashboard & MCP disetujui.
