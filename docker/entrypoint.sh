#!/bin/sh
set -eu

if [ ! -f "${BBS_CONFIG_PATH}" ]; then
    cp /home/mesh/bbs/example_config.ini "${BBS_CONFIG_PATH}"
fi

exec python3 /home/mesh/bbs/server.py "$@"
