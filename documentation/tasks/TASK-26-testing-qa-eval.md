# TASK-26 — Testing, QA, Contract Test & Eval Penghematan Token

| Item | Nilai |
|---|---|
| Milestone | M6 Production |
| Prioritas | P0 |
| Estimasi | 5 hari |
| Dependensi | TASK-01 s.d. TASK-25 |
| Referensi | PRD §3 (G1–G5), NFR-11 · USE_CASES (semua) · README §5 |

## Tujuan
Membuktikan bahwa MCP benar (unit/integration), patuh kontrak Dashboard (contract), bekerja end-to-end untuk semua use case (E2E/UAT), dan **benar-benar menghemat token** dengan kualitas setara (eval).

## Spesifikasi

### Piramida test
| Lapisan | Marker | Lingkup | Kapan |
|---|---|---|---|
| Unit | (default) | services, tools (dependensi di-mock), parser, kalkulator | setiap commit (`check.bat`) |
| Integration | `integration` | PostgreSQL `marketing_dashboard_test` (+ schema Dashboard dari `migration.sql`), worker, outbox dengan server palsu | setiap PR |
| Browser | `browser` | Chrome nyata vs server fixture lokal | harian / sebelum rilis |
| Contract | `contract` | Dashboard lokal (docker compose) + MCP: 5 tool `pm_*`, 7 event webhook, Agent API, Service API | sebelum rilis & saat kontrak berubah |
| Live | `live` | Gateway nyata, X/Threads/Trends (kuota kecil) | manual, sebelum rilis |
| Eval | `eval` | Penghematan token & kualitas | sebelum rilis |

### Contract test (`tests/contract/`)
- Fixture payload (TASK-08) divalidasi di kedua sisi.
- Skenario: dispatch → started → status → block.appended (append+update conflict) → subtask.created (client_ref) → comment.added → completed; mention; prompt block; cancel (webhook terlambat → 409 → stop); estimation; retry webhook 5xx; signature salah → 401.
- Laporan `docs/contract-report.md` (versi kontrak, commit kedua repo, hasil).

### E2E/UAT (`tests/e2e/`, checklist manual pendamping)
- UC-01 s.d. UC-18 dijalankan dengan data contoh "Kopi Nusantara"; bukti (screenshot Dashboard, `report_id`, `job_id`, token) dicatat di `docs/uat-report.md`.
- Pengguna UAT: 1 Marketing Manager, 2 Marketing Staff; kriteria PRD §3.

### Eval penghematan token (`tests/eval/token/`)
- **10 skenario baku**, masing-masing dengan 2 varian:
  - *Baseline*: agent yang sama diberi tool "naif" (mengembalikan data mentah penuh: semua post, HTML/teks halaman utuh, deret Trends harian penuh, dokumen brief utuh) dan tanpa sentimen lokal.
  - *MCP*: toolset MCP Marketing normal.
  - Skenario: (1) ringkas percakapan X 30 hari, (2) bandingkan 3 kata kunci Trends 12 bulan, (3) sentimen 1.000 post, (4) analisis 5 halaman kompetitor, (5) task overdue minggu ini, (6) jawab pertanyaan dari Project Brief panjang, (7) market analysis 3 kompetitor, (8) rencana campaign, (9) strategi digital 3 bulan + apply, (10) UC-01 content calendar.
- Data dibekukan (fixture handle/DB snapshot) agar baseline & MCP memakai data identik; LLM route sama (`smart`).
- Metrik: total token (prompt+completion, termasuk LLM internal tool), biaya USD (dari Gateway), latensi, jumlah tool call, dan **skor kualitas** (rubrik 1–5 oleh 2 reviewer buta varian + LLM-judge sebagai pendamping, bukan pengganti).
- Target lulus: total token MCP ≤ 30% baseline (penghematan ≥ 70%) agregat 10 skenario; skor kualitas MCP ≥ baseline (selisih rata-rata ≥ −0,2).
- Laporan `docs/eval/token-savings.md` + angka dari `/admin/savings`.

### Performa
- Overhead pipeline p95 < 100 ms (NFR-03); `data_get` 10.000 baris p95 < 300 ms; 20 tool call paralel dari 4 klien tanpa error.

### Kualitas kode
- Coverage total ≥ 80% (`services/`, `tools/`, `server/`, `worker/`); mypy strict modul inti; ruff bersih.

## Checklist Implementasi
- [ ] Fixture DB test (schema Dashboard + mcp) & factory data.
- [ ] Lengkapi unit/integration yang kurang hingga coverage target.
- [ ] Suite contract + `docs/contract-report.md`.
- [ ] Suite E2E + UAT + `docs/uat-report.md`.
- [ ] Harness eval token (tool naif, snapshot data, runner, rubrik) + laporan.
- [ ] Uji performa ringan (`scripts/bench_tools.py`).

## Acceptance Criteria
1. Semua suite (unit, integration, browser, contract) hijau pada commit rilis.
2. UAT: PRD G1–G5 tercapai dan ditandatangani perwakilan Marketing.
3. Eval: penghematan token agregat ≥ 70% dengan kualitas tidak lebih buruk (selisih ≥ −0,2).
4. Coverage ≥ 80%.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi.
- [ ] Laporan contract, UAT, eval, performa terbit di `docs/`.
- [ ] Bug kritis = 0; bug mayor punya rencana perbaikan terjadwal.
