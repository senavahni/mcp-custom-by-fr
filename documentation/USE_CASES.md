# Use Cases — MCP Marketing

Setiap use case mencantumkan aktor, prasyarat, alur utama, alur alternatif/error, dan task terkait. Nama tool mengacu ke [MCP_SPEC.md](MCP_SPEC.md). Contoh data memakai kampanye fiktif **"Kopi Nusantara — Kampanye Q4"**.

Aktor: **MS** = Marketing Staff, **MM** = Marketing Manager, **AG** = AI Agent (agent runtime MCP), **DB** = Dashboard backend, **CL** = klien MCP eksternal (Claude Desktop/Cursor), **AD** = Admin.

---

## Ringkasan

| ID | Use case | Aktor utama | Prioritas | Task |
|---|---|---|---|---|
| UC-01 | Assign task ke agent → agent mengerjakan & melapor ke Dashboard | MS, DB, AG | P0 | 08, 09, 10 |
| UC-02 | Estimasi tanggal oleh agent | DB, AG | P0 | 09 |
| UC-03 | Mention agent di komentar untuk revisi | MM, DB, AG | P0 | 09, 10 |
| UC-04 | Menjalankan AI Prompt Block | MS, DB, AG | P1 | 09, 10 |
| UC-05 | Membatalkan run (unassign/hapus task) | MS, DB | P0 | 08, 09 |
| UC-06 | Generate template Notion dari brief & terapkan ke project | MS/CL, AG | P0 | 11, 12 |
| UC-07 | Mengelola task & subtask dari klien MCP | CL | P0 | 11, 13 |
| UC-08 | Scraping website kompetitor dengan profil Chrome | AG/CL | P0 | 07, 14 |
| UC-09 | Crawling X berdasarkan kata kunci & periode | AG/CL | P0 | 15 |
| UC-10 | Crawling Threads berdasarkan kata kunci & periode | AG/CL | P0 | 16 |
| UC-11 | Membandingkan tren Google untuk beberapa kata kunci | AG/CL | P0 | 17 |
| UC-12 | Query & CRUD database Dashboard | AG/CL | P0 | 18 |
| UC-13 | Membangun & mencari knowledge base (vector DB) | AG/CL | P0 | 19 |
| UC-14 | Analisis sentimen hasil crawl | AG/CL | P0 | 20 |
| UC-15 | Analisis pasar & kompetitor end-to-end | AG | P0 | 21 |
| UC-16 | Merencanakan / mengevaluasi campaign | AG/CL | P0 | 22 |
| UC-17 | Menyusun strategi digital marketing & menerapkannya ke Dashboard | AG | P0 | 23 |
| UC-18 | Administrasi: API key, profil browser, kuota, laporan token | AD | P0 | 04, 14, 24 |

---

## UC-01 — Assign task ke agent → agent mengerjakan & melapor

- **Aktor:** MS (memicu), DB, AG `strategist`
- **Prasyarat:** Agent `Agent_Strategist` (`mcp_agent_key=strategist`) aktif di workspace; MCP & Gateway berjalan; secret sama di kedua `.env`.
- **Pemicu:** MS meng-assign task "Generate Content Calendar 3 Minggu" ke Agent_Strategist.

**Alur utama**
1. DB memanggil `pm_dispatch_task_to_agent` (run_id baru, TaskSnapshot, context_blocks, callback).
2. MCP memvalidasi `contract_version`, `agent_key`; menyimpan `mcp.runs` (status `accepted`) dan membalas `{accepted:true, mcp_run_id}` ≤ 15 s.
3. Runtime menaruh `agent.task.started` di outbox → terkirim ke webhook DB.
4. AG mengirim `agent.status.changed{thinking, "Membaca brief project…"}`, memanggil `pm_query_project_context("brief kampanye Q4 tone of voice")`.
5. AG memanggil `tpl_get("content_calendar_3w", outline)` lalu `tpl_render(..., target=task, mode=apply, run_id)` → blok kalender dikirim via `agent.block.appended`, subtask "Week 1/2/3" via `agent.subtask.created` (`assign_to_self:true`).
6. AG menambahkan komentar ringkasan (`agent.comment.added`) dan mengirim `agent.task.completed{summary}`.
7. DB menandai task `done`; user melihat hasil real-time.

