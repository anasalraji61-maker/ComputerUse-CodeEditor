@echo off
cd /d "%~dp0"
echo Installing PyInstaller (one time)...
py -m pip install --quiet pyinstaller
if errorlevel 1 goto fail
echo Building COS_Assistant.exe ...
py -m PyInstaller --noconfirm --onefile --noconsole --name COS_Assistant launch.py
if errorlevel 1 goto fail
echo.
echo Done: dist\COS_Assistant.exe  (double-click it to run)
goto end
:fail
echo Build failed. Send the messages above to Claude.
:end
pause
