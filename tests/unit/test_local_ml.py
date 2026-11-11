"""MCP-V1-04: klasifikasi lokal (KBLI, kolom, head sentimen, aspek, kontak, tema, kebutuhan).

Embedder palsu bag-of-words (hash kata -> dimensi) sehingga kemiripan = tumpang tindih kata;
cukup untuk menguji logika tanpa Gateway. Akurasi nyata diuji di `tests/eval/v1` (MCP-V1-18).
"""

from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from mcp_marketing.core.errors import ToolError
from mcp_marketing.services.kb.identity import EMBED_LOCAL_MODEL
from mcp_marketing.services.local_ml import columns as col
from mcp_marketing.services.local_ml import head as hd
from mcp_marketing.services.local_ml import kbli, text_tasks, themes
from mcp_marketing.services.local_ml.classifier_store import (
    LocalSentimentHead,
    StoredHead,
    sentiment_head,
)
from mcp_marketing.services.local_ml.contacts import classify_email
from mcp_marketing.services.local_ml.vectors import as_matrix, top_k

DIM = 256
_WORD = re.compile(r"[a-z0-9]+")


class BowEmbedder:
    model = EMBED_LOCAL_MODEL
    dim = DIM

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    @staticmethod
    def vec(text: str) -> list[float]:
        v = np.zeros(DIM, dtype=np.float32)
        for w in _WORD.findall(text.lower()):
            if len(w) > 2:
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
        return v.tolist()

    async def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        self.calls.append((len(texts), kind))
        return [self.vec(t) for t in texts]


# ---------------------------------------------------------------------------- vektor


def test_top_k_and_normalize() -> None:
    assert top_k(np.array([0.1, 0.9, 0.5], dtype=np.float32), 2) == [1, 2]
    m = as_matrix([[3.0, 4.0], [0.0, 0.0]])
    assert np.allclose(np.linalg.norm(m[0]), 1.0) and np.allclose(m[1], 0.0)


# ---------------------------------------------------------------------------- KBLI


INDUSTRIES = [
    ("85131", "pendidikan menengah umum swasta", "sekolah menengah atas sma swasta"),
    ("86103", "aktivitas rumah sakit swasta", "rumah sakit umum swasta rawat inap"),
    ("62019", "aktivitas pemrograman komputer lainnya", "pengembangan perangkat lunak aplikasi"),
]


def _index() -> kbli.KbliIndex:
    emb = BowEmbedder()
    texts = [kbli.industry_text(c, n, d) for c, n, d in INDUSTRIES]
    return kbli.KbliIndex(
        [c for c, _, _ in INDUSTRIES],
        [n for _, n, _ in INDUSTRIES],
        ["8521", "8610", "6201"],
        [emb.vec(t) for t in texts],
    )


async def test_kbli_top3_and_query_input_type() -> None:
    emb = BowEmbedder()
    text = kbli.company_text("RS Santo Borromeus", "rumah sakit swasta", "rawat inap umum")
    [res] = await _index().classify([text], emb)
    assert res.best is not None and res.best.kbli_code == "86103"
    assert res.best.isic_code == "8610"
    assert len(res.candidates) == 3
    assert emb.calls == [(1, "query")]


def test_kbli_decision_thresholds() -> None:
    assert kbli.decide([0.70, 0.60]) == "auto"
    assert kbli.decide([0.70, 0.67]) == "review"  # selisih < 0,05
    assert kbli.decide([0.50, 0.10]) == "review"  # skor < 0,55
    assert kbli.decide([]) == "review"


def test_kbli_index_requires_embeddings() -> None:
    with pytest.raises(ToolError, match="industries-embed"):
        kbli.KbliIndex([], [], [], [])


# ---------------------------------------------------------------------------- kolom

TABLE = {
    "Nama Instansi": ["SMA Negeri 3 Bandung", "RSUD Kota Bandung", "Dinas Pendidikan Kota Cimahi"],
    "Alamat": ["Jl. Belitung No. 8", "Jl. Rumah Sakit No. 22", "Jl. Rd. Demang Hardjakusumah"],
    "Kota": ["Bandung", "Bandung", "Cimahi"],
    "Provinsi": ["Jawa Barat", "Jawa Barat", "Jawa Barat"],
    "Telp": ["022-4203220", "(022) 7811794", "+62 22 6654321"],
    "E-mail": ["info@sman3-bdg.sch.id", "rsud@bandung.go.id", "disdik@cimahikota.go.id"],
    "Situs": ["https://sman3-bdg.sch.id", "www.rsud.bandung.go.id", "disdik.cimahikota.go.id"],
    "Kode Pos": ["40114", "40293", "40512"],
    "ID Internal": ["A-001", "A-002", "A-003"],
}
REGIONS = col.RegionVocab(
    cities=frozenset({"bandung", "cimahi", "kota bandung"}),
    provinces=frozenset({"jawa barat", "jabar"}),
)
EXPECTED = {
    "Nama Instansi": "name",
    "Alamat": "address_raw",
    "Kota": "city",
    "Provinsi": "province",
    "Telp": "phone",
    "E-mail": "email",
    "Situs": "website",
    "Kode Pos": "postal_code",
}


