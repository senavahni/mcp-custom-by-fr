# Kolektor Data Sosial & Trends (TASK-15, 16, 17)

Semua kolektor berjalan sebagai **job** di worker (checkpoint & resume), menyimpan data mentah di
schema `mcp`, dan mengembalikan **ringkasan + statistik + handle** (bukan ribuan post) agar hemat token.

| Tool | Job | Tabel |
|---|---|---|
| `x_search` | `x_search` | `social_posts`, `job_posts` |
| `threads_search` | `threads_search` | `social_posts`, `job_posts` |
| `social_stats` | – (dari handle) | – |
| `trends_interest` / `trends_by_region` / `trends_related` | `trends_interest` / `trends_region` / `trends_related` | `trend_queries`, `trend_points`, `trend_related` |

Post yang sama (sumber + `external_id`) disimpan sekali; metrik diperbarui bila muncul lagi. Relasi
`job_posts` mencatat kata kunci yang cocok, sehingga resume job atau hari yang tumpang tindih **tidak**
menggandakan data.

## X (Twitter) {#x}

### Pemilihan provider (`provider=auto`)
1. `X_BEARER_TOKEN` ada **dan** `since` ≤ 7 hari lalu **dan** estimasi bacaan ≤ sisa kuota harian
   (`X_MAX_READS_PER_DAY`) → **`x_api`** (X API v2 recent search, pay-per-use).
2. Selain itu, bila ada profil browser yang login x.com → **`browser`**.
3. Tidak ada keduanya → error dengan langkah perbaikan.

`provider=x_api` eksplisit di luar 7 hari / tanpa token → `VALIDATION_ERROR`; di atas kuota →
`BUDGET_EXCEEDED` **sebelum** API dipanggil. Periode maksimal 366 hari.

### Biaya & kuota
- Estimasi: `max_items × X_PRICE_PER_READ_USD` (default 0,005 USD) tampil di `JobAccepted.estimate`.
- Biaya dihitung per post yang dikembalikan API (sebelum filter lokal) dan tercatat di
  `result.cost.external_units`; kuota harian dihitung dari job `x_api` sejak 00:00 `TIMEZONE`.
- Rate limit: bila `x-rate-limit-remaining=0` atau HTTP 429, job menunggu sampai `x-rate-limit-reset`
  (status job tetap `running`, pesan progres "menunggu rate limit X").

### Mode browser
- Pencarian web per hari (mundur dari hari terakhir) dengan operator `since:`/`until:` dan tab *Latest*.
- Scroll bertahap (jeda acak 1,5–4 s) sampai 3 scroll tanpa post baru atau kuota harian
  (`ceil(max_items / hari × 1,5)`); jeda 10–20 s antar hari; batas keras 2.000 post/jam per profil.
- Checkpoint per hari selesai. Logout/challenge di tengah job → hasil parsial (bila ≥ 1 hari selesai)
  dengan catatan, atau `LOGIN_REQUIRED`.

## Threads {#threads}

- **`threads_api`**: Threads Keyword Search API (`GET /keyword_search`, izin
  `threads_keyword_search`), `since` ≥ 2023-07-05, cursor `after`, checkpoint per kata kunci.
- 5xx diulang 3× (backoff 2/5/10 s); bila tetap gagal dan `fallback_browser=true` → lanjut mode browser
  (catatan `threads_api_5xx_fallback_browser`).
- Kata kunci yang mengembalikan 0 post diberi catatan `keyword_returned_empty_possibly_filtered:<kw>`
  (API memfilter konten sensitif/akun privat).
- Token long-lived berlaku ±60 hari. Perbarui **sebelum** kedaluwarsa (error 190 → `LOGIN_REQUIRED`):
  ```bat
  "%PY%" scripts\manage.py threads-token-refresh
  ```
  Token baru ditulis ke `.env` (tidak dicetak); restart server & worker.
- **Mode browser**: tab *Recent* `threads.net/search`; UI tidak punya filter tanggal, jadi periode
  difilter lokal dan scroll berhenti setelah 20 post berturut-turut lebih tua dari `since`.

## Google Trends {#trends}