**Alternatif / error**
- A1. `agent_key` tidak dikenal → `isError`, `UNKNOWN_AGENT`, `retryable:false` → DB run `failed`.
- A2. Dispatch ulang dengan `run_id` sama (retry DB) → MCP mengembalikan `mcp_run_id` yang sama, tidak membuat run ganda.
- A3. Webhook 5xx/429 → outbox retry 1s, 2s, 4s … maks 5 menit/percobaan, total ≤ 30 menit, `event_id` sama; event berikutnya untuk run tsb menunggu.
- A4. Webhook 409 `RUN_NOT_ACTIVE` → runtime menghentikan agent, menandai run `cancelled_by_dashboard`, outbox pending dibuang.
- A5. Budget token run habis → AG mengirim komentar "hasil parsial" + `agent.task.failed{error_code:BUDGET_EXCEEDED, retryable:true}`.
- A6. Gateway `all_candidates_failed` 3× → `agent.task.failed{LLM_UNAVAILABLE, retryable:true}`.
- A7. Tidak ada progres 110 menit → runtime timeout → `agent.task.failed{RUN_TIMEOUT}` (sebelum timeout 120 menit DB).

## UC-02 — Estimasi tanggal oleh agent

- **Pemicu:** Assign ke agent pada task tanpa `start_date` & `end_date`.
1. DB memanggil `pm_request_agent_estimation` (sync, 30 s).
2. MCP menghitung heuristik (jumlah subtask, panjang deskripsi, hari kerja) lalu satu panggilan LLM `fast` (JSON) untuk koreksi & pesan.
3. MCP memvalidasi `start_date ≥ earliest_start_date`, `end_date ≥ start_date`, lalu membalas `{start_date, end_date, estimated_days, confidence, message}`.
- **Error:** LLM gagal/timeout 20 s → MCP mengembalikan estimasi heuristik saja (`confidence ≤ 0.4`); jika heuristik pun gagal → `isError UPSTREAM_TIMEOUT retryable:true` (DB lanjut dispatch tanpa tanggal).

## UC-03 — Mention agent di komentar untuk revisi

1. MM menulis "@Agent_Strategist ubah tone Week 2 jadi lebih formal" → DB memanggil `pm_notify_agent_mention`.
2. MCP membuat run `mention`; AG membaca thread + blok terkait (`pm_query_project_context`, scope task).
3. AG memperbarui blok Week 2 (`agent.block.appended` op `update` dengan `expected_version`).
4. AG membalas di thread yang sama (`agent.comment.added`, `parent_comment_id`) dan `agent.task.completed`.
- **Alternatif:** respons webhook berisi `conflicts` (versi blok berubah) → AG membaca ulang blok, menerapkan ulang sekali; bila masih konflik, AG menulis komentar meminta konfirmasi.

## UC-04 — Menjalankan AI Prompt Block

1. MS klik "Jalankan" pada blok `aiPrompt` ("Buat 5 ide caption Instagram untuk launch Kopi Gula Aren").
2. DB → `pm_execute_prompt_block`; AG menjalankan prompt dengan toolset terbatas (`kb`, `pm_agent`) dan route `fast` jika prompt ≤ 500 karakter tanpa kebutuhan data.
3. Hasil ditulis sebagai blok setelah prompt block (`after_block_id`).

## UC-05 — Membatalkan run

1. MS meng-unassign agent dari task yang sedang dikerjakan → DB menandai run `cancelled`, memanggil `pm_cancel_agent_run`.
2. MCP set flag cancel, menghentikan loop di langkah berikutnya (≤ 5 s), membatalkan job turunan (`jobs.parent_run_id`), membuang outbox pending, membalas `{cancelled:true}`.
- **Alternatif:** run tidak ditemukan → tetap `{cancelled:true}` (idempotent).

## UC-06 — Generate template Notion dari brief & terapkan ke project

- **Aktor:** MS lewat Claude Desktop (CL) atau AG.
1. CL memanggil `tpl_generate(brief="Launch produk Kopi Gula Aren 6 minggu, kanal IG+TikTok+X, budget 50 jt", kind="launch", mode="preview")`.
2. MCP memanggil Gateway `smart` (JSON mode) dengan katalog tipe blok ringkas; memvalidasi hasil dengan validator blok; mengembalikan **outline** (heading, jumlah blok, subtask beserta tanggal relatif).
3. MS menyetujui; CL memanggil `tpl_render(..., target{type:"project_brief", project_id}, mode="apply")` atau `tpl_generate(..., mode="save")` untuk dipakai lagi.
4. MCP menulis lewat Service API (`/mcp-api/blocks/ops`, `/mcp-api/projects/{id}/tasks`) dengan aktor agent `MCP_Assistant`; DB mencatat activity & real-time.
- **Error:** blok tidak valid setelah 2× perbaikan otomatis → `VALIDATION_ERROR` dengan daftar indeks blok; Service API 403 (agent bukan anggota workspace) → `FORBIDDEN`.

## UC-07 — Mengelola task & subtask dari klien MCP

