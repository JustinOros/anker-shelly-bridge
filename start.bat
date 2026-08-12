@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

if "%SOLIXAUTO_HOME%"=="" (
    set "DATA_DIR=%USERPROFILE%\solix-automation"
) else (
    set "DATA_DIR=%SOLIXAUTO_HOME%"
)
set "VENV_DIR=%DATA_DIR%\venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

echo.
echo ==============================================================
echo  Anker SOLIX to Shelly automation
echo ==============================================================
echo.

if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" -c "import anker_solix_api.api, yaml, aiohttp" >nul 2>&1
    if not errorlevel 1 (
        echo Using: %VENV_PYTHON%
        echo.
        "%VENV_PYTHON%" "%~dp0solixauto.py" setup
        goto :end
    )
)

set "BASE="
for %%P in (py python) do (
    if not defined BASE (
        where %%P >nul 2>&1 && set "BASE=%%P"
    )
)

if not defined BASE (
    echo Python 3.12 or newer is required and was not found on PATH.
    echo Install it from https://www.python.org/downloads/
    echo Tick "Add python.exe to PATH" during installation, then run this again.
    echo.
    pause
    exit /b 1
)

where git >nul 2>&1
if errorlevel 1 (
    echo Git is required to fetch the Anker library, and was not found.
    echo Install it from https://git-scm.com/download/win
    echo.
    pause
    exit /b 1
)

echo This project runs in its own Python environment, so that installing or
echo removing packages elsewhere on this machine cannot break your automation.
echo.
echo It will be created at:
echo     %VENV_DIR%
echo.
set /p REPLY="Set it up now? [Y/n]: "
if /i "%REPLY%"=="n" (
    echo Nothing was changed.
    goto :end
)

echo.
echo Creating the environment...
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"

%BASE% -m venv "%VENV_DIR%"
if not exist "%VENV_PYTHON%" (
    echo Could not create a virtual environment at %VENV_DIR%
    pause
    exit /b 1
)

echo Installing packages. This takes a minute.
echo.
"%VENV_PYTHON%" -m pip install --upgrade pip --quiet
"%VENV_PYTHON%" -m pip install -r "%~dp0requirements.txt" --quiet
"%VENV_PYTHON%" -m pip install "git+https://github.com/thomluther/anker-solix-api.git" --quiet
"%VENV_PYTHON%" -m pip install aiofiles cryptography paho-mqtt --quiet

"%VENV_PYTHON%" -c "import anker_solix_api.api" >nul 2>&1
if errorlevel 1 (
    echo.
    echo The install finished but the Anker library still will not import.
    echo Run this to see the error:
    echo     "%VENV_PYTHON%" -c "import anker_solix_api.api"
    pause
    exit /b 1
)

echo.
echo Environment ready.
echo.
echo From now on, run commands with:
echo     "%VENV_PYTHON%" solixauto.py ^<command^>
echo.
"%VENV_PYTHON%" "%~dp0solixauto.py" setup

:end
echo.
pause
