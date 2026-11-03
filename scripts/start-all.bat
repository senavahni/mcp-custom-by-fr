@echo off
rem Jalankan server dan worker di jendela terpisah.
start "mcp-marketing server" cmd /k ""%~dp0start-server.bat""
start "mcp-marketing worker" cmd /k ""%~dp0start-worker.bat""
