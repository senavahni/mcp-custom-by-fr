# Task v1.0.0 — MCP Marketing

> Draf brainstorming rev. 2 · 9 November 2026. Baca [README.md](README.md) dulu (keputusan V1-K1…K21, agent, alur F1–F5).
> Format mengikuti `documentation/tasks/`. DoD global ([README §5](../README.md)) tetap berlaku, ditambah: contract test 1.3 hijau, `graphify update .`, coverage modul baru ≥ 80%.

## Daftar Task

| ID | Task | Fase | Prio | Est. | Dependensi |
|---|---|---|---|---|---|
| MCP-V1-00 | Kontrak 1.3: job kind, toolset & event baru (bersama DSH-V1-00) | 0 | P0 | 2 pd | — |
| MCP-V1-01 | pgvector wajib, HNSW 1024, embedder `embed-local`, antrean prioritas | 1 | P0 | 2 pd | LGW-V1-03 (instal pgvector tidak) |
| MCP-V1-02 | Schema market intelligence, wilayah, taksonomi, import (`0005`) | 1 | P0 | 3 pd | 00 |
| MCP-V1-03 | Agent baru (`creative`, `curator`, `prospector`), kapabilitas tahap, injeksi doktrin | 1 | P0 | 3 pd | 02 |
| MCP-V1-04 | Layanan klasifikasi lokal berbasis embedding (KBLI/ISIC, kolom, sentimen, kontak, klaster) | 1 | P0 | 4 pd | 01, 02 |
| MCP-V1-05 | Analisis produk → industri, segmen, kebutuhan, query | 3 | P0 | 2 pd | 03, 04 |
| MCP-V1-06 | Discovery via Chrome: Google Search & Google Maps (volume rendah) | 1–3 | P0 | 5 pd | 02 |
| MCP-V1-07 | Enrichment website, kontak, geocoding, entity resolution | 3 | P0 | 5 pd | 04, 06 |
| MCP-V1-08 | Import XLSX/CSV agentic + Contact Finder | 3 | P0 | 5 pd | 04, 06, 07 |
| MCP-V1-09 | Pipeline F1 `company_discovery` + indeks `companies:<ws>` | 3 | P0 | 2 pd | 05, 07 |
| MCP-V1-10 | Pipeline F2 `social_listening`: kata kunci, sentimen aspek lokal, tema, sinyal | 4 | P0 | 4 pd | 04 |
| MCP-V1-11 | Upload PDF ≤ 100 MB & ingest `book_ingest` | 2 | P0 | 4 pd | 01 |
| MCP-V1-12 | Knowledge card & knowledge graph lintas koleksi | 2 | P0 | 5 pd | 11 |
| MCP-V1-13 | Doktrin per peran, playbook, `kn_retrieve`, evaluator (L2–L5) | 2 | P0 | 4 pd | 03, 12 |
| MCP-V1-14 | Opportunity Engine: product-fit lokal, scoring deterministik, penjelasan | 5 | P0 | 4 pd | 07, 09 |
| MCP-V1-15 | Market analytics & KPI | 5 | P0 | 3 pd | 09, 10, 14 |
| MCP-V1-16 | Strategi & Bank Konten (`strategy`, `content_bank`) | 5 | P0 | 5 pd | 13, 14 |
| MCP-V1-17 | Data quality & tata kelola (dedupe, stale, koreksi, PDP) | 5 | P0 | 3 pd | 07, 08 |
| MCP-V1-18 | Evaluasi & metrik | 6 | P0 | 3 pd | 04–16 |
| MCP-V1-19 | Security review, dokumentasi, E2E lintas repo, rilis v1.0.0 | 6 | P0 | 3 pd | semua |

**Total ±71 pd.** Jalur kritis: 00 → 02 → 06 → 07 → 08/09 → 14 → 16 → 19. Fase 2 (11–13) paralel dengan fase 3.

### Urutan mulai di repo ini
1. **MCP-V1-00** (kontrak) dan **MCP-V1-02** (schema) — memblokir Dashboard.
2. **MCP-V1-01 bagian instalasi pgvector** + indeks 1024 — tidak menunggu Gateway.
3. **MCP-V1-06 spike Chrome** (1–2 hari pertama task): ukur laju aman Google Search/Maps & frekuensi CAPTCHA di profil nyata — angka ini menentukan desain antrean F1/F1b.
4. **MCP-V1-03** agent baru, lalu **MCP-V1-04** setelah `embed-local` Gateway hidup.

---

## MCP-V1-00 — Kontrak 1.3

| Dependensi | — · bersama DSH-V1-00 | Referensi | `M3_TASK_000`, `contracts/` |
|---|---|---|---|

