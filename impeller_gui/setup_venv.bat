@echo off
setlocal
cd /d "%~dp0"

echo === Impeller Ramp Control: creating virtual environment (.venv) ===

rem Find a working Python 3.9+ interpreter. Each candidate is actually executed,
rem so a "py" launcher without any Python installed, or the Windows Store alias,
rem is skipped instead of being trusted. 'call' is required because on pyenv-win
rem "python" is a .bat shim and a bare .bat call would end this script.
set "PY="
call python -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>&1 && set "PY=python"
if not defined PY call py -3 -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>&1 && set "PY=py -3"
if not defined PY call python3 -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>&1 && set "PY=python3"

if not defined PY (
    echo.
    echo No Python 3.9 or newer was found on this PC.
    echo   - Install Python from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run this script again, or
    echo   - if you use Anaconda/Miniconda, run setup_conda.bat instead.
    pause
    exit /b 1
)
echo Using interpreter: %PY%

if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    call %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo Could not create a virtual environment.
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
