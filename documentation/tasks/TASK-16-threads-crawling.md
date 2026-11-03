# TASK-16 — Crawling Threads berdasarkan Kata Kunci & Periode

| Item | Nilai |
|---|---|
| Milestone | M3 Data Collection |
| Prioritas | P0 |
| Estimasi | 3 hari |
| Dependensi | TASK-14, TASK-15 (normalisasi & `social_stats`) |
| Referensi | MCP_SPEC §4.6 · USE_CASES UC-10 · README D7 |

## Tujuan
Mengumpulkan post publik Threads untuk kata kunci/topik dan periode tertentu ke `social_posts` (`source='threads'`), memakai Threads Keyword Search API resmi sebagai jalur utama dan browser sebagai cadangan, dengan output yang identik formatnya dengan X agar analisis lintas platform mudah.

## Konteks Platform
- Endpoint resmi `GET https://graph.threads.net/v1.0/keyword_search` dengan parameter `q`, `search_type` (`TOP`/`RECENT`), `search_mode` (`KEYWORD`/`TAG`), `media_type` (`TEXT`/`IMAGE`/`VIDEO`), `since`/`until` (unix timestamp; `since ≥ 1688540400`), `limit`, paginasi kursor.
- Butuh token akses dengan izin `threads_basic` + `threads_keyword_search`. Kata kunci yang dianggap sensitif oleh platform mengembalikan array kosong.

## Spesifikasi

### Input `threads_search`
`keywords[] (1–10), since, until, search_type=RECENT, search_mode=KEYWORD, media_type?, max_items (1–2.000, default 300), provider: auto|threads_api|browser, profile?, wait_s?`

### Adapter `threads_api` (`collectors/threads_api.py`)
- Satu rangkaian permintaan per kata kunci; `fields=id,text,media_type,permalink,timestamp,username,has_replies,is_reply,is_quote_post,topic_tag` (field metrik interaksi diambil bila tersedia untuk token tersebut; bila tidak, metrik = null dan dicatat di `notes`).
- Paginasi `paging.cursors.after` hingga `max_items`/habis; jeda 1 s; retry 5xx 3× backoff (endpoint ini pernah mengalami 500 berkepanjangan) → bila tetap gagal dan `provider=auto` → fallback browser.
- Hasil kosong untuk kata kunci → `notes:["keyword_returned_empty_possibly_filtered"]`.
- Token kedaluwarsa (OAuth error 190) → `LOGIN_REQUIRED` + petunjuk `manage.py threads-token-refresh` (refresh long-lived token, disimpan di `.env` oleh admin).

### Adapter `browser` (`collectors/threads_browser.py`)
- `https://www.threads.net/search?q=<kw>&serp_type=default` (tab Recent) dengan profil login; scroll sampai lewat `since` atau kuota; parsing post (id dari permalink, username, waktu `time[datetime]`, teks, metrik like/reply/repost bila terlihat). Selector di `threads_selectors.py`.
- Filter periode lokal (UI tidak menyediakan rentang tanggal) → hentikan scroll bila 20 post berturut-turut lebih tua dari `since`.

### Normalisasi
- Reuse normalizer TASK-15: upsert `social_posts`, `job_posts.matched_keywords`, hashtag dari `topic_tag` + teks.
- Output envelope sama dengan `x_search` (stats per_day, top tags, top authors, engagement bila ada, 5 post teratas).

## Checklist Implementasi
- [ ] Adapter API + paginasi + retry + fallback + deteksi token kedaluwarsa.
- [ ] Adapter browser + selectors + filter periode.
- [ ] CLI `threads-token-refresh`.
- [ ] Integrasi normalizer & envelope.
- [ ] Dokumentasi `docs/collectors.md#threads` (cara mendapatkan izin, refresh token).

## Pengujian
- `respx`: 3 halaman, cursor, array kosong, 500×3 → fallback dipanggil, error 190.
- Fixture HTML Threads → parser benar.
- Konversi `since/until` tanggal lokal → unix sesuai zona `Asia/Jakarta`.
- `-m live` (manual): 1 kata kunci, 7 hari, ≤ 50 post.

## Acceptance Criteria
1. UC-10 berhasil dengan API resmi; hasil dapat digabung dengan X via `social_stats(group_by=source)`.
2. Gangguan 5xx API tidak menggagalkan job bila browser tersedia (fallback otomatis tercatat di `stats.provider`).
3. Output ≤ 1.500 token untuk 300+ post.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; coverage ≥ 80%.
- [ ] Izin Threads API (app review/akses penguji) terdokumentasi statusnya.
- [ ] Review disetujui.
