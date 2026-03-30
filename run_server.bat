@echo off
setlocal

set VENV_PYTHON=
if exist ".venv\Scripts\python.exe" set VENV_PYTHON=.venv\Scripts\python.exe
if "%VENV_PYTHON%"=="" if exist "venv\Scripts\python.exe" set VENV_PYTHON=venv\Scripts\python.exe

if "%VENV_PYTHON%"=="" (
    echo ERROR: No virtual environment found (.venv or venv).
    echo Run setup first: setup.bat
    exit /b 1
)

"%VENV_PYTHON%" server.py %*
exit /b %ERRORLEVEL%
