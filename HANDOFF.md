# Handoff

State of the deployment, the decisions behind it, and what is still open.
For the feature backlog see [feature requests.txt](feature%20requests.txt);
this file is about running the thing.

Last updated at `v0.1.507`.

---

## The two live nodes

| | bbs.local (192.168.1.9) | forgecam.local (192.168.1.133) |
| --- | --- | --- |
| Radios | Meshtastic serial (primary) + MeshCore serial (secondary) | **none** — `[interface] type = none` |
| MQTT links | mqtt1 `baconbbs` (LAN broker 192.168.1.134), mqtt2 `baconbbsvt` (mqtt.nerdtunnel.net:8884) | same two |
| Active links | 4 | 2 |
| Python | 3.13.5 | **3.9.2** |
| Path | `/home/bacon/TC2-BaconBS-mesh` | same |
| Services | `mesh-bbs.service`, `bacon-web-admin.service` | same |

forgecam's Python 3.9 matters: `meshcore` requires 3.10+, so
`requirements.txt` carries an environment marker and pip skips it there. That
marker is not cosmetic — without it pip refuses the *entire* file on 3.9 and
the node silently ends up with no `paho-mqtt` either, which presents as MQTT
being broken for no visible reason.

A third node, `mqtt:baconbbsvt:Chattanooga`, belongs to
[materva](https://github.com/materva/TC2-BaconBS-mesh) and is reachable over
the `baconbbsvt` broker. It is not ours to deploy to.

---

## Deploying

Both nodes pull from `origin` (`dreamsofbacon/TC2-BaconBS-mesh`) and restart:

```sh
cd /home/bacon/TC2-BaconBS-mesh
git pull --ff-only
sudo systemctl restart mesh-bbs.service bacon-web-admin.service
./venv/bin/python3 version_info.py     # confirm the version moved
```

From Windows this is driven over Posh-SSH with the credential at
`$env:APPDATA\TC2-BaconBS\node-update-cred.xml`. Address the nodes by **IP**
rather than `.local` — mDNS resolution intermittently returns the IPv6
link-local address first and the SSH session then fails to open.

Worth checking after any deploy, because none of it is visible from a
successful `systemctl restart`:

```sh
systemctl is-active mesh-bbs.service bacon-web-admin.service
sudo journalctl -u mesh-bbs.service --since "2 min ago" | grep -ci traceback
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8081/login
```

---

## Versioning, and the fork trap

The version is `0.1.<mainline commit count + offset>`, resolved in
`version_info.py`, which is the **single** source of that rule — `docker/build.sh`
and the publish workflow call it rather than recomputing.

Counting uses `--first-parent` deliberately. Counting every reachable commit
counts whatever a clone happened to merge in, so a fork reports a different
number for byte-identical files; this repository's own two counts differ by
81 across 49 merge commits. `_BUILD_OFFSET` exists only so the changeover did
not make deployed nodes appear to downgrade from 415 to 334. **Do not change
the offset** — it renumbers every past release.

A fork with its own commits will still report a different number, and no
counting rule fixes that. The commit hash in `v0.1.507 (bca5151)` is the real
identity: matching hashes mean identical code whatever the numbers say.

---

## Open issues

**Public chatter from the Meshtastic radio is unconfirmed in the field.** The
capture bug is fixed and proven on the node (a real broadcast packet run
through the live config returns `CAPTURED as meshtastic/LongFast`), but no
`TEXT_MESSAGE_APP` packet has arrived over RF since — 30 minutes of
observation, 325 inbound messages, all MQTT. The radio is demonstrably alive
(429 nodes tracked, newest heard seconds ago); LongFast simply carries mostly
position and nodeinfo. Confirm by sending a message on LongFast from a phone.
If it still does not appear, suspect the radio's own channel indexes rather
than the BBS.

**`sqlite3.OperationalError: database is locked` on MQTT receive dispatch.**
Peaked at 103 occurrences in six hours on forgecam and has since fallen to
0–1; the cause was never found. `server.py` and `web_admin.py` are separate
processes sharing `bulletins.db`, so contention is structural. When it fires,
inbound MQTT frames are dropped, which looks like intermittent sync gaps
rather than an error.

**Zork launches without an interpreter installed.** Trivia King degrades
cleanly when its data is missing; `zork_port` still starts a session when
`dfrotz` is absent. Same shape of fix applies.

**Game output truncation is wrong.** `get_max_text_bytes(interface)` exists
for exactly this and the door responses do not consult it.

**Both nodes are bridged over *both* brokers.** mqtt1 and mqtt2 each carry the
same pair, roughly doubling sync traffic between them. Dropping mqtt2 between
these two would halve it, at the cost of the redundant path.

**Unpinned runtime dependencies:** `meshtastic`, `pypubsub`, `flask`.
`meshcore` is pinned `>=2.3.8,<3`; 2.3.9.1 is available.

**GHCR package visibility.** New packages publish private. The image must be
made public once under Packages → settings, or a pull returns 401.

---

## Decisions worth not undoing

**Bare menu letters are local, not global.** `n`, `q`, `b`, `p`, `g`, `s` act
only inside an active menu; `!n` is the global form. This is what stops a door
game losing its keys — Trivia King uses `N` for the next question and the main
menu uses `N` for Ask Nomad. `tests/test_menu_navigation.py` encodes the
contract.

**`BROADCAST_ADDRESSES` is a literal, not `meshtastic.BROADCAST_NUM`.** The
test suite stubs that module with `BROADCAST_NUM = 0`, so importing it makes
tests agree with a number no radio sends. That is precisely how the capture
bug survived being unit-tested.

**A peer's `zork_saves_disabled` sentinel hash means "opted out", not
"behind".** Only the sentinel counts; an empty hash or a real hash over zero
rows still reports a gap, because those are the cases where sending ours
across works.

**PEERGOSSIP relays must not blank stored hashes.** A relay carries counts and
nothing else; writing `''` over the hashes destroys first-hand SYNCSTATE
knowledge it never had.

**`_build_version.py` is generated, never committed.** A committed count
changes on every commit, which changes the count.

**Mail relay is opt-in.** Full message bodies go on the air on someone's
behalf, so the recipient chooses; the preference syncs between nodes via
`RELAYPREF` behind the `mrp` capability.

**`data/trivia.db` is committed** so the game works on pull. It is CC BY-SA
4.0 from the Open Trivia Database, and the attribution lives in the file's own
`meta` table so a copy carries its provenance.

---

## Testing

`python -m pytest tests/ -q` — 1017 passing across 66 files.

**Known Windows flake:** full runs intermittently fail one or two tests in
`tests/test_web_admin.py`, and *which* ones changes between runs. The file
passes 146/146 in isolation every time. It is a `TemporaryDirectory`
file-lock artifact on `bulletins.db`, not a real failure — but confirm by
rerunning rather than assuming, since it has masked a genuine regression
before.

Run the suite before deploying. Two real regressions have shipped this way:
an accounts schema test that had not been told about new columns, and a
menu-dispatch test asserting behaviour that had deliberately changed.

---

## Docker

The image runs both processes; the web admin anchors the container and
`server.py` is restarted under it with backoff, so a bad config cannot lock
you out of the page you would fix it on. Health reports unhealthy whenever
`server.py` is not running.

Build with `docker/build.sh`, never a bare `docker build` — the image has no
`.git`, so the version must be stamped in as a build argument. Everything
persistent lives on `/config`. See [docker/README.md](docker/README.md), and
`docker/baconbs-unraid.xml` for the Unraid template.
