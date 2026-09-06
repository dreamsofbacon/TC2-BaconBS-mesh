# Handoff

State of the deployment, the decisions behind it, and what is still open.
For the feature backlog see [feature requests.txt](feature%20requests.txt);
this file is about running the thing.

Last updated 2026-09-06 at commit `e4738fa` (`v0.1.566`).

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
| Fleet state | Healthy on `e4738fa` | Healthy on `e4738fa` |

forgecam's Python 3.9 matters: `meshcore` and the supported AsyncSSH release
require newer Python, so `requirements.txt` carries environment markers and
pip skips them there. forgecam is an MQTT-only BBS node and must not run
`bacon-ssh.service`. Both node environments passed `pip check` after the SSH
release.

A third node, `mqtt:baconbbsvt:Chattanooga`, belongs to
[materva](https://github.com/materva/TC2-BaconBS-mesh) and is reachable over
the `baconbbsvt` broker. It is not ours to deploy to.

**It is not enrolled, and waiting will not change that.** It sits on
`0.1.546` and has never stored a signed target. The transmission log settles
where the fault is: 161 `FLEETVER` frames sent to it and none ever relayed
back, against 161 sent and 160 returned for every peer that is enrolled,
while it sends us 304 `FLEETSTATUS` frames over the same link. So the
instruction reaches it and it declines to act -- its `[fleet] updates` is
off, or our key `fkec622a` is not in its `trusted_keys`, or `cryptography`
is missing. All three are on its side, and the second is a perfectly
reasonable choice about a key somebody else holds.

The Fleet page now reports this as **not enrolled** rather than `pending`,
because `pending` also describes the ninety seconds a healthy node spends
converging -- which is how this hid for a day. Enrolling it needs, on their
node:

```sh
python scripts/fleet_sign.py --group baconbbsvt enroll   "fkec622a:0hGvExa6i9yRn-kdbW4Kn6FHMfurPdmYeTCoud4vbuc"   --config config.ini --updates auto
sudo systemctl restart mesh-bbs.service bacon-web-admin.service
```

That is the public half; it verifies signatures and cannot create them.
Until then it keeps its pre-fix copies of the timestamp drift and the
source-field parse bug, which is why it stays on our mismatch list.

Recent deployed release sequence (2026-09-04):

| Commit | Change |
| --- | --- |
| `b5fe94c` | Bound the zork_saves repair deferral so it cannot starve |
| `58a2347` | One stable sender number per SSH account, for life |
| `8779c14` `f2d1337` | One timestamp spelling in both zork hashes |
| `f48cd26` | A Trivia question pays out once |
| `6e6183e` | Source-field parse anchored on the timestamp, not a guess |
| `69309bd` | Self-registered SSH accounts kept off the Urgent board |
| `4115cf9` | SSH delivers replies that arrive after the command |
| `224d871` | "That is you" instead of "not found" when self-addressing |
| `17afabf` | Fleet updates restart `bacon-ssh` too |
| `ccb238d` | Fleet page distinguishes *not enrolled* from *pending* |

Earlier the same day: `5174737` menu renumbering and a working `[0] Exit`,
`0bd9178` prefixed cancel words, `75e6ac5` safe account deletion and the
shared-connection test fix.

Since (2026-09-05/06):

| Commit | Change |
| --- | --- |
| `438ebf1` | Deletes for game scores and profiles are remembered |
| `e291ada` | Delete a score or profile from the web admin |
| `19caaea` | Node View: read one node, or all of them |
| `e4738fa` | Node View works in the SSH and web admin processes |

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

### When the nodes stop trusting the signing key

`fleet_sign.py` will happily sign a blob the nodes then refuse, with
`instruction was signed by key fkXXXXXX, which this node does not trust`.
The signature is fine; the node's `[fleet] trusted_keys` no longer lists it.

This happened on 2026-09-04: both nodes went from trusting `fkec622a` and
`fkf5f136` to `fkf5f136` alone, 32 seconds apart (forgecam 19:48:31, bbs
19:49:03) -- one deliberate action across the fleet, not a stray click. It
was *after* a successful deploy that same evening, so nothing about applying
an update caused it, and nothing in `fleet_update.py` or the apply path
writes `config.ini` at all; the only writers are the web admin's save
routes. The journal could not settle it either way because it only reached
back to the following day. The likeliest cause is
`scripts/fleet_sign.py enroll`, which rewrites `trusted_keys` wholesale
rather than appending.

To diagnose, compare the key ids across the config backups on the node:

```sh
cd ~/TC2-BaconBS-mesh
for f in config.ini config.ini.*; do
  echo "$f: $(grep -oh 'fk[0-9a-f]\{6\}' "$f" | sort -u | xargs)"
done
```

**Re-adding a key is the node owner's decision, not a step to take because
it unblocks you.** Whoever holds the private half can make that node run any
commit they choose, and having shell access is the reason to ask rather than
a reason not to. Once asked, add it through `/fleet/keys/add` rather than by
editing `config.ini`: that path validates the entry, derives the fingerprint
from the key instead of trusting the pasted label, and logs at warning
level. Add, do not replace -- back up `config.ini` first and check the other
key is still there afterwards.

### Companion services, and how eight releases shipped nothing

`mesh-bbs` refreshes itself: it exits on update and systemd restarts it under
`Restart=always`. Everything else needs `fleet_update.restart_companion_services`
to do it, and that list held only `bacon-web-admin`.

Nothing restarted `bacon-ssh`. It ran the code it had started with for seven
hours across eight releases while the file on disk carried every fix -- the
stable sender number, `[0] Exit`, the prefixed cancel words, the renumbered
menus. None were live over SSH, and every check made in that window read
stale behaviour and passed.

It surfaced only by driving a real terminal session: a fix for late Ask Nomad
replies still showed the old symptom after shipping. `git rev-parse` was
right, the file was right, the tests passed, and the service had never loaded
any of it. **"Deployed" was being measured as "the file changed."**

`COMPANION_UNITS` now covers both, restarted one at a time so forgecam --
MQTT-only, no `bacon-ssh` installed -- is not failed by a missing unit.
Confirmed working on `ccb238d`: `bacon-ssh` took a new PID with nobody
touching it.

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
accounts `DeployTest0903203111` and `bacon` also remain in the database, plus
`baconbot` from the field test. All four are `DELETABLE` from the Accounts
page whenever wanted.

**An account's sender number is stable for life.** `user_profiles`,
`game_scores` and `zork_saves` key on the numeric sender id, and SSH used to
mint a fresh one per connection -- so a save was written under a number
nothing would ever present again. It replicated to every peer and could never
be loaded by the person who made it. `accounts.sender_num` is now derived
once with blake2b and stored, with a partial unique index so two accounts
cannot share one. Verified live: `baconbot` holds `3781033626`, and a Zork
save written in one SSH session was restored by a separate connection.

The number is assigned lazily at first login, so an account that has not
signed in since the change still shows `NULL`. That is expected, not missing.

**One session per account.** `max_sessions_per_account` is 1, because
`user_states` keys on that same number: two concurrent sessions would share a
menu position and typing in one window would move the other. The old
behaviour was not better, only incoherent -- two sessions shared mail, which
keys on the node id, while having separate profiles and separate saves.

**Late replies arrive on their own.** Ask Nomad answers from a worker thread
up to a minute later, and nothing on this transport drained that buffer
except a keystroke -- so the answer surfaced only when the user typed, and
that keystroke was then eaten by the prompt the answer had just drawn.
`_drain_late_replies` polls once a second. Verified live: an answer arrived
unprompted about five seconds after the question.

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

### Node View, and the two id namespaces

`[V] Node View` on the main menu lets a user read **All nodes** (the
default), **This node**, or **one named peer**, across bulletins, mail,
channel comments and public chatter. It is a per-session lens over content
that still syncs everywhere, not a boundary -- `local_only` is the boundary,
and it remains admin-only and invisible to users.

The rule it lives or dies by: **nothing is ever silently hidden.** A
narrowed screen names the node, says how many records it is holding back,
and says `!V=all`. Most of all the empty mailbox, which would otherwise
flatly claim there is nothing while someone waits on a message. Two places
stay unscoped on purpose and say so in comments: the main-menu envelope
badge (it is what makes the count on a narrowed list checkable) and the
channel post list's latest-commenter preview (scoping it would label a busy
post "No comments yet").

