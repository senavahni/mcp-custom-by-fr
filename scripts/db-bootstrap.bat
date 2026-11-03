@echo off
rem Bootstrap sekali sebagai superuser: role mcp_owner/mcp_app, schema mcp, pgvector (bila ada), grant.
rem Password diminta lewat prompt dan TIDAK disimpan. Pakai: scripts\db-bootstrap.bat [nama_db] [superuser]
setlocal
call "%~dp0env.bat"
set "DB_NAME=%~1"
if "%DB_NAME%"=="" set "DB_NAME=marketing_dashboard"
set "SUPERUSER=%~2"
if "%SUPERUSER%"=="" set "SUPERUSER=postgres"

set /p "OWNER_PW=Password baru untuk role mcp_owner: "
set /p "APP_PW=Password baru untuk role mcp_app: "
echo Password superuser %SUPERUSER% akan diminta oleh psql.
"%PG_BIN%\psql.exe" -h 127.0.0.1 -p 5432 -U %SUPERUSER% -d %DB_NAME% -v owner_pw="%OWNER_PW%" -v app_pw="%APP_PW%" -f scripts\sql\bootstrap.sql
set "RC=%ERRORLEVEL%"
set "OWNER_PW="
set "APP_PW="
if not "%RC%"=="0" (echo bootstrap GAGAL & exit /b %RC%)
echo bootstrap OK. Isi DATABASE_URL (mcp_app) dan MIGRATION_DATABASE_URL (mcp_owner) di .env lalu jalankan scripts\migrate.bat
endlocal
