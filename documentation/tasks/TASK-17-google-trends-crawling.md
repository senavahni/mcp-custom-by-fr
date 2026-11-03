# TASK-17 — Crawling Google Trends berdasarkan Kata Kunci & Periode

| Item | Nilai |
|---|---|
| Milestone | M3 Data Collection |
| Prioritas | P0 |
| Estimasi | 3 hari |
| Dependensi | TASK-05, TASK-07, TASK-14 |
| Referensi | MCP_SPEC §4.7 · DATABASE §4.6 · USE_CASES UC-11 · README D8 |

## Tujuan
Mengambil minat pencarian Google Trends (interest over time, per wilayah, related queries/topics) untuk kata kunci dan periode tertentu dengan geo default Indonesia, lalu mengembalikan **statistik siap pakai** (bukan deret mentah) agar LLM tidak perlu menghitung.

## Konteks Platform (Okt 2026)
- `pytrends` sudah diarsip (April 2025) dan tidak lagi dipelihara.
- Google mengumumkan **Google Trends API resmi (alpha)** pada Juli 2025; aksesnya terbatas berbasis aplikasi.
- Maka: adapter `unofficial` (library yang masih dipelihara, dipilih di spike) + `browser` (unduh CSV dari trends.google.com memakai profil) + `official` (disiapkan, aktif bila akses disetujui).
- Nilai Trends relatif 0–100 per permintaan; perbandingan antar kata kunci hanya valid dalam satu permintaan (maks 5 kata kunci).

## Spesifikasi

### Spike (0,5 hari, awal task)
- Evaluasi ≥ 2 library unofficial yang masih aktif (rilis ≤ 6 bulan, dukungan Python 3.11) + mode browser: keberhasilan 20 query, frekuensi 429, kelengkapan (interest, region, related).
- Keputusan dicatat di README D8 & `docs/collectors.md#trends`.

### Antarmuka adapter (`collectors/trends_*.py`)
```python
class TrendsProvider(Protocol):
    async def interest_over_time(self, keywords, start, end, geo, category, gprop) -> InterestResult
    async def interest_by_region(self, keyword, start, end, geo, resolution) -> list[RegionValue]
    async def related(self, keyword, start, end, geo, kind) -> RelatedResult  # top & rising
```
- `browser`: buka `https://trends.google.com/trends/explore?date=<start> <end>&geo=ID&q=<k1>,<k2>&hl=id`, klik unduh CSV per widget (atau baca respons widget), parse CSV (tangani baris "<1", label bahasa Indonesia).
- Semua provider: retry 429 dengan backoff 30/60/120 s + jitter (job tetap `running`), maks 3; jeda antar permintaan ≥ 5 s.

### Normalisasi & cache
- Simpan `trend_queries` (+ `params_hash`) dan `trend_points` / `trend_related`. Permintaan identik < `TRENDS_CACHE_TTL_S` (24 jam) → pakai data tersimpan (cache hit, tanpa memanggil Google).
- Resolusi otomatis dari Google (harian ≤ 9 bulan, mingguan ≤ 5 tahun, bulanan di atasnya) dicatat di `resolution`; titik terakhir parsial → `is_partial`.
- `keywords > 5` → `VALIDATION_ERROR` dengan saran memecah; bila pemanggil butuh > 5, tool mendukung **anchor keyword** opsional (`anchor`): setiap kelompok 4 + anchor, lalu diskalakan relatif terhadap anchor (didokumentasikan sebagai perkiraan).

### Statistik (`services/analysis/trend_stats.py`, tanpa LLM)
Per kata kunci: `avg`, `peak{date,value}`, `slope_per_week` (regresi linear, `scipy.stats.linregress`), `change_pct_vs_prev_period` (bila periode sebelumnya diminta/dicache; jika tidak, otomatis ambil periode sebelumnya dengan panjang sama — dihitung sebagai 1 permintaan tambahan dan dilaporkan), `seasonality_hint` (bulan tertinggi rata-rata bila ≥ 2 tahun), `share_of_search` (porsi rata-rata antar kata kunci dalam permintaan sama).

### Tools
- `trends_interest` → handle deret + `stats` + `weekly` (resample mingguan ≤ 26 baris; jika lebih panjang → bulanan).
- `trends_by_region(top_n ≤ 20)` → tabel provinsi/kota.
- `trends_related(kind)` → `top` & `rising` masing-masing ≤ 10 (`Breakout` → 5000).
- Semua sinkron bila cache hit; bila tidak → job dengan `wait_s` default 15.

## Checklist Implementasi
- [ ] Spike & keputusan provider.
- [ ] Interface + adapter `unofficial`, `browser`, `official` (stub + konfigurasi).
- [ ] Penyimpanan + cache + anchor scaling.
- [ ] `trend_stats` + 3 tools.
- [ ] Dokumentasi keterbatasan data relatif.

## Pengujian
- Fixture CSV (multi keyword, region, related, nilai "<1") → parse benar.
- `trend_stats` terhadap deret sintetis (naik linear, musiman) → slope & peak benar.
- Cache: 2 panggilan identik → 1 panggilan provider.
- 429 berturut-turut → backoff lalu fallback browser (mock).
- `-m live` (manual): 3 kata kunci, 12 bulan, geo ID.

## Acceptance Criteria
1. UC-11: 3 kata kunci × 12 bulan → statistik + tabel mingguan/bulanan, output ≤ 800 token.
2. Permintaan ulang dalam 24 jam tidak memanggil Google (diverifikasi via log/metrics).
3. Kegagalan satu provider tidak menghentikan tool bila provider cadangan tersedia.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; coverage ≥ 80%.
- [ ] Hasil spike & keputusan D8 tercatat.
- [ ] Review disetujui.
