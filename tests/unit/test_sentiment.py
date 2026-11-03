"""TASK-20 unit: normalisasi, bahasa, parser LLM toleran, eskalasi, aspek, agregasi, model."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from mcp_marketing.core.errors import UpstreamError
from mcp_marketing.services.nlp import aggregate, sentiment_llm
from mcp_marketing.services.nlp.aspects import spot, synonyms
from mcp_marketing.services.nlp.lang import detect
from mcp_marketing.services.nlp.normalize import normalize, slang_table, split_hashtag
from mcp_marketing.services.nlp.sentiment import (
    Item,
    SentimentAnalyzer,
    is_sarcastic,
)
from mcp_marketing.services.nlp.sentiment_model import (
    labels_from_config,
    model_available,
    model_id,
    softmax,
)
from tests.fakes import SentimentLLM


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Kopinya enak bgt", "kopinya enak banget"),
        ("gk enak", "tidak enak"),
        ("ga recomended", "tidak rekomendasi"),
        ("mantul pokoknya", "mantap betul pokoknya"),
        ("bagusss", "bagus"),
        ("enaaaak", "enak"),
        ("lamaaa bgt", "lama banget"),
        ("#KopiSusu", "kopi susu"),
        ("#promo_akhir_tahun", "promo akhir tahun"),
        ("#Diskon50", "diskon 50"),
        ("@kedaikopi mantap", "@user mantap"),
        ("cek https://kopi.id/promo", "cek <url>"),
        ("www.kopi.id keren", "<url> keren"),
        ("enak 😍", "enak emoji_pos"),
        ("kecewa 😡😡", "kecewa emoji_neg emoji_neg"),
        ("oke 👍", "oke emoji_pos"),
        ("worth it bgt", "sepadan banget"),
        ("free ongkir dong", "gratis ongkos kirim dong"),
        ("fast respon admin", "cepat tanggap admin"),
        ("gw suka", "saya suka"),
        ("udh nyobain", "sudah mencoba"),
        ("blm dtg", "belum datang"),
        ("tp mahal", "tapi mahal"),
        ("wkwkwk lucu", "tertawa lucu"),
        ("thx min", "terima kasih admin"),
        ("ongkir mahal", "ongkos kirim mahal"),
        ("kurirnya telat", "kurirnya terlambat"),
        ("ROMBONGAN Bagus", "rombongan bagus"),
        ("  spasi   berlebih  ", "spasi berlebih"),
        ("harga 15k", "harga 15k"),
    ],
)
def test_normalize_cases(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_slang_dictionary_size() -> None:
    single, phrases = slang_table()
    assert len(single) + len(phrases) >= 500


def test_split_hashtag_and_detect() -> None:
    assert split_hashtag("KopiSusuGulaAren") == "kopi susu gula aren"
    assert detect(normalize("Pelayanannya lamaaa bgt, gk recomended")) == "id"
    assert detect("This coffee is really great, I love it") == "en"
    assert detect("Me gusta mucho este cafe de la tienda") == "other"
    assert detect("ok") == "id"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("1|pos|0.9\n2|neg|0.8\n3|neu|0.6", {0: ("positive", 0.9), 1: ("negative", 0.8)}),
        ("1: positif ; 0,7\n2 | NEGATIVE", {0: ("positive", 0.7), 1: ("negative", 0.75)}),
        ("Berikut hasilnya:\n1|pos|0.9\nrusak\n9|neg|0.9", {0: ("positive", 0.9)}),
        ("1|netral|0.55\n1|pos|0.9", {0: ("neutral", 0.55)}),
    ],
)
def test_parse_sentiment_lines_tolerant(content: str, expected: dict[int, Any]) -> None:
    parsed = sentiment_llm.parse_sentiment_lines(content, 3)
    for idx, value in expected.items():
        assert parsed[idx] == value


def test_parse_aspect_lines() -> None:
    parsed = sentiment_llm.parse_aspect_lines("1|harga|neg\n2 | rasa | positif\nx", 2)
    assert parsed == {0: ("harga", "negative"), 1: ("rasa", "positive")}


async def test_llm_classify_retries_missing_lines_once() -> None:
    llm = SentimentLLM(drop={2})
    result = await sentiment_llm.classify(llm, ["enak", "kecewa berat", "biasa"])
    assert result.labels == {
        0: ("positive", 0.88),
        1: ("negative", 0.88),
        2: ("positive", 0.88),
    }
    assert result.calls == 2 and result.tokens == 240
    assert "1|kecewa berat" in llm.calls[1]  # hanya baris yang hilang dikirim ulang


async def test_llm_classify_batches_of_50() -> None:
    llm = SentimentLLM()
    result = await sentiment_llm.classify(llm, [f"teks {i}" for i in range(120)])
    assert result.calls == 3 and len(result.labels) == 120


def test_sarcasm_heuristic() -> None:
    assert is_sarcastic(normalize("katanya enak, wkwk"))
    assert not is_sarcastic(normalize("enak banget"))
    assert not is_sarcastic(normalize("wkwk lucu"))


def test_aspect_spotting() -> None:
    found = spot(normalize("Rasanya enak. Tapi ongkir mahal bgt! Kurir telat."), ["harga", "rasa"])
    assert found == [("harga", "tapi ongkos kirim mahal banget !"), ("rasa", "rasanya enak .")]
    assert synonyms("Desain") == ("desain",)


def test_aggregations() -> None:
    rows = [
        {
            "label": "positive",
            "posted_at": "2026-10-01T02:00:00+00:00",
            "keywords": ["a"],
            "likes": 10,
            "text": "x",
            "aspects": [{"aspect": "harga", "label": "negative"}],
        },
        {
            "label": "negative",
            "posted_at": "2026-10-01T20:00:00+00:00",
            "keywords": ["a", "b"],
            "likes": 1,
            "text": "y",
        },
        {"label": "neutral", "posted_at": "2026-10-02T02:00:00+00:00", "keywords": ["b"]},
    ]
    dist = aggregate.distribution(rows)
    assert dist["counts"] == {"positive": 1, "neutral": 1, "negative": 1}
    assert dist["net_sentiment"] == 0.0
    by_day = aggregate.by_group(rows, "day", "Asia/Jakarta")
    assert [r[0] for r in by_day["rows"]] == ["2026-10-01", "2026-10-02"]
    assert by_day["rows"][1][1] == 2  # 20:00 UTC = 03:00 WIB hari berikutnya
    by_kw = aggregate.by_group(rows, "keyword", "Asia/Jakarta")
    assert by_kw["rows"][0][:2] in (["a", 2], ["b", 2])
    assert aggregate.aspects_table(rows)["rows"] == [["harga", 1, 0, 0, 1, -1.0]]
    assert aggregate.examples(rows)["positive"][0]["engagement"] == 10
    later = aggregate.distribution(rows[:1])
    assert aggregate.compare(later, dist)["net_sentiment_delta"] == 1.0
    assert aggregate.distribution([])["net_sentiment"] == 0.0


def test_model_helpers(tmp_path: Path) -> None:
    assert not model_available(tmp_path)
    cfg = {"id2label": {"0": "positive", "1": "neutral", "2": "negative"}, "_name_or_path": "m"}
    assert labels_from_config(cfg) == ["positive", "neutral", "negative"]
    with pytest.raises(ValueError):
        labels_from_config({"id2label": {"0": "LABEL_0", "1": "x", "2": "y"}})
    probs = softmax(np.array([[2.0, 1.0, 0.0]]))
    assert probs.sum() == pytest.approx(1.0) and probs[0][0] > probs[0][1]
    _fake_model_dir(tmp_path)
    assert model_available(tmp_path) and model_id(tmp_path) == "local:fake/indobert-sentiment"


def _fake_model_dir(path: Path) -> None:
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    (path / "model.onnx").write_bytes(b"")
    (path / "config.json").write_text(
        json.dumps(
            {
                "_name_or_path": "fake/indobert-sentiment",
                "id2label": {"0": "positive", "1": "neutral", "2": "negative"},
            }
        ),
        encoding="utf-8",
    )


class MemoryCache:
    """Pengganti tabel sentiment_results untuk test unit."""

    def __init__(self, analyzer: SentimentAnalyzer) -> None:
        self.rows: dict[tuple[str, str], Any] = {}

        async def get(model: str, hashes: list[str]) -> dict[str, Any]:
            return {h: self.rows[(model, h)] for h in hashes if (model, h) in self.rows}

        async def put(model: str, fresh: dict[str, Any], unique: dict[str, Any]) -> None:
            for h, v in fresh.items():
                self.rows[(model, h)] = v

        async def aspects(model: str, value: dict[str, Any]) -> None:
            return None

        analyzer._cache_get = get  # type: ignore[method-assign]
        analyzer._cache_put = put  # type: ignore[method-assign]
        analyzer._cache_aspects = aspects  # type: ignore[method-assign]


def _analyzer(tmp_path: Path, llm: Any, *, with_model: bool) -> SentimentAnalyzer:
    if with_model:
        _fake_model_dir(tmp_path)
    nlp = SimpleNamespace(sentiment_model_dir=tmp_path, sentiment_llm_threshold=0.6)
    services = SimpleNamespace(settings=SimpleNamespace(nlp=nlp), llm=llm, db=None)

    async def run_cpu(fn: Any, model_dir: str, texts: list[str]) -> list[Any]:
        out = []
        for t in texts:
            if "ragu" in t:
                out.append(("positive", {"positive": 0.4, "neutral": 0.35, "negative": 0.25}))
            elif "kecewa" in t:
                out.append(("negative", {"positive": 0.05, "neutral": 0.05, "negative": 0.9}))
            else:
                out.append(("positive", {"positive": 0.9, "neutral": 0.05, "negative": 0.05}))
        return out

    analyzer = SentimentAnalyzer(services, run_cpu=run_cpu)
    MemoryCache(analyzer)
    return analyzer


async def test_pipeline_escalates_only_uncertain_and_sarcastic(tmp_path: Path) -> None:
    llm = SentimentLLM()
    analyzer = _analyzer(tmp_path, llm, with_model=True)
    items = [
        Item("1", "Kopinya enak bgt"),
        Item("2", "Kopinya enak bgt"),  # duplikat (retweet) -> sekali saja
        Item("3", "agak ragu sama rasanya"),
        Item("4", "katanya enak, wkwk"),
        Item("5", "kecewa, kurirnya telat"),
        Item("6", "Me gusta mucho este cafe de la tienda"),
    ]
    out = await analyzer.analyze(items, aspects=["pengiriman"])
    assert out.cost["items_model"] == 4  # 4 teks unik berbahasa id (duplikat & es tidak)
    assert out.cost["items_llm"] == 3  # ragu + sarkas + bahasa lain
    assert out.verdicts["3"].method == "hybrid" and out.verdicts["6"].method == "llm"
    assert out.verdicts["1"].method == "model" and out.verdicts["2"].label == "positive"
    assert out.verdicts["5"].aspects == [{"aspect": "pengiriman", "label": "negative"}]
    again = await analyzer.analyze(items)
    assert again.cost["cache_hits"] == 6 and again.cost["llm_tokens"] == 0


async def test_pipeline_without_model(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path, SentimentLLM(), with_model=False)
    out = await analyzer.analyze([Item("1", "enak"), Item("2", "mahal bgt")])
    assert "model_unavailable_fallback_llm" in out.notes
    assert out.verdicts["2"].label == "negative" and out.cost["items_model"] == 0
    with pytest.raises(UpstreamError, match="download-models"):
        await analyzer.analyze([Item("1", "enak")], method="model")
