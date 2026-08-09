# BaconBS-mesh

A feature-rich, offline-first Bulletin Board System for [Meshtastic](https://meshtastic.org/) and [MeshCore](https://meshcore.co.uk/) mesh radio networks. BaconBS-mesh enables asynchronous communication across low-bandwidth LoRa links with no internet dependency — designed for resilience in the field.

Forked from [TC²-BBS-mesh](https://github.com/TheCommsChannel/TC2-BBS-mesh) with significant protocol and reliability improvements.

---

## Features

- **Private Mail** — Send and receive direct messages between mesh nodes
- **Bulletin Boards** — Post and browse community bulletins across configurable boards
- **Channel Directory** — Named discussion channels with threaded comments
- **User Profiles** — Short name, bio, and activity statistics per node
- **Interactive Games** — Zork I–III, Hitchhiker's Guide to the Galaxy, Enchanter, Planetfall, Starcross (via dfrotz); per-user save states synced across the mesh
- **JS8Call Bridge** — Optional integration with JS8Call for group, direct, and urgent radio messages
- **Node Statistics** — View node counts, hardware types, and roles on the mesh
- **Wall of Shame** — Devices with low battery levels
- **Fortune Teller** — Random fortunes from a configurable text file
- **Web Admin Dashboard** — Full moderation interface at `localhost:8081` with real-time sync monitoring, peer hash visualizations, transmission logs, and manual sync controls

---

## Sync Protocol

BaconBS-mesh uses a custom five-phase distributed sync protocol designed for lossy, low-bandwidth LoRa links. All data is eventually consistent across peers with no central server.

**Five sync phases (in priority order):**
1. **Mail** — Direct messages (highest priority; aborts remaining phases on failure)
2. **Bulletins** — Board posts
3. **Channels** — Discussion threads and comments
4. **Profiles** — User metadata
5. **Game saves** — Zork save files (lowest priority; skipped until other scopes converge)

**How it works:**
- Nodes periodically broadcast `SYNCSTATE` packets containing per-scope record counts and BLAKE2b hash fingerprints
- Hash mismatches trigger compressed manifest (`HASHZ`) exchanges using base85 encoding
- Missing records are requested individually via `HASHMISS`; gap-fill retries handle packet loss
- Tombstone-based deletion reconciliation propagates deletes across the mesh — removed records are not resurrected when peers reconnect
- Capability negotiation (`v2:caps`) allows protocol features (compact keys, epoch timestamps, bitmap gap-fill, UTF-8 encoding) to be adopted gracefully across heterogeneous peers

**Reliability features:**
- Jittered inter-frame spacing prevents LoRa half-duplex collisions
- Backpressure caps on manifest pulls per cycle
- Automatic reconnect to the radio interface on TCP/serial connection loss
- Main-loop watchdog detects and recovers from wedged radio sends

---

## Requirements

- Python 3.10+
- Either a Meshtastic device (serial or TCP) or a MeshCore companion radio
  (serial, TCP, or BLE)
- `pip install -r requirements.txt` (installs both radio client libraries,
  `pypubsub`, and `flask`)
- **dfrotz** (optional, required for games): `sudo apt install frotz`

---

## Installation

### Linux / Raspberry Pi

```sh
sudo apt update && sudo apt install git
git clone https://github.com/dreamsofbacon/TC2-BaconBS-mesh.git
cd TC2-BaconBS-mesh
bash setup.sh
cp example_config.ini config.ini
```

### Windows

```powershell
git clone https://github.com/dreamsofbacon/TC2-BaconBS-mesh.git
cd TC2-BaconBS-mesh
.\setup.ps1
copy example_config.ini config.ini
```

The setup scripts create a Python virtual environment and install all dependencies automatically.

---

## Configuration

Edit `config.ini` before running. Key sections:

### Interface

Meshtastic serial:

```ini
[interface]
type = serial
# port = /dev/ttyUSB0   # Linux serial
# port = COM3            # Windows serial

# Or for WiFi-connected devices:
# type = tcp
# hostname = 192.168.1.x
```

MeshCore companion serial:

```ini
[interface]
type = meshcore_serial
port = /dev/ttyACM0
baudrate = 115200
channel_index = 0  # MeshCore channel used for broadcast notifications
```

MeshCore companion TCP or BLE:

```ini
# TCP
[interface]
type = meshcore_tcp
hostname = 192.168.1.x
tcp_port = 5000

# BLE (use this section instead of TCP)
# [interface]
# type = meshcore_ble
# ble_address = 12:34:56:78:90:AB  # optional; omit to scan
# ble_pin = 123456                 # optional
```

The MeshCore radio must run **companion firmware**. A repeater, room-server,
or other non-companion build does not expose the client protocol used here.

### Sync Peers

Add the node IDs of other BaconBS-mesh nodes you want to sync with. For
Meshtastic, use the usual `!xxxxxxxx` node IDs. For MeshCore, use each
companion's full public key or a unique prefix of at least 12 hexadecimal
characters; these are visible in MeshCore contact details and the web admin
diagnostics.

```ini
[sync]
bbs_nodes = !f53f4abc,!f3abc123
```

MeshCore example:

```ini
[sync]
bbs_nodes = 7e18ca9d30a1,4b0264c19f6e
```

Every BBS node normally uses one radio protocol at a time — but any node can
optionally be configured as a **dual-radio bridge** between a Meshtastic
network and a MeshCore network. See [Dual-Radio Bridge Mode](#dual-radio-bridge-mode)
below.

### Dual-Radio Bridge Mode

A single node can run **two radios at once** — one Meshtastic, one MeshCore
(either order) — and participate in the full five-phase sync protocol on
both networks simultaneously. Mail, bulletins, channels, profiles, and game
saves stay consistent across both networks through this node, as if they
were one unified mesh.

This is *not* a packet-level RF relay — the bridge node doesn't retransmit
raw frames between the two radios. Instead, both radios' sync engines run
independently against the **same local database**: content synced in from
one network lands in the shared DB, and the other radio's own sync cycle
picks it up and pushes it out on its next scheduled or mismatch-triggered
pass. This means cross-network propagation is *eventually consistent* (on
the normal sync cadence — a few minutes by default, sooner via SYNCSTATE
heartbeats), not instantaneous.

To enable it, add a second `[interface2]` section (same keys as
`[interface]`) plus `[sync2]`/`[allow_list2]` for that radio's own peer
list — everything is additive, so a config file without these sections
behaves exactly as before:

```ini
[interface]
type = serial
port = /dev/ttyUSB0

[sync]
bbs_nodes = !f53f4abc

[interface2]
type = meshcore_tcp
hostname = 192.168.1.50
tcp_port = 5000

[sync2]
bbs_nodes = 7e18ca9d30a1

[allow_list2]
allowed_nodes = 7e18ca9d30a1
```

Notes:

- Keep `[sync]`/`[sync2]` peer lists strictly separate — a node ID belongs
  to exactly one network (Meshtastic `!xxxxxxxx` vs. MeshCore's bare hex
  keys make this easy to tell apart at a glance), and nothing downstream
  validates that a peer configured under one section is actually reachable
  on that radio.
- Each radio chunks outbound sync frames to its own transport's byte limit
  (220 bytes for Meshtastic, 160 for MeshCore) automatically.
- If one radio's connection drops, only that radio's side degrades — the
  other radio keeps syncing normally while the dead one reconnects with
  backoff in the background.
- Currently supported/tested with exactly one bridge node between a given
  Meshtastic network and a given MeshCore network. Running more than one
  bridge node between the same two networks is unanalyzed and not a
  supported topology yet.
- The web admin "Radio Device" and Settings → Diagnostics pages show a
  separate status card per active radio when bridge mode is on.

### Sync Tuning

The default pacing is conservative for busy meshes. On a small network (2–3 nodes), turbo mode dramatically speeds up initial replication:

```ini
[sync]
sync_turbo = true   # WARNING: safe for 2–3 nodes only — see note below
```

> **Turbo mode warning:** The inter-frame pause prevents LoRa packet collisions. With 3+ active BBS peers, turbo can worsen convergence by causing the packet loss it tries to outrun. Only enable on small meshes.

Fine-grained pacing controls (all optional):

```ini
sync_pause_seconds = 0.75          # delay between TX frames (turbo: 0.02)
hash_repair_pause_seconds = 0.1    # delay between repair frames (turbo: 0.0)
hash_chunk_pause_seconds = 1.5     # minimum gap between consecutive HASHZ chunks
repair_cycle_seconds = 90          # minimum seconds between repair cycles per peer
reconcile_max_per_pass = 20        # max records pulled/pushed per repair cycle
sync_interval_minutes = 5          # how often a full P1–P5 sync runs
```

### Menu Customization

Remove items you don't want to expose to users:

```ini
[menu]
main_menu_items = Q, B, U, P, X
bbs_menu_items = M, B, C, J, X
utilities_menu_items = S, F, W, G, X
```

---

## Running

### Server

```sh
# Linux (venv)
./venv/bin/python server.py

# Windows (venv)
.\.venv\Scripts\python.exe server.py

# Or use the launch scripts:
bash run_server.sh        # Linux/macOS
.\run_server.ps1          # Windows PowerShell
run_server.bat            # Windows CMD
```

### Web Admin

```sh
./venv/bin/python web_admin.py
```

Then open `http://localhost:8081` in your browser. Default credentials: `admin` / `change-me` (change these before exposing to a network).

Environment overrides:

```sh
export BBS_WEBGUI_USER=admin
export BBS_WEBGUI_PASSWORD=your-password
export BBS_WEBGUI_SECRET=your-session-secret
export BBS_WEBGUI_HOST=127.0.0.1
export BBS_WEBGUI_PORT=8081
```

---

## Running at Boot (Linux / systemd)

The repository includes `mesh-bbs.service`, `bacon-web-admin.service`, and an installer script.

```sh
chmod +x install_services.sh
bash install_services.sh
```

Non-interactive:

```sh
bash install_services.sh --yes --user "$USER" --dir "$HOME/TC2-BaconBS-mesh"
```

**Service controls:**

```sh
sudo systemctl status mesh-bbs.service bacon-web-admin.service
sudo systemctl restart mesh-bbs.service bacon-web-admin.service
journalctl -u mesh-bbs.service -f
```

**If using Zork**, add these to `mesh-bbs.service` so the interpreter is found under systemd:

```ini
Environment="PATH=/usr/games:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="BBS_ZORK_INTERPRETER=/usr/games/dfrotz"
```

---

## Remote Update (Windows → Linux Nodes)

For operators managing Linux nodes from a Windows machine:

- `scripts/update-two-nodes.ps1` — local orchestrator (requires Posh-SSH)
- `scripts/remote-node-update.sh` — runs on each node via SSH
- `scripts/node-update-config.json.example` — configuration template

```powershell
.\scripts\update-two-nodes.ps1
```

Credentials are stored securely in `%APPDATA%\TC2-BaconBS\node-update-cred.xml` on first run. Use `-ResetCredential` to update them.

---

## Radio Configuration

The following Meshtastic device roles are confirmed working:

- **Client**
- **Router_Client**

Some other roles have been reported to cause the node to stop responding after a short time.

For MeshCore, flash a **companion** build and make sure every intended peer is
present in the radio's contact list. BaconBS sends direct encrypted MeshCore
messages and uses MeshCore's automatic routing/flood fallback. MeshCore's
160-byte text ceiling is detected automatically; BBS sync frames and user
replies are chunked to fit it.

---

## Usage

Send a direct message to the BBS node from any Meshtastic or MeshCore contact.
Any message triggers the main menu. Navigate by sending the letter shown in
brackets — for example, send `B` for `[B]BS`.

---

## Smoke Test

Run a basic integration test (no radio required):

```sh
python tests/smoke_test.py
```

---

## Command Line Reference

```
python server.py --help

usage: server.py [-h] [--config CONFIG]
                 [--interface-type {serial,tcp,meshcore_serial,meshcore_tcp,meshcore_ble}]
                 [--port PORT] [--host HOST] [--tcp-port TCP_PORT]
                 [--baudrate BAUDRATE] [--ble-address BLE_ADDRESS]
                 [--ble-pin BLE_PIN] [--channel-index CHANNEL_INDEX]
                 [--mqtt-topic MQTT_TOPIC]

options:
  -h, --help                        show this help message and exit
  --config CONFIG, -c CONFIG        Path to config file
  --interface-type {...}            Radio interface type
  --port PORT, -p PORT              Serial port
  --host HOST                       TCP hostname
  --tcp-port TCP_PORT               MeshCore TCP port
  --baudrate BAUDRATE               MeshCore serial baud rate
  --ble-address BLE_ADDRESS         MeshCore BLE address (omit to scan)
  --ble-pin BLE_PIN                 Optional MeshCore BLE pairing PIN
  --channel-index CHANNEL_INDEX     MeshCore broadcast channel index
  --mqtt-topic MQTT_TOPIC           MQTT topic to subscribe
```

---

## Acknowledgements

- [TheCommsChannel](https://github.com/TheCommsChannel) — original TC²-BBS-mesh
- [Meshtastic](https://github.com/meshtastic) and [pdxlocations](https://github.com/pdxlocations) — Python library and examples
- [MeshCore](https://github.com/meshcore-dev/MeshCore) and [meshcore_py](https://github.com/meshcore-dev/meshcore_py) — companion protocol, firmware, and Python library
- [Jordan Sherer](https://bitbucket.org/widefido/js8call) — JS8Call and the TCP API example

---

## License

GNU General Public License v3.0
