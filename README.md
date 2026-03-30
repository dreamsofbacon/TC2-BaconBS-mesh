# TC²-BBS Meshtastic Version

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/B0B1OZ22Z)

This is the TC²-BBS system integrated with Meshtastic devices. The system includes bulletin boards, mail, channel directory, selective hash-based sync repair across peers, and tombstone-based deletion reconciliation.

### Docker

If you're a Docker user, TC²-BBS Meshtastic is available on Docker Hub!

[![Docker HUB](https://icon-icons.com/downloadimage.php?id=151885&root=2530/PNG/128/&file=docker_button_icon_151885.png)](https://hub.docker.com/r/thealhu/tc2-bbs-mesh)

## Setup

### Requirements

- Python 3.x
- Meshtastic
- pypubsub

### Update and Install Git
   
   ```sh
   sudo apt update
   sudo apt upgrade
   sudo apt install git
   ```

### Installation

1. Clone the repository:
   
   ```sh
   cd ~
   git clone https://github.com/dreamsofbacon/TC2-BaconBS-mesh.git
   cd TC2-BaconBS-mesh
   ```

#### Quick Setup (Automated)

Run the appropriate setup script for your system to automatically create a virtual environment and install all dependencies:

- **Windows (PowerShell):**
  ```powershell
  .\setup.ps1
  ```

- **Windows (Command Prompt):**
  ```cmd
  setup.bat
  ```

- **macOS and Linux:**
  ```sh
  bash setup.sh
  ```

These scripts will:
- Create a Python virtual environment
- Install all dependencies from `requirements.txt`
- Verify the `meshtastic` module can be imported in that virtual environment
- Create `config.ini` from `example_config.ini` (if it doesn't exist)

After setup, run the server with the virtual environment Python executable:

- **Windows (PowerShell/CMD):**
   ```powershell
   .\.venv\Scripts\python.exe server.py
   ```

- **macOS and Linux:**
   ```sh
   ./venv/bin/python server.py
   ```

#### Manual Setup

If you prefer manual setup, follow these steps:

2. Set up a Python virtual environment:  
   
   ```sh
   python -m venv venv
   ```

3. Activate the virtual environment:  
   
   - On Windows:  
   
   ```sh
   venv\Scripts\activate  
   ```
   
   - On macOS and Linux:
   ```sh
   source venv/bin/activate
   ```

4. Install the required packages:  
   
   ```sh
   pip install -r requirements.txt
   ```

5. Rename `example_config.ini`:

   ```sh
   cp example_config.ini config.ini
   ```

#### Configuration

6. Set up the configuration in `config.ini`:  

   You'll need to open up the config.ini file in a text editor and make your changes following the instructions below
   
   **[interface]**  
   If using `type = serial` and you have multiple devices connected, you will need to uncomment the `port =` line and enter the port of your device.   
   
   Linux Example:  
   `port = /dev/ttyUSB0`   
   
   Windows Example:  
   `port = COM3`   
   
   If using type = tcp you will need to uncomment the hostname = 192.168.x.x line and put in the IP address of your Meshtastic device.  
   
   **[sync]**  
   Enter a list of other BBS nodes you would like to sync messages and bulletins with. Separate each by comma and no spaces as shown in the example below.   
   You can find the nodeID in the menu under `Radio Configuration > User` for each node, or use this script for getting nodedb data from a device:  
   
   [Meshtastic-Python-Examples/print-nodedb.py at main · pdxlocations/Meshtastic-Python-Examples (github.com)](https://github.com/pdxlocations/Meshtastic-Python-Examples/blob/main/print-nodedb.py)  
   
   Example Config:  
   
   ```ini
   [interface]  
   type = serial  
   # port = /dev/ttyUSB0  
   # hostname = 192.168.x.x  
   
   [sync]  
   bbs_nodes = !f53f4abc,!f3abc123  
   ```

### Sync Model (Current)

Peer consistency uses a layered approach designed for low-bandwidth mesh links:

- Periodic count/hash exchange (`SYNCSTATE`) for `bulletins`, `mail`, `channels`, `profiles`, `game_scores`, and `zork_saves`
- Per-scope hash manifest repair (`HASHREQ`, `HASHREC`, `HASHEND`, `HASHMISS`) so only mismatched scopes are requested
- Optional compressed manifest transport (`HASHZ`) controlled by `BBS_HASH_MANIFEST_COMPRESSION=1`
- Tombstone replay for deletes so removed records do not get resurrected after peers reconnect

Deletion reconciliation details:

- Deleting bulletin/mail records creates tombstones that are synced to peers
- Re-adding a record with the same unique key clears the matching tombstone
- During hash reconciliation, if a peer is missing a deleted record, the system requests tombstone replay instead of requesting the deleted record itself

Note: deletes that happened before the tombstone feature was introduced have no historical tombstone entry.

### Running the Server

Run the server with the standalone launch script for your OS:

- Windows (PowerShell):

```powershell
.\run_server.ps1
```

- Windows (Command Prompt):

```cmd
run_server.bat
```

- macOS/Linux:

```sh
bash run_server.sh
```

You can also run directly with venv Python:

```sh
./venv/bin/python server.py
```

On Windows direct equivalent:

```powershell
.\.venv\Scripts\python.exe server.py
```

### Running the Web Admin GUI (Standalone Moderation)

You can run a standalone web interface to moderate the SQLite database (`bulletins`, `mail`, and `channels`).

Use the standalone launch script for your OS:

- Windows (PowerShell):

```powershell
.\run_web_admin.ps1
```

- Windows (Command Prompt):

```cmd
run_web_admin.bat
```

- macOS/Linux:

```sh
bash run_web_admin.sh
```

Or run directly with venv Python:

```sh
./venv/bin/python web_admin.py
```

By default it starts on `127.0.0.1:8081`.

Set secure credentials and optional host/port before launching:

```sh
export BBS_WEBGUI_USER=admin
export BBS_WEBGUI_PASSWORD=change-this
export BBS_WEBGUI_SECRET=change-this-session-secret
export BBS_WEBGUI_HOST=127.0.0.1
export BBS_WEBGUI_PORT=8081
python web_admin.py
```

Optional: point to a different DB file.

```sh
export BBS_DB_PATH=/path/to/bulletins.db
python web_admin.py
```

Security note: if you set `BBS_WEBGUI_HOST=0.0.0.0`, place it behind a trusted network/VPN/reverse proxy.

Web moderation supports creating new bulletin posts via the **New Bulletin Post** button in the Bulletins view.

Bulletin board categories for the dropdown are configurable and loaded in this order:
- `BBS_BULLETIN_BOARDS` environment variable (comma-separated)
- `[boards]` section in `config.ini` with `bulletin_boards = General,Info,News,Urgent`
- built-in defaults (`General, Info, News, Urgent`)

You can also edit categories in the web UI under the **Boards** tab. Changes are written to `config.ini` and applied immediately in the running web admin process.

The web admin also includes sync diagnostics under **Settings > Diagnostics** (or use the top nav **Diagnostics** link), including:

- Peer consistency status
- Mismatch re-sync attempt summary/details
- Peer-advertised per-scope counts

Example:

```sh
export BBS_BULLETIN_BOARDS=General,Info,News,Urgent,Events
```

To reduce lock/corruption risk while `server.py` and `web_admin.py` are both active, the web admin uses SQLite WAL mode, busy timeout, and atomic write transactions.

### Run Web Admin GUI with systemd

The repository includes `bacon-web-admin.service` and an installer script (`install_services.sh`) that prompts for your Linux username and project path, then installs both services.

1. Create an environment file for credentials and bind settings:

```sh
cat > /home/pi/TC2-BaconBS-mesh/web-admin.env << 'EOF'
BBS_WEBGUI_USER=admin
BBS_WEBGUI_PASSWORD=change-this
BBS_WEBGUI_SECRET=change-this-session-secret
BBS_WEBGUI_HOST=0.0.0.0
BBS_WEBGUI_PORT=8081
# Optional:
# BBS_DB_PATH=/home/pi/TC2-BaconBS-mesh/bulletins.db
# BBS_CONFIG_PATH=/home/pi/TC2-BaconBS-mesh/config.ini
EOF
```

2. Install both services (recommended):

```sh
chmod +x install_services.sh
bash install_services.sh
```

For automated installs (no prompts), use:

```sh
bash install_services.sh --yes --user "$USER" --dir "$HOME/TC2-BaconBS-mesh"
```

This installs and restarts:
- `mesh-bbs.service`
- `bacon-web-admin.service`

By default, the web admin service binds to `0.0.0.0:8081` so it is reachable from other devices on your LAN.

## Remote Two-Node Update Automation

For Windows operators updating Linux systemd nodes, the repository includes:

- `scripts/update-two-nodes.ps1` (local orchestrator; uses Posh-SSH)
- `scripts/remote-node-update.sh` (remote script run on each node)
- `scripts/node-update-config.json.example` (example config)

### One-time setup

1. Copy `scripts/node-update-config.json.example` to `scripts/node-update-config.json` and set your hostnames/IPs.
2. Copy `scripts/remote-node-update.sh` to each node (for example `~/remote-node-update.sh`).
3. On each node:

```sh
chmod +x ~/remote-node-update.sh
```

### Run updates from Windows

From the repository root:

```powershell
.\scripts\update-two-nodes.ps1
```

Notes:

- First run prompts for SSH credentials and stores them in `%APPDATA%\TC2-BaconBS\node-update-cred.xml`
- Use `-ResetCredential` to prompt again if credentials change
- Remote script performs `git fetch`, `git checkout`, `git pull --ff-only`, then restarts `mesh-bbs.service` and `bacon-web-admin.service`

3. Check status and logs:

```sh
sudo systemctl status mesh-bbs.service bacon-web-admin.service
journalctl -u mesh-bbs.service -u bacon-web-admin.service -f
```

## Smoke Test (No Radio Required)

Run a basic mocked integration smoke test for sync parsing and menu input validation:

```sh
python tests/smoke_test.py
```

This test does not require a connected Meshtastic device and is safe to run before deploys.


## Command line arguments
```
$ python server.py --help

████████╗ ██████╗██████╗       ██████╗ ██████╗ ███████╗
╚══██╔══╝██╔════╝╚════██╗      ██╔══██╗██╔══██╗██╔════╝
   ██║   ██║      █████╔╝█████╗██████╔╝██████╔╝███████╗
   ██║   ██║     ██╔═══╝ ╚════╝██╔══██╗██╔══██╗╚════██║
   ██║   ╚██████╗███████╗      ██████╔╝██████╔╝███████║
   ╚═╝    ╚═════╝╚══════╝      ╚═════╝ ╚═════╝ ╚══════╝
Meshtastic Version

usage: server.py [-h] [--config CONFIG] [--interface-type {serial,tcp}] [--port PORT] [--host HOST] [--mqtt-topic MQTT_TOPIC]

Meshtastic BBS system

options:
  -h, --help            show this help message and exit
  --config CONFIG, -c CONFIG
                        System configuration file
  --interface-type {serial,tcp}, -i {serial,tcp}
                        Node interface type
  --port PORT, -p PORT  Serial port
  --host HOST           TCP host address
  --mqtt-topic MQTT_TOPIC, -t MQTT_TOPIC
                        MQTT topic to subscribe
```



## Automatically run at boot

Use the installer script to configure and install both systemd services with your username and project path:

```sh
chmod +x install_services.sh
bash install_services.sh
```

Non-interactive variant:

```sh
bash install_services.sh --yes --user "$USER" --dir "$HOME/TC2-BaconBS-mesh"
```

If you plan to use Zork, keep these environment lines in `mesh-bbs.service` so the interpreter is found under systemd:

   ```sh
   Environment="PATH=/usr/games:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
   Environment="BBS_ZORK_INTERPRETER=/usr/games/dfrotz"
   ```

   Verify the interpreter exists:

   ```sh
   which dfrotz frotz
   ls -l /usr/games/dfrotz /usr/games/frotz
   ```

2. **Service controls**

   ```sh
   sudo systemctl status mesh-bbs.service bacon-web-admin.service
   sudo systemctl stop mesh-bbs.service bacon-web-admin.service
   sudo systemctl restart mesh-bbs.service bacon-web-admin.service
   ```

3. **Viewing Logs**

   Viewing past logs:
   ```sh
   journalctl -u mesh-bbs.service
   ```

   Viewing live logs:
   ```sh
   journalctl -u mesh-bbs.service -f
   ```

## Radio Configuration

Note: There have been reports of issues with some device roles that may allow the BBS to communicate for a short time, but then the BBS will stop responding to requests. 

The following device roles have been working: 
- **Client**
- **Router_Client**

## Features

- **Mail System**: Send and receive mail messages.
- **Bulletin Boards**: Post and view bulletins on various boards.
- **Channel Directory**: Add and view channels in the directory.
- **Channel Threads + Comments**: Channels are grouped by name, posts within a channel can be viewed individually, and users can read/add comments on each post.
- **Statistics**: View statistics about nodes, hardware, and roles.
- **Wall of Shame**: View devices with low battery levels.
- **Fortune Teller**: Get a random fortune. Pulls from the fortunes.txt file. Feel free to edit this file remove or add more if you like.

## Usage

You interact with the BBS by sending direct messages to the node that's connected to the system running the Python script. Sending any message to it will get a response with the main menu.  
Make selections by sending messages based on the letter or number in brackets - Send M for [M]ail Menu for example.

A video of it in use is available on our YouTube channel:

[![TC²-BBS-Mesh](https://img.youtube.com/vi/d6LhY4HoimU/0.jpg)](https://www.youtube.com/watch?v=d6LhY4HoimU)

## Thanks

**Meshtastic:**

Big thanks to [Meshtastic](https://github.com/meshtastic) and [pdxlocations](https://github.com/pdxlocations) for the great Python examples:

[python/examples at master · meshtastic/python (github.com)](https://github.com/meshtastic/python/tree/master/examples)

[pdxlocations/Meshtastic-Python-Examples (github.com)](https://github.com/pdxlocations/Meshtastic-Python-Examples)

**JS8Call:**

For the JS8Call side of things, big thanks to Jordan Sherer for JS8Call and the [example API Python script](https://bitbucket.org/widefido/js8call/src/js8call/tcp.py)

## License

GNU General Public License v3.0
