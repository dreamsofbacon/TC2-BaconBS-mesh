#!/bin/bash
#
# Bacon BBS -- the whole install, with questions.
#
#   git clone https://github.com/dreamsofbacon/TC2-BaconBS-mesh.git
#   cd TC2-BaconBS-mesh
#   bash install.sh
#
# It installs what the system needs, sets up Python, asks how this node
# connects and who can reach it, writes config.ini, installs the services
# and checks they came up. Run it again at any time: it keeps an existing
# config.ini unless you pass --reconfigure.
#
# Debian, Ubuntu and Raspberry Pi OS. Run it as the user the BBS will run
# as -- not root. On a fresh VPS where you only have root:
#   adduser bacon && usermod -aG sudo bacon && su - bacon

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"
# Not $USER: it is unset under su, sudo -u and docker exec.
RUN_USER="$(id -un)"

ASSUME_YES="false"
RECONFIGURE="false"
RADIO=""
RADIO_PORT=""
RADIO_HOST=""
ADMIN_USER=""
ADMIN_PASSWORD="${BBS_ADMIN_PASSWORD:-}"
WEB_ACCESS=""
SSH_MODE=""
GAMES="yes"
SERVICES="yes"
JOIN_INVITE=""
JOIN_NAME=""
ARM_UPDATES="false"

usage() {
    cat <<EOF
Usage: bash install.sh [options]

With no options it asks everything it needs. Options answer the questions
ahead of time, for a scripted install:

  --radio TYPE           meshtastic-usb | meshtastic-wifi | meshcore-usb |
                         meshcore-wifi | none (no radio, syncs over MQTT)
  --port PATH            serial port for a USB radio
  --host ADDRESS         IP address of a WiFi radio
  --admin-user NAME      web admin username (default: admin)
  --admin-password PASS  web admin password (or set BBS_ADMIN_PASSWORD)
  --web-access WHO       lan   = anyone on this network (radio default)
                         local = this machine only; reach it with an SSH
                                 tunnel or Tailscale (no-radio default)
  --ssh MODE             off | accounts | public   (default: off)
  --no-games             skip installing frotz (Zork and other text games)
  --no-services          set up only; do not install systemd services
  --join FILE            join a fleet from an invite file: broker, TLS,
                         credentials, topic and peers, in one step
  --join-name NAME       this node's name on that link (default: the name
                         the invite gives it)
  --arm-updates          also trust the signing key the invite carries, so
                         that fleet can update this node's code
  --reconfigure          ask the questions again even if config.ini exists
  -y, --yes              take the default for anything not given
  -h, --help             show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --radio) RADIO="${2:?--radio needs a value}"; shift 2 ;;
        --port) RADIO_PORT="${2:?--port needs a value}"; shift 2 ;;
        --host) RADIO_HOST="${2:?--host needs a value}"; shift 2 ;;
        --admin-user) ADMIN_USER="${2:?--admin-user needs a value}"; shift 2 ;;
        --admin-password) ADMIN_PASSWORD="${2:?--admin-password needs a value}"; shift 2 ;;
        --web-access) WEB_ACCESS="${2:?--web-access needs a value}"; shift 2 ;;
        --ssh) SSH_MODE="${2:?--ssh needs a value}"; shift 2 ;;
        --no-games) GAMES="no"; shift ;;
        --no-services) SERVICES="no"; shift ;;
        --join) JOIN_INVITE="${2:?--join needs a file}"; shift 2 ;;
        --join-name) JOIN_NAME="${2:?--join-name needs a value}"; shift 2 ;;
        --arm-updates) ARM_UPDATES="true"; shift ;;
        --reconfigure) RECONFIGURE="true"; shift ;;
        -y|--yes) ASSUME_YES="true"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

