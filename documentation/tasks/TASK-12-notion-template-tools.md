# TASK-12 — Tools Template Notion (`tpl_*`)

| Item | Nilai |
|---|---|
| Milestone | M2 Agent & Dashboard |
| Prioritas | P0 |
| Estimasi | 4 hari |
| Dependensi | TASK-05, TASK-06, TASK-08 (TASK-11 untuk mode apply di luar run) |
| Referensi | MCP_SPEC §4.3 · DATABASE §4.4 · USE_CASES UC-01, UC-06 · `block.schema.ts` Dashboard |

## Tujuan
Agent dan klien dapat membuat struktur kerja ala Notion (Project Brief, dokumen task, dan pohon subtask) yang **langsung valid** di block editor Modul 1, dari template siap pakai maupun hasil generate dari brief — dengan biaya token kecil (outline, bukan JSON blok penuh, yang dikirim ke LLM).

## Spesifikasi

### Format template (`template_versions`)
```yaml
key: content_calendar_3w
name: Kalender Konten 3 Minggu
category: content_calendar
variables:
  - {name: brand, type: string, required: true}
  - {name: start_date, type: date, required: true}
  - {name: channels, type: list, default: [instagram, tiktok]}
blocks:                         # markdown ringkas → dikonversi md_to_blocks saat render
  md: |
    ## Tujuan
    - Meningkatkan awareness {{brand}} di {{channels|join(", ")}}
    ## Pilar Konten
    | Pilar | Porsi | Contoh |
    |---|---|---|
    | Edukasi | 40% | … |
    ## Checklist
    - [ ] Brief kreatif disetujui
subtasks:
  - {client_ref: week-1, title: "Week 1 — {{brand}}", offset_days: 0, duration_days: 5, tags: [content],
     md: "## Ide konten\n- …", children: []}
  - {client_ref: week-2, title: "Week 2 — {{brand}}", offset_days: 7, duration_days: 5}
```
- Templating: Jinja2 **sandboxed** (`SandboxedEnvironment`, tanpa akses atribut), filter `join`, `date_add`, `upper`, `title`.
- `outline` di-cache: daftar heading + jumlah blok per bagian + subtask (judul, offset, durasi) — ±150 token per template.

### Seed template (`services/templates/seeds/*.yaml`, minimal 8)
`campaign_brief`, `content_calendar_3w`, `content_calendar_monthly`, `product_launch_6w`, `market_research_plan`, `social_listening_report`, `competitor_analysis`, `monthly_marketing_report` — berbahasa Indonesia, mengikuti contoh Modul 1 (Project Brief + task "Generate Content Calendar").

### Komponen
- `library.py`: CRUD template + versi (simpan = versi baru).
- `renderer.py`: `render(template, variables, base_date) -> RenderedTemplate{blocks: list[BlockInput], subtasks: list[SubtaskInput], outline}`; tanggal subtask = `start_date + offset_days` → hari kerja; kedalaman relatif dicek terhadap target (depth target + kedalaman pohon ≤ 3).
- `md_to_blocks.py` (dibagi dengan TASK-10): heading `#..###`, paragraf, `- `/`* `, `1. `, `- [ ]/- [x]`, `>`, ``` ``` ```, tabel pipe (≤ 20 kolom × 50 baris, header row), `---` divider, `[judul](url)` → bookmark bila satu baris sendiri, gaya **bold**/*italic*/`code`/~~strike~~.
- `validators.py`: validasi tiap blok dengan schema kontrak (TASK-08) + batas ukuran; pesan error menyebut indeks blok & field.

### Tools
| Tool | Detail |
|---|---|
| `tpl_list(category?)` | Tabel ringkas (cache 300 s) |
| `tpl_get(template_key, format)` | `outline` (default) atau `full` (markdown + subtask, bukan JSON blok) |
| `tpl_render(template_key, variables, target, mode, run_id?)` | `preview` → outline + peringatan validasi; `apply` → tulis: **dalam run** via outbox (`agent.block.appended` → chunk 50, `agent.subtask.created` → chunk 20, `initial_blocks` per subtask), **luar run** via Service API (`/mcp-api/blocks/ops`, `/mcp-api/projects/:id/tasks`); `target.type=new_task` membuat task dulu lalu blok |
| `tpl_generate(brief, kind, target?, mode, language?)` | LLM `smart` `chat_json` → `{name, variables, md, subtasks}` (markdown, bukan blok, ~60% lebih hemat); validasi render; maks 2 putaran repair; `mode=save` → `origin=generated` |
| `tpl_save(template)` | Validasi + versi baru; hanya klien dengan toolset `tpl` + `writes` |

- Input `tpl_generate` ke LLM: brief (≤ 4.000 char) + daftar tipe blok yang didukung (1 baris) + contoh outline singkat 1 template kategori sama (≤ 300 token).

## Checklist Implementasi
- [ ] Format YAML + loader seed + 8 template.
- [ ] Renderer sandboxed + perhitungan tanggal hari kerja.
- [ ] `md_to_blocks` lengkap + validator.
- [ ] 5 tool + jalur tulis run/non-run + chunking.
- [ ] Prompt `tpl_generate.v1` + repair.
- [ ] Outline cache.

## Pengujian
- Semua template seed: render dengan variabel contoh → 100% blok lolos validator; dan (contract) diterima Dashboard.
- `md_to_blocks` golden tests (input md → JSON blok) 25 kasus.
- Sandbox: `{{ ''.__class__ }}` → error, bukan eksekusi.
- Kedalaman: apply template 2 level ke task depth 2 → `HIERARCHY_DEPTH_EXCEEDED` sebelum menulis apa pun.
- Live: `tpl_generate` untuk 5 brief berbeda → semua valid ≤ 2 repair; catat token.

## Acceptance Criteria
1. `tpl_render(content_calendar_3w, …, target=task, mode=apply)` dalam run menghasilkan dokumen + 3 subtask yang tampil benar di Dashboard (UC-01).
2. `tpl_generate` → `apply` ke Project Brief berhasil dari Claude Desktop (UC-06) dalam ≤ 2 tool call setelah preview.
3. Output `tpl_get(format=outline)` ≤ 300 token untuk semua template seed.
4. Tidak ada blok yang ditolak Dashboard karena format (0 error 400 pada uji 20 render).

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; coverage `services/templates/` ≥ 90%.
- [ ] Katalog template terdokumentasi di `docs/templates.md` (variabel, outline, contoh).
- [ ] Review oleh perwakilan tim Marketing untuk isi template seed.
