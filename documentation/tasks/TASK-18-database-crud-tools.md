# TASK-18 — Tools Akses Database Dashboard (CRUD `db_*`)

| Item | Nilai |
|---|---|
| Milestone | M4 Data Access |
| Prioritas | P0 |
| Estimasi | 4 hari |
| Dependensi | TASK-03, TASK-05 (TASK-11 untuk pengalihan tulis entitas inti) |
| Referensi | MCP_SPEC §4.8 · DATABASE §3, §5 · USE_CASES UC-12 · README D3, D4 |

## Tujuan
Memberi agent akses data Dashboard yang **aman, terstruktur, dan hemat token**: membaca entitas inti (project, task, blok, komentar, activity) dan melakukan CRUD penuh pada data marketing milik MCP (kompetitor, watchlist, koleksi KB, laporan), tanpa SQL bebas untuk klien biasa.

## Spesifikasi

### Registry entitas (`services/db/entities.py`)
```python
EntitySpec(name="tasks", table=dashboard.Task, access="r", workspace_path="project.workspace_id",
           fields={"id": uuid, "title": str, "status": enum, ...}, default_fields=("id","title","status","end_date"),
           soft_delete_col="deleted_at", searchable=("title",), redirect_write="task_create/task_update")
```
- Whitelist sesuai DATABASE §5; field di luar daftar tidak bisa dibaca/difilter (mis. `users.email` memang tidak ada di grant).
- `workspace_path` dipakai untuk membatasi baris ke `api_clients.workspace_id` (atau workspace run).

### Query builder aman (`services/db/query.py`)
- Filter terstruktur → SQLAlchemy Core dengan parameter binding; operator: `eq, ne, lt, lte, gt, gte, in (≤ 100), contains, icontains, starts_with, is_null, between`; tipe dicek terhadap `fields` (tanggal, enum, uuid, array `contains` untuk `tags`).
- `order_by` hanya field terdaftar; `limit ≤ 100`; cursor keyset (`order_by` + `id`).
- Hasil > 20 baris → result store (handle) + 5 sampel (T2); proyeksi `fields` wajib dari whitelist.
- Batas: `statement_timeout 5s` per query (SET LOCAL), maks 10.000 baris dibaca per panggilan.

### Tulis
- `db_create/db_update/db_delete` hanya untuk entitas `access="rw"`; validasi Pydantic per entitas (mis. `competitors.website` URL http/https, `keywords` ≤ 50).
- `db_update(expected_updated_at)` → optimistic locking (`WHERE updated_at = $x`), gagal → `CONFLICT` dengan `updated_at` terkini.
- `db_delete` default soft (`deleted_at = now()`), `hard=true` hanya admin.
- Entitas `r` dengan `redirect_write` → `FORBIDDEN` + pesan "gunakan `task_create`/`task_update`" (D4).
- Setiap tulis dicatat di `tool_calls` (args preview tanpa nilai panjang).

### `db_sql_readonly` (scope `db_admin`, hanya `is_admin`)
- Parser sederhana (`sqlglot`) memastikan satu statement `SELECT`/`WITH … SELECT`, tanpa `;` ganda, tanpa fungsi berbahaya (`pg_read_file`, `dblink`, `lo_*`, `pg_sleep`), hanya schema `mcp`/`public` yang di-grant.
- Dieksekusi dalam `BEGIN READ ONLY; SET LOCAL statement_timeout='5s';` dengan `LIMIT` dipaksa ≤ 1.000; hasil → envelope (handle bila > 20).

### Tools
`db_entities` (cache 1 jam, ≤ 600 token), `db_query`, `db_get`, `db_create`, `db_update`, `db_delete`, `db_sql_readonly`.

## Checklist Implementasi
- [ ] Registry entitas + model Pydantic tulis.
- [ ] Query builder + cursor keyset + pembatasan workspace.
- [ ] CRUD entitas `rw` + optimistic locking + soft delete.
- [ ] Redirect/penolakan entitas inti.
- [ ] `db_sql_readonly` + validasi `sqlglot` + read-only transaction.
- [ ] Tambahan dependensi `sqlglot` di requirements.

## Pengujian
- Injection: nilai filter `"x'; DROP TABLE"` → aman (parameter); field `password_hash` → `VALIDATION_ERROR`.
- Pembatasan workspace: klien workspace A tidak melihat task workspace B.
- `db_query` task overdue (UC-12) hasil sama dengan SQL manual.
- `db_sql_readonly`: `UPDATE …`, `SELECT pg_sleep(10)`, dua statement → ditolak; `SELECT` valid → hasil; non-admin → `FORBIDDEN`.
- Optimistic locking: dua update bersamaan → satu `CONFLICT`.

## Acceptance Criteria
1. UC-12 berjalan dari Claude Desktop; output `db_query` ≤ 1.500 token untuk hasil 500 baris (handle).
2. Tidak ada jalur di tool `db_*` yang dapat menulis ke schema `public` (dibuktikan test + grant DB).
3. Security review (TASK-25) tidak menemukan injection/IDOR pada `db_*`.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; coverage `services/db/` ≥ 90%.
- [ ] DATABASE §5 sinkron dengan registry entitas (test membandingkan).
- [ ] Review disetujui.