# ── Small helpers ────────────────────────────────────────────────────────────

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mStopped:\033[0m %s\n' "$*" >&2; exit 1; }

# ask VAR "Question" "default"
ask() {
    local answer
    if [[ "$ASSUME_YES" == "true" ]]; then
        printf -v "$1" '%s' "$3"
        return
    fi
    read -r -p "  $2 [$3]: " answer
    printf -v "$1" '%s' "${answer:-$3}"
}

# choose VAR "Question" default_number "option one" "option two" ...
choose() {
    local var="$1" question="$2" default="$3" answer index
    shift 3
    if [[ "$ASSUME_YES" == "true" ]]; then
        printf -v "$var" '%s' "$default"
        return
    fi
    echo "  $question"
    index=1
    for option in "$@"; do
        echo "    $index) $option"
        index=$((index + 1))
    done
    while true; do
        read -r -p "  Choose 1-$#: [$default] " answer
        answer="${answer:-$default}"
        if [[ "$answer" =~ ^[0-9]+$ ]] && (( answer >= 1 && answer <= $# )); then
            printf -v "$var" '%s' "$answer"
            return
        fi
        echo "  Please enter a number from 1 to $#."
    done
}

set_config() {
    venv/bin/python scripts/config_set.py config.ini "$@"
}

# ── 0. Sanity ────────────────────────────────────────────────────────────────

if [[ "$EUID" -eq 0 ]]; then
    die "Run this as the user the BBS will run as, not as root.
  On a fresh server:  adduser bacon && usermod -aG sudo bacon && su - bacon
  then clone the repository again as that user and run bash install.sh."
fi
command -v sudo >/dev/null || die "sudo is not installed. As root: apt install sudo"
[[ -f server.py && -f example_config.ini ]] \
    || die "Run this from inside the TC2-BaconBS-mesh folder."

cat <<'EOF'

  Bacon BBS installer
  -------------------
  This installs the system packages the BBS needs, sets up Python, asks a
  few questions to write config.ini, and starts the BBS as a service.
  You will be asked for your password once, for sudo.
EOF

# setup.sh copies the example config when there is none, so whether this
# node already had its own config has to be known before it runs.
HAD_CONFIG="false"
[[ -f config.ini ]] && HAD_CONFIG="true"

# ── 1. System packages ───────────────────────────────────────────────────────

say "1/5  System packages"
if command -v apt-get >/dev/null; then
    PACKAGES=(git python3 python3-venv python3-pip)
    if [[ "$GAMES" == "yes" ]]; then
        PACKAGES+=(frotz)
    fi
    MISSING=()
    for package in "${PACKAGES[@]}"; do
        dpkg -s "$package" >/dev/null 2>&1 || MISSING+=("$package")
    done
    if (( ${#MISSING[@]} )); then
        echo "  Installing: ${MISSING[*]}"
        sudo apt-get update -qq > /tmp/baconbbs-apt.log 2>&1
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${MISSING[@]}" \
            > /tmp/baconbbs-apt.log 2>&1 \
            || { tail -20 /tmp/baconbbs-apt.log; die "Package install failed. Full log: /tmp/baconbbs-apt.log"; }
    fi
    ok "Installed: ${PACKAGES[*]}"
else
    warn "Not a Debian-family system: install git, python3 (3.10+), python3-venv"
    warn "and frotz yourself if anything below fails."
fi

# ── 2. Python ────────────────────────────────────────────────────────────────

say "2/5  Python environment (this takes a few minutes the first time)"
BBS_SETUP_SKIP_GAMES=1 bash setup.sh > /tmp/baconbbs-setup.log 2>&1 \
    || { tail -20 /tmp/baconbbs-setup.log; die "Python setup failed. Full log: /tmp/baconbbs-setup.log"; }
ok "Python packages installed (log: /tmp/baconbbs-setup.log)"

# ── 3. Configuration ─────────────────────────────────────────────────────────

say "3/5  Configuration"
if [[ "$HAD_CONFIG" == "true" && "$RECONFIGURE" != "true" ]]; then
    ok "Keeping your existing config.ini (bash install.sh --reconfigure to redo it)"
else
    [[ -f config.ini ]] || cp example_config.ini config.ini

    # Radio
    if [[ -z "$RADIO" ]]; then
        choose RADIO_CHOICE "How does this node reach the mesh?" 1 \
            "Meshtastic radio on USB" \
            "Meshtastic radio on WiFi" \
            "MeshCore companion radio on USB" \
            "MeshCore companion radio on WiFi" \
            "No radio -- a server (VPS) that syncs with other nodes over the internet"
        RADIO_TYPES=(meshtastic-usb meshtastic-wifi meshcore-usb meshcore-wifi none)
        RADIO="${RADIO_TYPES[$((RADIO_CHOICE - 1))]}"
    fi

    case "$RADIO" in
        meshtastic-usb|meshcore-usb)
            DETECTED="$(ls /dev/serial/by-id/* 2>/dev/null | head -1 || true)"
            if [[ -z "$RADIO_PORT" ]]; then
                if [[ -n "$DETECTED" ]]; then
                    echo "  Found a USB serial device: $DETECTED"
                fi
                if [[ "$RADIO" == "meshtastic-usb" ]]; then
                    ask RADIO_PORT "Serial port (blank = find it automatically)" "${DETECTED}"
                else
                    ask RADIO_PORT "Serial port" "${DETECTED:-/dev/ttyACM0}"
                fi
            fi
            TYPE=serial; [[ "$RADIO" == "meshcore-usb" ]] && TYPE=meshcore_serial
            if [[ -n "$RADIO_PORT" ]]; then
                set_config interface "type=$TYPE" "port=$RADIO_PORT"
            else
                set_config interface "type=$TYPE"
            fi
            # A serial port belongs to the dialout group. Without it the
            # service sees "permission denied" and retries forever.
            if ! id -nG "$RUN_USER" | grep -qw dialout; then
                sudo usermod -aG dialout "$RUN_USER"
                ok "Added $RUN_USER to the dialout group (serial port access)"
            fi
            ;;
        meshtastic-wifi|meshcore-wifi)
            [[ -n "$RADIO_HOST" ]] || ask RADIO_HOST "Radio's IP address" "192.168.1.100"
            if [[ "$RADIO" == "meshtastic-wifi" ]]; then
                set_config interface "type=tcp" "hostname=$RADIO_HOST"
            else
                set_config interface "type=meshcore_tcp" "hostname=$RADIO_HOST" "tcp_port=5000"
            fi
            ;;
        none)
            set_config interface "type=none"
            ;;
        *) die "Unknown radio type: $RADIO" ;;
    esac
    ok "Radio: $RADIO${RADIO_PORT:+ on $RADIO_PORT}${RADIO_HOST:+ at $RADIO_HOST}"

    # Web admin login
    [[ -n "$ADMIN_USER" ]] || ask ADMIN_USER "Web admin username" "admin"
    if [[ -z "$ADMIN_PASSWORD" ]]; then
        if [[ "$ASSUME_YES" == "true" ]]; then
            ADMIN_PASSWORD="$(venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(12))')"
            GENERATED_PASSWORD="true"
        else
            while true; do
                read -r -s -p "  Web admin password (8+ characters): " ADMIN_PASSWORD; echo
                if (( ${#ADMIN_PASSWORD} < 8 )); then
                    echo "  Too short -- use at least 8 characters."
                    continue
                fi
                read -r -s -p "  Same password again: " CONFIRM; echo
                [[ "$ADMIN_PASSWORD" == "$CONFIRM" ]] && break
                echo "  Those did not match. Try again."
            done
        fi
    fi
    # configparser reads % as the start of a placeholder; %% is a literal %.
    set_config admin "username=$ADMIN_USER" "password=${ADMIN_PASSWORD//%/%%}"
    ok "Web admin login: $ADMIN_USER"

    # Who can reach the web admin
    if [[ -z "$WEB_ACCESS" ]]; then
        DEFAULT_WEB=1; [[ "$RADIO" == "none" ]] && DEFAULT_WEB=2
        choose WEB_CHOICE "Who should be able to open the web admin?" "$DEFAULT_WEB" \
            "Anyone on this local network (home setups; never port-forward it)" \
            "Only this machine -- reach it with an SSH tunnel or Tailscale (servers)"
        WEB_ACCESS=lan; [[ "$WEB_CHOICE" == "2" ]] && WEB_ACCESS=local
    fi
    case "$WEB_ACCESS" in
        lan) WEB_HOST=0.0.0.0 ;;
        local) WEB_HOST=127.0.0.1 ;;
        *) die "--web-access must be lan or local" ;;
    esac
    touch web-admin.env
    sed -i '/^BBS_WEBGUI_HOST=/d' web-admin.env
    echo "BBS_WEBGUI_HOST=$WEB_HOST" >> web-admin.env
    ok "Web admin reachable from: $WEB_ACCESS"

    # SSH access to the BBS
    if [[ -z "$SSH_MODE" ]]; then
        choose SSH_CHOICE "Let people use the BBS over SSH (port 2222)?" 1 \
            "No" \
            "Yes -- people register and log in with their own password" \
            "Yes, public -- no SSH password; the BBS asks for their account"
        SSH_MODES=(off accounts public)
        SSH_MODE="${SSH_MODES[$((SSH_CHOICE - 1))]}"
    fi
    case "$SSH_MODE" in
        off) set_config ssh "enabled=false" ;;
        accounts) set_config ssh "enabled=true" "host=0.0.0.0" "port=2222" "public_access=false" ;;
        public) set_config ssh "enabled=true" "host=0.0.0.0" "port=2222" "public_access=true" ;;
        *) die "--ssh must be off, accounts or public" ;;
    esac
    ok "SSH access: $SSH_MODE"
