#!/usr/bin/env bash
set -euo pipefail

REPO_PATH="${REPO_PATH:-$HOME/TC2-BaconBS-mesh}"
BRANCH="${BRANCH:-main}"
SERVICES=(mesh-bbs.service bacon-web-admin.service)

usage() {
  cat <<'USAGE'
Usage: remote-node-update.sh [--repo-path PATH] [--branch BRANCH] [--services "svc1 svc2"]

Examples:
  ./remote-node-update.sh --repo-path /opt/TC2-BaconBS-mesh --branch main
  ./remote-node-update.sh --services "mesh-bbs.service bacon-web-admin.service"
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-path)
      REPO_PATH="$2"
      shift 2
      ;;
    --branch)
      BRANCH="$2"
      shift 2
      ;;
    --services)
      IFS=' ' read -r -a SERVICES <<< "$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown arg: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ ! -d "$REPO_PATH/.git" ]]; then
  echo "Repo not found at: $REPO_PATH"
  exit 1
fi

echo "[remote] Updating repo at $REPO_PATH on branch $BRANCH"
cd "$REPO_PATH"

git fetch --all --prune
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "[remote] Restarting services"
for svc in "${SERVICES[@]}"; do
  echo "[remote] restarting $svc"
  sudo systemctl restart "$svc"
  sudo systemctl is-active "$svc" --quiet
  echo "[remote] $svc is active"
done

echo "[remote] Update complete"
