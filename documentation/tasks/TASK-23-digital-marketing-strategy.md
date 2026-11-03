# TASK-23 — Tool Analisis Strategi Digital Marketing (`dm_strategy`)

| Item | Nilai |
|---|---|
| Milestone | M5 Analysis |
| Prioritas | P0 |
| Estimasi | 4 hari |
| Dependensi | TASK-12, TASK-13, TASK-21, TASK-22 |
| Referensi | MCP_SPEC §4.10 · USE_CASES UC-17 · PRD FR-25, G5 |

## Tujuan
Menyusun strategi digital marketing yang utuh (kerangka SOSTAC atau RACE) berbasis data hasil analisis sebelumnya, lengkap dengan pilar konten dan kalender outline, lalu — bila diminta — **langsung menerapkannya ke Dashboard** sebagai Project Brief dan task terjadwal.

## Spesifikasi

### Input
`business{name, category, usp, price_range, current_channels?[]}`, `goals[] (≤ 5, teks bebas atau metrik)`, `audience?`, `budget_idr?`, `period{since, until}`, `channels?[]`, `constraints?` (≤ 1.000 char), `inputs?{report_ids[], handles[]}`, `framework: SOSTAC|RACE`, `language='id'`, `apply_to?{project_id, template: bool, tasks: bool, granularity: week|month}`.

### Pipeline (`services/analysis/digital.py`)
1. **Konteks padat** (tanpa LLM): ambil `summary` + `key_numbers` dari `report_ids` (bukan isi penuh), statistik ringkas dari `handles` (`social_stats`, `sentiment_summary`), top related queries Trends bila ada; total ≤ 3.000 token. Bila tidak ada input sama sekali → peringatan `notes:["no_data_inputs"]` dan strategi ditandai "berbasis asumsi".
2. **Kerangka**:
   - SOSTAC: Situation, Objectives (SMART), Strategy (STP, positioning, channel strategy), Tactics (per kanal: format, frekuensi, pesan), Action (kalender & peran), Control (KPI, dashboard, cadence review).
   - RACE: Plan, Reach, Act, Convert, Engage — dengan KPI per tahap.
3. **Sintesis** (`smart`, `chat_json`, prompt `dm_strategy.v1`): output `DigitalStrategy{sections[], content_pillars[{name, share_pct, formats[], example_topics[≤3]}], channel_plan[{channel, objective, cadence_per_week, kpi}], calendar_outline[{period_label, theme, key_activities[≤4]}], budget_split?{cols,rows}, kpis[], risks[]}`.
4. **Validasi deterministik**: `share_pct` pilar = 100 (dinormalisasi), `calendar_outline` mencakup seluruh periode tanpa celah (granularitas week/month), budget split = budget input (dikoreksi), kanal hanya dari `channels` bila diberikan.
5. **Simpan** `analysis_reports` kind `digital_strategy`.

### Penerapan ke Dashboard (`apply_to`)
- `template=true` → buat/isi Project Brief dari template `digital_strategy_brief` (ditambahkan ke seed TASK-12) dengan variabel dari hasil strategi (`tpl_render`, mode apply).
- `tasks=true` → satu task per periode kalender (judul "Minggu 1 — {theme}"), subtask per aktivitas kunci (≤ 4), tanggal hari kerja sesuai periode, tag kanal; maks 20 task per batch (pecah otomatis).
- Dalam run → webhook; luar run → Service API. Hasil: `{report_id, brief_blocks, tasks_created, url}`.
- Preview dulu: bila `apply_to` ada dan `confirm` tidak `true`, tool mengembalikan preview outline (jumlah task, rentang tanggal) tanpa menulis — agent/klien memanggil ulang dengan `confirm: true`.

## Checklist Implementasi
- [ ] Schema input/output + `DigitalStrategy`.
- [ ] Penyusun konteks padat dari laporan & handle.
- [ ] Prompt SOSTAC & RACE + validasi deterministik.
- [ ] Template seed `digital_strategy_brief`.
- [ ] Penerapan ke Dashboard (brief + task + subtask) dengan preview/confirm.

## Pengujian
- Validasi: pilar 90% → dinormalisasi 100%; kalender bercelah → ditambal; kanal di luar daftar → dibuang.
- Konteks padat ≤ 3.000 token untuk 3 laporan + 2 handle.
- Live UC-17: strategi 3 bulan, `granularity=week` → 13 task di Timeline Dashboard; token LLM ≤ 10.000.

## Acceptance Criteria
1. UC-17 end-to-end: strategi tersimpan, Project Brief terisi, 13 task mingguan + subtask tampil di Timeline.
2. Tidak ada penulisan ke Dashboard tanpa `confirm: true`.
3. Rubrik reviewer Marketing rata-rata ≥ 4/5 untuk 3 bisnis contoh berbeda.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; coverage ≥ 85%.
- [ ] Contoh output SOSTAC & RACE di `docs/examples/`.
- [ ] Review disetujui (termasuk Marketing Manager).
