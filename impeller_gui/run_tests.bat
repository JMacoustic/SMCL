@echo off
setlocal
cd /d "%~dp0"
set "ENV_NAME=smcl"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m unittest discover -s tests -v
    goto :finish
)

where conda >nul 2>&1
if not errorlevel 1 (
    call conda env list 2>nul | findstr /B /C:"%ENV_NAME% " >nul 2>&1
    if not errorlevel 1 (
        call conda run -n %ENV_NAME% --no-capture-output python -m unittest discover -s tests -v
        goto :finish
    )
)

echo No Python environment found - run setup_venv.bat or setup_conda.bat first.
pause
exit /b 1

:finish
pause