1. CL: `task_list(project_id, status=["todo","in_progress"], due_before="2026-11-15")` → tabel 12 task.
2. CL: `task_breakdown(task_id, max_subtasks=6, mode="preview")` → 6 subtask usulan + tanggal.
3. CL: `task_breakdown(..., mode="apply")` → subtask dibuat (kedalaman ≤ 3).
4. CL: `task_reschedule(task_id, "2026-11-04", "2026-11-08", cascade=true)` → parent & subtask bergeser.
- **Error:** kedalaman > 3 → `HIERARCHY_DEPTH_EXCEEDED`; tanggal invalid → `VALIDATION_ERROR`.

## UC-08 — Scraping website kompetitor dengan profil Chrome

- **Prasyarat:** Profil `research-01` terdaftar (`browser_profile_list`), chromedriver versi cocok dengan Chrome.
1. AG: `web_scrape(urls=[5 halaman produk kompetitor], profile="research-01", mode="markdown", max_chars_per_page=6000)` → job.
2. Worker mengunci profil, membuka Chrome dengan `--user-data-dir`/`--profile-directory`, cek robots.txt, SSRF guard, rate limit per domain, ekstraksi konten utama → markdown.
3. Hasil → handle; output: judul, status, panjang konten, 1 cuplikan.
4. AG: `web_extract(handle, schema={"produk":"nama", "harga":"harga IDR", "promo":"promo aktif"})` → tabel 5 baris (LLM `fast`).
- **Alternatif:** profil sedang dipakai → job menunggu ≤ 60 s lalu `PROFILE_BUSY`; halaman diblokir robots → item `status: "skipped_robots"`; CAPTCHA terdeteksi → item `status: "blocked"` + rekomendasi cek manual.

## UC-09 — Crawling X berdasarkan kata kunci & periode

1. AG: `x_search(keywords=["kopi susu","kopi gula aren"], since="2026-10-01", until="2026-10-30", lang="id", max_items=1500)`.
2. MCP memilih provider (`auto`): periode > 7 hari → `browser` dengan profil X; mengembalikan `job_id` + estimasi.
3. Worker mengumpulkan post per hari (pencarian lanjutan `since:/until:`), deduplikasi `external_id`, simpan `social_posts`.
4. Output akhir: handle + `per_day`, `top_hashtags`, `top_authors`, engagement, 5 post teratas.
- **Alternatif:** provider `x_api` dengan estimasi biaya > kuota harian → `BUDGET_EXCEEDED` sebelum job dibuat (tanpa biaya); login profil kedaluwarsa → `LOGIN_REQUIRED` + instruksi `browser_profile_check`.

## UC-10 — Crawling Threads berdasarkan kata kunci & periode

1. AG: `threads_search(keywords=["kopi lokal"], since="2026-10-01", until="2026-10-30", search_type="RECENT")`.
2. Provider `threads_api` memanggil `GET /keyword_search` dengan `since/until` (unix), paginasi `after` hingga `max_items`.
3. Hasil diseragamkan ke `social_posts` (`source=threads`) → output seperti UC-09.
- **Alternatif:** API mengembalikan array kosong untuk kata kunci sensitif → `total:0` + `notes:["keyword_filtered_by_platform"]`; 5xx berulang → fallback `browser` bila diizinkan.

## UC-11 — Membandingkan tren Google

1. AG: `trends_interest(keywords=["kopi susu","kopi gula aren","es kopi"], start="2025-11-01", end="2026-10-30", geo="ID")`.
2. MCP cek cache (24 jam) → ambil via adapter → simpan `trend_points` → hitung rata-rata, puncak, slope, perubahan vs periode sebelumnya.
3. Output: 3 baris statistik + tabel mingguan diringkas (≤ 26 baris) + handle seri harian.
4. AG: `trends_by_region("kopi gula aren", ...)` → 10 provinsi teratas; `trends_related(..., kind="queries")` → rising queries.
- **Error:** 429 dari Google → retry backoff 3× → fallback browser → gagal: `UPSTREAM_ERROR retryable:true`.

## UC-12 — Query & CRUD database

1. CL: `db_entities()` → daftar entitas & akses.
2. CL: `db_query("tasks", filter={"end_date":{"op":"lt","value":"2026-10-30"}, "status":{"op":"ne","value":"done"}}, fields=["id","title","end_date"])` → task overdue.
3. CL: `db_create("competitors", {"name":"Kopi Kenangan","website":"https://…","keywords":["kopi kenangan"]})` (entitas schema `mcp`).
4. CL: `db_update("competitors", id, {"notes":"…"}, expected_updated_at)`.
- **Error:** `db_create("tasks", …)` → `FORBIDDEN` dengan saran "gunakan task_create"; field tidak dikenal → `VALIDATION_ERROR`.

## UC-13 — Knowledge base (vector DB)

