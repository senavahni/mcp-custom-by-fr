@echo off
rem Hentikan jendela server & worker yang dibuka start-all.bat (graceful shutdown via Ctrl+C lebih disarankan).
taskkill /FI "WINDOWTITLE eq mcp-marketing server*" /T >nul 2>&1
taskkill /FI "WINDOWTITLE eq mcp-marketing worker*" /T >nul 2>&1
echo stop-all selesai
