# TASK-05 — Token-Efficiency Layer: Handle, Kompaksi, Cache, Budget

| Item | Nilai |
|---|---|
| Milestone | M1 Platform |
| Prioritas | P0 (inti tujuan proyek) |
| Estimasi | 4 hari |
| Dependensi | TASK-04 |
| Referensi | MCP_SPEC §2, §3 · PRD G2, G3, FR-04, FR-05, FR-06, NFR-04 |

## Tujuan
Membuat semua tool **hemat token secara default** tanpa setiap developer tool harus mengingatnya: data besar otomatis menjadi handle, output dipadatkan dan dipotong rapi, hasil deterministik di-cache, dan pemakaian token LLM dibatasi budget.

## Spesifikasi

### 1. Result store (`services/results/result_store.py`)
- `create(kind, columns, rows_iter, stats, source_ref, ttl_days) -> ResultSet` — insert batch 1.000 baris ke `result_items` (proyeksi kolom), isi `sort_ts`/`sort_num` dari kolom yang ditandai.
- `page(handle, fields, filter, order_by, limit, cursor)` — cursor = base64(`idx` terakhir + order key); validasi `fields ⊆ columns`.
- `aggregate(handle, group_by, metrics, time_bucket)` — SQL atas `jsonb` (`(data->>'col')::numeric`), metrik: `count`, `sum:x`, `avg:x`, `min:x`, `max:x`, `p50:x`, `p90:x`, `top:x:n` (nilai terbanyak, untuk array pakai `jsonb_array_elements_text`).
- Kepemilikan: handle hanya bisa dibaca klien pembuatnya atau run yang sama (atau admin) → selain itu `NOT_FOUND`.
- Tool `data_get`, `data_aggregate`, `data_export` (CSV/JSONL/XLSX ke `storage/exports`, URL admin bertanda tangan HMAC berlaku 1 jam).

### 2. Envelope & kompaksi (`services/results/compact.py`)
- `Envelope` (Pydantic) sesuai MCP_SPEC §2.2; helper `to_table(rows, cols)` → `{cols, rows}`.
- `compact(output, response_format)`:
  1. Hitung token (`utils.tokens`).
  2. Bila > `TOOL_OUTPUT_SOFT_TOKENS` pada mode `concise`: pangkas berurutan — (a) teks per item ke `text_max_chars`, (b) kurangi `items` ke 5, (c) pangkas list di `stats` ke top-5, (d) hapus field opsional bertanda `drop_first`.
  3. Bila masih > `TOOL_OUTPUT_HARD_TOKENS` di mode apa pun → potong keras + `truncated:true` + `hint: "use data_get(handle,…)"`.
- `summary` wajib ≤ 300 karakter (dipotong dengan `…`).
- Aturan otomatis T2: handler yang mengembalikan > `INLINE_ITEMS_MAX` baris di `items` → pipeline memindahkan ke result store dan mengganti dengan handle + 5 sampel (log `warning` agar developer memperbaiki tool).

### 3. Cache (`services/results/cache.py`)
- Kunci = `sha256(tool | tool_version | canonical_json(args tanpa response_format/limit/cursor) | client.workspace_id)`.
- TTL dari `ToolSpec.cache_ttl_s` (contoh: trends 86.400, web_scrape 21.600, kb_search 300, db_query 30); `writes=True` → tidak pernah di-cache.
- Arg `cache: "bypass"` untuk paksa ulang. Hit → `cost.cache_hit=true`, status audit `cached`.
- Cache LLM (dipakai TASK-06): kunci = `sha256(route|messages|params)` untuk `temperature=0`, TTL `LLM_CACHE_TTL_S`.
- Pembersihan entri kedaluwarsa via schedule (TASK-07).

### 4. Budget (`services/agent/budget.py` — dipakai bersama)
- Tiga lapis: **per run** (`runs.token_budget`), **per klien per hari** (`api_clients.daily_token_budget`, dihitung dari `usage_daily` + counter in-memory), **per tool call** (maks token LLM internal per tool, default 20.000; analisis `deep` 60.000).
- `budget.reserve(estimate)` sebelum panggilan LLM, `budget.commit(actual)` sesudahnya; melebihi → `BUDGET_EXCEEDED retryable:false` dengan `details{used, limit, scope}`.
- External units (post X yang dibaca) punya kuota sendiri (`X_MAX_READS_PER_DAY`).

### 5. Pengukuran penghematan
- Pipeline mengisi `tool_calls.output_tokens` (token output yang diterima LLM pemanggil) dan `baseline_tokens` (token jika seluruh data mentah dikembalikan: dihitung dari `result_set.total × rata-rata token per baris`, atau dari panjang konten mentah untuk web) — dipakai laporan TASK-24 & eval TASK-26.

## Checklist Implementasi
- [ ] Result store + paginasi cursor + agregasi.
- [ ] Tool `data_get`, `data_aggregate`, `data_export`.
- [ ] Envelope, `to_table`, `compact` + aturan otomatis T2.
- [ ] Cache tool & LLM + bypass.
- [ ] Budget 3 lapis + external units.
- [ ] Pengisian `output_tokens` & `baseline_tokens`.
- [ ] Panduan developer `docs/dev-guide-tools.md`: cara membuat tool yang hemat token (contoh lengkap).

## Pengujian
- Result set 10.000 baris: `data_get` limit 20 dengan filter & order p95 < 300 ms; cursor stabil (tanpa duplikasi/lompatan).
- `data_aggregate(group_by=["day"], metrics=["count","avg:likes"], time_bucket="day")` benar terhadap perhitungan pandas.
- `compact`: output 9.000 token → ≤ 1.500 (concise) dengan urutan pemangkasan yang benar; > hard → `truncated:true`.
- Cache: dua panggilan identik → kedua `cached`; beda `workspace_id` → miss; tool `writes` → tidak pernah cache.
- Budget: run budget 1.000, reserve 1.200 → `BUDGET_EXCEEDED`; commit mengurangi sisa dengan benar; paralel 10 coroutine tidak melampaui (lock).
- Kepemilikan handle: klien lain → `NOT_FOUND`.

## Acceptance Criteria
1. Tidak ada tool (diuji via registry test) yang mengembalikan > 20 item inline atau > 4.000 token.
2. Tool contoh "dump 5.000 baris" otomatis menjadi handle + 5 sampel + statistik, output ≤ 1.500 token.
3. Cache hit mengurangi latensi tool trends contoh (mock 2 s) menjadi < 50 ms.
4. `BUDGET_EXCEEDED` muncul sebelum panggilan LLM yang melampaui budget dilakukan (tidak ada token terbuang).

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi.
- [ ] Coverage `services/results/` & `budget.py` ≥ 90%.
- [ ] Dev guide terbit dan direview minimal 1 developer lain.
- [ ] MCP_SPEC §2–3 sesuai implementasi.