### Hasil spike & keputusan D8 (Nov 2026)
| Kandidat | Status | Keputusan |
|---|---|---|
| `pytrends` | Diarsip April 2025 | Ditolak |
| `trendspy` | Rilis terakhir Des 2024 (> 6 bulan) | Ditolak |
| `pytrends-modern` 0.2.x | Aktif (rilis Sep 2026, Python 3.11) | Lolos kriteria, **belum dipasang** (instalasi paket baru ditahan saat implementasi); kandidat pengganti bila klien internal sering diblokir |
| Klien internal (`trends_google.py`) | Endpoint yang sama dengan UI web: `/trends/api/explore` → token widget → `/trends/api/widgetdata/{multiline,comparedgeo,relatedsearches}` | **Dipakai** sebagai `unofficial` |
| Mode browser | Request yang sama dijalankan sebagai XHR di tab Chrome profil `default` | **Dipakai** sebagai fallback |
| API resmi (alpha) | Akses terbatas | Adapter `official` stub; aktif bila `TRENDS_OFFICIAL_API_KEY` diisi & diimplementasikan |

Alasan: tidak menambah dependensi yang bisa diarsip lagi, parser bisa diuji dengan fixture, dan mode
browser memakai kode parser yang sama. Uji live 20 query (frekuensi 429) **belum** dijalankan; lakukan
manual sebelum produksi (`-m live`).

### Perilaku
- Rantai provider: `TRENDS_PROVIDER` lalu cadangan (`unofficial → browser`, `official → unofficial →
  browser`). 429 diulang dengan backoff 30/60/120 s + jitter, lalu pindah provider.
- Jeda minimum antar permintaan `TRENDS_MIN_INTERVAL_S` (5 s).
- **Cache**: permintaan identik (hash parameter) < `TRENDS_CACHE_TTL_S` (24 jam) dibaca dari DB tanpa
  memanggil Google; tool menjawab **sinkron** bila cache segar, selain itu job dengan `wait_s` 15.
- Statistik tanpa LLM (`services/analysis/trend_stats.py`): `avg`, `peak`, `slope_per_week`
  (regresi linear), `change_pct_vs_prev_period` (periode sebelumnya diambil otomatis = 1 permintaan
  tambahan, dilaporkan), `seasonality_hint` (≥ 2 tahun), `share_of_search`, tabel mingguan ≤ 26 baris
  (lebih panjang → bulanan).
- Parser CSV unduhan manual (`parse_trends_csv`) menangani label EN/ID, nilai `<1` → 0, dan
  `Breakout`/`Lonjakan` → 5000.

### Keterbatasan data relatif
- Nilai 0–100 **relatif terhadap puncak dalam satu permintaan**; tidak bisa dibandingkan antar
  permintaan berbeda.
- Maks 5 kata kunci per permintaan. Untuk > 5, isi `anchor`: kata kunci dibagi grup 4 + anchor lalu
  diskalakan terhadap anchor — hasilnya **perkiraan** (pembulatan Google memperbesar galat untuk nilai
  kecil).
- Resolusi ditentukan Google (harian ≤ 9 bulan, mingguan ≤ 5 tahun, bulanan di atasnya); titik terakhir
  bisa parsial (`is_partial_last`).

## Memperbarui selector DOM

UI X/Threads sering berubah. Selector **hanya** ada di:
- `mcp_marketing/services/collectors/x_selectors.py`
- `mcp_marketing/services/collectors/threads_selectors.py`

Langkah: simpan HTML hasil pencarian terbaru (DevTools → *Copy outerHTML*, hapus data pribadi) ke
`tests/fixtures/x_search.html` / `threads_search.html`, sesuaikan selector, lalu jalankan
`pytest tests/unit/test_social_collectors.py`.

## Risiko & ToS

- Scraping X/Threads lewat browser dapat melanggar ToS platform dan berisiko akun dibatasi/diblokir.
  Gunakan akun khusus riset, volume rendah (batas bawaan di atas), dan utamakan API resmi.
- Data post publik tetap data pribadi (handle, teks): simpan seperlunya, retensi
  `SOCIAL_RETENTION_DAYS`, `raw` hanya disimpan di `APP_ENV=dev`.
- Endpoint Google Trends yang dipakai tidak resmi dan bisa berubah/429 kapan saja; fallback & cache
  mengurangi dampaknya, bukan menghilangkannya.
