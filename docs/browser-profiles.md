# Profil Browser & Scraping (TASK-14)

Selenium dipakai **hanya oleh worker** (satu-satunya proses yang membuka Chrome). Tool MCP
(`web_scrape`, `web_crawl`, `web_extract`, `browser_profile_check`, `x_search` mode browser, Trends mode
browser) hanya mengantrekan job lalu menunggu ≤ `wait_s`.

## 1. Persiapan sekali

1. Chrome terpasang di `CHROME_BINARY` (default `C:\Program Files\Google\Chrome\Application\chrome.exe`).
2. Unduh chromedriver yang cocok:
   ```bat
   scripts\update-chromedriver.bat
   ```
   Skrip membaca versi Chrome, mencari build yang sama di *Chrome for Testing*, dan mengekstrak
   `chromedriver.exe` ke `CHROMEDRIVER_PATH` (default `.\bin\`). Selenium Manager **tidak** dipakai.
3. Saat start, worker mencocokkan major version Chrome vs chromedriver. Bila tidak cocok, log
   `browser.not_ready` (level critical) muncul dan job browser gagal dengan pesan berisi langkah perbaikan.
   **Setiap kali Chrome ter-update otomatis, jalankan ulang `update-chromedriver.bat`.**

## 2. Siklus hidup profil

Profil = folder `--user-data-dir` terpisah di `BROWSER_PROFILES_DIR\<nama>` + baris `mcp.browser_profiles`.
Profil `default` (headless, tanpa login) dibuat oleh `manage.py seed`.

```bat
call scripts\env.bat
"%PY%" scripts\manage.py profile-add riset-x --sites x.com,threads.net --purpose "crawler sosial"
"%PY%" scripts\manage.py profile-open riset-x --site x.com --site threads.net
rem   -> jendela Chrome terbuka; login MANUAL (termasuk 2FA), lalu tekan Enter di terminal
"%PY%" scripts\manage.py profile-check riset-x      & rem butuh worker berjalan
"%PY%" scripts\manage.py profile-list
"%PY%" scripts\manage.py profile-disable riset-x    & rem folder profil tidak dihapus
```

- `profile-check` (atau tool `browser_profile_check`, admin) membuka halaman tiap situs dan menyimpan
  `login_status` per situs. Crawler X/Threads hanya memakai profil yang **mengizinkan situs tsb dan
  login OK** (`ProfileStore.for_site`).
- Password **tidak pernah** disimpan MCP; sesi login hidup di folder profil Chrome. Perlakukan folder
  `browser_profiles\` seperti secret (sudah di `.gitignore`).
- `--proxy-env NAMA_ENV` menyimpan *nama* variabel environment, bukan URL proxy.

### Login dari Dashboard (Sumber & Koneksi)

Admin/Owner Dashboard bisa membuat profil dan login tanpa CLI (tool `intel_browser_profile_create`,
`intel_browser_login_start/get/cancel`):

1. **Buat profil & login akun Google** → profil `google-utama` (headless saat scraping) dibuat, lalu
   worker membuka **Chrome biasa tanpa chromedriver** di mesin MCP (`--user-data-dir` profil). Google
   sering menolak login di browser yang dikendalikan otomasi, jadi jendela ini sengaja bukan Selenium.
2. Login manual (termasuk 2FA), lalu **tutup jendela Chrome**. Job `browser_profile_login` (≤ 15 menit,
   profil terkunci selama jendela terbuka) lalu menjalankan cek login headless dan menyimpan `login_status`.
3. Scraper Web (`intel` sumber `web`) otomatis memakai profil pertama yang login Google
   (`sources.web_profile`), selain itu profil `default`. Tombol X/Threads memakai alur yang sama dan
   membuat sumber X/Threads mode browser tersedia.

Worker harus berjalan di sesi desktop pengguna (mis. `scripts\start-worker.bat` dari jendela cmd) agar
jendela Chrome terlihat.

## 3. Lock profil

Chrome mengunci `--user-data-dir`, jadi satu profil = satu job pada satu waktu. Lock diambil atomik
(`UPDATE … WHERE locked_by_job IS NULL`); job lain menunggu ≤ 60 s lalu gagal `PROFILE_BUSY`. Lock yang
lebih tua dari 3 jam dianggap basi (worker mati) dan boleh diambil alih. `profile-open` memakai lock
`cli-profile`, jadi jangan login manual saat job sedang memakai profil yang sama.

## 4. Kepatuhan & keamanan scraping

| Aturan | Implementasi |
|---|---|
| SSRF | Hanya `http/https`; IP private/loopback/link-local/metadata ditolak (`ssrf_denied`), dicek ulang setelah redirect. `SCRAPE_ALLOW_PRIVATE_NETWORK=true` hanya untuk dev |
| robots.txt | Dihormati (`SCRAPE_RESPECT_ROBOTS`, cache 1 jam). `respect_robots=false` hanya untuk klien admin |
| Rate limit | `SCRAPE_PER_DOMAIN_RPM` (default 20) per domain + jeda acak 1–3 s |
| CAPTCHA/blokir | Tidak dicoba dilewati; halaman ditandai `blocked` (penanda CAPTCHA hanya dihitung bila teks halaman < 1.000 karakter — reCAPTCHA di form kontak tidak memblokir konten) |
| Prompt injection | Konten halaman dibungkus `<untrusted_content>` saat dikirim ke LLM (`web_extract`) |
| Penyimpanan | `mcp.web_pages` (markdown ≤ 200 KB/halaman); retensi mengikuti `RAW_RETENTION_DAYS` |

Status per halaman: `ok`, `skipped_robots`, `blocked`, `http_error`, `ssrf_denied`, `timeout`, `invalid`.

## 5. Output hemat token

`web_scrape` ≤ 3 URL mengembalikan konten inline (total ±1.200 token); lebih dari itu hanya tabel
ringkas + `handle` (baca lewat `data_get`). Ekstraksi markdown (trafilatura, fallback markdownify)
memangkas ≥ 70 % token dibanding HTML mentah (diuji di `tests/unit/test_browser_extract.py`).

## 6. Troubleshooting

| Gejala | Penyebab / tindakan |
|---|---|
| `Browser belum siap: chromedriver tidak ditemukan` | Jalankan `scripts\update-chromedriver.bat` |
| `Versi tidak cocok (Chrome X vs chromedriver Y)` | Chrome ter-update → jalankan ulang skrip di atas |
| `PROFILE_BUSY` | Profil dipakai job lain / `profile-open` masih terbuka; tunggu atau pakai profil lain |
| `LOGIN_REQUIRED` | Sesi kedaluwarsa → `profile-open <nama> --site …` lalu `profile-check` |
| Chrome yatim setelah worker crash | Driver ditutup di `finally`; bila tetap ada, `taskkill /IM chrome.exe /F` (hati-hati Chrome pribadi) |