`!V`, not a bare `V` -- at a mail or bulletin list prompt a bare letter is
read as an item number and never reaches the menu handlers.

**A node is several ids, in two unrelated namespaces.** Bulletins, mail and
comments carry `source_node_id` from `user['id']`. Public chatter carries
`capture_node_id`, set *per radio* from `getMyNodeInfo()['publicKey']`
(`server.py`). The same physical node therefore appears as
`mqtt:baconbbsvt:Chattanooga` in one place and a base64 key in the other,
and nothing in the database relates them. `[node_names]` in `config.ini` is
the only thing that does -- names on the left, every id on the right, one
line per node. It is optional: an `mqtt:<topic>:<label>` id already reads as
its label, so an MQTT-bridged fleet is readable with no config at all.

Also note the chatter filter no longer has a "Heard by:" section. Those were
capture ids, so on a two-radio node it offered two unlabelled 64-character
keys meaning "my MeshCore radio" and "my Meshtastic radio". The lens owns
that dimension now.

**`get_local_link_identities()` is not the set to use for this.** It answers
"is this me?" during sync, and widening it is how a node ends up recording
sync state for itself and repairing against a phantom peer forever. The lens
uses `get_local_identities_for_scope()`, which adds the capture ids and
falls back to what `server.py` persisted.

