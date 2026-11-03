"""TASK-01: package bisa diimpor, Python 3.11 (sama dengan LLM Gateway), secret scan bekerja."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_python_version_matches_gateway() -> None:
    assert sys.version_info[:2] == (3, 11)


def test_package_imports() -> None:
    import mcp_marketing
    from mcp_marketing import main  # noqa: F401

    assert mcp_marketing.SERVER_NAME == "mcp-marketing"


def _secret_scan():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "secret_scan", ROOT / "scripts" / "secret_scan.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_secret_scan_detects_fake_keys(tmp_path: Path) -> None:
    scan = _secret_scan()
    # dibangun dinamis agar file test ini sendiri tidak memicu scan repo
    fakes = {
        "gateway.txt": "lgw_" + "abcd1234_" + "A" * 43,
        "client.py": "KEY = 'mcpk_" + "zyxw9876_" + "b" * 43 + "'",
        "aws.cfg": "AKIA" + "ABCDEFGHIJKLMNOP",
        "openai.md": "sk-" + "x" * 32,
    }
    for name, content in fakes.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    assert scan.main(["--path", str(tmp_path)]) == 1
    findings = scan.scan_files(scan.iter_repo_files(tmp_path))
    assert len(findings) == 4 and all("***" in f for f in findings)


def test_secret_scan_clean_dir_and_ignores_env(tmp_path: Path) -> None:
    scan = _secret_scan()
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("K=" + "lgw_" + "abcd1234_" + "A" * 43, encoding="utf-8")
    assert scan.main(["--path", str(tmp_path)]) == 0