**Spesifikasi**
- `intel_job_create.kind` baru: `company_discovery`, `company_import`, `contact_finder`, `social_listening`, `book_ingest`, `opportunity_scoring`, `strategy`, `content_bank`; parameter per kind (Pydantic ↔ Zod); semua job punya `llm_assist: bool = false` dan `budget_usd`.
- Toolset baru: `mi_*`, `ci_*`, `kn_*`, `kg_*`, `pb_*`, `opp_*`, `st_*`, `ct_*` (daftar final di task masing-masing).
- Event baru: `intel.job.awaiting_review`, `intel.job.paused` (alasan: `captcha`, `budget`, `manual`), `intel.company.upserted`, `ci.import.status`, `kn.asset.status`, `opp.scores.updated`, `ct.batch.ready`.
- Pagination seragam (`cursor`, `limit ≤ 200`, `total_estimate`). Error baru: `UPLOAD_TOO_LARGE`, `UNSUPPORTED_FILE`, `REVIEW_REQUIRED`, `CAPTCHA_REQUIRED`, `LOCAL_MODEL_UNAVAILABLE`.

**Acceptance Criteria:** fixture `contracts/1.3/` lulus di pytest & Jest; tool 1.2 tidak berubah perilaku.

---

## MCP-V1-01 — pgvector, HNSW 1024, embedder `embed-local`

| Dependensi | LGW-V1-03 (instalasi pgvector tidak) | Referensi | TASK-19, `docs/knowledge-base.md`, V1-K7/K8 |
|---|---|---|---|

**Spesifikasi**
- Pasang pgvector → `scripts\migrate.bat` mempromosikan `real[]` → `vector`. `KB_REQUIRE_PGVECTOR=true` (default v1) → startup menolak mode cadangan.
- Migrasi indeks HNSW parsial `embed_dim = 1024`.
- Provider embedding **per koleksi** (`embed_model`: `embed-default` | `embed-local`).
- Kirim `input_type: query|document` dan header prioritas `X-Priority: interactive|bulk` ke Gateway (LGW-V1-04). Ingest buku/import = `bulk`; pencarian & klasifikasi interaktif = `interactive`.
- Chunk ≤ 1.024 token untuk koleksi `embed-local`; batch dari hasil LGW-V1-05.
- `embed-local` mati → job `bulk` pause dengan `LOCAL_MODEL_UNAVAILABLE` (tidak jatuh ke model berdimensi lain).

**Pengujian:** promosi tanpa kehilangan data; `EXPLAIN` memakai HNSW 1024; eval `tests/eval/kb` live dengan `embed-local` → recall@6 hybrid ≥ 0,85.

**Acceptance Criteria:** 200.000 chunk 1024-dim → `kb_search` p95 < 150 ms (tanpa embed query); koleksi 1536 lama tetap jalan.

---

## MCP-V1-02 — Schema (`0005_market_intel`)

| Dependensi | 00 | Referensi | blueprint §4, V1-K3/K6/K14 |
|---|---|---|---|

**Tabel (schema `mcp`; `workspace_id`, `created_at`, `updated_at`, `deleted_at` bila relevan)**

| Tabel | Kolom kunci |
|---|---|
| `regions` | `code` (Kemendagri), `level` (province/regency_city), `parent_code`, `name`, `aliases text[]`, `centroid_lat/lng`; seed 38 provinsi + kab/kota |
| `industries` | `kbli_code`, `parent_code`, `level`, `name_id`, `name_en`, `description`, `embedding vector(1024)` (untuk klasifikasi lokal); seed KBLI 2020 |
| `industry_isic_map` | `kbli_code`, `isic_code` (Rev.4), `isic_name` |
| `product_analyses`, `segments` | seperti rev. 1 (status draft/approved/rejected, `framework_refs`) |
| `companies` | `name`, `legal_name`, `org_type` (company/government/education/health/ngo/other), `primary_domain` (unik per ws, nullable untuk instansi tanpa website), `description_quotes jsonb`, `kbli_code`, `size_band`, `completeness`, `status` (active/stale/merged), `merged_into`, `last_enriched_at`, `origin` (discovery/import/manual) |
| `company_domains` | `company_id`, `domain` (unik), `is_primary`, `verification` (auto/manual), `confidence` |
| `company_locations` | `kind`, `address_raw`, `street`, `region_code` (→ `regions`), `postal_code`, `lat numeric(9,6)`, `lng numeric(9,6)`, `geocode_source` (jsonld/gmaps/nominatim/import/manual), `precision` |
| `contacts` | `kind` (email/phone/whatsapp/social/form), `value_normalized`, `contact_type` (generic/personal/role), `person_name`, `job_title`, `source_url`, `verification_status`, `review_status`; UNIQUE(`company_id`,`kind`,`value_normalized`) |
| `contact_suppressions` | `value_hash`, `reason`, `created_by` |
| `company_needs` | `need`, `category`, `evidence jsonb [{source_record_id, quote ≤ 300}]`, `confidence`, `method` (local/llm) |
| `market_signals` | `kind`, `subject_type`, `subject_id`, `text`, `strength`, `observed_at`, `source_record_id` |
| `source_records` | provenance per field: `entity_type`, `entity_id`, `field`, `value_hash`, `source_url`, `source_kind` (web/gmaps/google_search/nominatim/social/import/manual), `method`, `collected_at`, `verified_at`, `reliability`, `job_id`, `import_row_id` |
| `company_snapshots` | riwayat perubahan |
| `import_batches` | `upload_id`, `filename`, `sheet`, `rows_total/ok/review/failed`, `column_mapping jsonb [{source_col, field, transform, confidence, method}]`, `mode` (fill_db/contact_targets), `status`, `rolled_back_at` |
| `import_rows` | `batch_id`, `row_no`, `raw jsonb`, `normalized jsonb`, `status` (inserted/merged/review/failed), `company_id`, `issues text[]` |
| `contact_finder_targets` | `company_id`, `priority`, `hint jsonb` (kota, alamat, website, kata kunci), `status` (queued/found/ambiguous/not_found/needs_hint), `candidates jsonb`, `attempts`, `last_attempt_at` |
| `geocode_cache`, `search_cache` | hash query → hasil, `fetched_at`, TTL |

