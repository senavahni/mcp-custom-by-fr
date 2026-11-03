# TASK-03 — Database, Schema `mcp`, pgvector & Migrasi

| Item | Nilai |
|---|---|
| Milestone | M0 Fondasi |
| Prioritas | P0 |
| Estimasi | 3 hari |
| Dependensi | TASK-02 |
| Referensi | DATABASE.md (seluruhnya) · README D3, D4, D5 |

## Tujuan
Menyediakan penyimpanan MCP di schema `mcp` pada database Dashboard, dengan hak akses minimum ke schema `public`, ekstensi pgvector, model ORM, dan migrasi Alembic yang bisa dijalankan dari nol tanpa mengganggu Prisma.

## Spesifikasi

### Bootstrap (sekali, superuser)
- `scripts/sql/bootstrap.sql` sesuai DATABASE §3 (pakai variabel psql `:'owner_pw'`, `:'app_pw'`).
- `scripts/db-bootstrap.bat` meminta password via prompt (tidak disimpan), menjalankan `psql -v owner_pw=… -v app_pw=… -f bootstrap.sql`.
- Idempoten: aman dijalankan ulang (`IF NOT EXISTS`, `DO $$ … $$` untuk role).

### Alembic
- `alembic.ini` + `migrations/env.py` async, memakai `MIGRATION_DATABASE_URL` (role `mcp_owner`).
- `version_table_schema="mcp"`, `include_schemas=True`, `include_object` **hanya** objek di schema `mcp` (tabel `public` tidak pernah di-autogenerate/di-drop).
- Revisi `0001_init`: seluruh DDL DATABASE §4.1–4.8 (tabel, constraint, indeks, kolom generated `tsvector`, indeks HNSW parsial ber-cast). Bagian yang tidak didukung autogenerate ditulis `op.execute()`.
- Revisi `0002_seed_reference`: tidak ada (seed lewat `manage.py seed`, bukan migrasi).

### ORM (`mcp_marketing/models/`)
- `base.py`: `DeclarativeBase` dengan `metadata = MetaData(schema="mcp", naming_convention=…)`.
- Satu modul per domain: `clients.py`, `usage.py`, `results.py`, `jobs.py`, `runs.py`, `templates.py`, `browser.py`, `social.py`, `trends.py`, `nlp.py`, `reports.py`, `master.py`, `kb.py`.
- Kolom `vector` memakai `pgvector.sqlalchemy.Vector()` tanpa dimensi; validasi dimensi di service.
- `dashboard.py`: model **read-only** untuk tabel `public` (hanya kolom yang di-grant) dengan `__table_args__ = {"schema": "public", "info": {"readonly": True}}`; session flush yang menyentuh model ini → `RuntimeError` (event listener `before_flush`).

### `core/database.py`
- `create_async_engine(DATABASE_URL, pool_size, pool_pre_ping=True)`; `async_session()` context manager; `server_settings={"application_name": "mcp-marketing", "search_path": "mcp,public"}`.
- Health check `SELECT 1` + cek ekstensi `vector` ada.

### `scripts/manage.py` (bagian DB)
- `seed`: agent profiles (`strategist`, `researcher`, `analyst`), templates seed (placeholder sampai TASK-12), klien sistem `dashboard` (kind `system`, toolset `pm`, tanpa key — auth via `MCP_SERVER_API_KEY`), schedule default (retensi harian 02:15, cache cleanup tiap jam).
- `db-check`: menampilkan versi PostgreSQL, pgvector, revisi Alembic, hasil `test grants` ringkas.

## Checklist Implementasi
- [ ] `bootstrap.sql` + `db-bootstrap.bat`.
- [ ] Alembic env async + filter schema.
- [ ] Revisi `0001_init` lengkap + downgrade (drop tabel schema `mcp` saja).
- [ ] Model ORM semua tabel + model read-only Dashboard.
- [ ] `core/database.py` + health check.
- [ ] `manage.py seed` & `db-check`.
- [ ] Dokumentasi singkat `docs/operations.md#database` (bootstrap, grant tabel baru).

## Pengujian (`-m integration`, database `marketing_dashboard_test`)
- `test_migrations.py`: `upgrade head` dari kosong → `downgrade base` → `upgrade head` lagi tanpa error; tabel `public` (dibuat dari `migration.sql` Dashboard di fixture) tidak berubah (checksum `information_schema.columns`).
- `test_grants.py`: sebagai `mcp_app`: `SELECT email FROM public.users` → `permission denied`; `SELECT display_name FROM public.users` → OK; `INSERT INTO public.tasks` → denied; DML di `mcp.*` → OK.
- `test_vector.py`: insert chunk 1536 & 384, query ber-cast memakai indeks (`EXPLAIN` memuat `kb_chunks_hnsw_1536`).
- `test_readonly_models.py`: flush model `dashboard.Task` → error.
- `test_seed.py`: seed idempoten (dua kali → jumlah baris sama).

## Acceptance Criteria
1. Dari database Dashboard yang sudah termigrasi Prisma, `db-bootstrap` + `migrate` + `seed` selesai tanpa error dan **tanpa** mengubah objek `public`.
2. `mcp_app` tidak dapat membaca `users.email`/`password_hash`/`refresh_tokens` dan tidak dapat menulis ke `public`.
3. Pencarian vector 10.000 chunk 1536-dim top-10 < 50 ms (lokal, HNSW).
4. `prisma migrate deploy` Dashboard tetap berjalan normal setelah schema `mcp` ada (diuji sekali).

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; test integration lulus.
- [ ] Migrasi bisa `upgrade` dari nol dan `downgrade -1`.
- [ ] DATABASE.md diperbarui bila ada perbedaan implementasi.
- [ ] Review DBA/backend disetujui.
