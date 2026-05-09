# TC²-BBS Meshtastic Web UI Setup Script (Windows)
# Clones and builds the Meshtastic web client and places it where web_admin.py can serve it.

$ErrorActionPreference = "Stop"

$REPO_URL = "https://github.com/meshtastic/web.git"
$CLONE_DIR = "meshtastic-web"
$WEB_PKG_DIR = Join-Path $CLONE_DIR "packages\web"
$DIST_DIR = Join-Path $WEB_PKG_DIR "dist"
$TARGET_DIR = "meshtastic-web-dist"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "TC²-BBS  Meshtastic Web UI Setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check prerequisites
foreach ($cmd in "git", "node", "pnpm") {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host "ERROR: '$cmd' not found in PATH." -ForegroundColor Red
        if ($cmd -eq "pnpm") {
            Write-Host "Install pnpm:  npm install -g pnpm" -ForegroundColor Yellow
        }
        exit 1
    }
}
Write-Host "Prerequisites OK (git, node, pnpm found)" -ForegroundColor Green
Write-Host ""

# Clone or update the repo
if (Test-Path $CLONE_DIR) {
    Write-Host "Updating existing clone at .\$CLONE_DIR ..." -ForegroundColor Yellow
    Push-Location $CLONE_DIR
    git pull --ff-only
    Pop-Location
} else {
    Write-Host "Cloning $REPO_URL ..." -ForegroundColor Yellow
    git clone --depth 1 $REPO_URL $CLONE_DIR
}
Write-Host ""

# Install dependencies (workspace root handles all packages)
Write-Host "Installing pnpm workspace dependencies ..." -ForegroundColor Yellow
Push-Location $CLONE_DIR
pnpm install --frozen-lockfile
Pop-Location
Write-Host ""

# Build packages/web
Write-Host "Building Meshtastic web client ..." -ForegroundColor Yellow
Push-Location $WEB_PKG_DIR
pnpm run build
Pop-Location
Write-Host ""

# Copy dist to the target directory used by web_admin.py
if (Test-Path $TARGET_DIR) {
    Remove-Item -Recurse -Force $TARGET_DIR
}
Write-Host "Copying built files to .\$TARGET_DIR ..." -ForegroundColor Yellow
Copy-Item -Recurse $DIST_DIR $TARGET_DIR
Write-Host ""

Write-Host "========================================" -ForegroundColor Green
Write-Host "Meshtastic Web UI ready!" -ForegroundColor Green
Write-Host "Start the web admin and navigate to /mesh-ui/" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