Indeks: `companies (workspace_id, kbli_code)`, `company_locations (region_code)`, `(lat,lng)` untuk bbox, `pg_trgm` atas `companies.name`, HNSW 1024 atas `industries.embedding`.

**Acceptance Criteria:** migrasi naik/turun bersih; seed wilayah & KBLI+ISIC lengkap; constraint unik mencegah duplikat domain & kontak; read model membaca `public.products` (setelah DSH-V1-01).

---

## MCP-V1-03 — Agent baru, kapabilitas tahap, injeksi doktrin

| Dependensi | 02 | Referensi | `services/agent/profiles.py`, `services/intel/capabilities.py`, `documentation/agents.md`, V1-K17 |
|---|---|---|---|

**Spesifikasi**
- `ProfileSeed` + prompt `agent_<key>.v1.md` (≤ 600 token):

| Agent | Route | Toolsets | Denylist / batasan | Budget awal |
|---|---|---|---|---|
| `creative` | `creative` | `ct`, `kn`, `kb`, `mi` (baca), `pb`, `pm_agent` | tanpa `web`, `social`, `db`, `ci` | 80k |
| `curator` | `smart` | `kn`, `kg`, `kb` | tanpa `web`, `social`, `task`, `ci` | 200k (buku panjang) |
| `prospector` | `extract` | `ci`, `web` (terbatas), `job` | tanpa `social`, `trends`, `tpl`, `task`; satu-satunya yang boleh tulis `companies`/`contacts` | 60k |

- Perluas profil lama: `researcher` (+`mi_keyword_expand`, tanpa enrichment perusahaan), `analyst` (+`mi`, `opp`, `pb`, `kn`), `strategist` (+`st`, `pb`, `kn`, baca `opp`/`mi`).
- `capabilities.py`: kapabilitas baru `analyze_product`, `discover`, `enrich`, `import_map`, `find_contacts`, `extract_knowledge`, `strategize`, `create_content`; pemetaan tahap → kapabilitas per job kind; validasi `AGENT_NOT_CAPABLE` seperti sekarang.
- **Injeksi doktrin:** saat run dimulai, `agent_doctrines` aktif untuk peran tsb. (MCP-V1-13) disisipkan setelah prompt peran, jatah ±1.500 token, ditandai untuk prompt caching; versi doktrin dicatat di run.
- Key Gateway per agent baru (`intel_llm_key_rotate` & skrip provisioning) — bersama LGW-V1-03.
- Seed "Tambah agent default" (Dashboard) mencakup 6 agent.

**Acceptance Criteria:** 6 profil aktif; tahap dengan agent tak berkapabilitas ditolak; prompt run memuat doktrin versi aktif (test); biaya tercatat per agent di Gateway.

---

## MCP-V1-04 — Klasifikasi lokal berbasis embedding

| Dependensi | 01, 02 | Referensi | V1-K12, `docs/eval/sentiment.md` (model lokal belum dipilih) |
|---|---|---|---|

**Tujuan:** Pekerjaan volume tinggi tanpa LLM, memakai satu model lokal (`embed-local`) + aturan.

**Spesifikasi (`services/local_ml/`)**
- **KBLI/ISIC:** embedding nama + sektor + deskripsi → kNN ke `industries.embedding` → top-3 + skor; ambang yakin (mis. ≥ 0,55 & selisih top-1/top-2 ≥ 0,05) → otomatis; selain itu `review` atau LLM assist.
- **Mapping kolom:** fitur pola nilai (regex email/telepon/kode pos, rasio angka, panjang) + embedding header & sampel ↔ deskripsi field target; skor gabungan per pasangan; penugasan terbaik (Hungarian/greedy).
- **Sentimen:** head **logistic regression** (numpy, tanpa dependensi berat) di atas embedding; dataset 300–500 post Indonesia (draf label LLM sekali + koreksi manusia, ±$1); disimpan di `local_classifiers` (versi, metrik). Mengisi slot `method=model` TASK-20; `auto` → LLM hanya bila `llm_assist` dan confidence rendah.
- **Aspek:** kemiripan kalimat ↔ deskripsi `product_features` + aspek umum.
- **Tipe kontak:** aturan local-part email (info/sales/cs/humas/admin vs pola nama) + kamus nama Indonesia.
- **Klaster tema:** k-means/agglomerative pada embedding, label = kata khas klaster (c-TF-IDF).
- **Kebutuhan ekstraktif:** kalimat situs yang paling mirip prototipe "kebutuhan/masalah/rencana" → kutipan.

