#!/usr/bin/env bash
# Bacon BBS Meshtastic Web UI Setup Script (Linux/macOS)
# Clones and builds the Meshtastic web client and places it where web_admin.py can serve it.

set -euo pipefail

REPO_URL="https://github.com/meshtastic/web.git"
CLONE_DIR="meshtastic-web"
WEB_PKG_DIR="${CLONE_DIR}/packages/web"
DIST_DIR="${WEB_PKG_DIR}/dist"
TARGET_DIR="meshtastic-web-dist"

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}Bacon BBS Meshtastic Web UI Setup${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""

# Check prerequisites
for cmd in git node pnpm; do
    if ! command -v "$cmd" &>/dev/null; then
        echo -e "${RED}ERROR: '$cmd' not found in PATH.${NC}"
        [ "$cmd" = "pnpm" ] && echo -e "${YELLOW}Install pnpm:  npm install -g pnpm${NC}"
        exit 1
    fi
done
echo -e "${GREEN}Prerequisites OK (git, node, pnpm found)${NC}"
echo ""

# Clone or update
if [ -d "$CLONE_DIR" ]; then
    echo -e "${YELLOW}Updating existing clone at ./${CLONE_DIR} ...${NC}"
    git -C "$CLONE_DIR" pull --ff-only
else
    echo -e "${YELLOW}Cloning $REPO_URL ...${NC}"
    git clone --depth 1 "$REPO_URL" "$CLONE_DIR"
fi
echo ""

# Install workspace deps
echo -e "${YELLOW}Installing pnpm workspace dependencies ...${NC}"
(cd "$CLONE_DIR" && pnpm install --frozen-lockfile)
echo ""

# Build packages/web
echo -e "${YELLOW}Building Meshtastic web client ...${NC}"
(cd "$WEB_PKG_DIR" && pnpm run build)
echo ""

# Copy dist to target
if [ -d "$TARGET_DIR" ]; then
    rm -rf "$TARGET_DIR"
fi
echo -e "${YELLOW}Copying built files to ./${TARGET_DIR} ...${NC}"
cp -r "$DIST_DIR" "$TARGET_DIR"
echo ""

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Meshtastic Web UI ready!${NC}"
echo -e "${GREEN}Start the web admin and navigate to /mesh-ui/${NC}"
echo -e "${GREEN}========================================${NC}"
