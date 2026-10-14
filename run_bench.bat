@echo off
chcp 65001 >nul
pushd "%~dp0" || (echo Cannot enter script folder & pause & exit /b 1)
set PYEXE=python
where py >nul 2>nul && set PYEXE=py
echo Real-model benchmark: spends real API credit (cap 1 USD, checked before each task so one task may slightly exceed it). Press Ctrl+C to cancel.
%PYEXE% -m bench.run_bench --provider anthropic --runs 5 --max-usd 1.0 --ask-key
pause
