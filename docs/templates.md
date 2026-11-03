# Template dokumen Dashboard

Template disimpan sebagai versi di schema `mcp` dan menghasilkan Markdown yang dikonversi serta
divalidasi ke format blok Dashboard sebelum ditampilkan atau diterapkan.

## Seed tersedia

| Key | Kategori | Variabel wajib | Outline |
|---|---|---|---|
| `campaign_brief` | campaign | brand, objective, audience | Tujuan, audiens, pesan, kanal, KPI |
| `content_calendar_3w` | content_calendar | brand, start_date | Tujuan, pilar konten, checklist; 3 subtask mingguan |
| `content_calendar_monthly` | content_calendar | brand, month | Tujuan, kanal, metrik; 4 subtask pekanan |
| `product_launch_6w` | launch | product, launch_date | Strategi, kanal, risiko; persiapan, teaser, peluncuran, evaluasi |
| `market_research_plan` | market_research | market, question | Tujuan, metode, output; riset, wawancara, sintesis |
| `social_listening_report` | social_listening | brand, period | Cakupan, sentimen, insight, rekomendasi |
| `competitor_analysis` | competitor | brand, competitors | Tujuan, positioning, kanal, rekomendasi |
| `monthly_marketing_report` | report | brand, month | Ringkasan, KPI, kanal, insight, aksi |

`tpl_list` menampilkan metadata ringkas; `tpl_get(format="outline")` hanya mengembalikan outline,
sedangkan `format="full"` mengembalikan Markdown, definisi variabel, dan metadata subtask.

## Contoh preview

```json
{
  "template_key": "content_calendar_3w",
  "variables": {
    "brand": "Kopi Nusantara",
    "start_date": "2026-11-02",
    "channels": ["Instagram", "TikTok"]
  },
  "target": {
    "type": "task",
    "project_id": "00000000-0000-0000-0000-000000000001",
    "task_id": "00000000-0000-0000-0000-000000000002"
  },
  "mode": "preview"
}
```

Terapkan template dengan mengganti `mode` menjadi `apply`. Aksi tulis dibatasi pada workspace
klien atau project/task dalam cakupan agent run; seluruh blok divalidasi sebelum perubahan
pertama dikirim.
