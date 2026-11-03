# TASK-10 — Agent Runtime (Loop Tool-Calling via LLM Gateway)

| Item | Nilai |
|---|---|
| Milestone | M2 Agent & Dashboard |
| Prioritas | P0 |
| Estimasi | 6 hari |
| Dependensi | TASK-05, TASK-06, TASK-08, TASK-09 |
| Referensi | MCP_SPEC §4.2, §7, §8 · PRD FR-11 · README D12 · USE_CASES UC-01, UC-03, UC-04 |

## Tujuan
Menjalankan agent (`strategist`, `researcher`, `analyst`) yang mengerjakan task Dashboard secara otonom memakai tool MCP **in-process**, dengan pemakaian token minimal: toolset kecil per agent, riwayat yang dipadatkan, output tool yang sudah ringkas, dan budget per run.

## Spesifikasi

### Profil agent (`mcp.agent_profiles`, seed via `manage.py seed`)
- Field: `system_prompt` (≤ 600 token), `route`, `toolsets`, `tool_denylist`, `run_token_budget`, `max_steps`.
- Tool yang diberikan ke LLM = `ToolSpec` dari toolset profil, **tanpa** toolset `pm`; ditambah tool virtual runtime:
  - `pm_query_project_context(query, scope, top_k)` — Agent API `context/query` (+ `kb_search` koleksi project bila ada, digabung RRF), hasil dipotong 600 char/item.
  - `report_progress(status: thinking|working, message ≤ 120)` → `agent.status.changed`.
  - `write_blocks(operations[])` → `agent.block.appended` (chunk otomatis, validasi blok); hasil = `applied` + `conflicts`.
  - `create_subtasks(parent_task_id?, subtasks[])` → `agent.subtask.created`; hasil = peta `client_ref → id` (disimpan di `runs.subtask_refs`).
  - `post_comment(content, parent_comment_id?, mentions?)` → `agent.comment.added`.
  - `finish(summary ≤ 500)` / `fail(error_code, message, retryable)`.
- Tool `tpl_*`, `task_*`, `report_publish` dalam konteks run otomatis menulis lewat webhook (run_id diisi runtime, bukan oleh LLM).

### Loop (`services/agent/runtime.py`)
```
messages = [system(profile, aturan, format), user(task brief terpadatkan)]
for step in 1..max_steps:
    cek cancel → stop
    resp = gateway.chat(purpose="agent_loop", route=profile.route, tools=tool_schemas, messages)
    jika resp.tool_calls: jalankan paralel (maks 3, kecuali tool `writes` → berurutan) lewat pipeline yang sama dengan MCP (cache/compact/budget/audit)
                         tambahkan hasil sebagai pesan tool (structured JSON ringkas)
    jika tidak ada tool_calls dan belum finish → minta finish/tindakan (maks 2 kali), lalu finish otomatis dengan konten terakhir sebagai komentar
    jika total token riwayat > AGENT_HISTORY_COMPACT_AT_TOKENS → kompaksi
```
- **Brief awal terpadatkan**: judul, properti task, deskripsi (≤ 2.000 char), outline context_blocks (heading + 1 kalimat per bagian), subtask (judul+status). Detail diambil via `pm_query_project_context` hanya bila perlu.
- **Kompaksi riwayat**: pesan tool lama diganti ringkasan (`fast`, ≤ 300 token per blok riwayat) + handle yang masih relevan; 2 langkah terakhir dipertahankan utuh. Ringkasan disimpan di `run_steps(role=summary)`.
- **Hasil tool sebagai data tidak tepercaya**: konten dari web/sosmed dibungkus `<untrusted_content>` (TASK-25).
- Mode ringan untuk `prompt_block` & mention sederhana: `max_steps=6`, toolset `kb`, `pm_agent`, route `fast` bila prompt ≤ 500 char dan tidak menyebut data/riset.
- Setiap langkah disimpan di `run_steps` (konten terpadatkan) → resume setelah restart.

### Status ke Dashboard
- Awal: `agent.status.changed{thinking}`; saat tool pertama yang mengubah data/riset: `{working, message}`; runtime mengirim status otomatis bila tool berjalan > 10 s (`"Mengumpulkan data X…"`).
- Akhir sukses: komentar ringkasan (≤ 1.500 char) + `agent.task.completed{summary}`.
- Gagal: komentar sistem singkat + `agent.task.failed{error_code, message, retryable}`. Kode: `BUDGET_EXCEEDED`, `LLM_UNAVAILABLE`, `MAX_STEPS`, `RUN_TIMEOUT`, `WEBHOOK_REJECTED`, `INTERNAL`.

### Format output agent ke Dokumen
- Prompt sistem mewajibkan hasil berupa blok terstruktur (heading H2 per bagian, bullet, checklist, tabel untuk data), bahasa Indonesia, tanpa HTML.
- Konversi markdown → blok BlockNote dilakukan oleh `services/templates/md_to_blocks.py` (dipakai juga TASK-12/23) sehingga LLM cukup menulis markdown ringkas (lebih hemat token daripada JSON blok).

### Aturan keamanan kontrak
- Agent tidak bisa: ubah assignee, hapus task/blok, menyentuh project lain (tool tidak tersedia + validasi `task_id` milik run/turunannya).
- Mention agent lain di komentar agent tidak memicu agent (dijamin Dashboard; runtime juga tidak menyertakan mention agent).

## Checklist Implementasi
- [ ] Seed 3 profil agent + prompt sistem (versi `v1`).
- [ ] Tool virtual runtime (7 tool) + injeksi `run_id`.
- [ ] Loop, eksekusi paralel/berurutan, cancel, max_steps.
- [ ] Kompaksi riwayat + penyimpanan `run_steps` + resume.
- [ ] `md_to_blocks` (heading, list, checklist, tabel, quote, code, divider, link) + validasi.
- [ ] Status otomatis & penanganan gagal.
- [ ] Laporan token per run (`runs.tokens_used`) & `run_steps.tokens`.

## Pengujian
- Unit dengan LLM palsu (skrip respons): urutan tool call, budget habis di tengah → gagal rapi, cancel di langkah 3, max_steps.
- Unit `md_to_blocks`: 20 kasus markdown → blok valid menurut schema kontrak.
- Kompaksi: riwayat 30k token → < 12k setelah kompaksi, handle tetap ada.
- Live (`-m live`): UC-01 "Content Calendar 3 Minggu" dengan Dashboard lokal → blok + 3 subtask + komentar; catat token.
- Resume: kill server di langkah 4 → run dilanjutkan dari langkah 5.

## Acceptance Criteria
1. UC-01, UC-03, UC-04 berhasil end-to-end dengan agent nyata (Gateway `smart`) di Dashboard lokal.
2. Run UC-01 memakai ≤ 40.000 token LLM total (dicatat; target dievaluasi di TASK-26).
3. Semua blok yang dikirim lolos validasi Dashboard (0 respons 400 pada 10 run uji).
4. Cancel dari Dashboard menghentikan run ≤ 5 s tanpa webhook lanjutan.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi.
- [ ] Coverage `services/agent/` ≥ 85%.
- [ ] `docs/agents.md`: cara menambah agent baru (profil Dashboard + MCP), menyetel prompt & budget.
- [ ] Review disetujui; demo ke Marketing Manager.
