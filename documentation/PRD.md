# Product Requirements Document (PRD)
## MCP Marketing — Custom MCP Server untuk Agentic Marketing

| | |
|---|---|
| Versi | 1.0 |
| Status | Draft — menunggu review |
| Dokumen terkait | [README](README.md) · [Architecture](ARCHITECTURE.md) · [MCP Spec](MCP_SPEC.md) · [Use Cases](USE_CASES.md) · [Database](DATABASE.md) |
| Proyek terkait | `llm-genai` (LLM Gateway) · `marketingdashboardjtp` (Dashboard, Modul 1) |

---

### 1. Latar Belakang

Dashboard Marketing JTP (Modul 1) sudah memiliki ruang kerja ala Notion dengan AI Agent sebagai anggota tim, tetapi agent-nya belum ada: Dashboard hanya menyediakan **kontrak** (`M1_MCP_INTEGRATION.md`, contract 1.0) untuk "Project 2 — Custom MCP Server". Di sisi lain, LLM Gateway sudah menghimpun model lokal (OpenVINO di iGPU/NPU) dan API eksternal (OpenAI, Anthropic, Gemini, dll.) di balik API kompatibel OpenAI dengan route, fallback, kuota, dan pencatatan biaya.

Yang belum ada adalah lapisan **tools** yang membuat agent produktif untuk pekerjaan marketing (riset sosial media, tren, kompetitor, menyusun strategi, mengelola task) **tanpa membuang token** — misalnya dengan memasukkan ribuan tweet mentah ke prompt.

### 2. Visi

> Agent marketing yang bekerja seperti analis berpengalaman: mengumpulkan data sendiri, mengolahnya dengan kode, dan hanya "berpikir" (memakai LLM) pada bagian yang memang butuh penalaran — lalu menuliskan hasilnya langsung ke Dashboard sebagai dokumen dan task.

### 3. Tujuan & Ukuran Keberhasilan

| Tujuan | Indikator (diukur saat UAT, TASK-26) |
|---|---|
| G1. Menjadi implementasi Project 2 untuk Dashboard | 100% contract test `M1_MCP_INTEGRATION` lulus; UC2–UC4 Dashboard berjalan end-to-end memakai MCP Marketing (bukan Mock) |
| G2. Menghemat token LLM | Pada eval suite 10 skenario baku, total token LLM turun **≥ 70%** dibanding baseline "data mentah langsung ke LLM", dengan kualitas jawaban tidak lebih buruk (skor rubrik reviewer ≥ baseline) |
| G3. Output tool ringkas | p95 ukuran output tool mode `concise` ≤ **1.500 token**; tidak ada tool yang mengembalikan > 20 item data inline |
| G4. Data marketing bisa dikumpulkan mandiri | Crawl X, Threads, Google Trends, dan web untuk periode & kata kunci tertentu berhasil ≥ 95% job pada UAT (di luar gangguan upstream yang tercatat) |
| G5. Hasil analisis langsung bisa dipakai tim | Laporan analisis dapat dikirim ke Dashboard sebagai Project Brief/task dalam ≤ 1 tool call |

### 4. Persona

| Persona | Contoh | Kebutuhan dari MCP |
|---|---|---|
| Marketing Manager | Pemberi brief & reviewer | Brief diterjemahkan agent jadi rencana kerja + task di Dashboard; laporan ringkas & berbasis data |
| Marketing Staff / Analyst | Pembuat task, analis sosmed | Crawl data periode tertentu, sentimen, tren, tanpa koding |
| AI Agent (internal) | `strategist`, `researcher`, `analyst` | Toolset kecil & jelas, output padat, handle untuk data besar |
| Klien MCP eksternal | Claude Desktop/Cursor milik staf | Akses toolset yang diizinkan memakai API key pribadi |
| Admin / DevOps | Pengelola server | Kelola API key, profil browser, kuota, melihat pemakaian token & biaya |

### 5. Scope

