@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (py -m assistant) || (python -m assistant)
if errorlevel 1 pause
