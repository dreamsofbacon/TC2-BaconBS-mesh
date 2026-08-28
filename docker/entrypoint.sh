#!/bin/bash
#
# Starts the BBS and its web admin, and keeps the container's lifetime tied to
# both of them. The two are separate processes by design -- they signal each
# other through trigger files -- so a container that kept running after one of
# them died would look healthy while doing half its job.
set -euo pipefail

APP_DIR=/app
CONFIG_DIR="$(dirname "${BBS_CONFIG_PATH:-/config/config.ini}")"

log() { echo "[baconbs] $*"; }

# ---------------------------------------------------------------------------
# Ownership. Unraid runs everything as its own uid/gid pair (99:100 by
# default, 'nobody:users') and passes them in as PUID/PGID; files written by
# the wrong uid show up unreadable in the share. Skipped entirely when the
# container was already started as a non-root user, e.g. compose's `user:`.
# ---------------------------------------------------------------------------
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
RUN_AS="mesh"

if [ "$(id -u)" = "0" ]; then
    if [ "$(id -g mesh)" != "$PGID" ]; then
        # The gid may already belong to another group in the base image
        # (100 is 'users' on Debian, which is exactly what Unraid defaults
        # to), in which case adopting that group is the right move.
        existing_group="$(getent group "$PGID" | cut -d: -f1 || true)"
        if [ -n "$existing_group" ]; then
            usermod -g "$existing_group" mesh
        else
            groupmod -o -g "$PGID" mesh
        fi
    fi
    if [ "$(id -u mesh)" != "$PUID" ]; then
        usermod -o -u "$PUID" mesh
    fi

    log "running as uid $(id -u mesh) gid $(id -g mesh) (PUID=$PUID PGID=$PGID)"
else
    RUN_AS=""
    log "running as uid $(id -u) gid $(id -g); PUID/PGID ignored (not root)"
fi

mkdir -p "$CONFIG_DIR" "$CONFIG_DIR/data" "${BBS_MQTT_CERT_DIR:-$CONFIG_DIR/mqtt-certs}"

# ---------------------------------------------------------------------------
# First run. example_config.ini is the documented starting point; the web
# admin edits it in place from there.
# ---------------------------------------------------------------------------
if [ ! -f "${BBS_CONFIG_PATH:-/config/config.ini}" ]; then
    cp "$APP_DIR/example_config.ini" "${BBS_CONFIG_PATH:-/config/config.ini}"
    # The build context arrives mode 755 from a Windows checkout, and a
    # config file has no business being executable.
    chmod 644 "${BBS_CONFIG_PATH:-/config/config.ini}"
    log "seeded ${BBS_CONFIG_PATH:-/config/config.ini} from example_config.ini"
    log "web admin login is admin / change-me -- change it under Settings, or"
    log "set BBS_WEBGUI_USER and BBS_WEBGUI_PASSWORD on the container."
fi

# Flask signs session cookies with this. Left at its built-in default, every
# install shares one key and anyone can forge a login cookie for any of them.
# Generated once and kept on the volume so sessions survive a restart.
if [ -z "${BBS_WEBGUI_SECRET:-}" ]; then
    secret_file="$CONFIG_DIR/.session_secret"
    if [ ! -s "$secret_file" ]; then
        python3 -c "import secrets; print(secrets.token_hex(32))" > "$secret_file"
        chmod 600 "$secret_file"
        log "generated a session secret at $secret_file"
    fi
    BBS_WEBGUI_SECRET="$(cat "$secret_file")"
    export BBS_WEBGUI_SECRET
fi

# Reown AFTER everything above has been created, not before. Chowning first
# left config.ini and the session secret owned by root, because they are
# written during this same startup -- and a config file the BBS cannot write
# means every save from the Settings page fails.
#
# Only the config tree is reowned. /app is image content already owned
# correctly, and walking it every boot buys nothing.
if [ "$(id -u)" = "0" ]; then
    chown -R "$(id -u mesh):$(id -g mesh)" "$CONFIG_DIR" 2>/dev/null || true
fi

# A radio passed through with --device that the runtime user cannot open fails
# deep inside the meshtastic library, where the error reads like a radio
# fault rather than a permissions one. Say so up front instead.
for dev in /dev/ttyUSB* /dev/ttyACM* /dev/serial/by-id/*; do
    [ -e "$dev" ] || continue
    if [ -n "$RUN_AS" ]; then
        gosu "$RUN_AS" test -r "$dev" -a -w "$dev" && continue
    else
        [ -r "$dev" ] && [ -w "$dev" ] && continue
    fi
    log "WARNING: $dev is not readable/writable by the runtime user."
    log "  Add the device's group to the container (--group-add \$(stat -c %g $dev))."
done

if [ -n "${BBS_BUILD_NUMBER:-}${BBS_GIT_COMMIT:-}" ]; then
    log "version $(cd "$APP_DIR" && python3 -c 'import version_info; print(version_info.get_display_version())')"
else
    log "WARNING: built without BBS_BUILD_NUMBER/BBS_GIT_COMMIT, so this image"
    log "  reports a fallback version. Build with docker/build.sh to stamp it."
fi


# ---------------------------------------------------------------------------
# Supervision.
#
# The web admin is the control plane: it is the only way to configure a radio,
# an MQTT broker, or anything else on this node. So it anchors the container's
# lifetime -- if it exits, the container exits -- and server.py is restarted
# under it with backoff instead of taking the whole container down.
#
# The alternative was tried and is worse. A BBS that cannot start leaves the
# operator with nothing to fix it WITH: the container restarts, the web admin
# is up for the fraction of a second before server.py fails again, and the
# GUI is effectively unreachable. The health check reports the crash loop
# (see healthcheck.py), which is the honest way to say "running, but not
# doing its job" without also removing the means of repair.
# ---------------------------------------------------------------------------
run() {
    if [ -n "$RUN_AS" ]; then
        exec gosu "$RUN_AS" python3 "$APP_DIR/$1" "${@:2}"
    fi
    exec python3 "$APP_DIR/$1" "${@:2}"
}

cd "$APP_DIR"

run web_admin.py &
WEB_PID=$!

start_bbs() {
    run server.py "$@" &
    BBS_PID=$!
    BBS_STARTED_AT=$SECONDS
}
start_bbs "$@"

shutdown() {
    log "stopping"
    kill -TERM "$WEB_PID" "${BBS_PID:-}" 2>/dev/null || true
    wait 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

# Sleep in one-second steps so a `docker stop` during a restart backoff is
# acted on immediately: bash runs a trap between commands, not during one.
interruptible_sleep() {
    local remaining="$1"
    while [ "$remaining" -gt 0 ]; do
        sleep 1
        remaining=$((remaining - 1))
    done
}

set +e
delay=5
while :; do
    wait -n
    if ! kill -0 "$WEB_PID" 2>/dev/null; then
        log "web_admin.py exited; stopping the container"
        kill -TERM "$BBS_PID" 2>/dev/null
        wait 2>/dev/null
        exit 1
    fi
    if ! kill -0 "$BBS_PID" 2>/dev/null; then
        # A BBS that ran for a while and then died is a fresh fault, not part
        # of the same failing loop, so it starts over at the short delay.
        if [ $((SECONDS - BBS_STARTED_AT)) -ge 60 ]; then
            delay=5
        fi
        log "WARNING: server.py exited; restarting in ${delay}s."
        log "  The web admin is still up -- check its log above for the cause."
        interruptible_sleep "$delay"
        start_bbs "$@"
        delay=$((delay * 2))
        [ "$delay" -gt 30 ] && delay=30
    fi
done
