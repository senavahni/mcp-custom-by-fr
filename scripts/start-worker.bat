@echo off
rem Worker job queue + scheduler (satu-satunya proses yang membuka Chrome).
call "%~dp0env.bat"
"%PY%" scripts\run_worker.py %*
