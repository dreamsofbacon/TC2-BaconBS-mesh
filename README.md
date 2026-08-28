# Bacon BBS

A feature-rich, offline-first Bulletin Board System for [Meshtastic](https://meshtastic.org/) and [MeshCore](https://meshcore.co.uk/) mesh radio networks. Bacon BBS enables asynchronous communication across low-bandwidth LoRa links with no internet dependency — designed for resilience in the field.

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
- **Mesh Client Roster** — Every device seen on any active radio/MQTT link, persisted to the database (not just held in memory) so it survives a restart; browsable from Web Admin → Clients
- **Unique Account Aliases** — An account's alias is the byline on everything it posts, so it's claimed exclusively on this BBS: no two accounts can hold the same one, compared ignoring case and extra whitespace. Aliases are local to this node (accounts don't sync), and clearing yours frees it for someone else
- **Delayed Link Codes** — Request an account link code that's held for a couple of minutes and then sent to your already-linked devices, so a dual-boot node has time to reboot into its other protocol first
- **Per-Link Reconnect** — Drop and re-establish any single radio or MQTT link from Web Admin → Settings → Links & Services, without restarting the service or disturbing the other links
- **Fortune Teller** — Random fortunes from a configurable text file
- **Web Admin Dashboard** — Full moderation interface at `localhost:8081` with real-time sync monitoring, peer hash visualizations, transmission logs, and manual sync controls

---

## Sync Protocol

Bacon BBS uses a custom five-phase distributed sync protocol designed for lossy, low-bandwidth LoRa links. All data is eventually consistent across peers with no central server.

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

- Python 3.10+ (3.9 works for everything except MeshCore, which requires
  3.10 -- pip skips it automatically on an older host rather than failing
  the whole install)
- Either a Meshtastic device (serial or TCP) or a MeshCore companion radio
  (serial, TCP, or BLE)
- `pip install -r requirements.txt` (both radio client libraries, `pyserial`,
  `paho-mqtt`, `pypubsub`, and `flask`)
- To run the test suite: `pip install -r requirements-dev.txt`, then `pytest`
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

### Docker / Unraid

```sh
git clone https://github.com/dreamsofbacon/TC2-BaconBS-mesh.git
cd TC2-BaconBS-mesh
docker/build.sh
cd docker && docker compose up -d
```

The web admin is then on port 8081, and `/config` holds everything that has to
survive an update. Use `docker/build.sh` rather than a bare `docker build`: the
image has no `.git`, so the version has to be stamped in at build time.

Unraid has a ready-made template at `docker/baconbs-unraid.xml`. See
[docker/README.md](docker/README.md) for that, for attaching a USB radio, and
for running with no radio at all as an MQTT-only node.

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

No radio at all — an MQTT-only node that mirrors another BBS's bulletins, mail
and channels over a shared broker:

```ini
[interface]
type = none
```

Say that rather than naming a serial port that will never appear: an absent
device is retried forever and shows as a permanently broken link.

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

Add the node IDs of other Bacon BBS nodes you want to sync with. For
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
below. A node can also bridge to a BBS node it can't reach over RF at all,
via an internet-connected MQTT broker — see [MQTT Internet Bridge](#mqtt-internet-bridge).

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

### MQTT Internet Bridge

Dual-radio bridge mode connects two *co-located* networks through one node.
MQTT bridging solves a different problem: reaching a BBS node you can't hear
over RF **at all** — a separate LoRa mesh island in another city, say — by
relaying sync over an internet-connected MQTT broker instead. It participates
in the exact same five-phase sync protocol as a radio link, over a plain
MQTT topic.

This project is an MQTT **client** only — it connects to a broker you
already run (e.g. self-hosted Mosquitto). No broker code ships here, and
message content is only as private as your broker: anyone with the topic
and broker credentials can read it, so use a private/trusted broker (or add
your own transport-level encryption) unless you're comfortable with that.

A node can run any number of MQTT links at once (`[mqtt1]`, `[mqtt2]`, ...),
each an independent bridge relationship — unlike `[interface2]`'s single
slot, this isn't capped at one secondary connection. To bridge three remote
sites, configure three sections with three distinct `topic_prefix` values;
every subscriber on a topic sees every message published to it, so don't
reuse one prefix across unrelated bridges.

```ini
[mqtt1]
host = broker.example.com
port = 8883
tls = true
username = your-username
password = your-password
topic_prefix = baconbs/cityA-cityB
local_id = cityA-node

[sync_mqtt1]
bbs_nodes = mqtt:baconbs/cityA-cityB:cityB-node

[allow_list_mqtt1]
allowed_nodes = mqtt:baconbs/cityA-cityB:cityB-node
```

**TLS / certificates.** `tls = true` on its own verifies the broker against
the system CA store — correct for any broker with a publicly-trusted
certificate, and nothing else is needed. The following are for a
self-hosted broker with a private CA, or one requiring client-certificate
(mutual TLS) auth. All optional, all configurable from Settings → MQTT
Bridges → *Advanced TLS / Certificates* as well as `config.ini`:

| Setting | Purpose |
|---|---|
| `tls_ca_certs` | CA certificate to verify the broker against, instead of the system store (private / self-signed CA) |
| `tls_certfile` | Client certificate, for brokers requiring mutual TLS |
| `tls_keyfile` | Private key for that client certificate |
| `tls_keyfile_password` | Only if the private key is encrypted |
| `tls_insecure` | Disables broker hostname verification — leave off (see below) |

```ini
[mqtt1]
host = broker.example.com
port = 8883
tls = true
tls_ca_certs = /etc/ssl/certs/my-broker-ca.crt
tls_certfile = /etc/baconbs/client.crt
tls_keyfile = /etc/baconbs/client.key
topic_prefix = baconbs/cityA-cityB
```

**Applying broker changes without a restart.** Saving MQTT settings in the
web admin applies them to the running service automatically — no restart.
Behind the scenes it asks the server to reload links from `config.ini`,
which opens brokers you added, closes ones you removed, and reconnects ones
whose connection settings changed. If you edit `config.ini` by hand
instead, press **Settings → Links & Services → Reload Links From Config**
to do the same thing.

Use **Reconnect** on a single link when the config hasn't changed but the
connection is wedged — it re-establishes that one connection (re-reading
`config.ini` as it does, so it also picks up edits) while the BBS, the sync
engine, and every other link keep running.

Radio devices (`[interface]` / `[interface2]`) are the exception: they're
opened once at startup and still need a service restart after a change,
since reopening a serial device is materially riskier than reopening a
socket.

**Uploading certificates from the browser.** Settings → MQTT Bridges →
*Advanced TLS / Certificates* has a file picker for each of the three, so
no shell access is needed: the file is validated, saved onto the node under
`data/mqtt-certs/mqtt<N>/`, and the path is filled in for you. Uploads are
checked for being genuine PEM of the *expected kind* — uploading a private
key into a certificate field, or a binary DER/PKCS#12 file, is rejected
immediately with an explanation instead of failing later as an opaque SSL
error. Private keys are stored readable only by the service user, and
`data/mqtt-certs/` is gitignored so an uploaded key can never be committed
and propagated by `git pull` to other nodes.

> **Uploading a private key:** the web admin serves plain **HTTP**, so a key
> uploaded through the form crosses your network unencrypted. On a trusted
> LAN that's usually acceptable; otherwise copy the key to the node
> yourself and just type its path in the field instead. Public certificates
> (the CA and client cert) carry no such concern.

Typed paths are on **this node's** filesystem and must be readable by the
user the service runs as (`User=` in `mesh-bbs.service`) — a common gotcha
with keys in root-owned directories. A missing or unreadable file fails
fast at startup naming the exact setting and path, rather than surfacing
later as an opaque SSL error that looks like the broker being down.

Setting any of these turns TLS on even without `tls = true`, so
certificates can't be silently ignored on a plaintext connection. Avoid
`tls_insecure = true`: it disables the check that the broker is who it
claims to be, so the connection can be impersonated, which defeats most of
the point of TLS — for a self-signed broker, point `tls_ca_certs` at its CA
instead.

Notes:

- MQTT links get fast ("turbo-equivalent") pacing automatically, since MQTT
  has none of LoRa's payload-size or half-duplex constraints — do **not**
  set `sync_turbo = true` to speed one up, especially on a node that also
  has a radio: that would also speed up (and likely destabilize) the
  radio's own LoRa pacing. Radio and MQTT links are always paced
  independently on the same node.
- An MQTT link's own node ids are shaped `mqtt:<topic_prefix>:<label>` —
  distinct from both Meshtastic's `!xxxxxxxx` and MeshCore's bare hex keys.
- If a broker is unreachable, the local BBS keeps running normally on its
  other link(s) — an MQTT outage never restarts the process the way a
  wedged serial radio can.
- Web admin Settings → Diagnostics shows a status card per active MQTT
  link, same as it does for radios. Brokers can also be added, edited, and
  removed from Settings → MQTT Bridges instead of hand-editing `config.ini`
  — same restart-required caveat as the Device Configuration section, since
  links are opened once at startup.
**What each broker receives.** Beyond the sync traffic a bridge exists for,
each broker independently opts in to telemetry — Settings → MQTT Bridges →
*What this broker receives*, or the `publish_*` keys in `[mqttN]`. So one
node can send full telemetry to a home broker while a remote bridge gets
sync only.

| Setting | Publishes | Topic |
|---|---|---|
| `publish_status` *(default on)* | Health of every radio/MQTT link | `<prefix>/<id>/status` |
| `publish_clients` | Devices in range, one topic per node | `<prefix>/<id>/clients` |
| `publish_telemetry` | Hardware/role counts, battery, low-battery list | `<prefix>/<id>/telemetry` |
| `publish_activity` | New bulletins/mail/comments as events | `<prefix>/<id>/activity` |
| `publish_sync_stats` | Sync phase/percent, record counts, DB size | `<prefix>/<id>/sync` |
| `publish_prefix` | Overrides `<prefix>` for the above only | — |

Everything except activity is **retained**, so a subscriber connecting
later immediately gets current state. Activity events are deliberately not
retained — replaying the last one to every new subscriber would misrepresent
it as current. Activity is polled on the diagnostics cadence (5–30s) rather
than hooked into the write path, so it costs the sync engine nothing; the
tradeoff is up to one cycle of latency.

`publish_prefix` moves telemetry only — **never** the sync topic, which
identifies the bridge relationship and must stay identical on both ends.

Topic-forming fields (`topic_prefix`, `local_id`, `publish_prefix`) are
normalized: whitespace becomes `-` and MQTT wildcards (`+`, `#`) are
replaced. `Burlington NNE` becomes `Burlington-NNE`. Spaces are legal in
MQTT but break shell tooling and broker ACL patterns. Since `local_id` is
also this node's id on the link, peers must use the normalized form.

- Every configured MQTT link also **publishes** this node's status back to
  its broker, separate from the `{topic_prefix}/bbs` sync topic above:
  ```
  {topic_prefix}/{local_id}/status                     ← one retained message,
                                                           single-line JSON,
                                                           every link's status
  {topic_prefix}/{local_id}/status/links/<name>         ← one retained message
                                                           per link (primary,
                                                           secondary, each
                                                           mqttN), same fields
  ```
  Each broker gets this node's *whole* status (every radio and every MQTT
  link, not just that one broker's own connection), refreshed on the same
  cadence as the local diagnostics snapshot (every 5s while syncing, 30s
  otherwise). Retained, so a client that subscribes later still gets the
  last known state immediately. This is the same data the nav-bar status
  badges and Settings → Diagnostics show — all three always agree.

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
main_menu_items = Q, B, U, P, N, X
bbs_menu_items = M, B, C, J, X
utilities_menu_items = S, F, W, G, X
```

`N` (Ask Nomad) is a homescreen shortcut straight to the Project Nomad AI
question prompt — the same destination as Utilities > API Gateway > Ask
Project Nomad, without the extra menu hops. After a reply arrives, you can
immediately ask a follow-up question or send `0` to return to the main menu.

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
present in the radio's contact list. Bacon BBS sends direct encrypted MeshCore
messages and uses MeshCore's automatic routing/flood fallback. MeshCore's
160-byte text ceiling is detected automatically; BBS sync frames and user
replies are chunked to fit it.

### USB Stability (Linux)

`install_services.sh` disables USB autosuspend automatically (see below) --
this section explains why, and covers the manual fix for anyone who set up
the service some other way.

Linux's default USB autosuspend (2s idle timeout on most distros) does not
play well with the CP210x/CH340/FTDI-based USB-serial adapters most
Meshtastic/MeshCore radio boards use. Symptoms: a radio silently stops
responding after a period of idle, or intermittently drops and reconnects.
Check `dmesg` for the signature:

```
usb_serial_generic_read_bulk_callback - urb stopped: -32
usb 1-1.3: USB disconnect, device number 5
```

### Use stable device paths, not `/dev/ttyUSBn`

**On a multi-radio node, always set `port` to a `/dev/serial/by-*` path.**
`/dev/ttyUSB0`, `ttyUSB1`, ... are assigned in *enumeration order*, not tied
to a particular physical device — a reboot, a USB reconnect (see autosuspend
above), or simply powering the radios up in a different order can renumber
them or **swap which radio is which**. Two failure modes follow, and the
second is nastier than it looks:

- `port` points at a number that no longer exists → that radio never
  connects, and the log just says the connect attempt failed.
- `port` still exists but is now *the other radio* → the BBS tries a
  Meshtastic handshake against a MeshCore board (or vice versa), holds the
  port open, and never completes. This looks like a dead/misconfigured
  radio, but both boards are fine.

List the stable aliases:

```bash
ls -l /dev/serial/by-id/     # keyed by the adapter's USB serial number
ls -l /dev/serial/by-path/   # keyed by which physical USB port it's in
```

Prefer `by-id` — it follows the device to any port. But many cheap CP2102
adapters ship with the **same** hard-coded serial number (`...Controller_0001`),
so two of them collide and only one `by-id` symlink appears. When that
happens use `by-path`, which is unambiguous because it describes the
physical port:

```ini
[interface]
type = serial
port = /dev/serial/by-path/platform-3f980000.usb-usb-0:1.1.3:1.0-port0

[interface2]
type = meshcore_serial
port = /dev/serial/by-path/platform-3f980000.usb-usb-0:1.2:1.0-port0
```

The tradeoff: a `by-path` alias is tied to the physical USB port, so moving
a radio to a different port changes it. That's usually what you want on a
fixed install — it stays correct across reboots and renumbering, which is
the failure that actually bites.

To confirm which board is on which port, ask each one directly (stop the
service first so the ports are free) — the MeshCore radio answers, the
Meshtastic one doesn't:

```bash
sudo systemctl stop mesh-bbs.service
venv/bin/python3 -c "
import asyncio
from meshcore import MeshCore
async def main():
    for p in ['/dev/ttyUSB0', '/dev/ttyUSB1']:
        try:
            mc = await asyncio.wait_for(MeshCore.create_serial(p, 115200, default_timeout=3), timeout=20)
            print(p, '-> MeshCore:', (mc.self_info or {}).get('name') if mc else 'no response')
            if mc: await mc.disconnect()
        except Exception as e:
            print(p, '-> not MeshCore (', type(e).__name__, ')')
asyncio.run(main())
"
sudo systemctl start mesh-bbs.service
```

Disabling autosuspend on just the radio's own USB device isn't enough -- an
upstream USB hub it's plugged into can still be suspended and drag the
device down with it regardless of its own setting. `install_services.sh`
installs `scripts/99-baconbs-usb-no-autosuspend.rules`, a udev rule that
disables autosuspend for every USB device on the bus (hubs included) rather
than targeting specific radio vendor/product IDs, so it keeps working for
future hardware without needing updates. Applies immediately, no reboot
required. To install it by hand on a node set up some other way:

```bash
sudo cp scripts/99-baconbs-usb-no-autosuspend.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

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
