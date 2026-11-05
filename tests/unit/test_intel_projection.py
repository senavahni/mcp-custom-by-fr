"""Unit test proyeksi PCA 3D (M3_TASK_011): murni numpy, tanpa DB."""

from __future__ import annotations

import numpy as np
import pytest

from mcp_marketing.services.intel.projection import pca_3d


def test_kosong_dan_satu_titik():
    assert pca_3d([]) == []
    assert pca_3d([[1.0, 2.0, 3.0, 4.0]]) == [(0.0, 0.0, 0.0)]


def test_dua_titik_hanya_sumbu_pertama():
    out = pca_3d([[0.0, 0.0], [3.0, 4.0]])
    assert len(out) == 2
    assert out[0][1:] == (0.0, 0.0) and out[1][1:] == (0.0, 0.0)
    # jarak antar titik terjaga (5.0) pada komponen utama
    assert abs(out[1][0] - out[0][0]) == pytest.approx(5.0)


def test_dimensi_kurang_dari_tiga_diisi_nol():
    out = pca_3d([[1.0, 0.0], [0.0, 1.0], [2.0, 3.0], [4.0, 1.0]])
    assert all(len(p) == 3 for p in out)
    assert all(p[2] == 0.0 for p in out)


def test_data_identik_menghasilkan_nol():
    assert pca_3d([[1.0, 2.0, 3.0]] * 5) == [(0.0, 0.0, 0.0)] * 5


def test_hasil_terpusat_dan_variansi_menurun():
    rng = np.random.default_rng(0)
    scales = np.array([10.0, 5.0, 2.0, 0.5, 0.1, 0.1])
    data = rng.normal(size=(200, 6)) * scales
    out = np.array(pca_3d(data.tolist()))
    assert out.shape == (200, 3)
    assert np.allclose(out.mean(axis=0), 0.0, atol=1e-9)
    variances = out.var(axis=0)
    assert variances[0] > variances[1] > variances[2]


def test_mempertahankan_jarak_pada_data_berpangkat_tiga():
    rng = np.random.default_rng(1)
    basis = np.linalg.qr(rng.normal(size=(50, 3)))[0].T  # 3 sumbu ortonormal dalam 50-D
    data = rng.normal(size=(40, 3)) @ basis
    out = np.array(pca_3d(data.tolist()))
    original = np.linalg.norm(data[0] - data[1])
    projected = np.linalg.norm(out[0] - out[1])
    assert projected == pytest.approx(original, rel=1e-6)


def test_deterministik_dan_tanda_stabil():
    rng = np.random.default_rng(2)
    data = rng.normal(size=(30, 8)).tolist()
    first = pca_3d(data)
    assert pca_3d(data) == first
    # urutan baris diubah -> koordinat tiap titik sama (tanda komponen tidak terbalik)
    shuffled = pca_3d(list(reversed(data)))
    assert np.allclose(np.array(shuffled)[::-1], np.array(first), atol=1e-9)


def test_dua_klaster_terpisah_pada_komponen_pertama():
    rng = np.random.default_rng(3)
    a = rng.normal(loc=0.0, scale=0.1, size=(25, 10))
    b = rng.normal(loc=5.0, scale=0.1, size=(25, 10))
    out = np.array(pca_3d(np.vstack([a, b]).tolist()))
    assert abs(out[:25, 0].mean() - out[25:, 0].mean()) > 10


def test_kinerja_2000_titik_embedding():
    rng = np.random.default_rng(4)
    out = pca_3d(rng.normal(size=(2000, 256)).tolist())
    assert len(out) == 2000


def test_dimensi_tidak_seragam_ditolak():
    with pytest.raises(ValueError, match="berdimensi sama"):
        pca_3d([[1.0, 2.0], [1.0]])


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_nilai_non_finite_ditolak(bad: float):
    with pytest.raises(ValueError, match="non-finite"):
        pca_3d([[1.0, 2.0], [bad, 0.0]])


def test_tipe_keluaran_tuple_float():
    out = pca_3d([[1.0, 2.0, 3.0], [3.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    assert all(isinstance(p, tuple) and all(isinstance(c, float) for c in p) for p in out)