**Termasuk (v1.0):**
1. Server MCP Streamable HTTP + autentikasi API key per klien + filter toolset.
2. Implementasi penuh kontrak Dashboard 1.0: `pm_dispatch_task_to_agent`, `pm_request_agent_estimation`, `pm_notify_agent_mention`, `pm_execute_prompt_block`, `pm_cancel_agent_run`, tool agent `pm_query_project_context`, webhook outbox, Agent API client.
3. Agent runtime (loop tool-calling via LLM Gateway) dengan profil agent dan anggaran token.
4. Toolset: template Notion, task & subtask, web scraping Selenium, crawling X, Threads, Google Trends, CRUD database, vector DB, sentimen, analisis pasar, strategi campaign, strategi digital marketing.
5. Lapisan hemat token: result handle, paginasi, proyeksi field, agregasi server-side, cache, mode `concise/detailed`, token budget.
6. Ekstensi Dashboard **Service API** (contract 1.1, aditif) agar task/blok bisa dikelola di luar agent run.
7. Observability: log, metrics Prometheus, laporan pemakaian token & penghematan.

**Di luar scope v1.0:**
- Posting/publikasi ke sosial media (hanya baca/analisis).
- Crawling Instagram, TikTok, Facebook, YouTube (disiapkan pola adapter-nya saja).
- UI admin sendiri (pengelolaan via CLI `manage.py` + endpoint admin JSON).
- OAuth 2.1 untuk klien MCP (pakai API key dulu).
- Fine-tuning model; inferensi model besar di dalam MCP (semua LLM lewat Gateway).
- Bypass CAPTCHA otomatis / rotasi proxy residensial.

### 6. Requirement Fungsional

| ID | Requirement | Prioritas | Task |
|---|---|---|---|
| FR-01 | Server MCP Streamable HTTP di `/mcp`, health `/health/live` & `/health/ready`, metrics `/metrics` | P0 | 04, 24 |
| FR-02 | API key per klien dengan scope toolset; daftar tool yang terlihat mengikuti scope | P0 | 04 |
| FR-03 | Semua tool punya input & output schema; error terstandar `{code, message, retryable}` + `isError` | P0 | 04, 05 |
| FR-04 | Result handle: data besar disimpan, tool mengembalikan `handle` + ringkasan; `data_get`/`data_aggregate` untuk membaca bertahap | P0 | 05 |
| FR-05 | Cache hasil tool deterministik (TTL per tool) dan cache respons LLM untuk prompt identik | P1 | 05 |
| FR-06 | Token budget per run/per klien; tool menolak bila budget habis (`BUDGET_EXCEEDED`) | P0 | 05, 10 |
| FR-07 | Klien LLM Gateway: route policy per jenis pekerjaan, JSON mode, retry, pencatatan usage | P0 | 06 |
| FR-08 | Job queue untuk pekerjaan lama (crawl, ingest, analisis besar), progress & pembatalan | P0 | 07 |
| FR-09 | Webhook outbox ke Dashboard: HMAC, idempotent `event_id`, berurutan per run, retry sesuai tabel kontrak | P0 | 08 |
| FR-10 | Tool kontrak `pm_*` 3.1–3.5 sesuai input/output schema contract 1.0 | P0 | 09 |
| FR-11 | Agent runtime: profil agent (`strategist`, `researcher`, `analyst`), estimasi tanggal, tulis blok, buat subtask, komentar, `pm_query_project_context`, pembatalan, timeout | P0 | 10 |
| FR-12 | Service API Dashboard (contract 1.1) untuk project/task/blok/komentar di luar run, ber-aktor agent | P0 | 11 |
| FR-13 | Template Notion: daftar, render, generate dari brief (LLM), simpan, terapkan ke Project Brief/task (blok + subtask) dengan format blok Modul 1 | P0 | 12 |
| FR-14 | Task & subtask: list, get, create, update, move, reschedule, komentar, breakdown otomatis (preview/apply), batas kedalaman 3 | P0 | 13 |
| FR-15 | Profil browser Chrome terdaftar (user-data-dir, chromedriver, UA, proxy), lock eksklusif, cek status login | P0 | 14 |
| FR-16 | Scraping web: banyak URL, mode text/markdown/selector, crawl domain terbatas, robots.txt dihormati (default), SSRF guard | P0 | 14 |
| FR-17 | Crawl X berdasarkan kata kunci & periode (`since`, `until`), bahasa, batas item; adapter API & browser | P0 | 15 |
| FR-18 | Crawl Threads berdasarkan kata kunci & periode; adapter API resmi & browser | P0 | 16 |
| FR-19 | Google Trends: interest over time, by region, related queries/topics untuk kata kunci & periode, geo default `ID` | P0 | 17 |
| FR-20 | CRUD database: entitas whitelist, filter terstruktur (tanpa SQL bebas untuk non-admin), paginasi cursor, proyeksi field | P0 | 18 |
| FR-21 | Vector DB: koleksi, ingest (teks/URL/handle/project Dashboard), chunking, embedding via Gateway, pencarian hybrid, sinkron blok Dashboard | P0 | 19 |
| FR-22 | Analisis sentimen Bahasa Indonesia/Inggris bertingkat (model lokal → LLM), agregasi per hari/kata kunci/sumber, aspek opsional | P0 | 20 |
| FR-23 | Analisis pasar: tren, share of voice, sentimen kompetitor, SWOT, peluang; menghasilkan laporan tersimpan | P0 | 21 |
| FR-24 | Analisis strategi campaign: evaluasi/perencanaan dengan funnel, KPI SMART, rekomendasi kanal & anggaran | P0 | 22 |
| FR-25 | Strategi digital marketing (SOSTAC/RACE), pilar konten, kalender outline; dapat diterapkan jadi template + task di Dashboard | P0 | 23 |
| FR-26 | Laporan pemakaian token/biaya per tool, klien, agent, run; laporan penghematan vs baseline | P1 | 24 |
| FR-27 | Tool `toolbox_list` untuk penemuan toolset secara bertahap (progressive disclosure) | P1 | 04 |