That fallback exists because of a bug worth remembering: `bacon-ssh` and
`bacon-web-admin` are **separate processes** with their own module globals,
so `set_local_link_identities()` -- called only by `server.py` -- left them
empty. "This node" was then an empty id list, which is indistinguishable
from no filter, so the SSH picker starred All nodes and This node at once
and narrowing did nothing. It raised nowhere. Every unit test set those
globals in-process, which is precisely why none of them saw it, and it was
caught only by driving a real SSH session against the deployed node.
`tests/test_node_view_scope.py::SeparateProcessTests` now forgets everything
only `server.py` could know before asserting.

### Deleting a synced record, and why raw SQL is not deleting

Every scope that syncs keeps a tombstone, and the tombstone is the delete.
The row is incidental: remove it with `DELETE FROM` and the first peer that
still holds a copy hands it straight back, because nothing local has any
reason to refuse it.

That is not hypothetical. A Trivia score of 2200, farmed through a scoring
exploit, was deleted from both our nodes with raw SQL and was back within the
hour from Chattanooga, which runs pre-fix code and holds its own copy. The
returned row carried the `T`-form timestamp, so it plainly came off the wire.

**The reasoning error is the part worth not repeating.** Chattanooga's
`game_scores` hash differed from ours, and I read that as proof it held a
*different* row -- when a differing hash is exactly what timestamp spelling
produces, which I had proven hours earlier in another scope. Absence of a
match is not evidence of difference in a system where matching is known to
be unreliable.

So use the scope's own delete function, never SQL:

| Scope | Function |
| --- | --- |
| `bulletins` | `delete_bulletin` |
| `mail` | `delete_mail` |
| `channels` | `delete_channel` / `delete_channel_comment` |
| `zork_saves` | `delete_zork_save` |
| `game_scores` | `delete_game_score` |
| `profiles` | `delete_user_profile` |