**Pengujian:** KBLI golden 200 entitas → top-1 ≥ 0,75, top-3 ≥ 0,9; mapping kolom 10 file contoh → ≥ 95% kolom benar; sentimen macro-F1 ≥ 0,80 (bila tidak tercapai → laporan + rekomendasi LLM assist).

**Acceptance Criteria:** semua tugas di atas berjalan dengan 0 token LLM saat `llm_assist=false`.

---

## MCP-V1-05 — Analisis produk (`mi_product_analyze`)

| Dependensi | 03, 04 | Referensi | F1 stage 1 |
|---|---|---|---|

**Spesifikasi**
- Input: `product_id`, wilayah (default Indonesia), `max_segments` (8), `mode` (`auto`/`review`). Agent: `analyst`.
- Langkah: (1) peringkat kandidat KBLI **lokal** (embedding produk + fitur ↔ `industries`), top-20; (2) `kn_retrieve` framework segmentasi; (3) LLM `smart` menyaring kandidat (tidak boleh di luar daftar), membentuk segmen (industri × org_type × ukuran × wilayah), kebutuhan yang dijawab, alasan, confidence, **query Google Search & Maps** per segmen; (4) simpan + `awaiting_review`.
- Tools: `mi_product_analyze`, `mi_product_analysis_get`, `mi_product_analysis_review`.

**Acceptance Criteria:** < 60 s, ≤ 8 segmen, semua kode KBLI valid & ter-map ISIC, ~$0,05/analisis.

---

## MCP-V1-06 — Discovery via Chrome (Google Search & Maps)

| Dependensi | 02 | Referensi | V1-K4/K5, TASK-14 (profil browser, SSRF/robots) |
|---|---|---|---|

**Spesifikasi**
- `DiscoveryProvider` dengan dua adapter Selenium (profil Chrome khusus riset, headful):
  - `google_search`: hasil organik halaman 1–2; buang iklan, marketplace, direktori, sosial (daftar blokir dapat dikonfigurasi).
  - `google_maps`: query "nama/kategori + wilayah" → listing: nama, kategori, alamat, telepon, website, koordinat (dari URL/atribut listing), rating ringkas (tidak menyimpan ulasan & data reviewer).
- **Rem volume** (angka awal, dikalibrasi di spike): Maps ≤ 30 lookup/jam/profil, Search ≤ 40 query/jam/profil, jeda acak, batas harian, `search_cache` (TTL 30 hari).
- **CAPTCHA/consent:** deteksi halaman `sorry`/reCAPTCHA → job `paused` (`CAPTCHA_REQUIRED`) + event; user menyelesaikan di jendela Chrome; tombol lanjut. Tidak ada otomatisasi pemecah CAPTCHA, rotasi proxy, atau penyamaran sidik jari.
- Selector DOM hanya di `services/collectors/google_selectors.py` + fixture HTML (pola sama dengan X/Threads).
- Tools: `ci_discover` (job), `ci_discovery_status` (laju, kuota tersisa, status pause).

**Spike (hari 1–2):** 100 query Search + 50 lookup Maps di profil nyata → catat frekuensi CAPTCHA & laju aman; perbarui angka default.

**Acceptance Criteria:** 1 segmen × 1 provinsi → ≥ 30 kandidat unik; job pause/lanjut tanpa kehilangan progres; tidak ada domain direktori/marketplace.

---

## MCP-V1-07 — Enrichment, kontak, geocoding, entity resolution

| Dependensi | 04, 06 | Referensi | V1-K5/K6, blueprint §4.3–4.4 |
|---|---|---|---|

**Spesifikasi**
- Crawl terarah (`web_crawl`): beranda + tautan `about|tentang|profil|contact|kontak|hubungi|produk|layanan|lokasi`; ≤ 8 halaman/domain; rate limit per domain; robots dihormati.
- **Ekstraksi deterministik** (default): JSON-LD/microdata `Organization`/`LocalBusiness`/`EducationalOrganization`/`Hospital`/`GovernmentOrganization`, `mailto:`/`tel:`/`wa.me`, regex email & telepon Indonesia, tautan sosial, data listing Maps.
- **Lokal:** KBLI/ISIC, tipe kontak, kebutuhan ekstraktif (MCP-V1-04).
- **LLM assist** (opsional, `extract`, tanpa tools, konten dibungkus sebagai data): hanya field yang kosong/ragu.
- Normalisasi: telepon E.164 `+62`, email lowercase + format + cek MX (tanpa SMTP), alamat → `region_code` (pencocokan nama + alias `regions`), domain eTLD+1.
- Geocoding: JSON-LD `geo` → koordinat Maps → Nominatim (`geocode_cache`, ≤ 1 req/s, `NOMINATIM_USER_AGENT`) → centroid wilayah (`precision=city/province`).
- Entity resolution: domain sama, atau nama trigram ≥ 0,85 + `region_code` sama, atau kemiripan embedding nama+alamat ≥ 0,92 → merge + snapshot.
- Completeness score; setiap field → `source_records`.
- Tools: `ci_company_enrich` (job), `ci_company_get`, `ci_company_list` (filter KBLI, org_type, wilayah, completeness, skor; cursor), `ci_geo_clusters` (bbox+zoom), `ci_contacts_list` (kontak personal sesuai izin & review).

