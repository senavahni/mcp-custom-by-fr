# TASK-22 — Tool Analisis Strategi Campaign (`campaign_analyze`)

| Item | Nilai |
|---|---|
| Milestone | M5 Analysis |
| Prioritas | P0 |
| Estimasi | 3 hari |
| Dependensi | TASK-20, TASK-21 |
| Referensi | MCP_SPEC §4.10 · USE_CASES UC-16 · PRD FR-24 |

## Tujuan
Membantu tim merencanakan campaign (`plan`) dan mengevaluasi hasilnya (`evaluate`) dengan kerangka yang konsisten (funnel, KPI SMART, alokasi anggaran), di mana kalkulasi angka dilakukan kode dan LLM hanya menyusun rekomendasi.

## Spesifikasi

### Input
```jsonc
{
  "mode": "plan" | "evaluate",
  "campaign": {
    "name": "Launch Kopi Gula Aren", "objective": "awareness|consideration|conversion|retention",
    "audience": "18–30, urban, Jabodetabek", "channels": ["instagram","tiktok","x"],
    "budget_idr": 50000000, "period": {"since": "2026-11-01", "until": "2026-12-15"},
    "kpis": [{"metric": "reach", "target": 2000000}], "assets": "opsional, ≤ 1.000 char"
  },
  "results": { "instagram": {"impressions": 1200000, "reach": 800000, "clicks": 9000, "spend_idr": 18000000, "conversions": 320} },
  "handles": ["h_…"],                // crawl selama campaign (sentimen/percakapan)
  "benchmarks": "default|custom",    // default = tabel benchmark internal per kanal (config YAML, dapat diubah tim)
  "report_ids": ["r_…"]               // laporan pasar sebagai konteks
}
```

### Kalkulasi deterministik (`services/analysis/campaign.py`)
- Validasi konsistensi `results` (clicks ≤ impressions, conversions ≤ clicks, spend ≥ 0) → `VALIDATION_ERROR` dengan field.
- Metrik turunan per kanal: CTR, CPC, CPM, CVR, CPA, ROAS (bila `revenue_idr`), frekuensi (impressions/reach); total & bobot.
- `plan`: alokasi anggaran awal berbasis bobot objective × kanal (matriks di `config/campaign_weights.yaml`), estimasi jangkauan dari benchmark CPM; jadwal fase funnel (teaser/launch/sustain) berdasar panjang periode.
- `evaluate`: capaian vs KPI (%), vs benchmark, kanal paling efisien (CPA/CPM), tren sentimen selama campaign (dari `sentiment_summary` handle), anomali (hari dengan lonjakan volume/negatif).
- Pembuatan KPI SMART otomatis bila `kpis` kosong (angka dari benchmark × anggaran).

### Sintesis LLM
- `smart`, `chat_json`, prompt `campaign_plan.v1` / `campaign_eval.v1`; input = tabel metrik (kolom-baris) + ringkasan laporan pasar (`report.summary` saja) + ringkasan sentimen; ≤ 4.000 token.
- Output `CampaignReport{funnel[{stage, goal, channels, messages[≤3]}], kpi_smart[], channel_mix[{channel, role, budget_pct, rationale}], budget_split{cols,rows}, timeline_phases[], risks[], recommendations[≤7], next_experiments[≤3]}`.
- Angka anggaran/target dari LLM **dikunci** ke hasil kalkulasi Python (LLM hanya boleh menyesuaikan alokasi ±10 poin persen dengan alasan; di luar itu dikoreksi otomatis dan dicatat).

### Simpan & terbitkan
- `analysis_reports` kind `campaign_plan`/`campaign_eval`; `report_publish` (TASK-21) untuk menerbitkan; opsi `apply_tasks=true` di mode `plan` → `tpl_render(campaign_brief)` + task per fase (melalui TASK-12/13).

## Checklist Implementasi
- [ ] Schema input/output.
- [ ] Kalkulator metrik + validasi konsistensi + benchmark YAML.
- [ ] Alokasi anggaran & fase (plan).
- [ ] Evaluasi vs KPI/benchmark + anomali (evaluate).
- [ ] Prompt plan/eval + penguncian angka.
- [ ] Integrasi `apply_tasks`.

## Pengujian
- Kalkulator: 10 kasus angka (termasuk pembagian nol) → nilai benar.
- Penguncian angka: LLM mengusulkan alokasi menyimpang 30 poin → dikoreksi.
- Live: UC-16 plan & evaluate; token tercatat (target ≤ 6.000 per panggilan).

## Acceptance Criteria
1. UC-16 mode `plan` dan `evaluate` menghasilkan laporan lengkap; semua angka di laporan sama dengan hasil kalkulator.
2. Mode `plan` + `apply_tasks` membuat brief & task fase di Dashboard.
3. Rubrik reviewer Marketing rata-rata ≥ 4/5.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; coverage ≥ 85%.
- [ ] Benchmark default disetujui tim Marketing & terdokumentasi sumbernya.
- [ ] Review disetujui.
