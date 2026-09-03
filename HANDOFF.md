# Handoff

State of the deployment, the decisions behind it, and what is still open.
For the feature backlog see [feature requests.txt](feature%20requests.txt);
this file is about running the thing.

Last updated 2026-09-03 at commit `0dc79ba` (`v0.1.542`).

---

## The two live nodes

| | bbs.local (192.168.1.9) | forgecam.local (192.168.1.133) |
| --- | --- | --- |
| Radios | Meshtastic serial (primary) + MeshCore serial (secondary) | **none** — `[interface] type = none` |
| MQTT links | mqtt1 `baconbbs` (LAN broker 192.168.1.134), mqtt2 `baconbbsvt` (mqtt.nerdtunnel.net:8884) | same two |
| Active links | 4 | 2 |
| Python | 3.13.5 | **3.9.2** |
| Path | `/home/bacon/TC2-BaconBS-mesh` | same |
| Services | `mesh-bbs.service`, `bacon-web-admin.service`, `bacon-ssh.service` | `mesh-bbs.service`, `bacon-web-admin.service` |
| Bacon BBS SSH | Active, dual-stack port 2222 | Disabled/inactive |
| Fleet state | Healthy on `0dc79ba` | Healthy on `0dc79ba` |

forgecam's Python 3.9 matters: `meshcore` and the supported AsyncSSH release
require newer Python, so `requirements.txt` carries environment markers and
pip skips them there. forgecam is an MQTT-only BBS node and must not run
`bacon-ssh.service`. Both node environments passed `pip check` after the SSH
release.

