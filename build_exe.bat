@echo off
chcp 65001 >nul
pushd "%~dp0" || (echo Cannot enter script folder & pause & exit /b 1)
set "COS_BUILD_SMOKE_DIR="

rem 1) Pick the Python: C:\Python314 first, then the py launcher, then PATH.
set "PYEXE="
if exist "C:\Python314\python.exe" set "PYEXE=C:\Python314\python.exe"
if defined PYEXE goto have_py
py -3.14 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3.14"
if defined PYEXE goto have_py
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto have_py
python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto have_py
echo Python was not found. Install Python 3.14 from python.org first.
goto fail

:have_py
%PYEXE% -c "import sys; print('Using Python', sys.version.split()[0], 'at', sys.executable)"
%PYEXE% -c "import tkinter, sqlite3, ssl, ctypes.wintypes" 2>nul
if errorlevel 1 goto no_libs

rem 2) The exe must not be running, Windows locks it and the build would fail.
tasklist /FI "IMAGENAME eq COS_Assistant.exe" 2>nul | find /I "COS_Assistant.exe" >nul
if not errorlevel 1 goto running

rem 3) PyInstaller: quick no-op when already installed, offline is tolerated.
echo Checking PyInstaller...
%PYEXE% -m pip install --upgrade "pyinstaller>=6.16,<7"
if errorlevel 1 (
  %PYEXE% -m PyInstaller --version >nul 2>nul
  if errorlevel 1 goto fail
  echo pip failed but the installed PyInstaller will be used.
)

rem 4) Build. Hidden imports cover modules the app does not import yet. Optional files are used only if present.
set "HID="
if exist "assistant\verify_review.py" set "HID=%HID% --hidden-import assistant.verify_review"
if exist "assistant\redact.py" set "HID=%HID% --hidden-import assistant.redact"
set "VER="
if exist "version_info.txt" set "VER=--version-file version_info.txt"
set "ICO="
if exist "app.ico" set "ICO=--icon app.ico"
echo Building COS_Assistant.exe ...
%PYEXE% -m PyInstaller --noconfirm --clean --onefile --noconsole --noupx --name COS_Assistant %HID% %VER% %ICO% launch.py
if errorlevel 1 goto fail
if not exist "dist\COS_Assistant.exe" goto fail

rem 5) Smoke test with a throw-away data folder. The instance lock is keyed by data folder, so a real running copy is not affected.
echo Smoke test: starting the exe for 15 seconds with a temporary data folder...
set "COS_BUILD_SMOKE_DIR=%TEMP%\cos_build_smoke"
if exist "%COS_BUILD_SMOKE_DIR%" rmdir /s /q "%COS_BUILD_SMOKE_DIR%"
set "COS_DATA_DIR=%COS_BUILD_SMOKE_DIR%\data"
set "COS_WORKSPACE=%COS_BUILD_SMOKE_DIR%\ws"
set "COS_NO_DIALOG=1"
start "" "%CD%\dist\COS_Assistant.exe"
ping -n 16 127.0.0.1 >nul
tasklist /FI "IMAGENAME eq COS_Assistant.exe" 2>nul | find /I "COS_Assistant.exe" >nul
set "ALIVE=%ERRORLEVEL%"
taskkill /IM COS_Assistant.exe /T /F >nul 2>nul
ping -n 3 127.0.0.1 >nul
set "SMOKE_OK=1"
if not "%ALIVE%"=="0" set "SMOKE_OK=0"
if exist "%COS_DATA_DIR%\crash.log" set "SMOKE_OK=0"
if not exist "%COS_DATA_DIR%\ledger.db" set "SMOKE_OK=0"
if "%SMOKE_OK%"=="0" goto smoke_fail
echo.
echo Done: dist\COS_Assistant.exe passed the smoke test. Double-click it to run.
goto end

:smoke_fail
echo SMOKE TEST FAILED: the exe did not start cleanly.
if exist "%COS_DATA_DIR%\crash.log" type "%COS_DATA_DIR%\crash.log"
goto fail
:running
echo COS_Assistant.exe is running. Close it, then run this file again.
goto fail
:no_libs
echo tkinter, sqlite3 or ssl is missing in this Python. Reinstall Python from python.org with the tcl/tk option.
goto fail
:fail
echo Build failed. Send the messages above to Claude.
:end
if defined COS_BUILD_SMOKE_DIR if exist "%COS_BUILD_SMOKE_DIR%" rmdir /s /q "%COS_BUILD_SMOKE_DIR%"
popd
pause
