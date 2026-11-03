# Evaluasi Sentimen (TASK-20) — status: model lokal BELUM dipilih

Pipeline sudah lengkap; spike model & evaluasi akurasi **tertunda** (keputusan: bangun pipeline
dulu, model menyusul). Tanpa model lokal, `method=auto` otomatis memakai LLM `fast` untuk semua
teks (catatan `model_unavailable_fallback_llm`) — lebih boros token, tetap memakai cache.

## Pipeline
1. Normalisasi (`services/nlp/normalize.py`): lowercase, URL → `<url>`, mention → `@user`, hashtag
   dipisah, huruf berulang dipendekkan, slang → baku (`services/nlp/data/slang_id.tsv`, ±760
   entri, frasa multi-kata didukung), emoji → `emoji_pos`/`emoji_neg`.
2. Deteksi bahasa (lingua, atas teks ternormalisasi; porsi Indonesia/Melayu/Jawa ≥ 0,25 → `id`).
3. Cache `mcp.sentiment_results` per `sha256(teks ternormalisasi) + model` — teks duplikat/retweet
   dan analisis ulang tidak memanggil model/LLM lagi.
4. Model lokal ONNX (batch 64, process pool worker).
5. Eskalasi LLM `fast` (prompt `sentiment_batch.v1`, format baris `i|label|skor`, batch 50) untuk
   `max_prob < SENTIMENT_LLM_THRESHOLD` (0,60), heuristik sarkasme, atau bahasa lain. Baris rusak
   diulang sekali, lalu jatuh ke label model (atau netral 0,5 bila tidak ada).
6. Aspek opsional: keyword-spotting sinonim (bentuk baku) → hanya kalimat terkait ke LLM
   (`aspect_batch.v1`, batch 40).
7. Agregasi Python: distribusi, net = (pos − neg)/total, per hari/kata kunci/sumber/penulis, aspek,
   3 contoh positif & negatif dengan engagement tertinggi; `sentiment_summary` membandingkan handle.

## Langkah spike yang tersisa (perlu tim Marketing)
1. Pilih ≥ 2 kandidat model 3 kelas berbasis IndoBERT/IndoRoBERTa dengan lisensi yang mengizinkan
   pemakaian internal; ekspor ke ONNX + kuantisasi int8 dengan `optimum` **di mesin dev**
   (bukan dependensi runtime), unggah ke repo HF internal/privat yang berisi `tokenizer.json`,
   `config.json` (`id2label` positive/neutral/negative), `onnx/model_quantized.onnx`.
2. Pasang: `python scripts/manage.py download-models --skip-embed --sentiment-repo <org/repo>`
   (ke `SENTIMENT_MODEL_DIR`).
3. Set uji berlabel ≥ 300 teks (campuran X/Threads/ulasan) dilabel 2 orang, catat Cohen's κ.
4. Laporkan di dokumen ini: macro-F1 model saja & pipeline `auto` (target ≥ 0,80), F1 per kelas,
   confusion matrix, token LLM per 1.000 teks (target ≤ 15.000), throughput (target 5.000 teks
   < 3 menit), lisensi model.

## Yang sudah diuji (tanpa model nyata)
- 30 kasus normalisasi (slang, emoji, hashtag, URL, mention, huruf berulang).
- Parser baris LLM toleran (pemisah `|:;,`, label pos/positif/positive, skor koma), baris hilang
  diulang sekali, batch 50.
- Eskalasi hanya item ragu/sarkas/bahasa lain; teks duplikat dihitung sekali; analisis ulang =
  100 % cache hit dan 0 token LLM (model diganti dengan prediktor palsu di test).