**Pengujian:** golden 30 situs (termasuk sekolah, RS, instansi `.go.id`): email/telepon presisi ≥ 0,95, recall ≥ 0,85; 50 alamat berlabel → ≥ 70% ≤ 2 km untuk presisi `street`; fixture prompt injection.

**Acceptance Criteria:** 50 domain < 30 menit (di luar waktu tunggu rem Maps/Search); provenance lengkap; 0 token LLM saat `llm_assist=false`.

---

## MCP-V1-08 — Import XLSX/CSV agentic + Contact Finder

| Dependensi | 04, 06, 07 | Referensi | F1b, V1-K9 |
|---|---|---|---|

**Spesifikasi — Import (`company_import`, agent `prospector`)**
- Upload lewat `POST /uploads` (MCP-V1-11): `.xlsx` (openpyxl read-only, **nilai saja, formula tidak dieksekusi**, batas sel/sheet untuk mencegah zip bomb) dan `.csv` (deteksi encoding & delimiter); ≤ 20 MB / 50.000 baris.
- Profiling: daftar sheet, header (deteksi baris header bila bukan baris 1), 20 sampel, tipe per kolom.
- Mapping kolom otomatis (MCP-V1-04) → field: `name`, `org_type`, `sector_text`, `address_raw`, `city`, `province`, `postal_code`, `website`, `phone`, `email`, `notes`/`hint`. Kolom tak dikenal → `extra` jsonb (tidak dibuang). Transform: pecah alamat, gabung kolom, buang prefiks.
- Normalisasi deterministik + klasifikasi KBLI/ISIC lokal + dedupe vs DB (sama dengan MCP-V1-07).
- **Insert otomatis** baris yakin; baris ragu → `import_rows.status=review` (tidak memblokir batch). Ringkasan mapping disimpan (`column_mapping` + confidence) untuk ditampilkan.
- Rollback batch: hapus perusahaan yang **dibuat** batch ini dan kembalikan field yang **diubah** batch ini (dari snapshot).
- Data import → `source_kind=import`; tidak ditimpa enrichment, hanya dilengkapi.
- Mode `contact_targets` → setiap baris juga masuk `contact_finder_targets`.

**Spesifikasi — Contact Finder (`contact_finder`, agent `prospector`)**
1. Google Maps "nama + alamat/kota" → website, telepon, koordinat, kategori.
2. Tanpa website → Google Search `"<nama>" <kota>` (+ hint) → kandidat domain.
3. Verifikasi domain: kemiripan nama ↔ judul/halaman "tentang" (embedding), kecocokan kota, aturan TLD (`.ac.id`, `.sch.id` pendidikan; `.go.id` pemerintah; RS `.co.id`/`.id`/`.go.id`; perusahaan `.co.id`).
4. Status: `found` (1 kandidat yakin) / `ambiguous` (≥ 2 → user memilih) / `not_found` / `needs_hint`.
5. Domain terverifikasi → enrichment MCP-V1-07.
- Prioritas target dari user; antrean menghormati rem volume; estimasi waktu ditampilkan.
- Tools: `ci_import_profile`, `ci_import_start` (job), `ci_import_get`, `ci_import_review_row`, `ci_import_rollback`, `ci_targets_list`, `ci_target_update` (hint/prioritas/pilih kandidat), `ci_contact_finder_start` (job).

**Pengujian:** 10 file contoh (header berbeda, Bahasa Indonesia/Inggris, sel gabungan, baris kosong, encoding Windows-1252); file berisi formula `=HYPERLINK(...)`; rollback; 50 instansi berlabel (sekolah, RS, dinas) → domain benar ≥ 80% dari yang `found`.

**Acceptance Criteria:** kriteria rilis F1b (README §9.2); rollback mengembalikan DB persis ke sebelum batch.

---

## MCP-V1-09 — Pipeline F1 `company_discovery`

| Dependensi | 05, 07 | Referensi | M3_TASK_007, F1 |
|---|---|---|---|