fi

# ── 3b. Join a fleet, if an invite was given ────────────────────────────────

if [[ -n "$JOIN_INVITE" ]]; then
    say "Joining a fleet from $JOIN_INVITE"
    if [[ ! -f "$JOIN_INVITE" ]]; then
        die "No such invite file: $JOIN_INVITE"
    fi
    JOIN_FLAGS=(--config "$REPO_DIR/config.ini")
    [[ -n "$JOIN_NAME" ]] && JOIN_FLAGS+=(--name "$JOIN_NAME")
    [[ "$ARM_UPDATES" == "true" ]] && JOIN_FLAGS+=(--arm-updates)
    # The passphrase is prompted for by the script itself unless
    # BBS_INVITE_PASSPHRASE is set: it must not sit in a shell history.
    venv/bin/python scripts/join_fleet.py "$JOIN_INVITE" "${JOIN_FLAGS[@]}" \
        || die "Could not join from that invite."
fi

# Passwords live in these two files.
chmod 600 config.ini
[[ -f web-admin.env ]] && chmod 600 web-admin.env
ok "config.ini is readable only by $RUN_USER"

# ── 4. Services ──────────────────────────────────────────────────────────────

if [[ "$SERVICES" != "yes" ]]; then
    say "4/5  Services -- skipped (--no-services)"
    echo "  Start it by hand with: ./venv/bin/python server.py"
    exit 0
