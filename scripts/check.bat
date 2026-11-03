@echo off
rem Gerbang kualitas: lint, format, tipe, unit test, secret scan. Berhenti di langkah pertama yang gagal.
rem Test integrasi (PostgreSQL) dijalankan terpisah: scripts\test.bat -m integration
call "%~dp0env.bat"

echo [1/5] ruff check
"%PY%" -m ruff check . || exit /b 1
echo [2/5] ruff format --check
"%PY%" -m ruff format --check . || exit /b 1
echo [3/5] mypy mcp_marketing
"%PY%" -m mypy mcp_marketing || exit /b 1
echo [4/5] pytest (unit)
"%PY%" -m pytest -q -m "not integration and not browser and not live and not contract and not eval" || exit /b 1
echo [5/5] secret scan (repo + logs)
"%PY%" scripts\secret_scan.py || exit /b 1

echo CHECK OK
