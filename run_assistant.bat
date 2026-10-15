@echo off
chcp 65001 >nul
pushd "%~dp0" || (echo Cannot enter script folder & pause & exit /b 1)
rem GUI app: start without a black console window (pythonw, then pyw, then python).
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw -m assistant
  goto done
)
where pyw >nul 2>nul
if not errorlevel 1 (
  start "" pyw -m assistant
  goto done
)
python -m assistant
if errorlevel 1 pause
:done
popd
