"""Secret scan: cari pola secret di file repo (yang akan di-commit) dan file log. Harus 0 temuan.

    python scripts/secret_scan.py                  :: repo + logs/
    python scripts/secret_scan.py --no-logs
    python scripts/secret_scan.py --path some/dir  :: hanya path tertentu

Pola: key OpenAI `sk-...`, Anthropic `sk-ant-...`, Google `AIza...`, AWS `AKIA...`, key Gateway
`lgw_<prefix>_<secret>`, key klien MCP `mcpk_<prefix>_<secret>`, bearer token X (app-only) dan
access token Threads/Meta. Temuan ditampilkan TERSAMAR (6 karakter awal); exit 1 bila ada temuan."""

import argparse
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai": re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_\-]{20,}"),
    "google": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"),
    "aws": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "gateway": re.compile(r"\blgw_[a-z0-9]{8}_[A-Za-z0-9_\-]{43}"),
    "mcp_client": re.compile(r"\bmcpk_[a-z0-9]{8}_[A-Za-z0-9_\-]{43}"),
    "x_bearer": re.compile(r"\bAAAAAAAAAAAAAAAAAAAAA[A-Za-z0-9%]{30,}"),
    "threads_token": re.compile(r"\b(?:THAA|EAA)[A-Za-z0-9]{60,}"),
}
SKIP_DIRS = {
    ".git",
    ".claude",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "browser_profiles",
    "models",
    "storage",
    "bin",
    "logs",
    "htmlcov",
}
SKIP_SUFFIXES = {".zip", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".onnx", ".pyc", ".exe", ".xlsx"}
LOG_SUFFIXES = {".log", ".out", ".err", ".json", ".jsonl", ".txt"}


def mask(secret: str) -> str:
    return secret[:6] + "***"


def scan_text(text: str) -> Iterator[tuple[str, str]]:
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            yield kind, mask(match.group(0))


def _is_env_file(path: Path) -> bool:
    # .env & turunannya tidak di-commit (gitignore); .env.example wajib bersih
    return path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example")


def iter_repo_files(base: Path) -> Iterator[Path]:
    if base.is_file():
        yield base
        return
    for path in sorted(base.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(base).parts):
            continue
        if path.is_file() and path.suffix.lower() not in SKIP_SUFFIXES and not _is_env_file(path):
            yield path


def iter_log_files(log_dir: Path) -> Iterator[Path]:
    if not log_dir.exists():
        return
    for path in sorted(log_dir.rglob("*")):
        # file rotasi: mcp-2026-11-03.log
        if path.is_file() and any(s in LOG_SUFFIXES for s in path.suffixes):
            yield path


def scan_files(paths: Iterable[Path]) -> list[str]:
    findings = []
    for path in paths:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    for kind, masked in scan_text(line):
                        findings.append(f"{path}:{lineno} {kind} {masked}")
        except OSError as exc:
            print(f"lewati {path}: {exc}", file=sys.stderr)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--path", action="append", help="path yang dipindai (default: repo)")
    parser.add_argument("--no-logs", action="store_true")
    parser.add_argument("--log-dir", default=str(ROOT / "logs"))
    args = parser.parse_args(argv)

    bases = [Path(p).resolve() for p in args.path] if args.path else [ROOT]
    findings: list[str] = []
    for base in bases:
        findings += scan_files(iter_repo_files(base))
    if not args.no_logs and not args.path:
        findings += scan_files(iter_log_files(Path(args.log_dir)))

    for line in findings:
        print(line)
    print(f"secret scan: {len(findings)} temuan")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
