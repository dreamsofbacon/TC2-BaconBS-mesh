#!/bin/bash

# Install the Bacon BBS systemd services with user-selected values.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_USER="${SUDO_USER:-${USER:-$(id -un)}}"
DEFAULT_PROJECT_DIR="$REPO_DIR"
NON_INTERACTIVE="false"
SERVICE_USER=""
PROJECT_DIR=""
USB_FIX="true"

usage() {
    cat <<EOF
Usage: bash install_services.sh [options]

Options:
  -u, --user USER    Linux user for the systemd service User= field
  -d, --dir PATH     Project directory containing server.py and web_admin.py
  -y, --yes          Non-interactive mode (use provided/default values)
      --no-usb-fix   Skip the USB autosuspend rule (for a node with no radio)
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
        --no-usb-fix)
            USB_FIX="false"
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

if [[ ! -f "$REPO_DIR/mesh-bbs.service" || ! -f "$REPO_DIR/bacon-web-admin.service" \
    || ! -f "$REPO_DIR/bacon-ssh.service" ]]; then
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

for UNIT in mesh-bbs.service bacon-web-admin.service bacon-ssh.service; do
    sed \
        -e "s|__SERVICE_USER__|$ESC_USER|g" \
        -e "s|__PROJECT_DIR__|$ESC_DIR|g" \
        "$REPO_DIR/$UNIT" > "$TMP_DIR/$UNIT"
done

echo "Installing systemd units..."
sudo cp "$TMP_DIR/mesh-bbs.service" /etc/systemd/system/mesh-bbs.service
sudo cp "$TMP_DIR/bacon-web-admin.service" /etc/systemd/system/bacon-web-admin.service
sudo cp "$TMP_DIR/bacon-ssh.service" /etc/systemd/system/bacon-ssh.service

sudo systemctl daemon-reload
sudo systemctl enable mesh-bbs.service bacon-web-admin.service
sudo systemctl restart mesh-bbs.service bacon-web-admin.service

# Fleet updates: the mesh server switches to the new code and exits, and
# systemd brings it back -- no privilege needed. The web admin and SSH
# front end do not exit with it, so fleet_update.py restarts them with
# `sudo -n systemctl restart <unit>`. Without this rule that fails on every
# update and both keep running the old code, which is how an SSH fix once
# sat unloaded for seven hours across eight deploys. The rule allows exactly
# those two restarts and nothing else.
SUDOERS_FILE=/etc/sudoers.d/baconbbs-fleet
SUDOERS_TMP="$TMP_DIR/baconbbs-fleet"
{
    echo "# Installed by install_services.sh: lets fleet updates restart the"
    echo "# BBS web admin and SSH services onto new code. Nothing else."
    for SYSTEMCTL in /usr/bin/systemctl /bin/systemctl; do
        for UNIT in bacon-web-admin.service bacon-ssh.service; do
            echo "$SERVICE_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart $UNIT"
        done
    done
} > "$SUDOERS_TMP"
if command -v visudo >/dev/null 2>&1 && sudo visudo -cf "$SUDOERS_TMP" >/dev/null; then
    sudo install -m 0440 -o root -g root "$SUDOERS_TMP" "$SUDOERS_FILE"
    echo "Fleet updates can restart the web admin and SSH services ($SUDOERS_FILE)."
else
    echo "WARNING: Could not install $SUDOERS_FILE. Fleet updates will still"
    echo "  update the BBS, but the web admin and SSH services will keep running"
    echo "  old code until you restart them yourself."
fi

# USB autosuspend disable -- see the rules file itself for why. Best-effort:
# a udev rule install failure (e.g. non-systemd-udev environment, container,
# read-only /etc) must not fail the whole install, since the BBS itself
# doesn't depend on it to run, only to stay reliably connected to its radios.
UDEV_RULE="$REPO_DIR/scripts/99-baconbs-usb-no-autosuspend.rules"
if [[ "$USB_FIX" == "true" && -f "$UDEV_RULE" ]] && command -v udevadm >/dev/null 2>&1; then
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
WEB_HOST="0.0.0.0"
if [[ -f "$PROJECT_DIR/web-admin.env" ]]; then
    WEB_HOST="$(sed -n 's/^BBS_WEBGUI_HOST=//p' "$PROJECT_DIR/web-admin.env" | tail -1)"
    WEB_HOST="${WEB_HOST:-0.0.0.0}"
fi
echo "Web admin listens on $WEB_HOST:8081 (set BBS_WEBGUI_HOST in web-admin.env to change it)."
# install.sh enables SSH itself right after this, from its own question.
if [[ -z "${BBS_INSTALLER:-}" ]] && ! systemctl is-enabled --quiet bacon-ssh.service 2>/dev/null; then
    echo "SSH service installed but left disabled. Configure [ssh], then run:"
    echo "  sudo systemctl enable --now bacon-ssh.service"
fi
echo ""
echo "Quick checks:"
echo "  sudo systemctl status mesh-bbs.service bacon-web-admin.service"
echo "  sudo journalctl -u mesh-bbs.service -u bacon-web-admin.service -f"
