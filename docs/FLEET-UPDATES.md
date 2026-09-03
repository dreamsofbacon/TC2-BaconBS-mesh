# Fleet updates

Tell every node in a group to run the same commit, without SSH-ing to each
one. A node fetches the code from git itself; the mesh only ever carries a
short signed instruction saying which commit to be on.

---

## What the signature is actually protecting

Worth understanding before you arm this, because it decides what you have to
be careful about.

An update instruction is remote code execution. It travels over LoRa, which
anyone can transmit on, and over MQTT, where the sender's identity is read
out of the message body with no binding to whoever published it — so anyone
with publish rights to your topic can claim to be any of your nodes. The BBS
has no other authentication: inbound frames are trusted on a node id
appearing in a config list, and node ids are public.

So the signature is not a layer on top of a secure channel. **It is the only
thing standing between a stranger on your broker and root-less code
execution on your nodes.** Everything below follows from that:

- The private key lives on your admin machine and nowhere else. A node holds
  only the public half, so compromising a node does not let it command the
  fleet.
- A node with no `trusted_keys` ignores every instruction. That is the
  default, and it is what lets you share a broker with someone without
  joining fleets.
- An instruction is verified before it is stored, logged as trusted, or
  acted on.
- A replayed instruction is rejected, so a captured one cannot later pin a
  node to old, vulnerable code.

What it does **not** protect against: if your private key leaks, whoever has
it owns every node that trusts it. Back it up offline, and treat it like an
SSH key you never rotate.

---

## Setting it up

### 1. Create the key — once, ever

On your admin machine, in the repo:

```sh
python scripts/fleet_sign.py --group baconbbsvt init
```

It writes the private key to the platform's protected per-user configuration
directory, prints the exact path, and prints an `enroll` command for each node.
No downloaded file or manual key placement is involved. It also prints the
equivalent configuration block for recovery:

```ini
[fleet]
group = baconbbsvt
trusted_keys = fk15a23a:Z26fdWpjkQTHQCmKIUfvFOT3HoXuPZ1jGe-g2KBTolI
updates = auto
api_token_hash =
```

**Back that key up now, somewhere offline.** It cannot be recovered. Losing
it means visiting every node by hand to install a new one — which is why
`init` refuses to overwrite an existing key unless you pass `--force`.

The `group` is just a name. It scopes an instruction, so nodes in a
different group ignore yours even if they trust your key. Using your MQTT
topic prefix keeps it memorable.

### 2. Configure each node

Copy the public `fk...:...` entry from `show-pubkey --entry-only` to each node,
then run this in the node's repository:

```sh
python scripts/fleet_sign.py --group baconbbsvt enroll "fk...:..." --config config.ini --updates auto
```

The command validates the key, preserves existing trusted keys, writes
atomically, and creates `config.ini.fleet-backup` before the first change.
Alternatively, use the Fleet web page to enroll the public key. Then restart
the services:

```sh
sudo systemctl restart mesh-bbs.service bacon-web-admin.service
```

Check it took: open **Fleet** in the web admin. It should show your key id
under *Trusted keys* and `updates: auto`. If it says *no trusted key, so
every instruction is ignored*, the paste did not land.

`updates` has three settings:

| Value | Behaviour |
| --- | --- |
| `auto` | Fetch, smoke-test, switch, restart. Rolls back if the new version cannot run. |
| `notify` | Record the target and show it on the Fleet page. You apply it yourself. |
| `off` | Ignore instructions entirely. The default. |

Add `pin_commit = <sha>` to freeze a node on one commit while you debug it.
Targets are still recorded, just not applied — useful when you do not want
to remove the key.

### 3. Configure one seed node

Generate a scoped API token on the admin machine:

```sh
python scripts/fleet_sign.py token
```

Add the printed `api_token_hash` to the seed node's `[fleet]` section and
restart its web admin. Keep the raw token on the admin machine only. Set these
environment variables to avoid repeating it on the command line:

```sh
BBS_FLEET_SEED=http://seed-node:8081
BBS_FLEET_API_TOKEN=the-raw-token
```

Use the platform's normal persistent environment mechanism if desired. The
same variables and commands work on Windows and Linux.

Before the first release, verify the key, pushed commit, seed API, group,
update mode, and trusted signer in one pass:

```sh
python scripts/fleet_sign.py --group baconbbsvt doctor HEAD
```

