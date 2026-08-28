# Running Bacon BBS in Docker

The container runs **both** halves of the BBS: `server.py`, which drives the
radios and the sync protocol, and `web_admin.py`, which serves the admin GUI.
They are separate processes that signal each other through files in a shared
directory, so they live in one container together.

The web admin anchors the container. It is the only way to configure a radio,
a broker, or anything else on the node, so if `server.py` fails it is
restarted underneath with backoff rather than taking the container down --
otherwise a bad setting locks you out of the very page you would fix it on.
If the *web admin* exits, the container exits and the restart policy takes
over.

That state is not hidden: the health check reports **unhealthy** whenever
`server.py` is not running, so "up but doing nothing" is visible in
`docker ps` and on the Unraid dashboard. The reason is in `docker logs`.

Everything that has to survive an update lives on the `/config` volume: the
config file, the bulletins database, uploaded MQTT certificates, downloaded
Zork story files, and the trigger files the GUI uses to nudge the BBS.

---

## Quick start (docker compose)

```sh
git clone https://github.com/dreamsofbacon/TC2-BaconBS-mesh.git
cd TC2-BaconBS-mesh
docker/build.sh          # stamps the version into the image
cd docker && docker compose up -d
```

Open `http://<host>:8081`. The first run seeds `config.ini` from
`example_config.ini` and logs the default login, **admin / change-me**. Change
it under Settings, or set `BBS_WEBGUI_USER` and `BBS_WEBGUI_PASSWORD` on the
container.

Use `docker/build.sh` rather than a bare `docker build`. The image contains no
`.git` and no git binary, so it cannot work out its own version at runtime the
way a normal install does — `build.sh` passes the commit count and hash in as
build arguments. An unstamped image reports a fallback version forever, and the
entrypoint says so in the log when that happens.

---

## Unraid

Unraid installs from a registry rather than building from source, so the image
has to be published first. Pushing to `main` runs
`.github/workflows/docker-publish.yml`, which builds for amd64 and arm64 and
pushes to `ghcr.io/dreamsofbacon/tc2-baconbs-mesh`.

The package is public, so no login is needed to pull it:

```sh
docker pull ghcr.io/dreamsofbacon/tc2-baconbs-mesh:latest
```

Each build is tagged `latest` and with its version (`0.1.398`), so a bad
update can be rolled back by pinning the previous number.

### Installing the template

Download `docker/baconbs-unraid.xml` to
`/boot/config/plugins/dockerMan/templates-user/` on the Unraid server. It then
appears under *Docker → Add Container → User templates*.

The template fills in:

| Setting | Default | Notes |
| --- | --- | --- |
| Web GUI Port | `8081` | Unraid's *WebUI* button points here |
| Config and Data | `/mnt/user/appdata/baconbs` → `/config` | Everything persistent |
| PUID / PGID | `99` / `100` | Unraid's `nobody:users`; the entrypoint remaps the runtime user to match, so files in the share stay readable |
| TZ | — | Set it, or bulletin timestamps read in UTC |
| Admin Username / Password | — | Overrides `config.ini` |
| USB Radio | `/dev/ttyUSB0` | Delete the entry if there is no USB radio |

### Attaching a USB radio

Prefer the stable by-id path — `ttyUSB0` numbering changes when a device is
replugged or the server reboots:

```
--device=/dev/serial/by-id/usb-1a86_USB_Single_Serial_54D3017218-if00:/dev/ttyUSB0
```

The runtime user is in the `dialout` group. If the host's device node uses a
different group, add it:

```
--group-add=$(stat -c %g /dev/ttyUSB0)
```

The entrypoint checks each passed-through device on start and logs a warning
naming this fix if it cannot open one — otherwise the failure surfaces deep
inside the radio library and reads like a hardware fault.

### Running without a radio

A radio is optional. Leave the device mapping off, then in the web GUI set
*Settings → Radio → Device type* to **No radio — MQTT only** (`type = none` in
`config.ini`) and add an MQTT broker. The node then mirrors another BBS's
bulletins, mail and channels over the broker with no hardware of its own.

Say it explicitly rather than pointing at a serial port that is not there: a
named-but-absent device is retried forever and shows as a permanently broken
link.

---

## Syncing with another node over MQTT

Two BBS nodes replicate to each other by sharing an MQTT topic. Both need:

- the **same** `topic_prefix` — it names the shared topic `{prefix}/bbs`
- a **different** `local_id` each, or each node treats the other's traffic as
  its own echo and drops it

Set both under *Settings → Sync & Transmission*. That page shows this node's
own address (`mqtt:{prefix}:{local_id}`) with a copy button, and lists other
nodes it has seen on the topic with a one-click *Add as sync peer*. Nothing
needs to be typed by hand on either side.

---

## Updating

```sh
git pull
docker/build.sh
cd docker && docker compose up -d
```

On Unraid, *Check for Updates* on the container, or force an update to re-pull
`:latest`. The `/config` volume is untouched by either.

---

## Environment variables

The image sets sensible defaults for all of these; override only what you need.

| Variable | Default in the image | Purpose |
| --- | --- | --- |
| `PUID` / `PGID` | `1000` / `1000` | uid/gid the BBS runs as (Unraid: `99`/`100`) |
| `BBS_WEBGUI_HOST` | `0.0.0.0` | The app's own default is `127.0.0.1`, unreachable from outside a container |
| `BBS_WEBGUI_PORT` | `8081` | |
| `BBS_WEBGUI_USER` / `BBS_WEBGUI_PASSWORD` | unset | Overrides `config.ini` |
| `BBS_WEBGUI_SECRET` | generated | Flask session key. Generated once into `/config/.session_secret` so sessions survive a restart and no two installs share a key |
| `BBS_CONFIG_PATH` | `/config/config.ini` | |
| `BBS_DB_PATH` | `/config/bulletins.db` | |
| `BBS_BUILD_NUMBER` / `BBS_GIT_COMMIT` | build args | The reported version |
| `TZ` | UTC | |

The remaining `BBS_*` variables — trigger paths, sync pacing, debug switches —
are listed on the web admin's Diagnostics page and work the same here as on a
bare-metal install.

---

## No radio attached

A BBS with no serial port present no longer treats that as fatal. It logs the
failed attempts, carries on without the radio, and keeps retrying in the
background, so a container started with no device passed through comes up
healthy and lands you in the web GUI to configure one. Plug a radio in later,
or point the node at a TCP or MQTT link, and the reconnect loop picks it up
with no restart.

---

## Notes

- **Zork** needs `dfrotz`, which the image installs. Story files download to
  `/config/data` on first play and persist there.
- **The health check** requires two things: that the web admin answers on
  `/login` (the one route that works without a session) and that a `server.py`
  process exists. Checking only the first would report a node moving no mail
  at all as perfectly healthy.
- **Logs** go to stdout: `docker logs -f baconbs`, or the Unraid log button.
- **No dashboard icon yet.** Add a square PNG at `static/img/icon.png` and
  restore the `<Icon>` element in `baconbs-unraid.xml`.