1. AG: `kb_sync_dashboard(project_id)` → blok Project Brief & task di-chunk dan di-embed (inkremental).
2. AG: `kb_ingest(collection_id, {"type":"handle","value":"h_web…"})` → hasil scraping kompetitor masuk KB.
3. AG: `kb_search("positioning harga kompetitor", project_id, top_k=6)` → 6 potongan ≤ 600 karakter.
- **Alternatif:** `OPENAI_API_KEY` di Gateway kosong → `EMBED_PROVIDER=local` (model ONNX), koleksi dibuat dengan dimensi sesuai; mencampur dimensi dalam satu koleksi ditolak.

## UC-14 — Analisis sentimen hasil crawl

1. AG: `sentiment_analyze(source={"handle":"h_x…"}, aspects=["harga","rasa","pelayanan"], group_by="day")` → job (1.500 teks).
2. Worker: normalisasi teks (slang, emoji), deteksi bahasa, model lokal → label + skor; item skor < 0.60 (± 15%) → LLM `fast` batch 50.
3. Aspek: keyword-spotting + LLM hanya untuk kalimat yang memuat aspek.
4. Output: distribusi, net sentiment, tren per hari, aspek, 3 contoh positif/negatif, biaya token.

## UC-15 — Analisis pasar end-to-end

1. MM meng-assign task "Riset pasar kopi siap minum Q4" ke `Agent_Researcher`.
2. AG memanggil `market_analyze(subject="kopi siap minum", competitors=[3 brand], period, depth="standard")` → job orkestrasi: trends (5 kata kunci) + X + Threads + sentimen per brand + scraping situs brand.
3. Pipeline menghitung share of voice, sentimen per brand, momentum tren; menyusun input padat (≤ 6.000 token) → `smart` → laporan JSON tervalidasi (SWOT, matriks kompetitor, peluang).
4. AG: `report_publish(report_id, target{type:"task", task_id}, run_id)` → blok laporan di task; `agent.task.completed`.

## UC-16 — Merencanakan / mengevaluasi campaign

- **Plan:** CL: `campaign_analyze(mode="plan", campaign{objective:"awareness", audience:"18–30 urban", channels:["instagram","tiktok","x"], budget_idr:50000000, period})` → funnel AARRR, KPI SMART, alokasi budget, risiko.
- **Evaluate:** setelah campaign, `campaign_analyze(mode="evaluate", results{per channel metrics}, handles=[crawl selama campaign])` → capaian vs KPI, insight sentimen, rekomendasi.
- **Error:** `results` tidak konsisten (klik > impresi) → `VALIDATION_ERROR` dengan field.

## UC-17 — Strategi digital marketing & menerapkannya

1. AG `strategist`: `dm_strategy(business{…}, goals, budget_idr, period="2026-11-01..2027-01-31", framework="SOSTAC", inputs{report_ids:[laporan UC-15]}, apply_to{project_id, template:true, tasks:true})`.
2. Pipeline: memakai ringkasan laporan (bukan isi penuh) → `smart` → strategi (situasi, objektif, strategi, taktik, aksi, kontrol), pilar konten, kalender outline.
3. `apply_to`: render template brief + task per bulan/minggu ke Dashboard (Service API atau webhook bila dalam run).

## UC-18 — Administrasi

1. AD: `manage.py create-client --name "rina-claude-desktop" --toolsets tpl,task,social,trends,analysis,data,job --daily-token-budget 300000 --dashboard-agent MCP_Assistant` → key ditampilkan sekali.
2. AD: `manage.py profile-add --name research-01 --purpose "X & Threads"`, lalu `manage.py profile-open research-01` (Chrome terbuka non-headless untuk login manual).
3. AD: `GET /admin/usage?group_by=tool&since=2026-10-01` → token, biaya, cache hit, penghematan estimasi per tool.
4. AD: `manage.py revoke-client <prefix>` → key tidak berlaku ≤ 60 s (cache).

---

## Matriks Traceability

| UC | FR | Task |
|---|---|---|
| UC-01 | FR-09, FR-10, FR-11 | 08, 09, 10 |
| UC-02 | FR-10 | 09 |
| UC-03 | FR-10, FR-11 | 09, 10 |
| UC-04 | FR-10 | 09, 10 |
| UC-05 | FR-09, FR-10 | 08, 09 |
| UC-06 | FR-12, FR-13 | 11, 12 |
| UC-07 | FR-12, FR-14 | 11, 13 |
| UC-08 | FR-08, FR-15, FR-16 | 07, 14 |
| UC-09 | FR-17 | 15 |
| UC-10 | FR-18 | 16 |
| UC-11 | FR-19 | 17 |
| UC-12 | FR-20 | 18 |
| UC-13 | FR-21 | 19 |
| UC-14 | FR-22 | 20 |
| UC-15 | FR-23 | 21 |
| UC-16 | FR-24 | 22 |
| UC-17 | FR-25 | 23 |
| UC-18 | FR-02, FR-15, FR-26 | 04, 14, 24 |
| Semua | FR-03–FR-07, NFR-04/05 | 05, 06, 26 |
