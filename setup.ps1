# BaconBS Meshtastic + MeshCore - Windows Setup Script
# This script sets up the environment with all necessary dependencies

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "BaconBS Meshtastic + MeshCore - Windows Setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check if Python is installed
Write-Host "Checking Python installation..." -ForegroundColor Yellow
python --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Python is not installed or not in PATH!" -ForegroundColor Red
    Write-Host "Please install Python 3.x from https://www.python.org/" -ForegroundColor Red
    exit 1
}
Write-Host "✓ Python found" -ForegroundColor Green
Write-Host ""

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Python 3.10 or newer is required for MeshCore support." -ForegroundColor Red
    exit 1
}

# Create virtual environment if it doesn't exist
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    python -m venv .venv
    Write-Host "✓ Virtual environment created" -ForegroundColor Green
} else {
    Write-Host "✓ Virtual environment already exists" -ForegroundColor Green
}
Write-Host ""

# Activate virtual environment
Write-Host "Activating virtual environment..." -ForegroundColor Yellow
& ".\\.venv\\Scripts\\Activate.ps1"
Write-Host "✓ Virtual environment activated" -ForegroundColor Green
Write-Host ""

$venvPython = ".\\.venv\\Scripts\\python.exe"

# Upgrade pip
Write-Host "Upgrading pip..." -ForegroundColor Yellow
& $venvPython -m pip install --upgrade pip
Write-Host "✓ pip upgraded" -ForegroundColor Green
Write-Host ""

# Install dependencies
Write-Host "Installing dependencies from requirements.txt..." -ForegroundColor Yellow
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to install dependencies!" -ForegroundColor Red
    exit 1
}
Write-Host "✓ Dependencies installed" -ForegroundColor Green
Write-Host ""

# Verify meshtastic import in the virtual environment
Write-Host "Verifying meshtastic installation..." -ForegroundColor Yellow
& $venvPython -c "import meshtastic.stream_interface"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: meshtastic is not importable in .venv." -ForegroundColor Red
    Write-Host "Try reinstalling with: .\\.venv\\Scripts\\python.exe -m pip install --no-cache-dir -r requirements.txt" -ForegroundColor Red
    exit 1
}
Write-Host "✓ meshtastic import verified" -ForegroundColor Green
Write-Host ""

Write-Host "Verifying MeshCore installation..." -ForegroundColor Yellow
& $venvPython -c "import meshcore"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: meshcore is not importable in .venv." -ForegroundColor Red
    exit 1
}
Write-Host "✓ MeshCore import verified" -ForegroundColor Green
Write-Host ""

# Note about dfrotz (required for Zork / Infocom games)
Write-Host "NOTE: Zork and other Infocom games require the 'dfrotz' interpreter." -ForegroundColor Yellow
Write-Host "      dfrotz is a Linux system package and is not available natively on Windows." -ForegroundColor Yellow
Write-Host "      To use games on Windows, install WSL and run 'sudo apt install dfrotz' inside it," -ForegroundColor Yellow
Write-Host "      or use a pre-built dfrotz.exe and set BBS_ZORK_INTERPRETER in config.ini." -ForegroundColor Yellow
Write-Host ""

# Check for config file
if (-not (Test-Path "config.ini")) {
    if (Test-Path "example_config.ini") {
        Write-Host "Creating config.ini from example..." -ForegroundColor Yellow
        Copy-Item "example_config.ini" "config.ini"
        Write-Host "✓ config.ini created (review and update as needed)" -ForegroundColor Green
    }
}
Write-Host ""

Write-Host "========================================" -ForegroundColor Green
Write-Host "Setup Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "1. Review and update config.ini with your settings"
Write-Host "2. Start using venv Python: .\\.venv\\Scripts\\python.exe server.py"
Write-Host ""
Write-Host "Optional interactive activation for your current shell:" -ForegroundColor Cyan
Write-Host "   .\\.venv\\Scripts\\Activate.ps1"
Write-Host "Then run: python server.py"
Write-Host ""
