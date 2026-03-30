#!/bin/bash

# TC²-BBS Meshtastic - Linux/Mac Setup Script
# This script sets up the environment with all necessary dependencies

set -euo pipefail

echo "========================================"
echo "TC²-BBS Meshtastic - Setup"
echo "========================================"
echo ""

# Check if Python is installed
echo "Checking Python installation..."
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is not installed!"
    echo "Please install Python 3.x using your package manager:"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip"
    echo "  macOS: brew install python3"
    exit 1
fi
python3 --version
echo "✓ Python found"
echo ""

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    echo "✓ Virtual environment created"
else
    echo "✓ Virtual environment already exists"
fi
echo ""

VENV_PYTHON="venv/bin/python"
VENV_PIP="venv/bin/pip"

# Upgrade pip
echo "Upgrading pip..."
$VENV_PYTHON -m pip install --upgrade pip
echo "✓ pip upgraded"
echo ""

# Install dependencies
echo "Installing dependencies from requirements.txt..."
$VENV_PIP install -r requirements.txt
echo "✓ Dependencies installed"
echo ""

# Verify meshtastic import in the virtual environment
echo "Verifying meshtastic installation..."
if ! $VENV_PYTHON -c "import meshtastic.stream_interface"; then
    echo "ERROR: meshtastic is not importable in venv."
    echo "Try reinstalling with: $VENV_PIP install --no-cache-dir -r requirements.txt"
    exit 1
fi
echo "✓ meshtastic import verified"
echo ""

# Install dfrotz (required for Zork / Infocom games)
echo "Checking for dfrotz (Z-machine interpreter for games)..."
if command -v dfrotz &> /dev/null || command -v frotz &> /dev/null; then
    echo "✓ frotz/dfrotz already installed"
else
    if command -v apt-get &> /dev/null; then
        echo "dfrotz not found. It is required to play Zork and other Infocom games."
        read -r -p "Install dfrotz via apt? (requires sudo) [Y/n]: " REPLY
        REPLY=${REPLY:-Y}
        if [[ "$REPLY" =~ ^[Yy]$ ]]; then
            sudo apt-get install -y dfrotz
            echo "✓ dfrotz installed"
        else
            echo "  Skipping dfrotz install. Games will not work until dfrotz is installed."
            echo "  Install later with: sudo apt install dfrotz"
        fi
    elif command -v brew &> /dev/null; then
        echo "dfrotz not found. It is required to play Zork and other Infocom games."
        read -r -p "Install frotz via Homebrew? [Y/n]: " REPLY
        REPLY=${REPLY:-Y}
        if [[ "$REPLY" =~ ^[Yy]$ ]]; then
            brew install frotz
            echo "✓ frotz installed"
        else
            echo "  Skipping frotz install. Games will not work until frotz is installed."
            echo "  Install later with: brew install frotz"
        fi
    else
        echo "  WARNING: Could not find apt-get or brew."
        echo "  Install dfrotz manually before using games."
    fi
fi
echo ""

# Check for config file
if [ ! -f "config.ini" ]; then
    if [ -f "example_config.ini" ]; then
        echo "Creating config.ini from example..."
        cp example_config.ini config.ini
        echo "✓ config.ini created (review and update as needed)"
    fi
fi
echo ""

echo "========================================"
echo "Setup Complete!"
echo "========================================"
echo ""
echo "Next steps:"
echo "1. Review and update config.ini with your settings"
echo "2. Start using venv Python: ./venv/bin/python server.py"
echo ""
echo "Optional interactive shell activation: source venv/bin/activate"
echo "Then you can run: python server.py"
echo ""
