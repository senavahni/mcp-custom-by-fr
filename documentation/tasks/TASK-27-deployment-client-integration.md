# TASK-27 — Deployment, Operasional & Integrasi Klien

| Item | Nilai |
|---|---|
| Milestone | M6 Production |
| Prioritas | P0 |
| Estimasi | 3 hari |
| Dependensi | TASK-24, TASK-25, TASK-26 |
| Referensi | ARCHITECTURE §4, §7, §9 · README D11 · `llm-genai/docs/deployment.md` |

## Tujuan
MCP Marketing berjalan stabil berdampingan dengan LLM Gateway dan Dashboard di mesin Windows target, mudah dinyalakan/dimatikan, otomatis start, ter-backup, dan dapat dipakai oleh Dashboard serta klien MCP tim (Claude Desktop, Cursor, Cline).

## Spesifikasi

### Mode deployment
| Mode | Isi | Kapan |
|---|---|---|
| **A — Windows native (utama)** | conda env + `scripts\start-all.bat` (server + worker), Chrome & chromedriver lokal | Produksi internal di mesin yang sama dengan Gateway |
| **B — Docker (opsional)** | `Dockerfile` (python:3.11-slim) untuk **server saja** (tanpa browser), `docker-compose.mcp.yml` bergabung ke jaringan Dashboard; worker browser tetap native | Bila Dashboard berjalan di Docker dan ingin MCP server di jaringan yang sama |

### Skrip (`scripts/`)
- `start-all.bat` → cek Gateway (`wait_port 8000`) & PostgreSQL, `migrate`, start server (`:4100`) & worker di jendela/proses terpisah, tulis PID ke `run/*.pid`; opsi `--no-worker`, `--no-browser`.
- `stop-all.bat` → graceful (server menyelesaikan outbox in-flight ≤ 10 s; worker menyimpan checkpoint & melepas lock profil).
- `healthcheck.py` → exit code dari `/health/ready`.
- Autostart: Task Scheduler Windows (tanpa admin, "At log on") — langkah di `docs/deployment.md`, pola sama dengan Gateway.
- `update.bat` → `git pull` + `pip install -r … ` + `migrate` + restart.

### Urutan start ekosistem
1. PostgreSQL → 2. LLM Gateway (`start-all.bat --no-runtime` pada profil `remote-only`) → 3. MCP Marketing → 4. Dashboard (backend memanggil `MCP_SERVER_URL`).
- Dashboard di Docker: `MCP_SERVER_URL=http://host.docker.internal:4100/mcp`, `PUBLIC_API_URL` yang dapat dijangkau MCP untuk webhook (mis. `http://localhost:8088`), MCP mengikat `APP_HOST=0.0.0.0` **hanya** bila firewall membatasi ke localhost/jaringan docker; jika tidak, tetap `127.0.0.1` dan Dashboard dev native.

### Konfigurasi Dashboard (`.env` Dashboard)
```dotenv
MCP_SERVER_URL=http://localhost:4100/mcp
MCP_SERVER_API_KEY=<sama dengan MCP>
MCP_WEBHOOK_SECRET=<sama dengan MCP>
MCP_SERVICE_TOKEN=<sama dengan MCP>
```
+ daftar agent: `strategist`, `researcher` (seed), `analyst`, `assistant` (TASK-11).

### Integrasi klien MCP (`docs/client-integration.md`)
- **Claude Desktop** (`claude_desktop_config.json`, transport HTTP lewat jembatan resmi bila klien hanya mendukung stdio):
```json
{ "mcpServers": { "mcp-marketing": {
    "command": "npx", "args": ["-y", "mcp-remote", "http://127.0.0.1:4100/mcp",
                               "--header", "Authorization: Bearer ${MCP_MARKETING_KEY}"],
    "env": { "MCP_MARKETING_KEY": "mcpk_..." } } } }
```
- **Cursor / Cline / VS Code**: konfigurasi server HTTP `url: http://127.0.0.1:4100/mcp` + header Authorization.
- **MCP Inspector** untuk debugging.
- Rekomendasi toolset per peran (staf konten: `tpl,task,trends,analysis,data,job`; analis: `social,trends,web,analysis,kb,db,data,job`).
- Tips hemat token untuk pengguna: pakai `toolbox_list` & `?toolsets=`, minta ringkasan, gunakan `report_get(section=…)`.

### Operasional (`docs/operations.md`)
- Backup: ikut backup DB Dashboard (pastikan schema `mcp` termasuk) + `browser_profiles/` **tidak** di-backup ke lokasi bersama (atau terenkripsi).
- Rotasi key klien, secret Dashboard, token X/Threads.
- Troubleshooting: chromedriver tidak cocok, profil terkunci, 429 Trends, outbox macet, budget habis, Gateway `all_candidates_failed`.
- Runbook insiden: matikan crawling (`CRAWL_ENABLED=false`), cabut key klien, hentikan semua run (`manage.py runs-cancel-all`).

## Checklist Implementasi
- [ ] Skrip start/stop/update/healthcheck + PID + graceful shutdown.
- [ ] Autostart Task Scheduler terdokumentasi & diuji.
- [ ] Dockerfile + compose opsional (server saja).
- [ ] `docs/deployment.md`, `docs/client-integration.md`, `docs/operations.md`.
- [ ] Konfigurasi Dashboard & agent terdaftar; seed `MCP_Assistant`.
- [ ] Go-live checklist (di bawah) dijalankan.

### Go-live checklist
- [ ] Semua secret produksi dibuat baru (bukan dari dev) dan sama di MCP & Dashboard.
- [ ] Gateway: key project `mcp-marketing` + kuota harian disetel.
- [ ] Profil browser produksi dibuat & login dicek (`profile-check`).
- [ ] Schedule retensi & rollup aktif.
- [ ] `/health/ready` = ok; metrics terbaca.
- [ ] Kontrak & UAT report terbaru (TASK-26) lulus.

## Pengujian
- Mesin bersih (VM Windows) mengikuti `docs/deployment.md` → sistem jalan < 60 menit.
- Restart Windows → ketiga layanan hidup otomatis; run yang sedang berjalan dilanjutkan/gagal eksplisit.
- Claude Desktop & Cursor terhubung dan memanggil 3 tool contoh.

## Acceptance Criteria
1. Operator baru dapat memasang dari nol hanya dari dokumentasi (diuji orang di luar tim).
2. Restart mesin tidak kehilangan job/outbox (dilanjutkan).
3. Dashboard produksi internal memakai MCP Marketing untuk semua agent.

## Definition of Done
- [ ] Checklist & go-live checklist tercentang; Acceptance Criteria terpenuhi.
- [ ] Rilis `v1.0.0` ditag, `CHANGELOG.md` lengkap.
- [ ] Serah terima ke operator + sesi pelatihan singkat pengguna (cara menghubungkan Claude Desktop & memakai toolset).
