# TASK-13 — Tools Task & Subtask (`task_*`)

| Item | Nilai |
|---|---|
| Milestone | M2 Agent & Dashboard |
| Prioritas | P0 |
| Estimasi | 3 hari |
| Dependensi | TASK-05, TASK-08, TASK-11 |
| Referensi | MCP_SPEC §4.4 · USE_CASES UC-07 · Dashboard PRD §6 (timeline), FR-05/06 |

## Tujuan
Agent dan klien dapat membaca dan mengatur task/subtask Dashboard (status, prioritas, jadwal, hierarki, komentar) secara aman dan ringkas, termasuk memecah task besar menjadi subtask terjadwal secara otomatis.

## Spesifikasi

### Jalur baca (cepat & hemat)
- Baca langsung dari DB (role read-only, model `dashboard.*`) dengan filter `deleted_at IS NULL` dan batas workspace klien/run.
- `task_list`: kolom default `id(8 char pertama + lookup), title, status, priority, start, end, depth, assignee` (assignee = display_name/handle agent). ID penuh dikembalikan di kolom `id`; LLM memakai ID penuh untuk aksi.
- `task_get(include)`: `blocks_outline` = heading + 120 char pertama blok non-heading (maks 40 baris); `blocks_full` = markdown hasil konversi blok → md (lebih hemat dari JSON); `subtasks` (pohon 2 level); `comments` (10 terakhir, 300 char).

### Jalur tulis
| Tool | Dalam run (run_id tersedia) | Di luar run |
|---|---|---|
| `task_create` | `agent.subtask.created` — hanya di bawah task run/turunannya | Service API `POST /projects/:id/tasks` / `tasks/:id/subtasks` |
| `task_update` | Tidak tersedia via webhook kontrak 1.0 → Service API dengan `X-MCP-Agent-Id = run.agent_id` (task harus task run/turunan) | Service API `PATCH /tasks/:id` |
| `task_move`, `task_reschedule` | Service API (task run/turunan) | Service API |
| `task_comment` | `agent.comment.added` | Service API `POST /comments` |

- Validasi lokal sebelum kirim: `end_date ≥ start_date`, milestone wajib bertanggal & `start=end`, kedalaman ≤ 3, judul 1–500 char, tag ≤ 20 × 50 char.
- `task_update` menolak field `assignee*` (`VALIDATION_ERROR` + pesan "assignee hanya bisa diubah manusia").

### `task_breakdown(task_id, max_subtasks, granularity, mode)`
1. Ambil task (judul, deskripsi ≤ 2.000 char, tanggal, subtask yang sudah ada).
2. LLM: `fast` bila `max_subtasks ≤ 8`, `smart` bila lebih; `chat_json` → `[{client_ref, title, duration_days, depends_on?, tags?, checklist?[]}]`.
3. Penjadwalan deterministik di Python: urut topologis `depends_on`, hari kerja, dalam rentang task induk bila ada (jika melampaui → tandai `outside_parent_range: true`, tetap diperbolehkan sesuai PRD Dashboard §6).
4. `preview` → tabel subtask + total durasi; `apply` → `task_create` batch (≤ 20) + checklist sebagai `initial_blocks`.

## Checklist Implementasi
- [x] Query baca + konversi blok → markdown (`blocks_to_md.py`).
- [x] 8 tool + validasi lokal + pemilihan jalur tulis.
- [x] Penjadwal hari kerja & dependensi (`services/tasks/scheduler.py`).
- [x] Prompt `breakdown.v1`.
- [x] Batas workspace untuk klien eksternal (`api_clients.workspace_id`).

## Pengujian
- `task_list` 1.000 task → p95 < 200 ms; output ≤ 1.500 token (20 baris).
- Jalur tulis: dalam run memakai webhook untuk create/comment, Service API untuk update (mock), luar run Service API.
- `task_update` pada task di luar run → `FORBIDDEN` bila dipanggil dari agent run.
- Penjadwal: dependensi siklik → `VALIDATION_ERROR`; 5 subtask 2 hari mulai Kamis → tanggal benar melompati akhir pekan.
- Contract/live UC-07 terhadap Dashboard lokal belum dijalankan; lingkungan Dashboard/Redis tidak tersedia.

### Hasil verifikasi terakhir
- MCP: 239 test lulus, 79 dilewati; coverage `tools/task.py` 88%; Ruff dan mypy modul terkait lulus.
- Pembatasan workspace/run dan jalur tools diuji pada unit tests. E2E UC-07 di Timeline Dashboard dan review akhir masih menunggu.

## Acceptance Criteria
1. UC-07 berjalan dari Claude Desktop: list → breakdown preview → apply → reschedule cascade, semua tampil di Timeline Dashboard.
2. Tidak mungkin mengubah assignee atau menghapus task melalui tool apa pun.
3. `task_breakdown` 6 subtask memakai ≤ 2.500 token LLM.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; coverage ≥ 85%.
- [x] MCP_SPEC §4.4 sesuai implementasi.
- [ ] Review disetujui.
