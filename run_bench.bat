@echo off
chcp 65001 >nul
pushd "%~dp0" || (echo Cannot enter script folder & pause & exit /b 1)
set PYEXE=python
where py >nul 2>nul && set PYEXE=py
echo.
echo Real-model benchmark. Choose the provider:
echo   1 = Gemini (Google AI Studio key)  [default]
echo   2 = Anthropic (Claude API key)
set CHOICE=1
set /p CHOICE=Type 1 or 2 then Enter (just Enter = 1): 
set PROVIDER=gemini
if "%CHOICE%"=="2" set PROVIDER=anthropic
echo.
echo Selected: %PROVIDER%. This run spends REAL API credit. The estimated cost and the spending cap are shown next,
echo and nothing is spent until you confirm. Your key is typed hidden, used in memory only, and never saved.
%PYEXE% -m bench.run_bench --provider %PROVIDER% --ask-key
pause
