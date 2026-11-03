# TASK-11 — Ekstensi Dashboard: Service API untuk MCP (Contract 1.1)

| Item | Nilai |
|---|---|
| Milestone | M2 Agent & Dashboard |
| Prioritas | P0 |
| Estimasi | 5 hari (backend NestJS, repo `marketingdashboardjtp`) |
| Dependensi | Modul 1 TASK_002/003 selesai (sudah) |
| Referensi | MCP_SPEC §5.1 · README D4 · `M1_MCP_INTEGRATION.md` §6, §8 |

## Tujuan
Memungkinkan MCP mengelola project, task, blok, dan komentar **di luar agent run** (mis. dari Claude Desktop atau tool `tpl_render`/`task_*`) tanpa menulis langsung ke database, sehingga activity log, aturan bisnis (kedalaman, validasi blok, soft delete), RBAC workspace, dan event real-time Socket.IO tetap berlaku.

## Spesifikasi (di repo Dashboard)

### Modul `mcp-api` (`backend/src/modules/mcp-api/`)
- Guard `McpServiceTokenGuard` (sudah ada untuk Agent API) + header wajib `X-MCP-Agent-Id: <uuid>`:
  - agent harus ada, `is_active`, tidak `deleted_at`;
  - semua resource yang disentuh harus berada di `workspace_id` agent tersebut → selain itu 404 (anti-IDOR, konsisten dengan TASK_001 §C.2).
- Aktor semua perubahan: `actor_type = agent`, `actor_id = agent.id` (activity log, `created_by_*`, `last_edited_by_*`).
- Rate limit: 120 req/menit per agent (Redis, guard yang sudah ada).
- Versi: header respons `X-Contract-Version: 1.1`; `contract_version` dokumen MCP naik ke 1.1 (aditif, tidak mengubah 1.0).

### Endpoint
| Method & path | Body/Query | Catatan |
|---|---|---|
| `GET /api/v1/mcp-api/workspaces/:ws/projects` | `?q=&limit=&cursor=` | ringkas: id, title, icon, updated_at |
| `POST /api/v1/mcp-api/projects` | `{workspace_id, title, description?, icon?}` | |
| `GET /api/v1/mcp-api/projects/:id/tasks` | `?status=&parent_task_id=&assignee=&due_before=&tag=&fields=&limit=&cursor=` | memakai `task-reader.service` |
| `POST /api/v1/mcp-api/projects/:id/tasks` | `{tasks:[{client_ref?, title, parent_task_id?, priority?, start_date?, end_date?, tags?, is_milestone?, initial_blocks?}] ≤ 20}` | validasi kedalaman ≤ 3 (`task.rules.ts`), respons `created[{client_ref,id}]` |
| `POST /api/v1/mcp-api/tasks/:id/subtasks` | sama seperti di atas | |
| `PATCH /api/v1/mcp-api/tasks/:id` | `{title?, status?, priority?, start_date?, end_date?, tags?, is_milestone?}` | **assignee tidak diterima** (422) |
| `PATCH /api/v1/mcp-api/tasks/:id/move` | `{new_parent_task_id?, before_task_id?}` | reuse logika move (anti circular) |
| `PATCH /api/v1/mcp-api/tasks/:id/reschedule` | `{start_date, end_date, cascade}` | reuse timeline service |
| `POST /api/v1/mcp-api/blocks/ops` | `{target:{project_id, task_id?}, operations[] ≤ 50}` (format = `agent.block.appended`) | `task_id` null = Project Brief; respons `applied/conflicts` |
| `POST /api/v1/mcp-api/comments` | `{project_id, entity_type, entity_id, content, parent_comment_id?, mentions?}` | mention user → notifikasi; mention agent **tidak** memicu agent |

- Hapus task/blok/project **tidak** disediakan (sesuai §8 kontrak).
- Semua perubahan memancarkan event real-time yang sama dengan aksi user (task.updated, block.updated, comment.created, …).

### Pendaftaran agent "MCP_Assistant"
- Seed opsional di Dashboard: agent `MCP_Assistant` (`mcp_agent_key=assistant`) per workspace sebagai aktor default klien eksternal; `api_clients.dashboard_agent_id` di MCP menunjuk ke agent ini.

### Sisi MCP (`services/dashboard/service_api.py`)
- Klien typed untuk semua endpoint, header `X-MCP-Agent-Id` dari `client.dashboard_agent_id`, error mapping (404 → `NOT_FOUND`, 422 → `VALIDATION_ERROR`/`HIERARCHY_DEPTH_EXCEEDED`, 409 → `CONFLICT`, 429 → `RATE_LIMITED`).

## Checklist Implementasi
- [x] (Dashboard) Modul `mcp-api`: guard, controller, DTO Zod di `shared/schemas/mcp-api.schema.ts`.
- [x] (Dashboard) Reuse service yang ada (tasks, blocks, comments, timeline) — tanpa duplikasi aturan.
- [x] (Dashboard) Swagger `/api/docs` memuat endpoint baru; `.env.example` tidak berubah (pakai `MCP_SERVICE_TOKEN`).
- [x] (Dashboard) Seed demo `MCP_Assistant` (`mcp_agent_key=assistant`) di workspace Marketing Demo; `dashboard_agent_id` klien MCP dapat menunjuk ke UUID deterministik `00000000-0000-4000-8000-000000000203`.
- [x] (Dashboard) Update `M1_MCP_INTEGRATION.md` → contract 1.1 (§6.1 Service API).
- [x] (MCP) `service_api.py` + test.

## Pengujian
- (Dashboard, jest + supertest) token salah → 401; agent dari workspace lain → 404; `PATCH tasks/:id` dengan assignee → 422; kedalaman 4 → 422 `HIERARCHY_DEPTH_EXCEEDED`; activity log ber-aktor agent; event Socket.IO terkirim.
- (Dashboard) Blok invalid → 400 dengan indeks operasi.
- (MCP, `-m contract`) alur UC-06 & UC-07 terhadap Dashboard lokal.

### Hasil verifikasi terakhir
- Dashboard MCP API: 11 unit test lulus; coverage service 89,55% statements / 93,22% lines.
- Dashboard typecheck produksi lulus; Swagger document test mengonfirmasi route Service API dan autentikasi Bearer.
- MCP: 239 test lulus, 79 dilewati; Ruff dan mypy modul terkait lulus.
- Uji integrasi/live UC-06 dan UC-07 belum dijalankan: layanan Dashboard/Redis lokal tidak tersedia. Verifikasi real-time UI dan persetujuan kontrak kedua tim masih menunggu.

## Acceptance Criteria
1. Perubahan via Service API tampil real-time di UI Dashboard (< 2 s) dengan label aktor agent.
2. Tidak ada endpoint yang bisa menghapus data atau mengubah assignee/member.
3. Uji IDOR (resource workspace lain) seluruhnya 404.
4. Kontrak 1.0 (webhook & Agent API) tidak berubah perilakunya (regresi E2E Modul 1 lulus).

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; test Dashboard (unit + integration) lulus, coverage modul ≥ 80%.
- [ ] Dokumen kontrak 1.1 disetujui kedua tim.
- [x] Swagger terbarui; migrasi DB tidak diperlukan.
- [ ] Review backend Dashboard disetujui.
