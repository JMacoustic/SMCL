@echo off
setlocal
cd /d "%~dp0"
set "ENV_NAME=smcl"

rem Prefer the local .venv, then the conda environment, otherwise run setup first.
if exist ".venv\Scripts\python.exe" goto :run_venv
call :conda_env_exists && goto :run_conda

if "%~1"=="setup-done" (
    echo.
    echo Setup did not produce a usable environment. See the messages above.
    pause
    exit /b 1
)
echo No Python environment found - running setup first.
where conda >nul 2>&1
if not errorlevel 1 (
    call "%~dp0setup_conda.bat"
) else (
    call "%~dp0setup_venv.bat"
)
call "%~f0" setup-done
exit /b

:run_venv
".venv\Scripts\python.exe" -m impeller_control
goto :finish

:run_conda
call conda run -n %ENV_NAME% --no-capture-output python -m impeller_control
goto :finish

:finish
if errorlevel 1 (
    echo.
    echo The program exited with an error. See the messages above.
    pause
)
exit /b

:conda_env_exists
where conda >nul 2>&1
if errorlevel 1 exit /b 1
call conda env list 2>nul | findstr /B /C:"%ENV_NAME% " >nul 2>&1
exit /b
