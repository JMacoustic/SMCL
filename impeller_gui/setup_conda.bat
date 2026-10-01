@echo off
setlocal
cd /d "%~dp0"
set "ENV_NAME=smcl"

echo === Impeller Ramp Control: creating conda environment "%ENV_NAME%" ===

where conda >nul 2>&1
if errorlevel 1 (
    echo.
    echo conda was not found on PATH.
    echo Open "Anaconda Prompt" ^(or "Miniconda Prompt"^) from the Start menu and run this script there,
    echo or install Miniconda from https://docs.conda.io/en/latest/miniconda.html
    pause
    exit /b 1
)

call conda env list 2>nul | findstr /B /C:"%ENV_NAME% " >nul 2>&1
if errorlevel 1 (
    echo Creating environment "%ENV_NAME%" from environment.yml ...
    call conda env create -f environment.yml -n %ENV_NAME%
) else (
    echo Environment "%ENV_NAME%" already exists - updating packages ...
    call conda env update -f environment.yml -n %ENV_NAME% --prune
)
if errorlevel 1 (
    echo.
    echo conda could not create or update the environment. Check your internet connection and try again.
    pause
    exit /b 1
)

echo.
echo Setup complete. Double-click run_gui.bat to start the program.
pause
