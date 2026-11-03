@echo off
rem Jalankan perintah di dalam Anaconda Prompt (miniconda3) dengan env mcp aktif.
rem Contoh: scripts\conda-run.bat python scripts\manage.py seed
set "CONDA_ROOT=D:\Miniconda3"
if not defined CONDA_ENV set "CONDA_ENV=D:\Data.Fahrul\.conda\envs\mcp"
call "%CONDA_ROOT%\Scripts\activate.bat" "%CONDA_ROOT%" || exit /b 1
call conda activate "%CONDA_ENV%" || exit /b 1
cd /d "%~dp0.."
set "PYTHONUTF8=1"
%*
