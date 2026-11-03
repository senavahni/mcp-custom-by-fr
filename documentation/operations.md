# Operasional — MCP Marketing

## Database

### Bootstrap (sekali, superuser)
```bat
scripts\db-bootstrap.bat marketing_dashboard postgres
```
Menjalankan `scripts/sql/bootstrap.sql`: ekstensi `pgcrypto` (+ `vector` bila tersedia), role
`mcp_owner` (pemilik schema `mcp`, hanya untuk migrasi) dan `mcp_app` (runtime), schema `mcp`,
default privileges, grant baca **berkolom** ke tabel Dashboard. Aman dijalankan ulang (password role
diperbarui). Nama role bisa diganti: `psql -v owner_role=... -v app_role=...`.

### Migrasi
```bat
scripts\migrate.bat              & rem = manage.py migrate (head; berhenti di 0001 bila pgvector belum ada)
scripts\conda-run.bat alembic downgrade -1
```
Alembic hanya menyentuh schema `mcp` (`include_object`); tabel `public` milik Prisma tidak pernah
di-autogenerate/di-drop. Setelah pgvector dipasang: jalankan ulang `db-bootstrap.bat` lalu `migrate.bat`.

### Tabel Dashboard baru
Bila Dashboard menambah tabel `public` yang perlu dibaca MCP, tambahkan grant manual:
```sql
GRANT SELECT ON public.<tabel_baru> TO mcp_app;          -- atau GRANT SELECT (kolom, …)
```
`manage.py db-check` menampilkan versi PostgreSQL/pgvector, revisi Alembic, dan cek grant
(`users.email` harus **False**).

## Server

| Hal | Perintah / lokasi |
|---|---|
| Start | `scripts\start-server.bat` (uvicorn `:4100`, graceful 25 s) |
| Health | `GET /health/live`, `GET /health/ready` (DB + flag `vector`) |
| Metrics | `GET /metrics` (tool latency, webhook, outbox pending, runs, token LLM) |
| Log | stdout + `logs\mcp-YYYY-MM-DD.log` (JSON, 14 file, secret disamarkan) |
| Klien | `manage.py create-client / list-clients / revoke-client / rotate-client` atau `/admin/clients` (key admin) |

Pencabutan key efektif ≤ 60 detik (cache auth). Proses server juga menjalankan **outbox sender**
(webhook ke Dashboard) dan **run supervisor** (timeout run > `AGENT_RUN_TIMEOUT_MINUTES`, melanjutkan run
aktif setelah restart).

## Worker

| Hal | Keterangan |
|---|---|
| Start/stop | `scripts\start-worker.bat`; Ctrl+C = graceful (job berjalan dikembalikan ke antrean, checkpoint tetap) |
| Paralel | `WORKER_CONCURRENCY` slot; aman menjalankan > 1 worker (`SKIP LOCKED`) |
| Job macet | heartbeat 10 s; reaper mengembalikan job `running` tanpa heartbeat > 60 s ke `queued` (atau `failed WORKER_LOST` bila percobaan habis) |
| Jadwal bawaan | `cache_cleanup` tiap jam, `retention` 02:15, `usage_rollup` tiap 10 menit (zona `TIMEZONE`) |
| Job gagal | `SELECT id, kind, error FROM mcp.jobs WHERE status = 'failed' ORDER BY finished_at DESC LIMIT 20;` |
| Retry manual | `scripts\conda-run.bat python scripts\manage.py job-retry j_xxxxxxxxxx` |

## Outbox webhook

```sql
-- event yang tertahan / gagal per run
SELECT run_id, seq, event_type, status, attempts, last_http_status, last_error
FROM mcp.webhook_outbox WHERE status IN ('pending', 'failed') ORDER BY run_id, seq;
```
Event dikirim berurutan per run. `401` = secret/jam salah (cek `MCP_WEBHOOK_SECRET`), `409 RUN_NOT_ACTIVE`
= seluruh event pending run tersebut dibuang dan agent berhenti, `429/5xx` = retry backoff hingga
`WEBHOOK_MAX_RETRY_MINUTES`.
