@echo off
setlocal

cd /d "%~dp0"

echo.
echo ==============================================================
echo  Anker SOLIX to Shelly automation - setup
echo ==============================================================
echo.

set PYTHON=

for %%P in (py python) do (
    if not defined PYTHON (
        where %%P >nul 2>&1 && set PYTHON=%%P
    )
)

if not defined PYTHON (
    echo No Python found on PATH.
    echo Install Python 3.12 or newer from https://www.python.org/downloads/
    echo Tick "Add python.exe to PATH" during installation, then run this again.
    echo.
    pause
    exit /b 1
)

echo Using: %PYTHON%
echo.

%PYTHON% "%~dp0solixauto.py" setup

echo.
pause