### 7. Requirement Non-Fungsional

| ID | Kategori | Requirement |
|---|---|---|
| NFR-01 | Kompatibilitas | Python **3.11** (selaras LLM Gateway); jalan di Windows 10/11 tanpa admin; Linux didukung untuk mode tanpa browser |
| NFR-02 | Kontrak | Dashboard → MCP: tool async membalas ≤ 15 s, estimasi ≤ 30 s (sesuai `MCP_CALL_TIMEOUT_MS`/`MCP_ESTIMATION_TIMEOUT_MS`) |
| NFR-03 | Performa | Overhead server (tanpa upstream) p95 < 100 ms per tool call sinkron; `data_get` p95 < 300 ms untuk 10.000 baris |
| NFR-04 | Hemat token | Output mode `concise` p95 ≤ 1.500 token, maks keras 4.000 token (dipotong + `truncated: true`); deskripsi tool ≤ 300 karakter |
| NFR-05 | Hemat token | Analisis sentimen 1.000 teks memakai ≤ 15.000 token LLM (sisanya oleh model lokal) |
| NFR-06 | Keandalan | Job crawl dapat dilanjutkan (resume) setelah restart worker; webhook tidak hilang saat Dashboard down (outbox) |
| NFR-07 | Keamanan | API key hash HMAC + pepper; secret ≥ 32 byte; SSRF guard; konten eksternal ditandai *untrusted*; tidak ada `password_hash`/email user yang terbaca MCP |
| NFR-08 | Privasi | Data sosial media disimpan dengan retensi (default 180 hari); PII pengguna sosmed tidak dikirim ke LLM eksternal kecuali diperlukan (handle di-hash pada mode `privacy=strict`) |
| NFR-09 | Kepatuhan | robots.txt dihormati secara default; rate limit per domain; scraping platform hanya dengan akun khusus dan persetujuan internal |
| NFR-10 | Observability | Log JSON (`structlog`) dengan `request_id`, `client_id`, `tool`, `run_id`, `job_id`; metrics Prometheus; audit tool call di DB |
| NFR-11 | Kualitas | Coverage ≥ 80% untuk `services/` & `tools/`; contract test Dashboard; eval suite token |
| NFR-12 | Bahasa | Deskripsi tool berbahasa Inggris singkat (lebih hemat token & dipahami semua model); output untuk user (laporan, komentar) Bahasa Indonesia secara default (`language` dapat diubah) |
| NFR-13 | Zona waktu | Default `Asia/Jakarta`; semua timestamp disimpan UTC (`timestamptz`) |

