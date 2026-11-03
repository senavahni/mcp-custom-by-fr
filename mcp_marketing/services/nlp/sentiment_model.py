"""Klasifikasi sentimen lokal ONNX (CPU) — model IndoBERT/IndoRoBERTa 3 kelas hasil ekspor.

Isi `SENTIMENT_MODEL_DIR`: `tokenizer.json`, `config.json` (`id2label`), dan `model.onnx` atau
`onnx/model_quantized.onnx`. Fungsi `predict_batch` top-level & picklable agar bisa dijalankan
di `ProcessPoolExecutor` worker (`JobCtx.run_cpu`); sesi ONNX di-cache per proses.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

BATCH = 64
MAX_TOKENS = 128
MODEL_FILES = ("model.onnx", "onnx/model_quantized.onnx", "onnx/model.onnx")
_LABEL_ALIASES = {
    "positive": "positive",
    "pos": "positive",
    "positif": "positive",
    "neutral": "neutral",
    "neu": "neutral",
    "netral": "neutral",
    "negative": "negative",
    "neg": "negative",
    "negatif": "negative",
}


def model_path(model_dir: Path) -> Path | None:
    for name in MODEL_FILES:
        candidate = model_dir / name
        if candidate.is_file():
            return candidate
    return None


def model_available(model_dir: Path) -> bool:
    return (
        model_path(model_dir) is not None
        and (model_dir / "tokenizer.json").is_file()
        and (model_dir / "config.json").is_file()
    )


def model_id(model_dir: Path) -> str:
    """Nama model untuk kunci cache `sentiment_results.model` (berubah bila model diganti)."""
    config = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
    name = config.get("_name_or_path") or model_dir.name
    return f"local:{str(name)[-80:]}"


def labels_from_config(config: dict[str, Any]) -> list[str]:
    id2label = config.get("id2label") or {}
    labels = []
    for i in range(len(id2label)):
        raw = str(id2label.get(str(i), id2label.get(i, ""))).lower()
        if raw not in _LABEL_ALIASES:
            raise ValueError(f"label model tidak dikenal: {raw} (butuh positive/neutral/negative)")
        labels.append(_LABEL_ALIASES[raw])
    if sorted(labels) != ["negative", "neutral", "positive"]:
        raise ValueError("model sentimen harus 3 kelas positive/neutral/negative")
    return labels


@lru_cache(maxsize=2)
def _load(model_dir: str) -> tuple[Any, Any, list[str]]:
    import onnxruntime as ort
    from tokenizers import Tokenizer

    base = Path(model_dir)
    path = model_path(base)
    if path is None:
        raise FileNotFoundError(f"model ONNX tidak ditemukan di {base}")
    tokenizer = Tokenizer.from_file(str(base / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=MAX_TOKENS)
    tokenizer.enable_padding()
    config = json.loads((base / "config.json").read_text(encoding="utf-8"))
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    return tokenizer, session, labels_from_config(config)


def softmax(logits: Any) -> Any:
    shifted = logits - logits.max(axis=1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=1, keepdims=True)


def predict_batch(model_dir: str, texts: list[str]) -> list[tuple[str, dict[str, float]]]:
    """`[(label, {positive, neutral, negative})]` untuk teks yang sudah dinormalisasi."""
    tokenizer, session, labels = _load(model_dir)
    names = {i.name for i in session.get_inputs()}
    out: list[tuple[str, dict[str, float]]] = []
    for start in range(0, len(texts), BATCH):
        encoded = tokenizer.encode_batch(texts[start : start + BATCH])
        ids = np.asarray([e.ids for e in encoded], dtype=np.int64)
        feeds = {
            "input_ids": ids,
            "attention_mask": np.asarray([e.attention_mask for e in encoded], dtype=np.int64),
        }
        if "token_type_ids" in names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        probs = softmax(session.run(None, feeds)[0])
        for row in probs:
            scores = {label: float(p) for label, p in zip(labels, row, strict=True)}
            out.append((max(scores, key=scores.__getitem__), scores))
    return out
