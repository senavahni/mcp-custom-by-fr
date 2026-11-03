# TASK-20 — Tools Analisis Sentimen (`sentiment_*`)

| Item | Nilai |
|---|---|
| Milestone | M5 Analysis |
| Prioritas | P0 |
| Estimasi | 4 hari |
| Dependensi | TASK-06, TASK-07, TASK-15, TASK-16 |
| Referensi | MCP_SPEC §4.10 · DATABASE §4.7 · USE_CASES UC-14 · PRD NFR-05 · README D9 |

## Tujuan
Analisis sentimen Bahasa Indonesia (termasuk bahasa gaul/campur) dan Inggris untuk ribuan teks dengan **pemakaian token LLM minimal**: model klasifikasi lokal mengerjakan sebagian besar, LLM hanya untuk kasus ragu dan analisis aspek, lalu hasil diagregasi di server.

## Spesifikasi

### Spike model (0,5 hari)
- Kandidat model klasifikasi sentimen Indonesia berbasis IndoBERT/IndoRoBERTa (3 kelas) dari Hugging Face; ekspor ke ONNX (`optimum` di mesin dev, bukan dependensi runtime) dan kuantisasi int8.
- Set uji berlabel internal ≥ 300 teks (campuran X/Threads/ulasan, dilabel 2 orang, Cohen's κ dicatat).
- Kriteria pilih: macro-F1 ≥ 0,75, throughput ≥ 100 teks/detik di CPU mesin target, lisensi mengizinkan penggunaan komersial/internal.

### Pipeline (`services/nlp/`)
1. **Normalisasi** (`normalize.py`): lowercase, URL → `<url>`, mention → `@user`, hashtag dipisah (`#KopiSusu` → `kopi susu`), kamus slang Indonesia (`gk`→`tidak`, `bgt`→`banget`, `mantul`→`mantap betul`, …, file `slang_id.tsv` ≥ 500 entri), emoji → token sentimen (`😍`→`emoji_pos`).
2. **Deteksi bahasa** (`lang.py`): id/en/lainnya; bahasa lain → LLM langsung atau `neutral` + flag (konfigurable).
3. **Cache** per `sha256(teks ternormalisasi)+model` di `sentiment_results` (teks retweet/duplikat tidak dihitung ulang).
4. **Model lokal** (`sentiment_model.py`): batch 64 via `ProcessPoolExecutor`; output label + probabilitas.
5. **Eskalasi LLM** (`sentiment_llm.py`): item dengan `max_prob < SENTIMENT_LLM_THRESHOLD (0,60)` atau mengandung sarkasme heuristik (kata "katanya", "wkwk" + kata positif, dll.) → route `fast`, batch 50, prompt `sentiment_batch.v1`, format keluaran per baris `i|label|score` (bukan JSON per item → ±50% lebih hemat), parse toleran.
6. **Aspek (opsional)**: `aspects=["harga","rasa"]` → keyword-spotting sinonim (kamus aspek per kata kunci, dapat diperluas LLM sekali per permintaan) → hanya kalimat yang memuat aspek dikirim ke LLM `fast` (batch 40, `i|aspect|label`).
7. **Agregasi** (Python): distribusi, net sentiment = (pos − neg)/total, per group (day/keyword/source/author), perubahan vs periode/handle pembanding, 3 contoh positif & negatif dengan engagement tertinggi.

### Tools
- `sentiment_analyze(source{handle|texts[] ≤ 300}, aspects?, method=auto|model|llm, language, group_by?)`: ≤ 300 teks & `wait_s` → sinkron; selebihnya job. `method=llm` membutuhkan konfirmasi biaya (`estimate`) & tunduk budget.
- `sentiment_summary(handle, group_by, compare_handle?)`: tanpa LLM, dari `sentiment_results`.
- Output `cost{llm_tokens, items_model, items_llm, cache_hits}`.

## Checklist Implementasi
- [ ] Spike model + ekspor ONNX + set uji berlabel + keputusan D9.
- [ ] Normalisasi + kamus slang + emoji.
- [ ] Deteksi bahasa.
- [ ] Inferensi ONNX batch + process pool.
- [ ] Eskalasi LLM batch + parser toleran.
- [ ] Aspek (keyword-spotting + LLM).
- [ ] Agregasi + 2 tools + cache.
- [ ] `manage.py download-models` memuat model sentimen.

## Pengujian
- Unit normalisasi (30 kasus slang/emoji/hashtag).
- Akurasi: set uji 300 → macro-F1 pipeline `auto` ≥ 0,80 (model + eskalasi), dilaporkan per kelas.
- Token: 1.000 teks campuran → token LLM ≤ 15.000 (NFR-05), dicatat di laporan.
- Parser LLM: baris rusak/hilang → item tersebut diulang sekali lalu jatuh ke label model.
- Throughput: 5.000 teks < 3 menit di mesin target (tanpa eskalasi berlebih).

## Acceptance Criteria
1. UC-14: 1.500 post → distribusi, tren harian, aspek, contoh; output ≤ 1.500 token.
2. macro-F1 ≥ 0,80 pada set uji internal; token LLM per 1.000 teks ≤ 15.000.
3. Analisis ulang handle yang sama → ≥ 95% cache hit, 0 token LLM tambahan.

## Definition of Done
- [ ] Checklist & Acceptance Criteria terpenuhi; coverage `services/nlp/` ≥ 85%.
- [ ] Laporan evaluasi model (`docs/eval/sentiment.md`): data uji, F1, confusion matrix, lisensi model.
- [ ] Review disetujui (termasuk tim Marketing untuk sampel label).