**Spesifikasi**
- Tahap: `product_analysis` (analyst) → `awaiting_review` → `discovery` (prospector) → `enrichment` (prospector) → `index` (sistem) → `fit` (analyst/engine, bila MCP-V1-14 aktif).
- Parameter: `product_id`, wilayah, `segments` override, `max_companies` (100, maks 500), `review_mode`, `llm_assist`, `budget_usd`.
- `intel_job_estimate`: jumlah query/lookup, **perkiraan durasi** (dari rem volume), token LLM, embedding (gratis).
- Checkpoint per domain; resume; cancel aman; retry = job baru.
- `index`: halaman → `companies:<ws>` + `kg_chunk_links` (MCP-V1-12).

**Acceptance Criteria:** kriteria rilis F1 (README §9.1) di stack nyata; retry tanpa duplikasi perusahaan.

---

## MCP-V1-10 — Pipeline F2 `social_listening`

| Dependensi | 04 | Referensi | TASK-15/16/17/20, F2 |
|---|---|---|---|

**Spesifikasi**
- `researcher`: `mi_keyword_expand` (nama, varian, kategori, kompetitor, istilah kebutuhan; + `trends_related`) → review → koleksi X/Threads/Trends.
- KB `social:<ws>` (dokumen per hari × kata kunci).
- Sentimen per aspek **lokal** (MCP-V1-04); tema lokal; market signal (`need/complaint/purchase_intent/competitor_mention`) via prototipe embedding → `market_signals`; LLM assist opsional.
- `analyst`: ringkasan & laporan `analysis_reports.kind=social_listening` (share of voice, tren sentimen, tema, sinyal, rekomendasi) — satu-satunya panggilan LLM wajib (~$0,1/job).
- Tema & sinyal → node KG (bukan per post).
- Tools: `mi_keyword_expand`, `mi_signals_list`, `mi_themes_get`.

**Acceptance Criteria:** kriteria rilis F2 (README §9.3).

---

## MCP-V1-11 — Upload PDF & `book_ingest`

| Dependensi | 01 | Referensi | blueprint §5.4 A–B, V1-K9 |
|---|---|---|---|

**Spesifikasi**
- **`POST /uploads`** (auth key Dashboard, scope `kn`/`ci`): streaming ke `STORAGE_DIR/uploads/<ws>/<sha256>.<ext>`; tipe diizinkan: PDF (≤ 100 MB, magic `%PDF`, tidak terenkripsi, ≤ `PDF_MAX_PAGES` 2.000), XLSX/CSV (≤ 20 MB). Nama file asli tidak dipakai sebagai path; dedupe sha256 per workspace.
- `knowledge_assets` (`kind`: book/brand/article; `title`, `authors`, `edition`, `year`, `isbn`, `language`, `license_note`, `sha256`, `pages`, `status`, `stats`) + `knowledge_pages` (`page_no`, `text`, `needs_ocr`).
- Ekstraksi `pypdf`/`pdfplumber` per halaman (batas waktu & memori), outline/bookmark → bab, fallback heuristik heading.
- Chunk semantik ≤ 800 token, overlap 100, breadcrumb, metadata `{asset_id, kind, title, edition, chapter, section, page_start, page_end}`.
- `embed-local` (`bulk`) → `books:<ws>`; progres via `kn.asset.status`.
- Tools: `kn_asset_register`, `kn_asset_ingest` (job), `kn_assets_list`, `kn_asset_get`, `kn_asset_delete`, `kn_search` (≤ 600 karakter + sitasi; teks penuh hanya admin).

**Pengujian:** PDF 100/101 MB, terenkripsi, hasil scan, dengan/tanpa outline; ingest ulang = 0 panggilan embedding; path traversal.

**Acceptance Criteria:** buku ±500 halaman selesai dalam target LGW-V1-05; 20 pertanyaan berlabel → ≥ 85% sitasi halaman tepat (±1).

---

## MCP-V1-12 — Knowledge card & knowledge graph lintas koleksi

| Dependensi | 11 | Referensi | blueprint §5.3–5.4, V1-K10/K15 |
|---|---|---|---|

**Spesifikasi**
- **`curator`** mengekstrak per bab (LLM `smart`, JSON schema, tanpa tools, setia teks) menjadi `knowledge_cards`:
  - `card_type`: `strategy_framework`, `persuasion_principle`, `copywriting_formula`, `content_format`, `channel_tactic`, `metric_kpi`, `research_method`, `anti_pattern`, `brand_rule` (dari aset `brand`).
  - Isi: konsep, tujuan, kapan dipakai, langkah/komponen, kriteria evaluasi (rubrik), contoh (parafrase, bukan salinan), keterbatasan, `sources [{chunk_id, page}]`, `roles` (analyst/strategist/creative), `status` draft/approved/rejected, `version`.
