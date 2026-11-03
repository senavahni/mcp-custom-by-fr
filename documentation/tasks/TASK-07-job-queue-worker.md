# TASK-07 — Job Queue, Worker & Scheduler

| Item | Nilai |
|---|---|
| Milestone | M1 Platform |
| Prioritas | P0 |
| Estimasi | 3 hari |
| Dependensi | TASK-03, TASK-05 |
| Referensi | MCP_SPEC §2.4, §4.1 · PRD FR-08, NFR-06 · README D10 |

## Tujuan
Menjalankan pekerjaan lama (crawl, scraping, ingest, analisis besar) di luar request MCP secara andal: antrean di PostgreSQL, worker yang bisa di-resume, progres, pembatalan berantai, dan job berkala.

## Spesifikasi

### Antrean (`worker/queue.py`)
- `enqueue(kind, params, *, client_id, run_id=None, parent_job_id=None, priority=5, run_after=None, timeout_s=None, dedupe_key=None) -> JobAccepted`
  - `dedupe_key` (hash params) mencegah job identik ganda dalam status `queued/running` → kembalikan job lama.
  - `estimate` diisi handler (`Handler.estimate(params)`) → `{seconds, external_units, usd}`.
- Pengambilan: `UPDATE mcp.jobs SET status='running', locked_by=$w, locked_at=now(), attempts=attempts+1 WHERE id = (SELECT id FROM mcp.jobs WHERE status='queued' AND run_after<=now() ORDER BY priority, run_after FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *`.
- Heartbeat tiap 10 s; job `running` dengan heartbeat > 60 s → dikembalikan ke `queued` (reaper) bila `attempts < max_attempts`, selain itu `failed`.

### Worker (`worker/runner.py`, `scripts/run_worker.py`)
- `WORKER_CONCURRENCY` slot asyncio; handler CPU-bound (ONNX) dijalankan di `ProcessPoolExecutor(1)`; handler browser memakai pool driver (TASK-14).
- Antarmuka handler:
```python
class Handler(Protocol):
    kind: str
    async def estimate(self, params) -> Estimate: ...
    async def run(self, job: JobCtx) -> JobResult: ...   # JobCtx: params, checkpoint, progress(), save_checkpoint(), is_cancelled(), result_store, llm, session
```
- `JobCtx.is_cancelled()` membaca `cancel_requested` (cache 2 s); handler wajib memeriksa di setiap iterasi.
- Timeout per job (`timeout_s`) → `failed{code:"JOB_TIMEOUT"}` dengan hasil parsial disimpan bila ada (`result.partial=true`).
- Hasil: `result_handle` + `result` (envelope tanpa items) → dipakai `job_status`.

### Pembatalan berantai
- `cancel_job(id)` set `cancel_requested=true` untuk job dan seluruh turunannya (`parent_job_id` rekursif).
- `cancel_run_jobs(run_id)` dipanggil TASK-09 (`pm_cancel_agent_run`).

### Tool
- `job_status(job_id)` → `{status, progress{done,total}, message, result?}` (hasil hanya saat `succeeded`; T12).
- `job_wait(job_id, timeout_s≤20)` → `LISTEN/NOTIFY` kanal `mcp_job_done` (fallback polling 500 ms).
- `job_cancel(job_id)`, `job_list(status?, kind?, limit≤20)`.
- Kepemilikan: hanya klien/run pembuat (atau admin).

### Scheduler (`worker/scheduler.py`)
- Membaca `mcp.schedules` tiap menit (`croniter`, zona `TIMEZONE`), enqueue job `kind` dengan `dedupe_key=schedule_id+slot`.
- Schedule bawaan (seed): `cache_cleanup` (tiap jam), `retention` (harian 02:15), `usage_rollup` (tiap 10 menit), `kb_sync_dashboard` per project aktif (opsional, harian 03:00), watchlist crawl (dari `keyword_watchlists.schedule_id`).

## Checklist Implementasi
- [ ] `jobs` service: enqueue, dedupe, estimate, pick, heartbeat, reaper.
- [ ] Runner + slot + process pool + timeout.
- [ ] `JobCtx` (progress, checkpoint, cancel).
- [ ] Pembatalan berantai + by run.
- [ ] Tool `job_*` + LISTEN/NOTIFY.
- [ ] Scheduler + schedule seed + handler `cache_cleanup`, `retention`, `usage_rollup`.
- [ ] `scripts/start-worker.bat`, graceful shutdown (selesaikan checkpoint, lepas lock).

## Pengujian
- Dua worker paralel, 100 job → setiap job diproses tepat sekali.
- Kill worker saat job berjalan → reaper mengembalikan job; handler melanjutkan dari `checkpoint`.
- Cancel parent → child ikut `cancelled` ≤ 5 s.
- `job_wait` mengembalikan hasil segera setelah selesai (< 1 s dari NOTIFY).
- Scheduler: cron `*/1` membuat tepat satu job per slot walau 2 worker aktif.

## Acceptance Criteria
1. Job crawl 30 hari yang dimatikan di hari ke-12 dilanjutkan dari hari ke-12 setelah restart, tanpa duplikasi data.
2. Pembatalan run Dashboard menghentikan semua job turunan ≤ 5 s.
3. Tidak ada job "nyangkut" `running` > 60 s setelah worker mati.
4. Tool `job_status` tidak pernah mengirim ulang hasil lengkap selain saat status `succeeded`.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; test integration lulus.
- [ ] Coverage `worker/` ≥ 85%.
- [ ] `docs/operations.md#worker` (start/stop, membaca job gagal, retry manual `manage.py job-retry`).
- [ ] Review disetujui.
