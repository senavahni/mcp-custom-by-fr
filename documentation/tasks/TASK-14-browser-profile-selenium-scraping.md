# TASK-14 — Manajemen Profil Browser & Tools Web Scraping (Selenium + chromedriver)

| Item | Nilai |
|---|---|
| Milestone | M3 Data Collection |
| Prioritas | P0 |
| Estimasi | 5 hari |
| Dependensi | TASK-05, TASK-06, TASK-07 |
| Referensi | MCP_SPEC §4.5 · DATABASE §4.5 · USE_CASES UC-08, UC-18 · PRD FR-15, FR-16, NFR-09 |

## Tujuan
Scraping website yang andal dan dapat diaudit memakai Selenium dengan **chromedriver eksplisit** dan **profil Chrome yang jelas** (user-data-dir terdaftar, tujuan, situs login), yang juga menjadi fondasi crawler X/Threads/Trends mode browser. Konten dikembalikan dalam bentuk ringkas (markdown utama) agar hemat token.

## Spesifikasi

### Profil browser (`services/browser/profiles.py`)
- Disimpan di `mcp.browser_profiles`; direktori `BROWSER_PROFILES_DIR/<name>` (Chrome `--user-data-dir`) + `--profile-directory=<profile_directory>`.
- CLI:
  - `manage.py profile-add --name research-01 --purpose "X & Threads" --sites x.com,threads.net --headless false [--user-agent …] [--proxy-env PROXY_RESEARCH]`
  - `manage.py profile-open research-01` → membuka Chrome non-headless dengan profil tsb agar admin login manual (2FA, dll.), menunggu Enter, lalu menutup rapi.
  - `manage.py profile-check research-01` → cek status login per situs (selector/URL tertentu) → `login_status`.
  - `manage.py profile-list`, `profile-disable`.
- **Lock eksklusif**: satu profil hanya dipakai satu job (Chrome mengunci user-data-dir). `UPDATE … SET locked_by_job WHERE locked_by_job IS NULL` + timeout; job lain menunggu ≤ 60 s → `PROFILE_BUSY retryable:true`.
- Profil `default` (tanpa login, headless) dibuat oleh seed untuk scraping umum.

### Driver factory (`services/browser/driver_pool.py`)
```python
opts = ChromeOptions()
opts.binary_location = CHROME_BINARY
opts.add_argument(f"--user-data-dir={profile.user_data_dir}")
opts.add_argument(f"--profile-directory={profile.profile_directory}")
opts.add_argument(f"--window-size={profile.window_size}")
opts.add_argument("--lang=id-ID"); opts.add_argument("--disable-dev-shm-usage")
if headless: opts.add_argument("--headless=new")
if profile.user_agent: opts.add_argument(f"--user-agent={profile.user_agent}")
if proxy: opts.add_argument(f"--proxy-server={os.environ[profile.proxy_url_env]}")
service = ChromeService(executable_path=CHROMEDRIVER_PATH, log_output=LOG_DIR/"chromedriver.log")
driver = webdriver.Chrome(service=service, options=opts)
driver.set_page_load_timeout(BROWSER_PAGE_TIMEOUT_S)
```
- Selenium Manager **tidak** dipakai; saat start worker: cek versi `chromedriver --version` vs `chrome --version` (major sama) → bila beda, log `critical` dan tool browser mengembalikan `UPSTREAM_ERROR` dengan petunjuk update (`scripts\update-chromedriver.bat` mengunduh versi Chrome for Testing yang cocok).
- Satu driver per job, ditutup di `finally` (dan `kill` proses anak bila hang); maksimal `WORKER_CONCURRENCY` driver.

