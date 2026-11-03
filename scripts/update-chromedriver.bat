@echo off
rem Unduh chromedriver yang cocok dengan versi Chrome terpasang (Chrome for Testing).
rem Jalankan setiap kali Chrome ter-update dan worker mencatat browser.not_ready.
call "%~dp0env.bat"
"%PY%" scripts\update_chromedriver.py %*