### 4. Check it before you rely on it

On the admin machine, sign the commit the nodes are already on:

```sh
python scripts/fleet_sign.py --group baconbbsvt deploy origin/main
```

The seed should accept it, and the Fleet page should show *This node is on the
target commit*. Nothing restarts, because there is nothing to change.

That proves the whole path — signing, verification, storage — with no risk.

---

## Issuing an update

```sh
git push                                                  # nodes fetch from the remote
python scripts/fleet_sign.py --group baconbbsvt deploy HEAD
```

The seed verifies and stores the instruction, advertises it before applying,
and relays it during normal sync heartbeats. Nodes that reconnect later receive
the newest signed target because it remains durable until superseded.

The raw signed-instruction box remains on the Fleet page as a recovery path.

Check convergence from the same admin machine:

```sh
python scripts/fleet_sign.py --group baconbbsvt status
```

The local row includes probation or rollback-guard state. Peer rows are
self-reported and advisory; `healthy` means their reported commit matches the
signed target, while `pending` includes drifted, stale, or not-yet-updated peers.

`sign` refuses a commit that is not on a remote branch, because nodes fetch
from the remote and a local-only commit converges nowhere — a failure that
would otherwise show up much later, on the nodes, as a fetch error.

To see what a blob says before publishing it:

```sh
python scripts/fleet_sign.py verify <blob>
```

---

## What a node does with it

1. **Verify.** Signature, group, and that the instruction is newer than the
   last one accepted. A failure is logged with the reason and nothing else
   happens.
2. **Fetch** the commit from the remote. The hash is content-addressed, so a
   hostile mirror can fail to serve it but cannot substitute other code.
3. **Smoke-test.** The target is checked out into a throwaway worktree and
   compiled and imported there. **If it does not load, the update is refused
   and the node stays exactly where it is** — this is the check that stops a
   syntax error taking a node off the air.
4. **Switch** — `git checkout`, `pip install -r requirements.txt`.
5. **Restart.** The process exits; systemd (`Restart=always`) starts it on
   the new code. No sudo is involved anywhere: the repo and the venv are
   owned by the service user.
6. **Probation.** For the next few minutes the node is on trial. If it fails
   to start three times, it reverts to the previous commit and comes back on
   that. Once it has been serving for five minutes, the rollback disarms.

---

## When something goes wrong

**A node reverted itself.** `update_state.json` in the repo says which
commit failed and which was restored. The journal has the reason:

```sh
sudo journalctl -u mesh-bbs.service | grep update_guard
```

**Roll the fleet back deliberately.** Name the known-good commit or tag:

```sh
python scripts/fleet_sign.py --group baconbbsvt rollback
```

With no ref, the CLI selects the previous distinct signed target from history.
Pass an explicit known-good commit or tag to override it. Confirm the prompt
(or use `--yes` in automation). Rollback uses the normal signed deployment
path. Nothing distinguishes going backwards from going forwards, other than
the replay check, which compares when the instruction was issued rather than
which commit it names.

**Freeze one node.** Set `pin_commit` on it, or `updates = off`.

**Recover by hand.** Nothing here removes the old path:

```sh
cd /home/bacon/TC2-BaconBS-mesh
git checkout main && git pull --ff-only
sudo systemctl restart mesh-bbs.service bacon-web-admin.service
```

---

## Adding someone else's node

You cannot update a node that has not chosen to let you, and that is
deliberate. Give them the `[fleet]` block from `--show-pubkey`; they paste
it into their own `config.ini`. Until they do, your instructions are
ignored by their node.

Point out to them that accepting your key means you can run code on their
hardware. That is a real thing to consent to, and `updates = notify` is a
reasonable middle ground: they see the target and apply it themselves.

---

## Limits

- **Docker nodes cannot converge.** A container cannot `git checkout`
  itself. It reports its version and shows drift; updating it means pulling
  a new image.
- **A node must be able to reach the git remote.** The instruction is tiny,
  but the code comes over HTTPS from GitHub.
- **Git commit hashes are SHA-1.** Content-addressing keeps a hostile mirror
  from substituting code, but the signature over the hash is what carries
  authority.
- **The web admin password is plaintext** in `config.ini` and defaults to
  `change-me`. Signing stays offline precisely so that password is not an
  RCE credential — but change it anyway.
