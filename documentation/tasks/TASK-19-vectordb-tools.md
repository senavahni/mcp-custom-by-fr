# TASK-19 — Vector DB (pgvector) & Tools Knowledge Base (`kb_*`)

| Item | Nilai |
|---|---|
| Milestone | M4 Data Access |
| Prioritas | P0 |
| Estimasi | 5 hari |
| Dependensi | TASK-03, TASK-06, TASK-07 |
| Referensi | MCP_SPEC §4.9 · DATABASE §4.8 · USE_CASES UC-13 · README D5 |

## Tujuan
Menyediakan vector DB untuk ekosistem Dashboard (yang saat ini hanya punya full-text search) agar agent bisa mengambil konteks **paling relevan saja** (RAG) dari brief, dokumen task, hasil scraping, dan laporan — mengganti kebiasaan memasukkan dokumen utuh ke prompt.

## Spesifikasi

### Embedder (`services/kb/embedder.py`)
- `EMBED_PROVIDER=gateway` (default): `gateway_client.embed()` route `embed-default` (1536 dim), batch 64, cache `embedding_cache`.
- `EMBED_PROVIDER=local`: ONNX (`onnxruntime` CPU) model multilingual kecil (kandidat `intfloat/multilingual-e5-small`, 384 dim; prefix `query:`/`passage:`), diunduh sekali ke `LOCAL_EMBED_MODEL_DIR` oleh `manage.py download-models`.
- Koleksi mengunci `embed_model` & `embed_dim`; ganti model = buat koleksi baru + re-ingest (`manage.py kb-reembed`).

### Chunker (`services/kb/chunker.py`)
- Markdown-aware: pecah per heading → paragraf → kalimat; target `chunk_size` token (default 800, overlap 100); tabel tidak dipotong di tengah baris; judul bagian (breadcrumb heading) disisipkan di awal chunk (`[Brief > Target Audiens] …`) agar chunk mandiri.
- Teks pendek < 30 token digabung dengan tetangga.

### Ingest (`kb_ingest`, job)
- Sumber: `text`, `url` (pakai TASK-14 scrape markdown), `handle` (`web_pages` → markdown; `social_posts` → dikelompokkan per hari/keyword menjadi dokumen ringkas, bukan 1 post = 1 dokumen), `report` (`analysis_reports.content`), `file_text` (teks yang sudah diekstrak klien).
- Idempoten: `kb_documents (collection, source_type, source_ref)` + `content_hash`; konten sama → skip; berubah → hapus chunk lama, buat baru.

### Sinkron Dashboard (`kb_sync_dashboard`, job + schedule opsional)
- Koleksi `dashboard:<project_id>`; dokumen = Project Brief (seluruh blok brief) dan tiap task (judul + properti + blok task), dari `document_blocks.plain_text` urut `order_index`.
- Inkremental: bandingkan `max(version)`/`max(updated_at)` blok per dokumen dengan `kb_documents.source_version`/`updated_at`; task/blok `deleted_at` → hapus dokumen/chunk.
- Dijalankan otomatis oleh runtime saat run dimulai bila sinkron terakhir > 1 jam (non-blocking).

### Pencarian (`services/kb/search.py`)
- `vector`: query embedding → `ORDER BY (embedding::vector(D)) <=> $q LIMIT 30` (ekspresi sama dengan indeks, DATABASE §4.8), `SET LOCAL hnsw.ef_search = 64`.
- `keyword`: `ts_rank(text_tsv, websearch_to_tsquery('simple', q))` top 30.
- `hybrid` (default): Reciprocal Rank Fusion (k=60) dua daftar → top_k; filter `metadata @> $filter`; `min_score` opsional.
- Output: `{chunk_id, document_title, source, text ≤ 600 char (dipotong di batas kalimat), score}`; dedupe chunk bertetangga dari dokumen sama (gabung bila berurutan).
- Cache 300 s per (koleksi, query, mode, filter).

### Tools
`kb_collections`, `kb_collection_create`, `kb_ingest`, `kb_search`, `kb_delete`, `kb_sync_dashboard`. Akses dibatasi workspace klien/run.

## Checklist Implementasi
- [ ] Embedder gateway + local + cache; `manage.py download-models`.
- [ ] Chunker markdown-aware + breadcrumb.
- [ ] Ingest semua tipe sumber + idempotensi.
- [ ] Sinkron Dashboard inkremental + pemicu di runtime.
- [ ] Search vector/keyword/hybrid + RRF + dedupe.
- [ ] 6 tools + pembatasan workspace.
- [ ] Integrasi ke `pm_query_project_context` (TASK-10).

## Pengujian
- Chunker golden tests (heading, tabel, teks panjang).
- Idempotensi: ingest 2× → jumlah chunk tetap; ubah 1 blok → hanya dokumen terkait yang di-embed ulang (hitung panggilan embed).
- Kualitas: set evaluasi 30 pertanyaan atas 3 Project Brief contoh → `hybrid` recall@6 ≥ 0,85 dan lebih baik dari FTS saja (dibandingkan Agent API `context/query`).
- Performa: 50.000 chunk 1536-dim → `kb_search` p95 < 150 ms (tanpa waktu embed query).
- Dimensi campur dalam satu koleksi → ditolak.

## Acceptance Criteria
1. UC-13 berjalan: sinkron project, ingest hasil scraping, pencarian hybrid mengembalikan 6 potongan relevan ≤ 1.000 token total.
2. Recall@6 hybrid ≥ 0,85 pada set evaluasi; `pm_query_project_context` memakai hybrid bila koleksi project ada.
3. Re-sinkron tanpa perubahan tidak memanggil embedding sama sekali.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; coverage `services/kb/` ≥ 85%.
- [ ] Set evaluasi retrieval disimpan di `tests/eval/kb/` dan dijalankan di TASK-26.
- [ ] `docs/knowledge-base.md` (koleksi, sinkron, biaya embedding).
- [ ] Review disetujui.