### Keamanan & kepatuhan (`services/browser/ssrf.py`, `robots.py`)
- Hanya `http/https`; resolve DNS → tolak IP private/loopback/link-local/metadata (`169.254.169.254`) kecuali `SCRAPE_ALLOW_PRIVATE_NETWORK=true`; cek ulang `final_url` setelah redirect.
- robots.txt (`protego`) dihormati bila `respect_robots=true` (default; mengubahnya butuh klien admin).
- Rate limit per domain (`SCRAPE_PER_DOMAIN_RPM`, default 20) + jeda acak 1–3 s antar halaman.
- Deteksi blokir/CAPTCHA (judul/elemen umum: "captcha", `iframe[src*=recaptcha]`, HTTP 403/429) → status `blocked`, tidak dicoba melewati.

### Ekstraksi (`services/browser/extract.py`)
- `mode=text|markdown`: `trafilatura` (konten utama, buang nav/footer/iklan) → markdown (`markdownify` untuk fallback) → normalisasi whitespace → potong `max_chars_per_page`.
- `mode=selectors`: dict `{name: css}` → teks/atribut (`css::attr(href)`) per selector (list maks 50).
- `mode=links`: daftar link unik (teks ≤ 80 char) ter-filter domain.
- `published_at` dari meta (`article:published_time`, JSON-LD `datePublished`).
- Simpan `web_pages` (content_md ≤ 200 KB, hash konten).

### Tools
- `browser_profile_list`, `browser_profile_check(profile, sites)` (job singkat, admin atau pemilik profil).
- `web_scrape(urls ≤ 50, …)`: ≤ 3 URL + `wait_s` → inline (konten dipotong agar total ≤ 1.500 token, sisanya via handle); selebihnya job. Cache 6 jam per URL+mode.
- `web_crawl(start_url, max_pages ≤ 200, max_depth ≤ 3, same_domain, include/exclude regex, published_since/until, mode)`: BFS, normalisasi URL (hapus fragment, utm_*), dedupe hash konten, checkpoint per halaman.
- `web_extract(source, schema{field: description} ≤ 15 field, instructions?, max_pages ≤ 20)`: per halaman kirim ke LLM `fast` **hanya** markdown terpotong 6.000 char + schema → JSON; hasil tabel; total token dilaporkan.

## Checklist Implementasi
- [ ] Tabel & CLI profil (add/open/check/list/disable) + lock.
- [ ] Driver factory + cek versi + `update-chromedriver.bat`.
- [ ] SSRF guard, robots, rate limit per domain, deteksi blokir.
- [ ] Ekstraksi text/markdown/selectors/links + published_at.
- [ ] Handler job `web_scrape`, `web_crawl` (checkpoint, cancel).
- [ ] Tools 5 buah + cache.
- [ ] `docs/browser-profiles.md`: membuat profil, login manual, keamanan akun, kebijakan ToS.

## Pengujian
- Unit (tanpa browser): SSRF (10.0.0.1, 127.0.0.1, `http://[::1]`, redirect ke private) → ditolak; robots disallow → `skipped_robots`; ekstraksi dari 10 fixture HTML (berita, e-commerce, blog) → markdown bersih (golden).
- `-m browser`: server HTTP lokal fixture + Chrome nyata → scrape 5 halaman, selectors, scroll; profil terkunci → job kedua menunggu lalu `PROFILE_BUSY`.
- Driver hang (halaman tak pernah load) → timeout, proses Chrome mati (tidak ada zombie, cek `psutil`).

## Acceptance Criteria
1. UC-08 berhasil: 5 halaman produk → markdown + `web_extract` tabel 5 baris, total output ke LLM pemanggil ≤ 1.500 token.
2. Setiap job browser tercatat dengan nama profil yang dipakai; dua job tidak pernah memakai profil yang sama bersamaan.
3. Versi chromedriver tidak cocok terdeteksi saat start dengan pesan yang dapat ditindaklanjuti.
4. Markdown hasil ekstraksi rata-rata ≥ 70% lebih kecil (token) dibanding HTML mentah pada 10 fixture.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; coverage `services/browser/` ≥ 80% (unit).
- [ ] Persetujuan internal kebijakan scraping (robots, rate limit, akun khusus) terdokumentasi.
- [ ] Review disetujui.
