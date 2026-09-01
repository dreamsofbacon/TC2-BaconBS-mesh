"""Signed fleet update instructions: build, verify, and decide whether to act.

A fleet update tells every node in a group to converge on one commit. That is
a remote-code-execution primitive travelling over a medium anyone can
transmit on, so the signature is not a nicety layered on a secure channel --
it IS the security boundary.

What the BBS trusted before this module existed was a single string
membership test: `sender_node_id in bbs_nodes`. On MQTT the sender identity
is read straight out of the message body with no binding to the publishing
client, so anyone with publish rights to the topic can claim to be any peer.
Nothing here may rely on who a frame claims to be from.

The rules that follow from that, each enforced below:

  * Verify before anything else. The signature is checked before the payload
    reaches persistence, logging as trusted, or any action.
  * A node with no configured trusted key ignores every instruction. That is
    the default, and it is what lets someone share a broker with this fleet
    without joining it.
  * Replay is rejected by a per-key monotonic timestamp. Without it a
    captured instruction could pin a node to a known-bad version forever.
  * Capabilities are never authorization. `peer_supports()` reports what a
    peer claims about itself; it decides only whether we bother sending.

The instruction carries a commit hash rather than code. Git objects are
content-addressed, so fetching that hash from a hostile mirror still yields
the same tree or fails -- the mesh only ever carries the instruction.
"""
import base64
import binascii
import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Optional

