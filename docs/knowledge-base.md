# Knowledge Base / Vector DB (TASK-19)

KB memberi agent **potongan paling relevan saja** (RAG) dari brief, dokumen task, hasil scraping,
post sosial, dan laporan — bukan dokumen utuh di prompt.

## Koleksi

| Konsep | Keterangan |
|---|---|
| Koleksi | `mcp.kb_collections`, dibatasi workspace klien/run. Model & dimensi embedding **dikunci** saat dibuat |
| Dokumen | unik per `(koleksi, source_type, source_ref)` + `content_hash` → ingest ulang konten sama dilewati |
| Chunk | markdown-aware ±800 token (overlap 100), diawali breadcrumb `[Brief > Target Audiens] …` |
| Koleksi Dashboard | `dashboard:<project_id>` — dibuat otomatis oleh `kb_sync_dashboard` |

Tools: `kb_collections`, `kb_collection_create`, `kb_ingest` (job), `kb_search`, `kb_delete`,
`kb_sync_dashboard` (job). Entitas `kb_collections` juga tersedia di `db_*`.

### Sumber ingest
| `source.type` | Isi |
|---|---|
| `text`, `file_text` | teks/markdown; `metadata.ref` / `metadata.filename` menjadi `source_ref` (stabil = idempoten) |
| `url` | di-scrape worker (profil `default`, aturan SSRF/robots TASK-14) → markdown |
| `handle` | `web_pages` → 1 dokumen per halaman; `social_posts` → 1 dokumen per (hari × kata kunci), bukan per post |
| `report` | `analysis_reports` (judul + ringkasan + bagian) milik pemanggil |

## Sinkron Dashboard
- Dokumen = Project Brief (blok tanpa task) + satu dokumen per task (judul, status, prioritas, tanggal,
  tag, blok task) dari `document_blocks.plain_text` urut `order_index`.
- Inkremental: dokumen yang hash-nya sama **tidak di-embed ulang** (re-sync tanpa perubahan = 0
  panggilan embedding); task/blok terhapus → dokumen dihapus; `full=true` membangun ulang.
- Otomatis: saat agent run dimulai, bila sinkron terakhir > 1 jam, job `kb_sync_dashboard`
  diantrekan di latar belakang (run tidak menunggu).
- `pm_query_project_context` menggabungkan hasil Agent API (full-text Dashboard) dengan `kb_search`
  hybrid koleksi project memakai RRF bila koleksinya ada.

## Pencarian
| `mode` | Cara |
|---|---|
| `keyword` | `ts_rank` atas `tsvector('simple')`; pertanyaan diubah ke OR kata bermakna (stopword dibuang) |
| `vector` | kosinus embedding query (prefix `query:` untuk model e5) |
| `hybrid` (default) | Reciprocal Rank Fusion (k=60) dua daftar top-30 |

Hasil: ≤ `top_k` (default 6) potongan ≤ 600 karakter (dipotong di batas kalimat), chunk berurutan
dari dokumen sama digabung, `filter` = `metadata @> {…}`. Cache tool 300 s.

## pgvector & mode cadangan
- **Dengan pgvector**: kolom `embedding vector` + indeks HNSW parsial (384/1536 dim),
  `hnsw.ef_search = 64`.
- **Tanpa pgvector** (kondisi PostgreSQL lokal saat ini): migrasi `0002` membuat kolom
  `embedding real[]`; pencarian vector dihitung di aplikasi atas ≤ 20.000 chunk terbaru per
  pencarian (cukup untuk koleksi kecil; target p95 < 150 ms untuk 50.000 chunk **butuh pgvector**).
- Setelah pgvector dipasang (`CREATE EXTENSION vector` sebagai superuser), jalankan
  `scripts\migrate.bat`: kolom otomatis diubah ke `vector` dan indeks HNSW dibuat (data tetap).

## Embedding & biaya
| `EMBED_PROVIDER` | Model koleksi | Dimensi | Biaya |
|---|---|---|---|
| `gateway` (default) | `embed-default` (route Gateway) | 1536 | token embedding Gateway; cache `embedding_cache` per sha256 teks; batch 64 |
| `local` | `local/multilingual-e5-small` (ONNX CPU, int8) | 384 | gratis; unduh sekali `python scripts/manage.py download-models` |

Ganti model = koleksi baru: `python scripts/manage.py kb-reembed <collection_id>` menyalin dokumen &
teks chunk ke koleksi baru dengan model saat ini, lalu hapus koleksi lama setelah diverifikasi.
Dimensi campur dalam satu koleksi ditolak (`VALIDATION_ERROR`).

## Evaluasi
`tests/eval/kb/` berisi 3 brief contoh + 30 pertanyaan. `scripts\test.bat tests/eval/kb -m eval`
melaporkan recall@6 `hybrid` vs `keyword`. Default memakai embedder hash (baseline, tanpa biaya);
`KB_EVAL_LIVE=1` memakai Gateway dan menegakkan target recall@6 hybrid ≥ 0,85.
Baseline saat ini (embedder hash): keyword 0,67, hybrid 0,37 — angka hybrid belum mewakili
embedding nyata; ukur ulang di TASK-26 dengan `KB_EVAL_LIVE=1`.