@pytest.mark.parametrize("use_embedder", [False, True])
async def test_column_mapping_pattern_and_semantic(use_embedder: bool) -> None:
    profiles = [col.ColumnProfile(h, v) for h, v in TABLE.items()]
    mapping = await col.map_columns(
        profiles, BowEmbedder() if use_embedder else None, regions=REGIONS
    )
    got = {m.source_col: m.field for m in mapping}
    for header, field in EXPECTED.items():
        assert got[header] == field, (header, got[header])
    assert got["ID Internal"] is None  # tidak dikenal -> extra
    assert len({m.field for m in mapping if m.field}) == sum(1 for m in mapping if m.field)


def test_pattern_scores_empty_column() -> None:
    assert set(col.pattern_scores([]).values()) == {0.0}


# ---------------------------------------------------------------------------- head


def _blobs(n: int = 20) -> tuple[list[list[float]], list[str]]:
    rng = np.random.default_rng(0)
    centers = {"positive": [1, 0, 0], "neutral": [0, 1, 0], "negative": [0, 0, 1]}
    vecs, labels = [], []
    for lab, c in centers.items():
        for _ in range(n):
            vecs.append((np.array(c, np.float32) + rng.normal(0, 0.15, 3)).tolist())
            labels.append(lab)
    return vecs, labels


def test_head_trains_and_scores_separable_data() -> None:
    vecs, labels = _blobs()
    train_idx, test_idx = hd.stratified_split(labels)
    assert set(train_idx).isdisjoint(test_idx) and len(test_idx) == 12
    model = hd.train([vecs[i] for i in train_idx], [labels[i] for i in train_idx])
    pred = [p[0] for p in model.predict([vecs[i] for i in test_idx])]
    metrics = hd.macro_f1([labels[i] for i in test_idx], pred, model.labels)
    assert metrics["macro_f1"] >= 0.95
    restored = hd.SoftmaxHead.from_json(model.to_json())
    assert restored.predict(vecs[:3]) == model.predict(vecs[:3])
    label, probs = model.predict([vecs[0]])[0]
    assert label == "positive" and abs(sum(probs.values()) - 1) < 1e-3


def test_head_rejects_thin_data_and_wrong_dim() -> None:
    with pytest.raises(ToolError):
        hd.train([[1.0, 0.0]] * 3 + [[0.0, 1.0]] * 3, ["a"] * 3 + ["b"] * 3)
    vecs, labels = _blobs(6)
    model = hd.train(vecs, labels)
    with pytest.raises(ToolError):
        model.proba([[0.1] * 5])


def test_macro_f1_counts_missing_class_as_zero() -> None:
    m = hd.macro_f1(["a", "a", "b"], ["a", "a", "a"], ["a", "b"])
    assert m["per_label"]["b"]["f1"] == 0.0 and m["macro_f1"] == pytest.approx(0.4, abs=1e-3)


async def test_sentiment_head_injected_and_used_with_zero_llm() -> None:
    from mcp_marketing.services.nlp.sentiment import Item, SentimentAnalyzer

    emb = BowEmbedder()
    texts = ["mantap layanan cepat", "biasa saja", "kecewa lambat sekali"]
    labels_ = ["positive", "neutral", "negative"]
    vecs = [emb.vec(t) for t in texts * 5]
    model = hd.train(vecs, labels_ * 5, l2=1e-4)
    stored = StoredHead("sentiment", 2, EMBED_LOCAL_MODEL, DIM, model, {})
    head = LocalSentimentHead(stored, emb)
    nlp = SimpleNamespace(sentiment_model_dir="tidak-ada", sentiment_llm_threshold=0.0)
    services = SimpleNamespace(
        settings=SimpleNamespace(nlp=nlp), llm=None, db=object(), extras={"sentiment_head": head}
    )
    analyzer = SentimentAnalyzer(services)

    async def no_cache(*_: Any, **__: Any) -> dict[str, Any]:
        return {}

    async def no_put(*_: Any, **__: Any) -> None:
        return None

    analyzer._cache_get = no_cache  # type: ignore[method-assign]
    analyzer._cache_put = no_put  # type: ignore[method-assign]
    out = await analyzer.analyze(
        [Item(key=str(i), text=t) for i, t in enumerate(texts)], method="model", language="id"
    )
    assert [out.verdicts[str(i)].label for i in range(3)] == labels_
    assert out.cost["llm_tokens"] == 0 and out.cost["items_model"] == 3
    assert stored.model_key == f"head:sentiment:v2:{EMBED_LOCAL_MODEL}"