- `kg_nodes` (`type`: concept, framework, step, principle, formula, format, metric, industry, segment, company, product, need, theme, signal; `label`, `canonical_key`, `aliases`, `embedding vector(1024)`, `props`), `kg_edges` (`type`: part_of, requires, measures, applies_to, related_to, has_need, fits, in_industry, mentions, evidences; `weight`, `evidence`), **`kg_chunk_links`** (`chunk_id`, `node_id`, `relation` mentions/defines/evidences, `score`).
- **Lintas koleksi:** buku → node dari card (LLM, sekali per buku); perusahaan → node dari hasil enrichment (tanpa LLM tambahan); sosial → node tema & sinyal (lokal). Chunk mana pun ditautkan ke node lewat kemiripan embedding + kecocokan label.
- **L3 "terikat ke data":** job terjadwal membuat edge `applies_to` antara card/framework approved ↔ segmen/industri/kebutuhan/tema yang relevan (kemiripan embedding + aturan tipe), sehingga profil perusahaan/segmen langsung membawa framework terkait tanpa dicari.
- Kanonikalisasi: kemiripan label ≥ 0,92 → kandidat merge (dikonfirmasi `curator` atau manusia).
- Tools: `kg_query`, `kg_neighbors` (depth ≤ 2), `kg_path` (depth ≤ 4), `kg_subgraph` (≤ 500 node), `kg_for_entity` (framework/insight yang melekat ke entitas), `kn_card_list/get/review`.

**Pengujian:** golden bab → card sesuai label manual (F1 ≥ 0,7); merge "STP" ↔ "Segmentation, Targeting, Positioning"; batas waktu traversal.

**Acceptance Criteria:** setiap node/edge punya provenance; hanya card `approved` yang masuk doktrin/playbook; `kg_for_entity` untuk perusahaan contoh mengembalikan framework relevan.

---

## MCP-V1-13 — Doktrin, playbook, `kn_retrieve`, evaluator

| Dependensi | 03, 12 | Referensi | blueprint §5.5–5.7, V1-K11 |
|---|---|---|---|

**Spesifikasi**
- **L2 Doktrin** (`agent_doctrines`: `role`, `version`, `content` ≤ 1.500 token, `card_ids`, `status`): `curator` merangkum card approved per peran (`analyst`, `strategist`, `creative`) menjadi prinsip ringkas + daftar framework yang tersedia; aset `brand` mendapat prioritas tertinggi. Versi baru perlu approve manusia; diinjeksi di MCP-V1-03.
- **L4 Playbook** (`agent_playbooks` YAML berversi: objective, when_to_use, inputs, steps, allowed_tools, output_schema, evaluation_rubric, card_ids) + `playbook_runs`. Seed: `market_segmentation`, `product_fit_assessment`, `digital_marketing_strategy`, `content_pillar_plan`, `content_bank_batch`.
- **L1+L3 `kn_retrieve(query, role, entity?)`:** hybrid `books:<ws>` + ekspansi KG 1-hop + `kg_for_entity` → konteks ≤ 1.500 token bersitasi.
- **L5 Evaluator:** fungsi internal yang menilai output terhadap `evaluation_rubric` card/playbook (skor per kriteria + kekurangan); dipakai playbook & bank konten; atribusi biaya ke agent pemanggil.
- Tools: `kn_doctrine_get/propose/review`, `pb_list`, `pb_get`, `pb_save`, `pb_run` (job), `pb_run_get`, `kn_retrieve`.

**Acceptance Criteria:** run agent memuat doktrin aktif (versi tercatat); `digital_marketing_strategy` pada data nyata → output schema valid, rekomendasi tertaut ke data & card (aset, halaman, versi).

---

## MCP-V1-14 — Opportunity Engine

| Dependensi | 07, 09 | Referensi | blueprint §6 |
|---|---|---|---|

**Spesifikasi**
- `scoring_models` berversi (bobot 30/20/20/15/10/5; high ≥ 80, medium ≥ 60).
- `product_fits`: kemiripan embedding `company_needs` ↔ `product_features` (lokal) + kecocokan KBLI dengan segmen hasil analisis produk; LLM assist opsional untuk rasional.
- `opportunity_scores` (komponen known/unknown + bukti, `overall`, `priority`, `confidence`, `data_completeness`, `missing_info`, riwayat, tanpa update di tempat). Komponen unknown tidak dihitung 0; completeness < 0,5 → `insufficient_data`.
- Penjelasan = template dari komponen (tanpa LLM); ringkasan naratif opsional on-demand.
- Tools: `opp_score_run` (job), `opp_list`, `opp_get`, `opp_explain`, `opp_compare_versions`.

**Pengujian:** engine 100% branch; 20 perusahaan berlabel → kesepakatan ≥ 70%; ganti bobot tanpa panggilan embedding/LLM ulang.

**Acceptance Criteria:** kriteria rilis F3; ~$0 untuk 3.500 perusahaan × 2 produk saat `llm_assist=false`.

---

## MCP-V1-15 — Market analytics & KPI

| Dependensi | 09, 10, 14 | Referensi | blueprint §3.1–3.2, §7 |
|---|---|---|---|

**Spesifikasi:** materialized view per industri (KBLI/ISIC), `org_type`, wilayah (`regions`), produk × industri, segmen; `kpi_definitions` (kode, rumus, periode, sumber). Tools: `mi_kpis`, `mi_industry_map`, `mi_geo_summary` (provinsi/kab-kota), `mi_product_matrix`, `mi_segment_compare`; cache 300 s; setiap angka membawa `as_of` & definisi.

