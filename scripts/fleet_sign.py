#!/usr/bin/env python3
"""Sign fleet update instructions. Runs on the admin machine, never on a node.

That separation is the point of the whole design. The private key here can
tell every node in the group to run a particular commit; if it lived on a
node, then compromising any one node -- or reading its config.ini -- would
hand over the entire fleet. Nodes hold only the public half.

    python scripts/fleet_sign.py --init            # once, ever
    python scripts/fleet_sign.py --sign HEAD       # each time you ship
    python scripts/fleet_sign.py --verify <blob>   # what does this blob say?

The signed blob is not secret. It is safe to paste into a chat, an issue, or
the web admin of a node you do not control: it authorises one specific commit
for one specific group, and it cannot be edited without breaking the
signature.
"""
import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet_update  # noqa: E402


def key_path() -> Path:
    """Where the private key lives, per platform convention."""
    override = os.getenv("BBS_FLEET_KEY_PATH")
    if override:
        return Path(override)
    if os.name == "nt":
        base = Path(os.getenv("APPDATA", Path.home())) / "TC2-BaconBS"
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME",
                              Path.home() / ".config")) / "bacon-bbs"
    return base / "fleet-key"


def _git(*args):
    root = Path(__file__).resolve().parent.parent
    return subprocess.run(["git", *args], cwd=str(root),
                          capture_output=True, text=True, check=False)


def load_private_key():
    from cryptography.hazmat.primitives import serialization
    path = key_path()
    if not path.is_file():
        sys.exit(f"No fleet key at {path}.\nRun --init first (once, ever).")
    try:
        return serialization.load_pem_private_key(
            path.read_bytes(), password=None)
    except Exception as exc:
        sys.exit(f"Could not read the fleet key at {path}: {exc}")


def cmd_init(args) -> int:
    path = key_path()
    if path.is_file() and not args.force:
        # Overwriting orphans every node that trusts the old key, and there
        # is no way back without visiting each one.
        print(f"A fleet key already exists at {path}.", file=sys.stderr)
        print("Generating a new one would orphan every node that trusts the "
              "current key.\nUse --show-pubkey to see it, or --force if you "
              "genuinely mean to start over.", file=sys.stderr)
        return 1

    from cryptography.hazmat.primitives import serialization
    private_key, public_raw, kid = fleet_update.generate_keypair()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass  # Windows ACLs; the directory is already per-user

    print(f"Fleet key created: {path}")
    print("Back this up somewhere offline. It cannot be recovered, and losing")
    print("it means visiting every node by hand to install a new one.\n")
    print(f"Key id: {kid}\n")
    print("Paste this into config.ini on every node that should follow you,")
    print("or into Settings -> Fleet in its web admin:\n")
    print(_config_block(public_raw, args.group))
    return 0


def _config_block(public_raw: bytes, group: str) -> str:
    return (
        "[fleet]\n"
        f"group = {group}\n"
        f"trusted_keys = {fleet_update.public_key_entry(public_raw)}\n"
        "updates = auto\n"
    )


def cmd_show_pubkey(args) -> int:
    from cryptography.hazmat.primitives import serialization
    public_raw = load_private_key().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    if args.entry_only:
        print(fleet_update.public_key_entry(public_raw))
    else:
        print(_config_block(public_raw, args.group))
    return 0


def cmd_sign(args) -> int:
    resolved = _git("rev-parse", f"{args.ref}^{{commit}}")
    if resolved.returncode != 0:
        return _fail(f"{args.ref!r} is not a commit in this repository.")
    commit = resolved.stdout.strip()

    if not args.allow_unpushed:
        on_remote = _git("branch", "-r", "--contains", commit)
        if on_remote.returncode != 0 or not on_remote.stdout.strip():
            # Nodes fetch from the remote. A commit that exists only here
            # converges nowhere, and the failure appears later, on the nodes,
            # as a fetch error rather than here as a mistake.
            return _fail(
                f"Commit {commit[:12]} is not on any remote branch.\n"
                "Nodes fetch from the remote, so this would fail on every one "
                "of them.\nPush first, or pass --allow-unpushed if you know "
                "what you are doing.")

    version = args.version or _version_for(commit)
    from cryptography.hazmat.primitives import serialization
    private_key = load_private_key()
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)

    payload = fleet_update.build_payload(
        args.group, commit, version, fleet_update.key_id(public_raw))
    blob = fleet_update.encode_instruction(
        payload, fleet_update.sign_payload(payload, private_key))

    subject = _git("log", "-1", "--format=%s", commit).stdout.strip()
    print(f"group   {payload['g']}")
    print(f"commit  {commit}")
    print(f"version {version}")
    print(f"subject {subject}")
    print(f"issued  {payload['t']}")
    print(f"key     {payload['k']}\n")
    print("Paste this into Settings -> Fleet on any one node; it propagates "
          "to the rest:\n")
    print(blob)
    return 0


def _version_for(commit: str) -> str:
    """The version string that commit will report once a node is on it."""
    counted = _git("rev-list", "--count", "--first-parent", commit)
    if counted.returncode != 0:
        return ""
    try:
        build = int(counted.stdout.strip()) + fleet_update_offset()
    except ValueError:
        return ""
    return f"0.1.{build}"


def fleet_update_offset() -> int:
    import version_info
    return version_info._BUILD_OFFSET


def cmd_verify(args) -> int:
    """Show what a blob claims, and whether this key signed it.

    Useful before publishing, and for anyone handed a blob who wants to know
    what they are being asked to run.
    """
    try:
        payload, signature = fleet_update.decode_instruction(args.blob)
    except fleet_update.FleetVerificationError as exc:
        return _fail(str(exc))

    print(f"group   {payload.get('g')}")
    print(f"commit  {payload.get('c')}")
    print(f"version {payload.get('v')}")
    print(f"issued  {payload.get('t')}")
    print(f"key     {payload.get('k')}")

    if not key_path().is_file():
        print("\nNo local key, so the signature was not checked.")
        return 0
    from cryptography.hazmat.primitives import serialization
    public_raw = load_private_key().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    trusted = {fleet_update.key_id(public_raw): public_raw}
    try:
        fleet_update.verify_instruction(args.blob, trusted, payload.get("g", ""))
        print("\nSignature verifies against your key.")
        return 0
    except fleet_update.FleetVerificationError as exc:
        return _fail(f"\nSignature does NOT verify: {exc}")


def _fail(message: str) -> int:
    # Flush first: the detail lines go to stdout and the verdict to stderr,
    # and unflushed buffering prints the verdict above the detail it refers to.
    sys.stdout.flush()
    print(message, file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", default="baconbbs",
                        help="fleet group name (default: baconbbs)")
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("--init", aliases=["init"],
                          help="create the fleet key and print the node config block")
    init.add_argument("--force", action="store_true",
                      help="overwrite an existing key (orphans every node)")
    init.set_defaults(func=cmd_init)

    sign = sub.add_parser("--sign", aliases=["sign"],
                          help="sign an instruction for a commit")
    sign.add_argument("ref", nargs="?", default="HEAD")
    sign.add_argument("--version", default="",
                      help="version string to advertise (default: derived)")
    sign.add_argument("--allow-unpushed", action="store_true")
    sign.set_defaults(func=cmd_sign)

    show = sub.add_parser("--show-pubkey", aliases=["show-pubkey"],
                          help="print the public key / config block")
    show.add_argument("--entry-only", action="store_true")
    show.set_defaults(func=cmd_show_pubkey)

    verify = sub.add_parser("--verify", aliases=["verify"],
                            help="show what a signed blob says")
    verify.add_argument("blob")
    verify.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
