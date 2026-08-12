#!/bin/bash

# Install mesh-bbs.service and bacon-web-admin.service with user-selected values.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_USER="${SUDO_USER:-${USER}}"
DEFAULT_PROJECT_DIR="$REPO_DIR"
NON_INTERACTIVE="false"
SERVICE_USER=""
PROJECT_DIR=""

usage() {
    cat <<EOF
Usage: bash install_services.sh [options]

Options:
  -u, --user USER    Linux user for the systemd service User= field
  -d, --dir PATH     Project directory containing server.py and web_admin.py
  -y, --yes          Non-interactive mode (use provided/default values)
  -h, --help         Show this help message

Examples:
  bash install_services.sh
  bash install_services.sh --user bacon --dir /home/bacon/TC2-BaconBS-mesh
  bash install_services.sh --yes --user bacon
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -u|--user)
            [[ $# -ge 2 ]] || { echo "ERROR: --user requires a value"; exit 1; }
            SERVICE_USER="$2"
            shift 2
            ;;
        -d|--dir)
            [[ $# -ge 2 ]] || { echo "ERROR: --dir requires a value"; exit 1; }
            PROJECT_DIR="$2"
            shift 2
            ;;
        -y|--yes)
            NON_INTERACTIVE="true"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

if [[ ! -f "$REPO_DIR/mesh-bbs.service" || ! -f "$REPO_DIR/bacon-web-admin.service" ]]; then
    echo "ERROR: Run this script from inside the TC2-BaconBS-mesh repository."
    exit 1
fi

SERVICE_USER="${SERVICE_USER:-$DEFAULT_USER}"
PROJECT_DIR="${PROJECT_DIR:-$DEFAULT_PROJECT_DIR}"

if [[ "$NON_INTERACTIVE" != "true" ]]; then
    read -r -p "Service Linux username [${SERVICE_USER}]: " INPUT_USER
    SERVICE_USER="${INPUT_USER:-$SERVICE_USER}"

    read -r -p "Project directory [${PROJECT_DIR}]: " INPUT_DIR
    PROJECT_DIR="${INPUT_DIR:-$PROJECT_DIR}"
fi

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

# USB autosuspend disable -- see the rules file itself for why. Best-effort:
# a udev rule install failure (e.g. non-systemd-udev environment, container,
# read-only /etc) must not fail the whole install, since the BBS itself
# doesn't depend on it to run, only to stay reliably connected to its radios.
UDEV_RULE="$REPO_DIR/scripts/99-baconbs-usb-no-autosuspend.rules"
if [[ -f "$UDEV_RULE" ]] && command -v udevadm >/dev/null 2>&1; then
    echo "Installing USB autosuspend fix for radio USB-serial adapters..."
    if sudo cp "$UDEV_RULE" /etc/udev/rules.d/99-baconbs-usb-no-autosuspend.rules \
        && sudo udevadm control --reload-rules \
        && sudo udevadm trigger; then
        echo "USB autosuspend disabled (applies immediately, no reboot needed)."
    else
        echo "WARNING: Could not install the USB autosuspend fix -- radios may"
        echo "  intermittently disconnect/reconnect under Linux's default USB"
        echo "  power management. See scripts/99-baconbs-usb-no-autosuspend.rules"
        echo "  to apply it manually."
    fi
fi

echo ""
echo "Installed and restarted services for user '$SERVICE_USER'."
echo "Web admin default bind is 0.0.0.0:8081 (override in web-admin.env if needed)."
echo ""
echo "Quick checks:"
echo "  sudo systemctl status mesh-bbs.service bacon-web-admin.service"
echo "  sudo journalctl -u mesh-bbs.service -u bacon-web-admin.service -f"
