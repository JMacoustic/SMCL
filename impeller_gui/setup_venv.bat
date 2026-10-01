@echo off
setlocal
cd /d "%~dp0"

echo === Impeller Ramp Control: creating virtual environment ===

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    rem 'call' is required: on pyenv-win 'python' is a .bat shim, and a bare .bat call would end this script.
    call %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo Could not create a virtual environment. Install Python 3.9-3.12 from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during installation.
        pause
        exit /b 1
    )
)

echo Installing packages from requirements.txt ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Package installation failed. Check your internet connection and try again.
    pause
    exit /b 1
)

echo.
echo Setup complete. Double-click run_gui.bat to start the program.
pause