**Acceptance Criteria:** KPI dapat direkonsiliasi dengan query mentah; 10.000 perusahaan → < 500 ms per tool.

---

## MCP-V1-16 — Strategi & Bank Konten

| Dependensi | 13, 14 | Referensi | TASK-22/23, V1-K15/K16 |
|---|---|---|---|

**Spesifikasi — Strategi (`strategy`, agent `strategist`, route `smart`)**
- Input: produk, segmen/industri, tujuan, periode. Playbook `digital_marketing_strategy`.
- Output: ringkasan, segmen prioritas (angka dari MCP-V1-15), positioning vs kompetitor, funnel, channel mix, **content pillar**, KPI, rencana aksi, asumsi & risiko, sitasi data + card.
- **Content brief** untuk `creative`: pillar, platform, tahap funnel, segmen, pesan kunci, insight pendukung (tema/sinyal/pain point), card yang wajib dipakai.
- `analysis_reports.kind` + `digital_strategy`, `social_listening`, `content_bank`. Tools: `st_generate` (job), `st_get`, `st_revise`.

**Spesifikasi — Bank Konten (`content_bank`, agent `creative`, route `creative`)**
- Input: content brief/strategi approved, jumlah item (default 30), platform (IG, TikTok, LinkedIn, X, Threads), periode.
- **Divergen:** 2–3× variasi ide per slot. **Konvergen:** evaluator rubrik card (hook, kejelasan, kecocokan segmen, kepatuhan brand) → ranking → top-N.
- **Orisinalitas:** kosinus draf ↔ chunk `books:<ws>` > 0,9 → buat ulang (maks 2×), lalu tandai.
- Item: platform, format (carousel/reel/thread/single image/article), pillar, funnel_stage, segmen, hook, caption, CTA, hashtag, brief visual (teks), `framework_refs [{card_id, asset, page}]`, `insight_refs`, skor evaluasi, alasan.
- Hasil dikirim ke Dashboard (`ct.batch.ready`); Dashboard yang menyimpan & mengelola status (V1-K3). MCP menyimpan `content_generation_runs` untuk audit.
- Tools: `ct_generate` (job), `ct_get_batch`, `ct_regenerate_item` (dengan feedback).

**Acceptance Criteria:** kriteria rilis F5 (README §9.6); tanpa strategi approved → `REVIEW_REQUIRED`.

---

## MCP-V1-17 — Data quality & tata kelola

| Dependensi | 07, 08 | Referensi | blueprint §4.4, V1-K6 |
|---|---|---|---|

**Spesifikasi:** dedupe terjadwal (laporan review), `stale` bila > 90 hari, refresh bertahap (mengikuti rem volume); koreksi manual (`ci_company_update` → `source_kind=manual` menang), `ci_company_merge`, `ci_contact_review`, `ci_suppression_add/remove`; ekspor kontak hanya generic + personal approved, tanpa suppressed, nilai yang diawali `= + - @` di-escape (CSV injection), tercatat di audit; hapus atas permintaan.

**Acceptance Criteria:** override manual tidak tertimpa; kontak suppressed tidak pernah muncul di list/ekspor (test).

---

## MCP-V1-18 — Evaluasi & metrik

| Dependensi | 04–16 | Referensi | blueprint §11.4, TASK-26 |
|---|---|---|---|

**Spesifikasi:** `tests/eval/v1/` — klasifikasi lokal (KBLI, kolom, sentimen), entity matching, ekstraksi kontak/alamat, geocode, Contact Finder, retrieval buku & sitasi, kualitas card, product-fit, rubrik konten, orisinalitas; metrik operasional `cost_per_job` per kind (Gateway LGW-V1-06), `recommendation_acceptance_rate` & `content_approval_rate` (approval Dashboard), `opportunity_conversion_rate`. Laporan `scripts\eval.bat v1` → `documentation/v1.0.0/eval-report.md`; regresi > 5 poin menggagalkan CI eval.

---

## MCP-V1-19 — Security, dokumentasi, rilis v1.0.0

**Checklist**
- [ ] Security review: SSRF (URL dari Search/Maps/situs), upload (ukuran, magic bytes, PDF/XLSX bomb, formula, path traversal), prompt injection, scope toolset & agent baru, kontak personal di log.
- [ ] `docs/`: `company-discovery.md` (Chrome, rem volume, CAPTCHA), `company-import.md`, `knowledge-engine.md` (card, KG, doktrin), `content-bank.md`, `opportunity-scoring.md`, `data-governance.md`; perbarui `knowledge-base.md`, `DATABASE.md`, `MCP_SPEC.md`, `agents.md`.
- [ ] `pyproject.toml` → `1.0.0`, `CHANGELOG.md`, tag `v1.0.0`; E2E F1–F5 bersama DSH-V1-14 & LGW-V1-07; `graphify update .`

**Acceptance Criteria:** kriteria rilis lintas repo (README §9) lulus.
