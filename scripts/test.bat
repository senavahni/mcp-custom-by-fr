@echo off
rem pytest dengan argumen diteruskan. Contoh: scripts\test.bat -m integration -x
call "%~dp0env.bat"
"%PY%" -m pytest %*
