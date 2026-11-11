"""Klasifikasi lokal berbasis embedding `embed-local` + aturan (MCP-V1-04, V1-K12).

Semua tugas di paket ini berjalan dengan 0 token LLM: KBLI/ISIC (kNN), mapping kolom import,
sentimen (head logistic regression), aspek, tipe kontak, klaster tema, kebutuhan ekstraktif.
LLM assist (opsional, default mati) hanya dipanggil pemakai untuk hasil ber-confidence rendah.
"""
