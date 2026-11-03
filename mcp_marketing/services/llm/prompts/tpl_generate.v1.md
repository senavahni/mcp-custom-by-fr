Buat sebuah template kerja Notion-like dari brief berikut.

Kategori: {{kind}}
Bahasa: {{language}}
Brief:
{{brief}}

Tipe blok yang didukung: {{block_types}}
Contoh outline kategori yang sama (maksimal sekitar 300 token):
{{example}}

Keluarkan JSON dengan bentuk:
{"name":"...","variables":[],"md":"...","subtasks":[{"client_ref":"...","title":"...","offset_days":0,"duration_days":1,"tags":[],"md":"","children":[]}]}

Markdown harus memakai heading ## atau ###, paragraf, checklist, bullet, daftar bernomor, quote,
code fence, tabel sederhana, divider, dan tautan bila berguna. Buat isi konkret berdasarkan brief,
bukan placeholder generik. Kembalikan variables kosong: klien tidak mengirim nilai tambahan saat apply.
Batasi sampai 20 subtask total dan kedalaman pohon yang aman. Jadwal memakai offset hari kalender
non-negatif; durasi memakai hari kerja positif. Jangan mengeluarkan JSON blok editor.
