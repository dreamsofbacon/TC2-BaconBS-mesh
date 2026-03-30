#!/bin/bash

set -euo pipefail

if [ -x "./.venv/bin/python" ]; then
  VENV_PYTHON="./.venv/bin/python"
elif [ -x "./venv/bin/python" ]; then
  VENV_PYTHON="./venv/bin/python"
else
  echo "ERROR: No virtual environment found (.venv or venv)."
  echo "Run setup first: bash setup.sh"
  exit 1
fi

exec "$VENV_PYTHON" ./web_admin.py "$@"