Each removes the row, records a tombstone with a snapshot of it, and pushes
a delete frame to peers. `TOMBSTONE_AWARE_SCOPES` in `message_processing.py`
is the list reconciliation consults before pulling a record it lacks; a scope
missing from it re-pulls whatever it deleted, and a scope in it without a
delete frame suppresses forever while the peer re-offers forever. A test
asserts every scope in the list can propagate.

Deletes are ordered by timestamp, not arrival: a score achieved *after* the
delete is a new score that happens to share a key, so it survives and clears
the tombstone. Both hash-side and tombstone-side timestamps are compared
through `_normalize_sync_timestamp`, because a `T`-form tombstone compared
raw sorts above every space-form timestamp on the same date and would
silently refuse the rest of that day's legitimate records.

Scores have their own page under **Tools -> Game Scores**, and a profile is
deleted from its client page under **Clients**. Both go through the functions
above, never SQL. Everything deleted this way is restorable from its snapshot
through the tombstone view.

The one trap left is `table_delete` in `web_admin.py`: its final `else` runs a
bare `DELETE FROM <table> WHERE id = ?`. That is fine for a local-only table
and wrong for every synced one, so a synced table added to `TABLE_CONFIG`
needs its own branch rather than that fallback.

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

**Timestamp spelling drift, everywhere except zork.** The same instant is
written `%Y-%m-%d %H:%M:%S` by a local write and `%Y-%m-%dT%H:%M:%S` by
`utils.decode_ts_second` when a record arrives from a peer, and the record
hash is built from that string -- so two nodes holding one identical record
disagree about it permanently. Fixed for `zork_saves`; `channels`,
`game_scores` and `public_chatter` still hash a timestamp in both hash
functions, and `bulletins` and `mail` hash one in the per-record manifest but
not the aggregate, which makes their drift *dormant* rather than absent.
Scoped in full, not started:
[docs/SYNC-HASH-TIMESTAMPS.md](docs/SYNC-HASH-TIMESTAMPS.md).

**Web Fetch is inoperable and says so in config syntax.** `[gateway]
allowed_hosts` is empty on the live node, so every fetch returns
`[ERR] blocked: no allowed_hosts configured` -- an internal setting name
shown to whoever tried to use the feature.

**No version anywhere a user can see it.** `get_display_version` is never
called outside the web admin, `version_info` and the Docker build, so a
person on the radio or over SSH cannot say what they are talking to.

**No post retraction.** `delete_bulletin` is never called from
`command_handlers.py`, so nobody can withdraw their own bulletin or comment;
it takes web-admin access. Bulletins are stored under a short name rather
than an account, so removing one person's posts is one at a time.

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

Tests use stdlib `unittest`, run under pytest:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
```

**1589 passing, 1 skipped** at `ccb238d`, with no known flakes.

### Mutate the code, or the tests are telling you what you hoped

Five times in one day a test written for a fix passed against code where that
fix had been removed. Each was found by breaking the code on purpose and
checking a test noticed, and none would have been found any other way:

- a guard rescued an assertion, so the thing it named was never exercised
- a mutation was anchored on the wrong line, and the pass was read as a result
- a mechanism was covered thoroughly and *nothing tested that anything called
  it* -- which was the bug
- a resolver was checked instead of the message it produces
- a database call was checked instead of the handler whose choice of argument
  was the actual behaviour

That last shape appeared twice: testing the thing underneath the decision
rather than the decision.

**Invalidate `__pycache__` between mutations.** A mutation that preserves file
size can survive its own restore -- Python validates bytecode on source mtime
and size, both of which matched -- so the mutated code keeps running while
`inspect.getsource` shows the clean original. One mutation was reported
"caught" against code that had never been restored.

**Do not report a verification script's verdict without reading its
transcript.** Two summary lines contradicted their own output, both times
saying a working fix was broken, because the assertion was written from the
wording expected rather than the wording the system emits ("Saved game
restored", not "resumed").

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
