@echo off
rem Alembic upgrade (schema mcp). Revisi pgvector (0002) dilewati otomatis bila ekstensi belum terpasang.
call "%~dp0env.bat"
"%PY%" scripts\manage.py migrate %*
