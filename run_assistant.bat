@echo off
chcp 65001 >nul
pushd "%~dp0" || (echo Cannot enter script folder & pause & exit /b 1)
set PYEXE=python
where py >nul 2>nul && set PYEXE=py
%PYEXE% -m assistant
if errorlevel 1 pause
