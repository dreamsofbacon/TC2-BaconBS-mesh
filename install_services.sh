#!/bin/bash

# Install mesh-bbs.service and bacon-web-admin.service with user-selected values.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_USER="${SUDO_USER:-${USER}}"
DEFAULT_PROJECT_DIR="$REPO_DIR"

if [[ ! -f "$REPO_DIR/mesh-bbs.service" || ! -f "$REPO_DIR/bacon-web-admin.service" ]]; then
    echo "ERROR: Run this script from inside the TC2-BaconBS-mesh repository."
    exit 1
fi

read -r -p "Service Linux username [${DEFAULT_USER}]: " SERVICE_USER
SERVICE_USER="${SERVICE_USER:-$DEFAULT_USER}"

read -r -p "Project directory [${DEFAULT_PROJECT_DIR}]: " PROJECT_DIR
PROJECT_DIR="${PROJECT_DIR:-$DEFAULT_PROJECT_DIR}"

if [[ ! -d "$PROJECT_DIR" ]]; then
    echo "ERROR: Project directory does not exist: $PROJECT_DIR"
    exit 1
fi

if [[ ! -f "$PROJECT_DIR/server.py" || ! -f "$PROJECT_DIR/web_admin.py" ]]; then
    echo "ERROR: Project directory does not contain server.py and web_admin.py: $PROJECT_DIR"
    exit 1
fi

if [[ ! -x "$PROJECT_DIR/venv/bin/python3" ]]; then
    echo "ERROR: Missing venv python at $PROJECT_DIR/venv/bin/python3"
    echo "Run setup first: bash setup.sh"
    exit 1
fi

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    echo "ERROR: Linux user not found: $SERVICE_USER"
    exit 1
fi

escape_sed() {
    printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'
}

ESC_USER="$(escape_sed "$SERVICE_USER")"
ESC_DIR="$(escape_sed "$PROJECT_DIR")"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

for UNIT in mesh-bbs.service bacon-web-admin.service; do
    sed \
        -e "s|__SERVICE_USER__|$ESC_USER|g" \
        -e "s|__PROJECT_DIR__|$ESC_DIR|g" \
        "$REPO_DIR/$UNIT" > "$TMP_DIR/$UNIT"
done

echo "Installing systemd units..."
sudo cp "$TMP_DIR/mesh-bbs.service" /etc/systemd/system/mesh-bbs.service
sudo cp "$TMP_DIR/bacon-web-admin.service" /etc/systemd/system/bacon-web-admin.service

sudo systemctl daemon-reload
sudo systemctl enable mesh-bbs.service bacon-web-admin.service
sudo systemctl restart mesh-bbs.service bacon-web-admin.service

echo ""
echo "Installed and restarted services for user '$SERVICE_USER'."
echo "Web admin default bind is 0.0.0.0:8081 (override in web-admin.env if needed)."
echo ""
echo "Quick checks:"
echo "  sudo systemctl status mesh-bbs.service bacon-web-admin.service"
echo "  sudo journalctl -u mesh-bbs.service -u bacon-web-admin.service -f"
