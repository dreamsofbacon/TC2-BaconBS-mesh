$ErrorActionPreference = "Stop"

$venvPython = $null
if (Test-Path ".\.venv\Scripts\python.exe") {
    $venvPython = ".\.venv\Scripts\python.exe"
} elseif (Test-Path ".\venv\Scripts\python.exe") {
    $venvPython = ".\venv\Scripts\python.exe"
} else {
    Write-Host "ERROR: No virtual environment found (.venv or venv)." -ForegroundColor Red
    Write-Host "Run setup first: .\setup.ps1" -ForegroundColor Red
    exit 1
}

& $venvPython .\web_admin.py @args
exit $LASTEXITCODE