### 8. Strategi Penghematan Token (ringkas)

Detail di [MCP_SPEC §3](MCP_SPEC.md#3-pola-hemat-token-wajib-untuk-semua-tool).

| # | Teknik | Contoh |
|---|---|---|
| S1 | **Kerjakan dengan kode, bukan LLM** | Hitung volume per hari, top hashtag, rata-rata engagement, slope tren di Python |
| S2 | **Handle, bukan data mentah** | `x_search` mengembalikan `handle` + 5 contoh + statistik; detail via `data_get` |
| S3 | **Proyeksi & paginasi** | `fields=["text","likes"]`, `limit=20`, `cursor` |
| S4 | **Model termurah yang cukup** | Klasifikasi → model lokal/`fast`; strategi → `smart` |
| S5 | **Pra-ringkas sebelum LLM** | Analisis pasar menerima ringkasan statistik + sampel representatif (bukan 5.000 post) |
| S6 | **Cache** | Trends sama dalam 24 jam, hasil scrape sama dalam 6 jam, respons LLM identik |
| S7 | **Toolset kecil per agent** | Agent `researcher` hanya melihat ±12 tool yang relevan |
| S8 | **Budget & pemotongan** | Budget per run; output dipotong rapi dengan penanda `truncated` |
| S9 | **Format padat** | Output tabular memakai `{"cols":[...],"rows":[[...]]}` bukan list objek berulang |

### 9. Integrasi

| Sistem | Arah | Mekanisme |
|---|---|---|
| Dashboard → MCP | Perintah agent | MCP `tools/call` (`pm_*`), Bearer `MCP_SERVER_API_KEY` |
| MCP → Dashboard | Hasil kerja agent | Webhook `POST /api/v1/webhooks/mcp/events` (HMAC `MCP_WEBHOOK_SECRET`) |
| MCP → Dashboard | Baca konteks dalam run | Agent API `/api/v1/agent-api/runs/{run_id}/…` (Bearer `MCP_SERVICE_TOKEN`) |
| MCP → Dashboard | Kelola task di luar run | Service API `/api/v1/mcp-api/…` (contract 1.1, TASK-11) |
| MCP → PostgreSQL Dashboard | Baca data, simpan data MCP | Role `mcp_app`: `SELECT` berkolom di `public`, penuh di schema `mcp` |
| MCP → LLM Gateway | Inferensi & embedding | `POST /v1/chat/completions`, `/v1/embeddings` dengan API key project `mcp-marketing` |
| MCP → Platform eksternal | Data | X API v2, Threads API, Google Trends, web (Selenium + chromedriver) |

### 10. Asumsi & Batasan

- Gateway berjalan di mesin yang sama (`http://127.0.0.1:8000/v1`); profil saat ini `remote-only` sehingga route `fast`/`smart` diarahkan ke provider remote — penghematan token langsung berdampak ke biaya.
- Embedding hanya tersedia lewat `embed-default` (OpenAI) — tidak ada embedding lokal di Gateway. Bila `OPENAI_API_KEY` kosong, vector DB memakai embedding lokal ONNX (D5 alternatif).
- Dashboard dan MCP memakai server PostgreSQL yang sama; ekstensi `pgvector` harus bisa dipasang (butuh superuser sekali).
- Akun X/Threads untuk mode browser adalah akun khusus organisasi.

### 11. Glosarium

| Istilah | Arti |
|---|---|
| Tool | Fungsi MCP yang dapat dipanggil LLM/klien |
| Toolset | Kelompok tool (mis. `social`, `trends`, `pm`) yang menjadi satuan izin & visibilitas |
| Handle | ID (`h_…`) yang menunjuk hasil data besar di schema `mcp` (tabel `result_sets`) |
| Job | Pekerjaan asinkron di antrean (`jobs`), mis. crawl 2.000 tweet |
| Run | Satu sesi kerja agent; ID korelasi utama `run_id` berasal dari Dashboard |
| Outbox | Tabel antrean webhook ke Dashboard (dikirim berurutan per run) |
| Route | Alias model di LLM Gateway (`fast`, `smart`, `embed-default`, …) |
| Baseline token | Jumlah token jika data mentah langsung dikirim ke LLM tanpa MCP |
