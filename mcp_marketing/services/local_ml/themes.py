"""Klaster tema lokal: k-means (k-means++ deterministik) atas embedding + label c-TF-IDF.

Label = kata paling khas klaster dibanding klaster lain (class-based TF-IDF), bukan buatan LLM.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from mcp_marketing.services.local_ml.vectors import Matrix, as_matrix

MAX_K = 12
MIN_K = 2
KMEANS_ITERS = 50
LABEL_WORDS = 3
_WORD = re.compile(r"[a-zA-Z][a-zA-Z\-]{2,}")
STOPWORDS = frozenset(
    """
    yang dan di ke dari untuk dengan ini itu ada tidak atau juga akan pada dalam karena jadi
    sudah saya kami kita kamu anda dia mereka aku gue lo nya kalau kalo bisa lebih sangat
    banget aja saja sih dong deh kok yg dgn utk tdk gak ga nggak enggak udah belum masih
    seperti oleh agar supaya bagi tapi tetapi namun hanya cuma lagi pun para sebagai adalah
    the and for with this that are was you your have has not but from they our
    https http www com amp rt
    """.split()
)


@dataclass(frozen=True)
class Theme:
    cluster: int
    label: str
    keywords: list[str]
    size: int
    share: float
    members: list[int]  # indeks dokumen


def choose_k(n: int) -> int:
    return max(MIN_K, min(MAX_K, round(math.sqrt(n / 2)))) if n >= 2 * MIN_K else 1


def kmeans(x: Matrix, k: int, *, iters: int = KMEANS_ITERS) -> NDArray[np.int64]:
    """K-means kosinus (vektor ternormalisasi), inisialisasi k-means++ deterministik."""
    n = x.shape[0]
    k = max(1, min(k, n))
    centers = [x[0]]
    for _ in range(1, k):
        sims = np.max(np.stack([x @ c for c in centers], axis=1), axis=1)
        centers.append(x[int(np.argmax(1.0 - sims))])  # titik terjauh (deterministik)
    c = np.stack(centers)
    assign = np.zeros(n, dtype=np.int64)
    for _ in range(iters):
        new = np.argmax(x @ c.T, axis=1)
        if np.array_equal(new, assign) and _ > 0:
            break
        assign = new
        for j in range(k):
            members = x[assign == j]
            if len(members):
                v = members.mean(axis=0)
                c[j] = v / (np.linalg.norm(v) or 1.0)
    return assign


def tokens(text: str) -> list[str]:
    return [w for w in (m.lower() for m in _WORD.findall(text)) if w not in STOPWORDS]


def ctfidf_keywords(
    docs_by_cluster: dict[int, list[str]], top_n: int = LABEL_WORDS
) -> dict[int, list[str]]:
    counts = {
        cid: Counter(t for d in docs for t in tokens(d)) for cid, docs in docs_by_cluster.items()
    }
    total = Counter[str]()
    for cnt in counts.values():
        total.update(cnt)
    avg_words = sum(total.values()) / max(1, len(counts))
    out: dict[int, list[str]] = {}
    for cid, cnt in counts.items():
        size = sum(cnt.values()) or 1
        scored = {w: (f / size) * math.log(1 + avg_words / total[w]) for w, f in cnt.items()}
        out[cid] = [w for w, _ in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]]
    return out


def cluster_themes(
    texts: Sequence[str], vectors: Sequence[Sequence[float]] | Matrix, *, k: int | None = None
) -> list[Theme]:
    if not texts:
        return []
    x = as_matrix(vectors)
    assign = kmeans(x, k or choose_k(len(texts)))
    docs: dict[int, list[str]] = {}
    for i, cid in enumerate(assign):
        docs.setdefault(int(cid), []).append(texts[i])
    keywords = ctfidf_keywords(docs)
    themes = [
        Theme(
            cluster=cid,
            label=" / ".join(keywords[cid]) or f"tema {cid + 1}",
            keywords=keywords[cid],
            size=len(members),
            share=round(len(members) / len(texts), 4),
            members=[i for i, a in enumerate(assign) if int(a) == cid],
        )
        for cid, members in docs.items()
    ]
    return sorted(themes, key=lambda t: -t.size)
