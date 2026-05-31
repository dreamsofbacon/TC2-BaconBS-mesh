# BaconBS-mesh

A feature-rich, offline-first Bulletin Board System for [Meshtastic](https://meshtastic.org/) mesh radio networks. BaconBS-mesh enables asynchronous communication across low-bandwidth LoRa links with no internet dependency — designed for resilience in the field.

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

- Python 3.9+
- A Meshtastic device connected via serial (USB) or TCP (WiFi)
- `pip install -r requirements.txt` (installs `meshtastic`, `pypubsub`, `flask`)
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

```ini
[interface]
type = serial
# port = /dev/ttyUSB0   # Linux serial
# port = COM3            # Windows serial

# Or for WiFi-connected devices:
# type = tcp
# hostname = 192.168.1.x
```

### Sync Peers

Add the node IDs of other BaconBS-mesh nodes you want to sync with. Find node IDs in the Meshtastic app under **Radio Configuration > User**, or via the web admin dashboard.

```ini
[sync]
bbs_nodes = !f53f4abc,!f3abc123
```

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

---

## Usage

Send a direct message to the BBS node from any Meshtastic device. Any message triggers the main menu. Navigate by sending the letter shown in brackets — for example, send `B` for `[B]BS`.

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

usage: server.py [-h] [--config CONFIG] [--interface-type {serial,tcp}]
                 [--port PORT] [--host HOST] [--mqtt-topic MQTT_TOPIC]

options:
  -h, --help                        show this help message and exit
  --config CONFIG, -c CONFIG        Path to config file
  --interface-type {serial,tcp}     Interface type
  --port PORT, -p PORT              Serial port
  --host HOST                       TCP hostname
  --mqtt-topic MQTT_TOPIC           MQTT topic to subscribe
```

---

## Acknowledgements

- [TheCommsChannel](https://github.com/TheCommsChannel) — original TC²-BBS-mesh
- [Meshtastic](https://github.com/meshtastic) and [pdxlocations](https://github.com/pdxlocations) — Python library and examples
- [Jordan Sherer](https://bitbucket.org/widefido/js8call) — JS8Call and the TCP API example

---

## License

GNU General Public License v3.0
