# TASK-15 — Crawling X/Twitter berdasarkan Kata Kunci & Periode

| Item | Nilai |
|---|---|
| Milestone | M3 Data Collection |
| Prioritas | P0 |
| Estimasi | 4 hari |
| Dependensi | TASK-07, TASK-14 |
| Referensi | MCP_SPEC §4.6 · DATABASE §4.6 · USE_CASES UC-09 · README D6 |

## Tujuan
Mengumpulkan post X untuk kata kunci dan rentang waktu tertentu ke tabel `social_posts` yang seragam, dengan dua adapter (API resmi & browser), estimasi biaya sebelum eksekusi, dan output ringkas berupa statistik + handle.

## Konteks Platform (Okt 2026)
- X API memakai model **pay-per-use** (default sejak Feb 2026): baca post ≈ **USD 0,005 per post** yang dikembalikan, tanpa tier gratis; endpoint **recent search hanya 7 hari terakhir**; full-archive butuh Enterprise. Karena itu adapter API hanya cocok untuk periode ≤ 7 hari dan volume terkontrol.
- Mode browser memakai pencarian lanjutan web (`<kata kunci> since:YYYY-MM-DD until:YYYY-MM-DD lang:id`) dengan profil login khusus. Risiko ToS & pemblokiran akun → rate limit konservatif, akun organisasi khusus, persetujuan internal (README §7).

## Spesifikasi

### Input `x_search`
`keywords[] (1–10, masing-masing ≤ 100 char), match: any|all, since, until, lang?='id', exclude_retweets=true, exclude_replies=false, min_likes?, max_items (1–5.000, default 500), provider: auto|x_api|browser, profile?, wait_s?`
- Validasi periode: `until > since`, maksimal 366 hari; untuk `x_api` `since ≥ now − 7 hari` (selain itu `VALIDATION_ERROR` dengan saran `provider=browser`).

### Pemilihan provider (`auto`)
1. `X_BEARER_TOKEN` ada **dan** periode dalam 7 hari **dan** `estimate.external_units ≤ sisa kuota harian (X_MAX_READS_PER_DAY)` → `x_api`.
2. Selain itu → `browser` (profil dengan `allowed_sites` memuat `x.com` dan `login_status.x.com.ok`).
3. Tidak ada keduanya → `UPSTREAM_ERROR retryable:false` + petunjuk konfigurasi.

### Adapter `x_api` (`collectors/x_api.py`)
- `GET https://api.x.com/2/tweets/search/recent` dengan query yang dibangun: `(k1 OR k2) lang:id -is:retweet [-is:reply]`, `start_time`, `end_time`, `max_results=100`, `tweet.fields=created_at,public_metrics,lang,entities,author_id,conversation_id,referenced_tweets`, `expansions=author_id`, `user.fields=username,public_metrics`; paginasi `next_token` hingga `max_items`.
- Hormati header rate limit (`x-rate-limit-remaining/reset`) → tidur sampai reset bila habis (job tetap `running`, progres "menunggu rate limit").
- Estimasi biaya = `max_items × 0,005 USD` (tarif di config `X_PRICE_PER_READ_USD` agar mudah diubah) → ditampilkan di `estimate`; `external_units` dicatat ke `usage_daily`.

### Adapter `browser` (`collectors/x_browser.py`)
- Strategi per-hari: untuk setiap hari dalam periode (urut mundur), buka `https://x.com/search?q=<query since:D until:D+1>&f=live`, scroll bertahap (jeda acak 1,5–4 s) sampai tidak ada post baru 3× berturut-turut atau kuota per hari (`ceil(max_items / jumlah_hari) × 1,5`) tercapai.
- Parsing DOM dari elemen artikel post: id (dari URL status), handle, waktu (`time[datetime]`), teks, metrik (reply/repost/like/view dari atribut aria), hashtag/mention dari teks. Selector dikumpulkan di satu file `x_selectors.py` (mudah diperbarui saat UI X berubah).
- Checkpoint per hari selesai (resume TASK-07). Deteksi logout/challenge → `LOGIN_REQUIRED` dan job berhenti dengan hasil parsial.
- Batas keras: ≤ 2.000 post/jam per profil; jeda antar hari 10–20 s.

### Normalisasi & penyimpanan
- Seragamkan ke `social_posts` (`source='x'`), upsert by `(source, external_id)` (metrik diperbarui bila lebih baru), relasi `job_posts` + `matched_keywords` (pencocokan case-insensitive di teks/hashtag).
- Filter lokal setelah ambil: `lang` (deteksi ulang bila kosong), `min_likes`, rentang waktu persis.
- `raw` disimpan hanya bila `APP_ENV=dev`.

### Output (envelope)
- `summary` (mis. "1.842 post, 1–30 Okt, puncak 14 Okt (312)"), `handle`, `total`,
- `stats`: `per_day{cols:[day,posts,likes]}`, `top_hashtags[10]`, `top_authors[10]{handle,posts,followers}`, `engagement{avg_likes, p90_likes, avg_reposts}`, `provider`, `cost{external_units, usd}`,
- `items`: 5 post engagement tertinggi (`posted_at, author, text≤280, likes, reposts, url`).

### `social_stats(handle, group_by, metrics)`
- Statistik tanpa LLM untuk handle `social_posts` (X & Threads): per day/keyword/author/source; share of voice antar keyword.

## Checklist Implementasi
- [ ] Builder query + validasi periode/provider.
- [ ] Adapter `x_api` + rate limit + estimasi biaya + kuota harian.
- [ ] Adapter `browser` + selectors terpisah + checkpoint per hari + deteksi logout.
- [ ] Normalisasi, upsert, `job_posts`, filter lokal.
- [ ] Envelope statistik + `social_stats`.
- [ ] Dokumentasi `docs/collectors.md#x` (biaya, risiko, cara memperbarui selector).

## Pengujian
- Unit: builder query (any/all, retweet, reply, lang); estimasi biaya; pemilihan provider (6 kombinasi).
- `respx` untuk `x_api`: 3 halaman + rate limit reset; error 401/403/429.
- Fixture HTML hasil pencarian X (disimpan dari browser, disamarkan) → parser menghasilkan post benar (id, waktu, metrik).
- `-m live` (manual, akun uji): 2 hari × 1 kata kunci, ≤ 100 post.
- Resume: hentikan di hari ke-3 dari 10 → lanjut tanpa duplikasi.

## Acceptance Criteria
1. UC-09: periode 30 hari, 1.500 post → selesai, statistik benar (dicocokkan dengan query SQL manual), output ≤ 1.500 token.
2. Mode `x_api` tidak pernah melebihi `max_items` maupun kuota harian (diuji dengan mock).
3. Estimasi biaya ditampilkan **sebelum** job berjalan; job dengan biaya di atas kuota ditolak tanpa memanggil API.
4. Perubahan selector UI X cukup diperbaiki di `x_selectors.py` (dibuktikan test parser terpisah).

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; coverage collector ≥ 80% (unit + fixture).
- [ ] Persetujuan pemakaian akun & anggaran X API tercatat.
- [ ] Review disetujui.