fi

say "4/5  Services"
INTERFACE_TYPE="$(venv/bin/python -c 'import configparser; c = configparser.ConfigParser(); c.read("config.ini"); print(c.get("interface", "type", fallback="serial"))')"
SERVICE_FLAGS=(--yes --user "$RUN_USER" --dir "$REPO_DIR")
[[ "$INTERFACE_TYPE" == "none" ]] && SERVICE_FLAGS+=(--no-usb-fix)
SSH_ENABLED="$(venv/bin/python -c 'import configparser; c = configparser.ConfigParser(); c.read("config.ini"); print(c.get("ssh", "enabled", fallback="false").strip().lower())')"
BBS_INSTALLER=1 bash install_services.sh "${SERVICE_FLAGS[@]}" 2>&1 | sed 's/^/  /'
if [[ "$SSH_ENABLED" == "true" ]]; then
    sudo systemctl enable --now bacon-ssh.service >/dev/null 2>&1
    sudo systemctl restart bacon-ssh.service
    ok "SSH service enabled"
else
    sudo systemctl disable --now bacon-ssh.service >/dev/null 2>&1 || true
fi

# ── 5. Check ─────────────────────────────────────────────────────────────────

say "5/5  Checking it came up"
UNITS=(mesh-bbs.service bacon-web-admin.service)
[[ "$SSH_ENABLED" == "true" ]] && UNITS+=(bacon-ssh.service)
HEALTHY="true"
for attempt in $(seq 1 20); do
    DOWN=()
    for unit in "${UNITS[@]}"; do
        systemctl is-active --quiet "$unit" || DOWN+=("$unit")
    done
    WEB_OK="false"
    venv/bin/python - <<'EOF' >/dev/null 2>&1 && WEB_OK="true"
import urllib.request
urllib.request.urlopen("http://127.0.0.1:8081/login", timeout=3)
EOF
    if (( ${#DOWN[@]} == 0 )) && [[ "$WEB_OK" == "true" ]]; then
        break
    fi
    sleep 2
done
for unit in "${UNITS[@]}"; do
    if systemctl is-active --quiet "$unit"; then
        ok "$unit is running"
    else
        warn "$unit is not running -- see: sudo journalctl -u $unit -n 50"
        HEALTHY="false"
    fi
done
if [[ "$WEB_OK" == "true" ]]; then
    ok "Web admin answers on port 8081"
else
    warn "Web admin is not answering yet -- see: sudo journalctl -u bacon-web-admin -n 50"
    HEALTHY="false"
fi

# ── Done ─────────────────────────────────────────────────────────────────────

WEB_HOST_NOW="$(sed -n 's/^BBS_WEBGUI_HOST=//p' web-admin.env 2>/dev/null | tail -1 || true)"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
say "Done"
if [[ "$WEB_HOST_NOW" == "127.0.0.1" ]]; then
    echo "  Web admin (this machine only). From your computer run:"
    echo "      ssh -L 8081:localhost:8081 $RUN_USER@${LAN_IP:-this-server}"
    echo "  then open http://localhost:8081"
    echo "  For Tailscale instead, see docs/VPS-NODE.md."
else
    echo "  Web admin:  http://${LAN_IP:-this-machine}:8081"
    echo "  Keep it on your local network: do not port-forward 8081."
fi
if [[ "${GENERATED_PASSWORD:-false}" == "true" ]]; then
    echo "  Login:      $ADMIN_USER / $ADMIN_PASSWORD   (generated -- write it down)"
fi
if [[ "${SSH_ENABLED:-false}" == "true" ]]; then
    echo "  BBS by SSH: ssh -p 2222 ${LAN_IP:-this-machine}"
fi
echo
if [[ -n "$JOIN_INVITE" ]]; then
    echo "  Joined the fleet from the invite. Check it with:"
    echo "      venv/bin/python scripts/node_doctor.py"
else
    echo "  Next: to join your fleet, download an invite from one of your nodes"
    echo "  (Settings > Invite & Join) and either open it there, or copy it here"
    echo "  and run: bash install.sh --join <file>"
fi
echo "  Logs:  sudo journalctl -u mesh-bbs -f"
[[ "$HEALTHY" == "true" ]] || exit 1
