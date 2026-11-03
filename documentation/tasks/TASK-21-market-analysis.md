# TASK-21 — Tool Analisis Pasar (`market_analyze`, `report_*`)

| Item | Nilai |
|---|---|
| Milestone | M5 Analysis |
| Prioritas | P0 |
| Estimasi | 4 hari |
| Dependensi | TASK-14, TASK-15, TASK-16, TASK-17, TASK-19, TASK-20 |
| Referensi | MCP_SPEC §4.10 · DATABASE §4.7 (`analysis_reports`) · USE_CASES UC-15 · PRD FR-23 |

## Tujuan
Satu tool yang mengorkestrasi pengumpulan data (Trends, X, Threads, web), menghitung metrik pasar secara deterministik, lalu memakai LLM **sekali** untuk sintesis (SWOT, matriks kompetitor, peluang) — menghasilkan laporan tersimpan yang bisa diterbitkan ke Dashboard.

## Spesifikasi

### Input `market_analyze`
`subject, competitors[] ≤ 5, period{since, until}, geo='ID', sources{trends=true, x=true, threads=true, web_urls?[] ≤ 20}, use_handles?[], keywords_override?{brand: [kw]}, frameworks?[swot, competitor_matrix, share_of_voice, porter5, pestel], depth: quick|standard|deep, language='id'`

### Kedalaman
| depth | Data | LLM |
|---|---|---|
| `quick` | Trends + data handle yang diberikan saja (tanpa crawl baru) | 1× `smart`, input ≤ 3.000 token |
| `standard` | + X & Threads ≤ 300 post/brand, sentimen model | 1× `smart`, input ≤ 6.000 token |
| `deep` | + ≤ 1.000 post/brand, web_urls, aspek, KB | 2× `smart` (analisis + kritik/perbaikan), input ≤ 10.000 token |

### Pipeline (`services/analysis/market.py`, job induk dengan job anak)
1. **Kata kunci**: per brand = nama + `keywords_override` + (bila ada) `mcp.competitors.keywords`; kategori = `subject` + 2 variasi.
2. **Kumpulkan** (job anak paralel, reuse handle/cache bila ada data periode sama): `trends_interest` (subject + brand, ≤ 5 per permintaan dengan anchor), `x_search`, `threads_search`, `web_scrape` (`web_urls`).
3. **Hitung** (Python):
   - Share of voice (jumlah post & engagement per brand), share of search (Trends).
   - Sentimen per brand (`sentiment_analyze` model/auto), net sentiment, aspek teratas.
   - Momentum: slope tren & perubahan % vs periode sebelumnya; volume post per minggu.
   - Top hashtag/tema per brand (n-gram 1–2 kata dengan TF-IDF antar brand, tanpa LLM).
4. **Sampel**: per brand ≤ 10 post (stratified: positif/negatif/engagement tinggi), ≤ 280 char; ringkasan web per URL ≤ 400 char (sudah diekstrak).
5. **Sintesis** (`smart`, `chat_json`, prompt `market_report.v1`): input = tabel angka (format kolom-baris) + sampel + framework terpilih; output schema `MarketReport{executive_summary[≤8], swot{s,w,o,t}[≤5 each], competitor_matrix{cols,rows}, opportunities[{title, rationale, evidence_refs}], risks[], recommended_actions[{action, priority, owner_hint}] }`; setiap klaim wajib merujuk angka/sampel (`evidence_refs` = id metrik/sampel) — validasi: ref harus ada.
6. **Simpan** `analysis_reports` (kind `market`) dengan `key_numbers`, `content.sections` (markdown per bagian), `input_handles`, `llm_tokens`.

### `report_get(report_id, section?, format)` & `report_publish(report_id, target, run_id?)`
- `outline` (judul bagian + 1 kalimat) default; `section` untuk mengambil bagian tertentu saja (hemat token).
- `report_publish` → `md_to_blocks` → tulis ke Project Brief/task (webhook dalam run, Service API di luar run); tabel angka menjadi blok `table`; menambahkan blok `bookmark` sumber web bila ada.

## Checklist Implementasi
- [ ] Skema input/output + `MarketReport`.
- [ ] Orkestrasi job anak + reuse handle/cache.
- [ ] Metrik deterministik (SoV, share of search, sentimen, momentum, TF-IDF tema).
- [ ] Sampling stratified.
- [ ] Prompt `market_report.v1` + validasi evidence.
- [ ] `report_get`, `report_publish`.
- [ ] Mode `deep` dengan putaran kritik.

## Pengujian
- Metrik dengan dataset sintetis (3 brand) → nilai SoV/sentimen/momentum benar.
- Validasi evidence: ref tidak dikenal → satu kali repair, lalu klaim tanpa bukti dibuang.
- Token: `standard` 3 kompetitor → total token LLM ≤ 12.000 (sintesis + eskalasi sentimen); dibandingkan baseline (seluruh post ke LLM) di TASK-26.
- Live UC-15 dengan Dashboard lokal → laporan terbit di task.

## Acceptance Criteria
1. UC-15 selesai end-to-end; laporan memuat SWOT, matriks kompetitor, SoV, sentimen per brand, rekomendasi — semua angka dapat ditelusuri ke data.
2. Penilaian 2 reviewer Marketing (rubrik: relevansi, akurasi angka, actionable; skala 1–5) rata-rata ≥ 4.
3. Output tool ke pemanggil ≤ 1.200 token (ringkasan + key numbers + report_id).

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; coverage `services/analysis/market.py` ≥ 85%.
- [ ] Contoh laporan disimpan di `docs/examples/market-report.md`.
- [ ] Review disetujui.
