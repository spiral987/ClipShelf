@echo off
setlocal
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0clip_shelf.py"
    exit /b
)
where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3 "%~dp0clip_shelf.py"
    exit /b
)
where python.exe >nul 2>nul
if not errorlevel 1 (
    python.exe "%~dp0clip_shelf.py"
    if errorlevel 1 pause
    exit /b
)
echo Python 3.10 or later is required.
echo Install Python from https://www.python.org/downloads/windows/
pause
