# SSH access

Reaching the BBS over SSH instead of a radio, with accounts anyone can
register for themselves.

The first implementation follows this design in `ssh_server.py` and
`ssh_auth.py`. It is disabled by default, uses password authentication and
explicit `new:<alias>` registration, and runs as the separate
`bacon-ssh.service`. Public-key authentication and moderation controls remain
future work.

---

## The problem you have to solve first

Today, identity is the node id a packet arrived from. That is not a weak
credential; it is not a credential at all. It works because possessing the
radio *is* the proof, and a stranger cannot cheaply become your neighbour's
node.

Mail authorization is where this becomes concrete. `_mail_recipient_scope`
(`db_operations.py:3758`) is the entire check:

```python
cursor.execute("SELECT account_id FROM linked_nodes WHERE node_id = ?", ...)
# ... then: SELECT ... FROM mail WHERE recipient IN (those node ids)
```

Read authorization is a string comparison against the node id you present.
There is no second factor, no ownership check, nothing else anywhere in the
codebase. **So the moment a self-registered stranger can choose or influence
the node id their session presents, they can read anyone's mail.** Not by
exploiting a bug — by using the system exactly as designed, with a different
value in one field.

Everything below follows from that.

### The rule: an SSH identity is a namespace it cannot leave

- An SSH session's node id is `ssh:<account_id>`, derived server-side from
  the authenticated account. It is never read from anything the client
  sends.
- A session presenting a `!`-prefixed (Meshtastic), `mqtt:`-prefixed, bare
  hex (MeshCore), or `emu:` id is **rejected at the door, not sanitised**.
  Stripping or rewriting a hostile value is how these bugs come back; the
  connection should fail.
- `utils.home_network()` (`utils.py:706`) gets an `ssh` branch, the same way
  `mqtt:` and `emu:` have theirs, so an SSH id can never fall through to the
  unrecognised-shape default and be mistaken for a MeshCore node.
- Linking an SSH account to a real mesh node uses the existing one-time code
  flow (`link_codes`, `db_operations.py:4789`) and nothing else. That flow
  proves radio possession, which is the only proof this system has ever
  accepted. A self-registered account that has not done this reaches only
  its own mail.

If you build one thing from this document, build the namespace rule. The
rest is ordinary engineering; this is the part that is irreversible if you
get it wrong, because mail already sent under a leaked identity cannot be
un-read.

---

## What already exists to build on

More than you would expect, which is why this is worth doing properly rather
than bolting a shell onto it.

| Piece | Where | What it gives you |
| --- | --- | --- |
| `accounts` | `db_operations.py:1091` | A stable `account_id`, an alias, a unique alias index |
| `linked_nodes` | `db_operations.py:1115` | Many node ids to one account — already how a dual-radio user is handled |
| `link_codes` | `db_operations.py:1129` | One-time codes, TTL, redemption |
| `link_attempts` | `db_operations.py:1140` | Rate-limit and audit trail, already written to |
| `EmulatorSession` | `bbs_emulator.py` | A non-radio session driving the real command path |

What is missing is exactly one thing: **a credential.** Nothing in this
codebase stores a per-user secret today. The web admin has a single shared
plaintext password; the fleet keys are per-operator, not per-user.

### The session core is already built

`bbs_emulator.EmulatorSession` exists and is in use behind the Emulator page.
It holds an identity, drives `message_processing.process_message`, captures
replies through a stub interface, and cleans up menu state and game
subprocesses when it closes. An SSH front end is that class with a different
transport:

```
asyncssh connection  ->  authenticate  ->  EmulatorSession(ssh:<account_id>)
                                            |
                     read a line  ------>  session.send(line)
                     write chunks <------  the reply
```

This is deliberate. The emulator was built first so that the risky part of
SSH — the session lifecycle, the fake interface, cleanup of dfrotz
subprocesses — is already written and already under test before anything
listens on a port.

One real difference: the emulator suppresses chunking-relevant pacing and an
SSH client has no 220-byte packet limit at all. An SSH session should set
`max_text_bytes` high, or the user reads a menu split into packets for a
radio that is not there.

---

## Registration, as chosen

Open self-registration: anyone who can reach the port picks a name and a
password.

This is the widest exposure of the options, and it is a legitimate choice for
a hobbyist BBS — it is how essentially every dial-up board worked, and the
content here is bulletins and games, not banking. But it means the port is
the security boundary, so these are not optional extras:

**Alias squatting must be refused.** Registration rejects an alias matching
any existing `accounts.alias`, or any `mesh_clients.short_name` /
`long_name`. Without this a stranger registers as "Zorak" and every board
post they make reads as his. The alias index is already unique
(`db_operations.py:1091`); the roster check is new.

**Registration is rate-limited per source address**, reusing
`record_link_attempt` (`db_operations.py`) rather than inventing a second
audit trail. A registration flood is the cheapest attack on this design.

**Passwords are hashed, and the hash is not homemade.** `cryptography` is
already a dependency (the fleet feature added it), so scrypt is available
with no new package: `cryptography.hazmat.primitives.kdf.scrypt`. Never
plaintext — the web admin's plaintext password is a known wart, not a
precedent to copy.

**A new account gets nothing but its own mail.** Spelled out explicitly
because each of these is a decision, not an accident:

- Not in `allowed_nodes`, so the Urgent board stays radio-gated
  (`db_operations.py:4868`)
- No fleet authority — that is Ed25519-signed and unreachable from a session
- No web admin access — a different credential entirely
- No `bbs_nodes` membership, so it cannot inject sync frames

**Per-account resource caps.** A Zork session is a `dfrotz` subprocess
(`zork_port.py:303`). One account, many connections, many subprocesses is a
trivial way to exhaust a Pi. Cap concurrent sessions per account and in
total, and reuse the emulator's idle sweep.

---

## Deployment shape

- `asyncssh`, in a **separate systemd unit** (`bacon-ssh.service`) following
  the `mesh-bbs` / `bacon-web-admin` split. A crash in the SSH front end
  must not take the radio off the air.
- **Non-root, on a high port** (2222). Nothing here needs a privileged bind,
  and nothing here should run as root.
- The host key is generated once and backed up. Losing it means every client
  sees a host-key-changed warning, which trains people to click through the
  one warning that matters.
- Password auth needs `fail2ban` or asyncssh's own throttling. Public-key
  auth, added later, is strictly better and worth offering as an option once
  an account exists.

### Before anything listens on a public port

`bacon-web-admin.service:13` sets `BBS_WEBGUI_HOST=0.0.0.0`, the web admin
serves plain HTTP, and the admin password defaults to `change-me` in
plaintext in `config.ini`. Today that is behind a LAN. Exposing this host to
the internet without fixing it hands over the entire database — every
bulletin, every private mail, and the Emulator page, which can post as any
node on the mesh.

That is a prerequisite, not a recommendation.

---

## What this does not solve

- **The mesh side is unchanged.** Anyone who can transmit can still claim any
  node id over the air. SSH accounts are strictly stronger than radio
  identity, which is worth stating plainly: they do not raise the floor for
  existing users.
- **An SSH user is not a mesh user.** They have no radio, so they cannot
  receive mail addressed to a node id, and nothing relays their traffic
  onto the mesh. They read boards, play games, and mail other accounts.
  Whether that is enough is the question worth settling before building.
- **Open registration means moderation becomes a real job.** There is no
  ban list, no rate limit on posting, and no way to remove a user's content
  in bulk. None of that matters at three friends on a LAN; all of it matters
  on day two of a public port.
