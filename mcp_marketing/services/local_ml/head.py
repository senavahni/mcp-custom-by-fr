"""Head klasifikasi softmax (multinomial logistic regression) di atas embedding — numpy murni.

Dipakai sentimen lokal (MCP-V1-04): dataset 300–500 post berlabel, embedding embed-local 1024.
Pelatihan full-batch gradient descent + L2, deterministik (tanpa sampling acak), bobot kelas
seimbang agar kelas minor (mis. negatif) tidak tenggelam. Diserialisasi ke JSON
(`mcp.local_classifiers.params`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.services.local_ml.vectors import Matrix, as_matrix

DEFAULT_L2 = 1e-3
DEFAULT_LR = 2.0
DEFAULT_EPOCHS = 400
MIN_PER_LABEL = 5


@dataclass
class SoftmaxHead:
    labels: list[str]
    weights: NDArray[np.float32]  # dim x kelas
    bias: NDArray[np.float32]  # kelas

    def proba(self, vectors: Sequence[Sequence[float]] | Matrix) -> NDArray[np.float32]:
        x = as_matrix(vectors)
        if x.shape[1] != self.weights.shape[0]:
            raise ValidationFailed(
                f"Dimensi embedding {x.shape[1]} != dimensi head {self.weights.shape[0]}"
            )
        return _softmax(x @ self.weights + self.bias)

    def predict(
        self, vectors: Sequence[Sequence[float]] | Matrix
    ) -> list[tuple[str, dict[str, float]]]:
        """Format sama dengan `sentiment_model.predict_batch`: `(label, {label: prob})`."""
        out = []
        for row in self.proba(vectors):
            probs = {lab: round(float(p), 4) for lab, p in zip(self.labels, row, strict=True)}
            out.append((self.labels[int(np.argmax(row))], probs))
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "labels": self.labels,
            "weights": self.weights.round(6).tolist(),
            "bias": self.bias.round(6).tolist(),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SoftmaxHead:
        return cls(
            labels=list(data["labels"]),
            weights=np.asarray(data["weights"], dtype=np.float32),
            bias=np.asarray(data["bias"], dtype=np.float32),
        )


def _softmax(z: NDArray[np.float32]) -> NDArray[np.float32]:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return (e / e.sum(axis=1, keepdims=True)).astype(np.float32)


def train(
    vectors: Sequence[Sequence[float]] | Matrix,
    labels: Sequence[str],
    *,
    label_order: Sequence[str] | None = None,
    l2: float = DEFAULT_L2,
    lr: float = DEFAULT_LR,
    epochs: int = DEFAULT_EPOCHS,
) -> SoftmaxHead:
    x = as_matrix(vectors)
    classes = list(label_order or sorted(set(labels)))
    counts = {c: sum(1 for y in labels if y == c) for c in classes}
    thin = [c for c, n in counts.items() if n < MIN_PER_LABEL]
    if len(classes) < 2 or thin:
        raise ValidationFailed(
            f"Data latih kurang: minimal {MIN_PER_LABEL} contoh per label", details=counts
        )
    index = {c: i for i, c in enumerate(classes)}
    y = np.zeros((len(labels), len(classes)), dtype=np.float32)
    y[np.arange(len(labels)), [index[v] for v in labels]] = 1.0
    sample_w = np.array([len(labels) / (len(classes) * counts[v]) for v in labels], np.float32)
    w = np.zeros((x.shape[1], len(classes)), dtype=np.float32)
    b = np.zeros(len(classes), dtype=np.float32)
    norm = float(sample_w.sum())
    for _ in range(epochs):
        grad = (_softmax(x @ w + b) - y) * sample_w[:, None] / norm
        w -= lr * (x.T @ grad + l2 * w)
        b -= lr * grad.sum(axis=0)
    return SoftmaxHead(classes, w, b)


def macro_f1(truth: Sequence[str], pred: Sequence[str], labels: Sequence[str]) -> dict[str, Any]:
    per: dict[str, dict[str, float]] = {}
    for lab in labels:
        tp = sum(1 for t, p in zip(truth, pred, strict=True) if t == lab and p == lab)
        fp = sum(1 for t, p in zip(truth, pred, strict=True) if t != lab and p == lab)
        fn = sum(1 for t, p in zip(truth, pred, strict=True) if t == lab and p != lab)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per[lab] = {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4)}
    accuracy = sum(1 for t, p in zip(truth, pred, strict=True) if t == p) / max(1, len(truth))
    return {
        "macro_f1": round(float(np.mean([v["f1"] for v in per.values()])), 4),
        "accuracy": round(accuracy, 4),
        "per_label": per,
        "n": len(truth),
    }


def stratified_split(labels: Sequence[str], test_ratio: float = 0.2) -> tuple[list[int], list[int]]:
    """Split deterministik per label (tiap k-ke-n masuk test) -> (train_idx, test_idx)."""
    every = max(2, round(1 / test_ratio))
    seen: dict[str, int] = {}
    train_idx: list[int] = []
    test_idx: list[int] = []
    for i, lab in enumerate(labels):
        seen[lab] = seen.get(lab, 0) + 1
        (test_idx if seen[lab] % every == 0 else train_idx).append(i)
    return train_idx, test_idx
