@echo off
chcp 65001 >nul
pushd "%~dp0" || (echo Cannot enter script folder & pause & exit /b 1)
set PYEXE=python
where py >nul 2>nul && set PYEXE=py
%PYEXE% -c "import tkinter" 2>nul
if errorlevel 1 (
  echo tkinter is missing in this Python - reinstall Python with the tcl/tk option.
  goto fail
)
echo Installing PyInstaller (one time)...
%PYEXE% -m pip install "pyinstaller==6.*"
if errorlevel 1 goto fail
echo Building COS_Assistant.exe ...
%PYEXE% -m PyInstaller --noconfirm --clean --onefile --noconsole --name COS_Assistant launch.py
if errorlevel 1 goto fail
echo.
echo Done: dist\COS_Assistant.exe  (double-click it to run). Test it once before relying on it.
goto end
:fail
echo Build failed. Send the messages above to Claude.
:end
pause
