"""Proyeksi PCA 3 komponen untuk visualisasi 3D awan titik (M3_TASK_011).

Fungsi murni: tidak membaca DB/jaringan, sehingga mudah diuji. Pemanggilan (tool
`intel_kb_projection`, cache per job) menyusul setelah TASK_010 selesai.
"""

from __future__ import annotations

import numpy as np

COMPONENTS = 3


def pca_3d(vectors: list[list[float]]) -> list[tuple[float, float, float]]:
    """Proyeksikan `vectors` (n × d) ke 3 komponen utama: center lalu SVD.

    - Kosong -> `[]`; satu titik -> `[(0, 0, 0)]`.
    - Bila komponen tersedia < 3 (n < 3, d < 3, atau data berpangkat rendah), sumbu sisanya 0.0.
    - Tanda tiap komponen dinormalkan (loading terbesar positif) agar hasil deterministik.
    - `ValueError` bila dimensi vektor tidak seragam atau ada nilai non-finite.
    """
    if not vectors:
        return []
    dims = {len(v) for v in vectors}
    if len(dims) != 1:
        raise ValueError("semua vektor harus berdimensi sama")
    matrix = np.asarray(vectors, dtype=np.float64)
    if matrix.ndim != 2:
        raise ValueError("vektor harus berupa daftar angka")
    if not np.isfinite(matrix).all():
        raise ValueError("vektor memuat nilai non-finite")

    n, d = matrix.shape
    out = np.zeros((n, COMPONENTS), dtype=np.float64)
    if n > 1 and d > 0:
        centered = matrix - matrix.mean(axis=0)
        _, singular, components = np.linalg.svd(centered, full_matrices=False)
        k = min(COMPONENTS, len(singular))
        # Komponen dengan nilai singular ~0 tidak membawa informasi: biarkan 0.0
        tolerance = singular[0] * max(n, d) * np.finfo(np.float64).eps if k else 0.0
        for i in range(k):
            if singular[i] <= tolerance:
                break
            axis = components[i]
            if axis[np.argmax(np.abs(axis))] < 0:
                axis = -axis
            out[:, i] = centered @ axis
    return [(float(x), float(y), float(z)) for x, y, z in out]
