@echo off
chcp 65001 >nul
pushd "%~dp0" || (echo Cannot enter script folder & pause & exit /b 1)
set PYEXE=python
where py >nul 2>nul && set PYEXE=py
echo.
echo Real-model benchmark. Choose the provider:
echo   1 = Gemini (Google AI Studio key)  [default]
echo   2 = Anthropic (Claude API key)
echo   3 = Ollama (local model, no key, no cost, much slower)
set CHOICE=1
set /p CHOICE=Type 1, 2 or 3 then Enter (just Enter = 1): 
set PROVIDER=gemini
if "%CHOICE%"=="2" set PROVIDER=anthropic
if "%CHOICE%"=="3" set PROVIDER=ollama
echo.
set WRITEDOC=
set /p WD=Save the result into docs\MEASUREMENT.md? Type y for yes (Enter = no, result is only shown): 
if /i "%WD%"=="y" set WRITEDOC=--write-doc
echo.
if "%PROVIDER%"=="ollama" (
  echo Selected: ollama. Needs Ollama running and the model pulled first: ollama pull qwen2.5:7b
  echo No key is needed and nothing is spent. You still confirm before starting.
  %PYEXE% -m bench.run_bench --provider ollama %WRITEDOC%
) else (
  echo Selected: %PROVIDER%. This run spends REAL API credit. The estimated cost and the spending cap are shown next,
  echo and nothing is spent until you confirm. Your key is typed hidden, used in memory only, and never saved.
  %PYEXE% -m bench.run_bench --provider %PROVIDER% --ask-key %WRITEDOC%
)
pause
