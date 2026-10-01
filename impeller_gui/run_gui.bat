@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found - running setup first.
    call setup_venv.bat
    if not exist ".venv\Scripts\python.exe" exit /b 1
)

".venv\Scripts\python.exe" -m impeller_control
if errorlevel 1 (
    echo.
    echo The program exited with an error. See the messages above.
    pause
)
