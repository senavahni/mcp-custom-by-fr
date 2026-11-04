@echo off
rem Hentikan jendela server & worker yang dibuka start-all.bat (graceful shutdown via Ctrl+C lebih disarankan).
taskkill /FI "WINDOWTITLE eq mcp-marketing server*" /T >nul 2>&1
taskkill /FI "WINDOWTITLE eq mcp-marketing worker*" /T >nul 2>&1
rem Filter WINDOWTITLE tidak andal (mis. judul berubah / Windows Terminal): matikan juga lewat command line
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'run_(worker|server)\.py' -and $_.CommandLine -match 'envs.mcp' } | ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null }" >nul 2>&1
echo stop-all selesai