async def test_sentiment_head_none_without_db() -> None:
    assert await sentiment_head(SimpleNamespace(extras={}, db=None)) is None


def test_local_head_rejects_other_embedder() -> None:
    model = hd.train(*_blobs(6))
    stored = StoredHead("sentiment", 1, EMBED_LOCAL_MODEL, 3, model, {})
    with pytest.raises(ToolError):
        LocalSentimentHead(stored, BowEmbedder())  # dim 256 != 3


# ---------------------------------------------------------------------------- aspek & kebutuhan


async def test_match_aspects_threshold() -> None:
    hits = await text_tasks.match_aspects(
        ["harga paketnya terlalu mahal", "cuaca hari ini cerah"],
        {"harga": "harga mahal murah biaya", "layanan": "pelayanan admin respon"},
        BowEmbedder(),
        threshold=0.3,
    )
    assert hits[0][0][0] == "harga" and hits[1] == []


async def test_extract_needs_returns_original_quotes() -> None:
    page = (
        "Selamat datang di website resmi sekolah kami yang berdiri sejak 1950. "
        "Saat ini kami membutuhkan sistem untuk mengelola data siswa dan alumni secara terpadu. "
        "Tahun depan kami berencana melakukan digitalisasi layanan administrasi sekolah."
    )
    quotes = await text_tasks.extract_needs(page, BowEmbedder(), threshold=0.4)
    cats = {q.category for q in quotes}
    assert {"need", "plan"} <= cats
    assert all(q.quote in page for q in quotes)  # ekstraktif: kutipan asli
    assert all(len(q.quote) <= text_tasks.QUOTE_MAX_CHARS for q in quotes)


def test_split_sentences_drops_short_fragments() -> None:
    assert text_tasks.split_sentences("Ok. Ini kalimat yang cukup panjang untuk dipakai.") == [
        "Ini kalimat yang cukup panjang untuk dipakai."
    ]


# ---------------------------------------------------------------------------- kontak


@pytest.mark.parametrize(
    ("email", "kind"),
    [
        ("info@sman3-bdg.sch.id", "generic"),
        ("humas@rsud.go.id", "generic"),
        ("hrd@contoh.co.id", "role"),
        ("kepsek@sekolah.sch.id", "role"),
        ("budi.santoso@contoh.co.id", "personal"),
        ("siti@contoh.co.id", "personal"),
        ("a.wijaya@contoh.co.id", "personal"),
        ("sman3bdg2019@gmail.com", "generic"),
    ],
)
def test_contact_type_rules(email: str, kind: str) -> None:
    assert classify_email(email).contact_type == kind


def test_unknown_local_part_treated_as_personal_for_review() -> None:
    res = classify_email("xyzq@contoh.co.id")
    assert res.contact_type == "personal" and res.needs_review


# ---------------------------------------------------------------------------- tema


def test_cluster_themes_two_groups_with_ctfidf_labels() -> None:
    emb = BowEmbedder()
    texts = [
        "harga paket internet mahal sekali",
        "harga paket internet naik lagi",
        "paket internet harga mahal",
        "sinyal internet lemot di malam hari",
        "sinyal lemot terus tiap malam",
        "sinyal lemot sekali malam ini",
    ]
    result = themes.cluster_themes(texts, [emb.vec(t) for t in texts], k=2)
    assert sorted(t.size for t in result) == [3, 3]
    labels = " ".join(t.label for t in result)
    assert "harga" in labels and ("sinyal" in labels or "lemot" in labels)
    assert sum(t.share for t in result) == pytest.approx(1.0)


def test_choose_k_bounds() -> None:
    assert themes.choose_k(3) == 1 and themes.choose_k(50) == 5 and themes.choose_k(10_000) == 12
