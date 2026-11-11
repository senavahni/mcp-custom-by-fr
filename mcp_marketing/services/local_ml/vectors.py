"""Utilitas vektor numpy (tanpa dependensi berat): normalisasi, kosinus, top-k."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

Matrix = NDArray[np.float32]


def as_matrix(vectors: Sequence[Sequence[float]] | Matrix) -> Matrix:
    """Baris dinormalisasi L2 (vektor nol tetap nol)."""
    m = np.asarray(vectors, dtype=np.float32)
    if m.ndim == 1:
        m = m[None, :]
    if m.size == 0:
        return m.reshape(0, m.shape[-1] if m.ndim == 2 else 0)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


def cosine(a: Matrix, b: Matrix) -> Matrix:
    """Matriks kosinus `len(a) x len(b)`; input harus sudah dinormalisasi (`as_matrix`)."""
    return (a @ b.T).astype(np.float32)


def top_k(scores: NDArray[np.float32], k: int) -> list[int]:
    """Indeks `k` skor tertinggi, urut menurun (stabil untuk skor sama)."""
    k = min(k, scores.shape[0])
    if k <= 0:
        return []
    idx = np.argpartition(-scores, k - 1)[:k]
    return sorted((int(i) for i in idx), key=lambda i: (-float(scores[i]), i))