# Key id prefix, so a node can hold several keys and say which one signed.
KEY_ID_PREFIX = "fk"
_KEY_ID_RE = re.compile(r"^fk[0-9a-f]{6}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

# Payload keys are single letters because this has to fit a LoRa packet.
_REQUIRED_FIELDS = ("g", "c", "v", "t", "n", "k")


class FleetVerificationError(Exception):
    """A signed instruction was rejected. The message says why.

    Every rejection reason is distinct on purpose: "signature invalid",
    "wrong group" and "replayed" send an operator to completely different
    places, and collapsing them into one error is how a misconfiguration gets
    mistaken for an attack.
    """


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padded = str(text) + "=" * (-len(str(text)) % 4)
    return base64.urlsafe_b64decode(padded)


def key_id(public_key_raw: bytes) -> str:
    """Short, stable identifier for a public key.

    Lets a payload name which key signed it without carrying the key, and
    lets an operator eyeball that the key a node trusts is the one they
    think it is.
    """
    digest = hashlib.blake2b(public_key_raw, digest_size=3).hexdigest()
    return f"{KEY_ID_PREFIX}{digest}"


def canonical_payload(payload: dict) -> bytes:
    """The exact bytes that get signed.

    Sorted keys and no whitespace: signing and verification must agree
    byte-for-byte, and a dict's insertion order is not something to bet a
    security boundary on.
    """
    return json.dumps(
        {k: payload[k] for k in sorted(payload)},
        separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def build_payload(group: str, commit: str, version: str, signer_key_id: str,
                  issued_at: Optional[str] = None) -> dict:
    """Assemble an unsigned instruction."""
    normalized_commit = str(commit or "").strip().lower()
    if not _COMMIT_RE.match(normalized_commit):
        raise ValueError(
            "commit must be a full 40-character hex sha; an abbreviated one "
            "is ambiguous and cannot be verified against a fetch")
    if not str(group or "").strip():
        raise ValueError("group is required: it is what scopes the instruction")
    return {
        "g": str(group).strip(),
        "c": normalized_commit,
        "v": str(version or "").strip(),
        "t": issued_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n": secrets.token_hex(8),
        "k": str(signer_key_id).strip(),
    }


def encode_instruction(payload: dict, signature: bytes) -> str:
    """Wire/paste form: two base64 fields, no separators that collide with |."""
    return f"{_b64(canonical_payload(payload))}.{_b64(signature)}"


def decode_instruction(blob: str) -> tuple:
    """Split a blob into (payload dict, signature bytes) WITHOUT trusting it.

    Nothing here has been verified yet. The caller must pass the result to
    verify_instruction() before treating any field as true.
    """
    text = str(blob or "").strip()
    if text.count(".") != 1:
        raise FleetVerificationError(
            "malformed instruction: expected <payload>.<signature>")
    payload_b64, signature_b64 = text.split(".", 1)
    try:
        payload = json.loads(_unb64(payload_b64).decode("utf-8"))
        signature = _unb64(signature_b64)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetVerificationError(f"malformed instruction: {exc}") from exc
    if not isinstance(payload, dict):
        raise FleetVerificationError("malformed instruction: payload is not an object")
    missing = [f for f in _REQUIRED_FIELDS if not str(payload.get(f, "")).strip()]
    if missing:
        raise FleetVerificationError(
            f"instruction is missing required field(s): {', '.join(missing)}")
    return payload, signature


def _load_public_key(raw: bytes):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    return Ed25519PublicKey.from_public_bytes(raw)


def parse_trusted_keys(configured) -> dict:
    """Map key id -> raw public key bytes, from config.

    Accepts a CSV string or a list of "fkabc123:<base64>" entries. A malformed
    entry is dropped with a warning rather than taking the whole list down --
    one bad paste should not silently disarm the keys either side of it, and
    the warning is what tells the operator which one to fix.
    """
    if isinstance(configured, str):
        entries = [part.strip() for part in configured.split(",")]
    else:
        entries = [str(part).strip() for part in (configured or [])]

    keys = {}
    for entry in entries:
        if not entry:
            continue
        if ":" not in entry:
            logging.warning("Fleet: ignoring trusted key without an id prefix: %r", entry[:24])
            continue
        declared_id, _, key_b64 = entry.partition(":")
        declared_id = declared_id.strip().lower()
        try:
            raw = _unb64(key_b64.strip())
        except binascii.Error:
            logging.warning("Fleet: ignoring trusted key %s: not valid base64", declared_id)
            continue
        if len(raw) != 32:
            logging.warning(
                "Fleet: ignoring trusted key %s: an ed25519 public key is 32 "
                "bytes, got %d", declared_id, len(raw))
            continue
        actual = key_id(raw)
        if declared_id and declared_id != actual:
            # The id is derived from the key, so a mismatch means the entry
            # was edited or assembled by hand. Trusting it would let the id
            # shown to an operator differ from the key actually in use.
            logging.warning(
                "Fleet: ignoring trusted key: declared id %s does not match "
                "the key's real id %s", declared_id, actual)
            continue
        keys[actual] = raw
    return keys


def verify_instruction(blob: str, trusted_keys: dict, group: str,
                       last_issued_at: Optional[str] = None) -> dict:
    """Return the verified payload, or raise FleetVerificationError.

    Order matters. The signature is checked before the group and replay
    checks so that an unsigned attacker learns nothing about our
    configuration from which rejection they get back.
    """
    if not trusted_keys:
        raise FleetVerificationError(
            "no trusted fleet keys configured on this node, so update "
            "instructions are ignored")

    payload, signature = decode_instruction(blob)

    named_key = str(payload.get("k", "")).strip().lower()
    public_raw = trusted_keys.get(named_key)
    if public_raw is None:
        raise FleetVerificationError(
            f"instruction was signed by key {named_key}, which this node does "
            "not trust")

    from cryptography.exceptions import InvalidSignature
    try:
        _load_public_key(public_raw).verify(signature, canonical_payload(payload))
    except InvalidSignature as exc:
        raise FleetVerificationError(
            "signature does not match the payload: it was tampered with or "
            "signed by a different key") from exc

    # Signature is good from here on; the payload can be trusted as data.
    if str(payload.get("g", "")).strip() != str(group or "").strip():
        raise FleetVerificationError(
            f"instruction is for fleet group {payload.get('g')!r}, this node "
            f"is in {group!r}")

    if not _COMMIT_RE.match(str(payload.get("c", "")).strip().lower()):
        raise FleetVerificationError("instruction does not name a full commit sha")

    if last_issued_at and str(payload.get("t", "")) <= str(last_issued_at):
        raise FleetVerificationError(
            f"instruction is a replay: issued {payload.get('t')}, but this "
            f"node has already accepted one from {last_issued_at}")

    return payload


def sign_payload(payload: dict, private_key) -> bytes:
    """Sign on the admin machine. Nodes never call this."""
    return private_key.sign(canonical_payload(payload))


def generate_keypair():
    """Returns (private_key, public_key_raw_bytes, key_id)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    private_key = Ed25519PrivateKey.generate()
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_key, public_raw, key_id(public_raw)


def public_key_entry(public_raw: bytes) -> str:
    """The `fkabc123:<base64>` form that goes in a node's trusted_keys."""
    return f"{key_id(public_raw)}:{_b64(public_raw)}"


# ---------------------------------------------------------------------------
# Applying a verified target.
#
# No privilege is needed anywhere here, which is the point. The repository and
# the venv are owned by the service user, so git and pip work unprivileged;
# both systemd units are Restart=always, so exiting is a restart onto the new
# code. Nothing has to be added to sudoers.
# ---------------------------------------------------------------------------
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

UPDATE_STATE_FILE = "update_state.json"

# Three starts is enough to distinguish "crashes every time" from a one-off
# failure to bind a port or open a serial device during a busy boot.
MAX_PROBATION_ATTEMPTS = 3


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def _git(*args, cwd=None, timeout=300):
    """Run git with the repo marked safe.

    -c safe.directory survives "dubious ownership", which git raises when the
    service user is not the one that cloned the repo -- a normal state for a
    daemon and otherwise a silent failure.
    """
    root = cwd or repo_root()
    return subprocess.run(
        ["git", "-c", f"safe.directory={root}", *args],
        cwd=str(root), capture_output=True, text=True, timeout=timeout, check=False,
    )


def current_commit() -> str:
    result = _git("rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else ""


def commit_exists(commit: str) -> bool:
    return _git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


def fetch_commit(commit: str, remote: str = "origin") -> tuple:
    """Bring the target into the local object store. Returns (ok, detail).

    The commit hash is content-addressed, so a hostile mirror cannot
    substitute different code under the same hash -- it can only fail to
    provide it. That is what keeps the transport out of the trust boundary.
    """
    if commit_exists(commit):
        return True, "already present locally"
    fetched = _git("fetch", "--quiet", remote, commit)
    if fetched.returncode != 0:
        # Some servers refuse fetch-by-sha; fall back to fetching everything.
        fetched = _git("fetch", "--quiet", "--all", "--prune")
    if not commit_exists(commit):
        return False, (str(fetched.stderr or "").strip()
                       or f"{remote} does not have commit {commit[:12]}")
    return True, "fetched"


def smoke_test_commit(commit: str) -> tuple:
    """Compile and import the target in a throwaway worktree.

    This is the layer that matters. A syntax or import error in the new code
    would otherwise be unrecoverable: the process that would roll it back is
    the one that cannot start. Checking BEFORE switching means the running
    node is never touched by a version that cannot load.

    Returns (ok, detail).
    """
    worktree = Path(tempfile.mkdtemp(prefix="bbs-update-"))
    added = False
    try:
        result = _git("worktree", "add", "--detach", str(worktree), commit)
        if result.returncode != 0:
            return False, f"could not create a test worktree: {result.stderr.strip()}"
        added = True

        compiled = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", str(worktree)],
            capture_output=True, text=True, timeout=300, check=False)
        if compiled.returncode != 0:
            return False, f"target does not compile: {compiled.stdout.strip()[:300]}"

        imported = subprocess.run(
            [sys.executable, "-c",
             "import server, web_admin, db_operations, message_processing"],
            cwd=str(worktree), capture_output=True, text=True,
            timeout=300, check=False,
            env={**os.environ, "BBS_SMOKE_TEST": "1"})
        if imported.returncode != 0:
            tail = (imported.stderr or "").strip().splitlines()
            return False, "target does not import: " + (tail[-1] if tail else "?")
        return True, "compiles and imports"
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"smoke test could not run: {exc}"
    finally:
        if added:
            _git("worktree", "remove", "--force", str(worktree))
        shutil.rmtree(worktree, ignore_errors=True)
        _git("worktree", "prune")


def _state_path() -> Path:
    return Path(os.getenv("BBS_UPDATE_STATE_PATH")
                or (repo_root() / UPDATE_STATE_FILE))


def read_update_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_update_state(state: dict) -> None:
    path = _state_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def clear_update_state() -> None:
    try:
        _state_path().unlink()
    except OSError:
        pass


def install_requirements() -> tuple:
    """A new version may need new dependencies. The venv is ours to write."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet",
         "-r", str(repo_root() / "requirements.txt")],
        capture_output=True, text=True, timeout=900, check=False)
    if result.returncode != 0:
        return False, (result.stderr or "").strip()[:300]
    return True, "dependencies up to date"


def apply_target(commit: str, version: str = "") -> tuple:
    """Fetch, smoke-test, switch, and arm the rollback guard.

    Returns (applied, detail). `applied` True means the caller should exit so
    systemd restarts onto the new code.
    """
    previous = current_commit()
    if previous.lower() == str(commit).lower():
        return False, "already on the target commit"

    ok, detail = fetch_commit(commit)
    if not ok:
        return False, f"fetch failed: {detail}"

    ok, detail = smoke_test_commit(commit)
    if not ok:
        # Refusing here is the difference between a declined update and an
        # unreachable node.
        logging.error("Fleet update refused, staying on %s: %s", previous[:12], detail)
        return False, f"refused: {detail}"

    # Armed BEFORE the checkout: if the switch or a later start fails, the
    # guard must already know what to go back to.
    write_update_state({
        "state": "probation",
        "previous_commit": previous,
        "target_commit": commit,
        "target_version": version,
        "attempts": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    checked_out = _git("checkout", "--quiet", "--detach", commit)
    if checked_out.returncode != 0:
        clear_update_state()
        return False, f"checkout failed: {checked_out.stderr.strip()}"

    installed, install_detail = install_requirements()
    if not installed:
        logging.warning("Fleet update: %s (continuing; the guard will catch a "
                        "failure to start)", install_detail)

    logging.warning("Fleet update: switched %s -> %s; exiting so systemd "
                    "restarts on the new code", previous[:12], str(commit)[:12])
    return True, f"applied {str(commit)[:12]}"
