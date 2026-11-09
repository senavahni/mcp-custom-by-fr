# Data referensi market intelligence (MCP-V1-02)

Di-seed oleh migrasi `0005_market_intel` lewat `reference_data.load_reference_data()` (upsert,
idempoten). Dibangkitkan ulang dengan `scripts/build_market_intel_seeds.py` (lihat docstring).

| File | Isi | Sumber | Lisensi |
|---|---|---|---|
| `regions.csv` | 38 provinsi + 514 kab/kota: kode Kemendagri, nama, ibu kota, centroid lat/lng | [cahyadsn/wilayah](https://github.com/cahyadsn/wilayah) `db/wilayah_level_1_2.sql` — Kepmendagri No 300.2.2-2138 Tahun 2025 (diunduh 9 Nov 2026) | MIT |
| `kbli2020.csv` | KBLI 2020: 21 kategori, 88 golongan pokok, 245 golongan, 567 subgolongan, 1.790 kelompok; judul & uraian Bahasa Indonesia | [perlancar/perl-TableDataBundle-Business-ID-KBLI](https://github.com/perlancar/perl-TableDataBundle-Business-ID-KBLI) `share/code2020.csv`, `share/category2020.csv` (teks dari Peraturan BPS No. 2/2020) | Perl_5 (paket); teks KBLI = peraturan pemerintah |
| `kbli2020_isic4.csv` | Pemetaan KBLI 2020 → ISIC Rev.4 | **Diturunkan** dari struktur kode terhadap [UNSD ISIC Rev.4](https://unstats.un.org/unsd/classifications/Econ/Download/In%20Text/ISIC_Rev_4_english_structure.Txt) | UNSD (data publik) |

## Catatan kualitas

- **Pemetaan ISIC bukan tabel konkordansi resmi BPS.** KBLI 2020 mewarisi struktur ISIC Rev.4 hingga
  4 digit, sehingga setiap kode KBLI dipetakan ke kode ISIC terpanjang yang cocok dengan prefiksnya.
  `match_level`: `exact` (kode identik), `class`/`group`/`division` (kode KBLI lebih rinci atau
  merupakan perluasan nasional; dipetakan ke induk ISIC). Ganti dengan tabel BPS bila tersedia.
- `name_en` diisi nama ISIC hanya bila kodenya identik (kategori s.d. subgolongan); kelompok 5 digit
  tidak punya nama Inggris resmi.
- Sumber KBLI tidak memuat 6 golongan (`151`, `331`, `511`, `512`, `662`, `872`) yang dirujuk
  subgolongannya; judulnya ditambahkan manual (tanpa uraian).
- Koreksi koordinat: Kabupaten Wakatobi (`74.07`) lng sumber `23.538901` → `123.538901`.
- Centroid wilayah dipakai sebagai geocode cadangan (`precision=city/province`), bukan lokasi alamat.
