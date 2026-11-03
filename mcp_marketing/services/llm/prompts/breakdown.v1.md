Pecah task induk menjadi rencana subtask yang dapat langsung dijalankan.

Task induk: {{title}}
Deskripsi:
{{description}}

Subtask yang sudah ada (jangan duplikasi):
{{existing}}

Jumlah maksimum subtask baru: {{max_subtasks}}
Granularitas: {{granularity}}

Keluarkan hanya JSON yang sesuai skema:
{
  "subtasks": [
    {
      "client_ref": "ref-unik",
      "title": "Judul aksi yang spesifik",
      "duration_days": 2,
      "depends_on": [],
      "tags": [],
      "checklist": []
    }
  ]
}

Ketentuan:
- Buat maksimal {{max_subtasks}} subtask dan minimal satu subtask.
- Gunakan judul singkat, spesifik, berorientasi hasil, dan berbahasa Indonesia.
- `client_ref` harus unik, sederhana, dan stabil; `depends_on` hanya boleh berisi ref
  subtask dalam respons yang sama. Hindari dependensi siklik.
- `duration_days` adalah bilangan hari kerja positif; jadwal aktual dihitung sistem.
- Gunakan `tags` dan `checklist` sebagai daftar string; checklist berisi langkah verifikasi nyata.
- Jangan mengulang pekerjaan pada daftar subtask yang sudah ada.
- Untuk granularitas `day`, pecah menjadi langkah harian yang masih bermakna. Untuk
  `deliverable`, susun berdasarkan keluaran yang dapat ditinjau.
- Jangan menambahkan tanggal, markdown, blok editor, penjelasan, atau teks di luar JSON.
