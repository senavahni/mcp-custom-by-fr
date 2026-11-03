@echo off
rem Siapkan environment tanpa `conda activate`. Dipanggil oleh script lain: call "%~dp0env.bat"
if not defined CONDA_ENV set "CONDA_ENV=D:\Data.Fahrul\.conda\envs\mcp"
set "PY=%CONDA_ENV%\python.exe"
rem Library\bin wajib agar DLL (numpy, openssl) termuat
set "PATH=%CONDA_ENV%;%CONDA_ENV%\Library\mingw-w64\bin;%CONDA_ENV%\Library\usr\bin;%CONDA_ENV%\Library\bin;%CONDA_ENV%\Scripts;%PATH%"
set "PG_BIN=D:\Data.Fahrul\Documents\PortableDev\postgres\pgsql\bin"
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set "PYTHONUTF8=1"
cd /d "%PROJECT_ROOT%"
rem muat variabel dari .env (baris non-komentar KEY=VALUE; jangan pakai komentar di akhir baris)
if exist ".env" for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
