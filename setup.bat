@echo off
REM BaconBS Meshtastic + MeshCore - Windows Setup Script (Batch)
REM This script sets up the environment with all necessary dependencies

cls
echo ========================================
echo BaconBS Meshtastic + MeshCore - Windows Setup
echo ========================================
echo.

REM Check if Python is installed
echo Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH!
    echo Please install Python 3.x from https://www.python.org/
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('python --version') do set PYTHON_VERSION=%%i
echo %PYTHON_VERSION%
echo [OK] Python found
echo.

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.10 or newer is required for MeshCore support.
    pause
    exit /b 1
)

REM Create virtual environment if it doesn't exist
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
    echo [OK] Virtual environment created
) else (
    echo [OK] Virtual environment already exists
)
echo.

REM Activate virtual environment
echo Activating virtual environment...
call .venv\Scripts\activate.bat
echo [OK] Virtual environment activated
echo.

set VENV_PYTHON=.venv\Scripts\python.exe

REM Upgrade pip
echo Upgrading pip...
%VENV_PYTHON% -m pip install --upgrade pip
echo [OK] pip upgraded
echo.

REM Install dependencies
echo Installing dependencies from requirements.txt...
%VENV_PYTHON% -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies!
    pause
    exit /b 1
)
echo [OK] Dependencies installed
echo.

REM Verify meshtastic import in the virtual environment
echo Verifying meshtastic installation...
%VENV_PYTHON% -c "import meshtastic.stream_interface"
if errorlevel 1 (
    echo ERROR: meshtastic is not importable in .venv.
    echo Try reinstalling with: .venv\Scripts\python.exe -m pip install --no-cache-dir -r requirements.txt
    pause
    exit /b 1
)
echo [OK] meshtastic import verified
echo.

echo Verifying MeshCore installation...
%VENV_PYTHON% -c "import meshcore"
if errorlevel 1 (
    echo ERROR: meshcore is not importable in .venv.
    pause
    exit /b 1
)
echo [OK] MeshCore import verified
echo.

REM Note about dfrotz (required for Zork / Infocom games)
echo NOTE: Zork and other Infocom games require the 'dfrotz' interpreter.
echo       dfrotz is a Linux system package and is not available natively on Windows.
echo       To use games on Windows, install WSL and run: sudo apt install dfrotz
echo       Or place a dfrotz.exe somewhere and set BBS_ZORK_INTERPRETER in config.ini.
echo.

REM Check for config file
if not exist "config.ini" (
    if exist "example_config.ini" (
        echo Creating config.ini from example...
        copy example_config.ini config.ini >nul
        echo [OK] config.ini created (review and update as needed)
    )
)
echo.

echo ========================================
echo Setup Complete!
echo ========================================
echo.
echo Next steps:
echo 1. Review and update config.ini with your settings
echo 2. Start using venv Python: .venv\Scripts\python.exe server.py
echo.
echo Optional interactive activation for this shell:
echo    .venv\Scripts\activate.bat
echo Then run: python server.py
echo.
pause