A third node, `mqtt:baconbbsvt:Chattanooga`, belongs to
[materva](https://github.com/materva/TC2-BaconBS-mesh) and is reachable over
the `baconbbsvt` broker. It is not ours to deploy to.

Recent deployed release sequence:

| Commit | Change |
| --- | --- |
| `1e89a9a` | Secure standalone SSH BBS access and menu cleanup |
| `e92f5a6` | SSH controls on the web Settings page |
| `831e6a7` | Optional shared SSH transport gate and live config reload |
| `d0ba506` | Simultaneous IPv4/IPv6 SSH binding |
| `0dc79ba` | Username-first registration and login prompts |

---

## Deploying

Both nodes enforce cryptographically signed fleet targets. A plain `git pull`
is not a deployment: the update guard will return the node to the last signed
commit. Sign the exact pushed commit with `scripts/fleet_sign.py`, submit it
through a node's normal `fleet_update.verify_instruction()` /
`store_fleet_target()` path, and trigger `apply_update.trigger`. MQTT propagates
the signed instruction to the other node. Never bypass signature validation.

Fleet group: `baconbbsvt`. The signing key is at
`$env:APPDATA\TC2-BaconBS\fleet-key`. Administrative deployment credentials
are at `$env:APPDATA\TC2-BaconBS\node-update-cred.xml`.

Use only the requested `.local` names:

- node 2: `bacon@bbs.local`
- node 4: `bacon@forgecam.local`

A duplicate submission may be rejected as a replay when MQTT delivered the
same fresh timestamp first. Check the stored target and convergence before
treating that as failure or signing another instruction.

After every deployment, confirm the commit, fleet state, services, HTTP, and
recent exceptions:

```sh
systemctl is-active mesh-bbs.service bacon-web-admin.service
git rev-parse --short HEAD
cat update_state.json
sudo journalctl -u mesh-bbs.service --since "2 min ago" | grep -ci traceback
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8081/login
```

`probation` and `mesh-bbs.service` briefly reporting `activating` are normal
after a switch. Do not finish until `update_state.json` reaches `healthy` and
the commit remains on target.

---

## Bacon BBS SSH

SSH access is a separate AsyncSSH service over the real `EmulatorSession`
command path. It exposes no shell, exec, SFTP, or SCP. Its server-derived
identity is always `ssh:<account_id>`, so a client cannot claim a radio or MQTT
identity.

Only Burlington runs it. Current live configuration:

- service: `bacon-ssh.service`, enabled and active
- endpoint: `bbs.local:2222`
- bind: `0.0.0.0, ::` (separate IPv4 and IPv6 sockets)
- host key: `data/ssh_host_key`, owner `bacon`, mode `600`
- shared transport username is configured in Burlington's private
	`config.ini`; do not put the password in this file or source control

The dual bind is required because Windows resolves `bbs.local` to its IPv6
link-local address first and OpenSSH does not retry IPv4 after a refusal. Both
`ssh -4` and `ssh -6` have negotiated AsyncSSH 2.24.0 successfully.

Connection flow when shared transport credentials are configured:

1. Connect with `ssh -p 2222 <shared-user>@bbs.local`.
2. Enter the shared transport password.
3. Enter a BBS username.
4. A known username gets only its account password prompt.
5. An unknown valid username gets create-password and confirmation prompts.

The shared gate does not share BBS identity or mail. Account passwords are
scrypt hashes; only the shared transport password is plaintext in the private
configuration, by explicit operator choice. Settings validates paired shared
credentials and hot-applies enablement, bind addresses, port, and credentials
within about two seconds. Leaving both shared fields blank preserves direct
`new:<alias>` / alias authentication at the SSH handshake.

Live SSH validation completed:

- shared transport login reached the BBS account prompt
- unknown plain username reached registration without `new:`
- known username reached only the login password prompt
- Main -> BBS -> Mail -> Send -> relay directory worked interactively
- account `Copilot09032216` sent subject `SSH relay hello` to relay-enabled
	account `🥓`; the mailbox row was verified
- DM delivery rows were queued for both linked recipient devices with no error

The generated password for `Copilot09032216` was intentionally not retained,
so the account cannot currently be used for another login. Earlier test
accounts `DeployTest0903203111` and `bacon` also remain in the database.

### Deleting an account

The Accounts page can now remove one, which is what these three are waiting
on. `db_operations.delete_account` refuses any account holding a node id
outside the `ssh:` namespace, so `🥓` — a real MeshCore key plus `!04058ac8`
— cannot be deleted from the web at all. Unlink the device first if that is
ever genuinely wanted.

Two details are load-bearing rather than incidental:

- Mail leaves through `delete_mail` one message at a time, so each deletion
	is tombstoned and the peers drop their copies. A bare `DELETE FROM mail`
	is undone by the next reconcile pass, which is what made hand-editing
	these rows unsafe.
- The `mail_relay_preferences` row is kept and set to disabled, not
	deleted. `get_mail_relay_directory` lists any node id with `enabled = 1`
	whether or not an account still backs it, so a deleted row would let a
	stale `RELAYPREF` from a peer re-advertise a dead mailbox.

Mail an account *sent* stays in its recipients' mailboxes. Deleting the
sender is not consent to reach into those.

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

**An SSH session gets a new numeric identity on every connection.**
`bbs_emulator.start_ssh_session` mints `_SYNTHETIC_NUM_BASE + counter` per
connection, and `user_profiles`, `game_scores` and `zork_saves` are all keyed
by that number rather than by the node id. So an SSH user's Zork save never
resumes, their high scores never accumulate, and each login leaves another
orphan `user_profiles` row. It is also why `delete_account` cannot reach
those rows — they are not addressable from an account. Deriving the number
from the account id would fix all of it at once, but it changes a live
service's behaviour, so it is deliberately not bundled with the delete work.
Nothing here leaks mail: the node id, which is what authorization checks, is
still server-derived and stable.

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

Tests use stdlib `unittest`. The latest SSH work was validated with:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_ssh_auth tests.test_ssh_server
.\.venv\Scripts\python.exe -m unittest tests.test_web_admin
```

Verified results during this release sequence:

- 18 SSH authentication/transport tests passed after registration changes
- 149 web-admin tests passed after shared credential/settings changes
- 267 tests passed for the original SSH/menu release
- editor diagnostics and `git diff --check` were clean before each commit
- live IPv4, IPv6, shared-gate, unknown-user, known-user, and relay paths passed

**The roaming `test_web_admin.py` failure is fixed, and it was not a flake.**
Full runs used to fail one or two tests in that file, with *which* ones
changing between runs and the file passing in isolation. That was read as a
`TemporaryDirectory` file-lock artifact. It was not: `get_db_connection`
cached one sqlite connection per thread without recording which file it had
opened, so the first connection a thread ever made won for the rest of the
process. A test file that left one open handed it to every file that ran
after it — pointed at a temporary directory already deleted. The connection
now remembers its path and reopens when `BBS_DB_PATH` changes;
`tests/test_db_connection_path.py` holds the mechanism.

The lesson stands even though the cause did not: a "known flake" is a place
regressions hide, and this one did mask real ones.

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
