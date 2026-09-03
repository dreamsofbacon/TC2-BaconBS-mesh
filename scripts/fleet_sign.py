#!/usr/bin/env python3
"""Manage signed fleet updates from an admin machine or enroll a node.

Signing commands run only on the admin machine. The private key here can
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
import configparser
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
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
    entry = fleet_update.public_key_entry(public_raw)
    print("On each node, run this command from its repository:\n")
    print(f'python scripts/fleet_sign.py --group {args.group} enroll "{entry}"')
    print("\nThe equivalent config block is:\n")
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
    try:
        payload, subject, blob = _build_signed_instruction(args)
    except ValueError as exc:
        return _fail(str(exc))

    print(f"group   {payload['g']}")
    print(f"commit  {payload['c']}")
    print(f"version {payload['v']}")
    print(f"subject {subject}")
    print(f"issued  {payload['t']}")
    print(f"key     {payload['k']}\n")
    print("Paste this into Settings -> Fleet on any one node; it propagates "
          "to the rest:\n")
    print(blob)
    return 0


def _build_signed_instruction(args):
    resolved = _git("rev-parse", f"{args.ref}^{{commit}}")
    if resolved.returncode != 0:
        raise ValueError(f"{args.ref!r} is not a commit in this repository.")
    commit = resolved.stdout.strip()

    if not args.allow_unpushed:
        on_remote = _git("branch", "-r", "--contains", commit)
        if on_remote.returncode != 0 or not on_remote.stdout.strip():
            # Nodes fetch from the remote. A commit that exists only here
            # converges nowhere, and the failure appears later, on the nodes,
            # as a fetch error rather than here as a mistake.
            raise ValueError(
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
    return payload, subject, blob


def _fleet_apply_url(seed: str) -> str:
    return str(seed or "").rstrip("/") + "/api/fleet/apply"


def _fleet_status_url(seed: str) -> str:
    return str(seed or "").rstrip("/") + "/api/fleet/status"


def _submit_instruction(seed: str, token: str, blob: str, timeout: int = 30) -> dict:
    body = json.dumps({"instruction": blob}).encode("utf-8")
    request = urllib.request.Request(
        _fleet_apply_url(seed), data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = ""
        raise ValueError(detail or f"Seed returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not submit to {_fleet_apply_url(seed)}: {exc}") from exc


def _fetch_status(seed: str, token: str, timeout: int = 30) -> dict:
    request = urllib.request.Request(
        _fleet_status_url(seed), method="GET",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = ""
        raise ValueError(detail or f"Seed returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {_fleet_status_url(seed)}: {exc}") from exc


def cmd_deploy(args) -> int:
    seed = str(args.seed or "").strip()
    token = str(args.token or "").strip()
    if not seed:
        return _fail("No seed URL. Pass --seed or set BBS_FLEET_SEED.")
    if not token:
        return _fail("No fleet API token. Pass --token or set BBS_FLEET_API_TOKEN.")
    try:
        payload, subject, blob = _build_signed_instruction(args)
        response = _submit_instruction(seed, token, blob, timeout=args.timeout)
    except ValueError as exc:
        return _fail(str(exc))

    print(f"Accepted by {seed}")
    print(f"group   {payload['g']}")
    print(f"commit  {payload['c']}")
    print(f"version {payload['v']}")
    print(f"subject {subject}")
    print(f"status  {response.get('code', 'accepted')}")
    return 0


def _require_seed_credentials(args):
    seed = str(args.seed or "").strip()
    token = str(args.token or "").strip()
    if not seed:
        raise ValueError("No seed URL. Pass --seed or set BBS_FLEET_SEED.")
    if not token:
        raise ValueError(
            "No fleet API token. Pass --token or set BBS_FLEET_API_TOKEN.")
    return seed, token


def _commit_matches(commit: str, target: str) -> bool:
    commit = str(commit or "").lower()
    target = str(target or "").lower()
    return bool(commit and target and target.startswith(commit))


def cmd_status(args) -> int:
    try:
        seed, token = _require_seed_credentials(args)
        status = _fetch_status(seed, token, timeout=args.timeout)
    except ValueError as exc:
        return _fail(str(exc))

    target = status.get("target") or {}
    target_commit = str(target.get("commit") or "")
    print(f"Fleet {status.get('group') or '(not configured)'}")
    print(f"target  {target_commit or 'none'}")
    local = status.get("local") or {}
    local_state = (local.get("update_state") or {}).get("state") or (
        "healthy" if local.get("on_target") else "pending")
    print(f"local   {str(local.get('commit') or 'unknown'):<12} {local_state}")
    for node in status.get("nodes") or []:
        commit = str(node.get("commit_hash") or "")
        state = str(node.get("fleet_state") or "") or (
            "healthy" if _commit_matches(commit, target_commit) else "pending")
        print(f"{str(node.get('node_id') or '?'):<8} {commit or 'unknown':<12} "
              f"{state}  {node.get('reported_at') or 'never'}")
    return 0


def cmd_rollback(args) -> int:
    if not str(args.ref or "").strip():
        try:
            seed, token = _require_seed_credentials(args)
            status = _fetch_status(seed, token, timeout=args.timeout)
        except ValueError as exc:
            return _fail(str(exc))
        current = str((status.get("target") or {}).get("commit") or "")
        previous = next(
            (str(item.get("commit") or "")
             for item in (status.get("history") or [])
             if item.get("commit") and str(item.get("commit")) != current),
            "",
        )
        if not previous:
            return _fail("No previous distinct signed target is available.")
        args.ref = previous
    if not args.yes:
        answer = input(
            f"Sign and deploy {args.ref!r} as the new fleet target? "
            "Type 'rollback' to continue: ").strip().lower()
        if answer != "rollback":
            return _fail("Rollback cancelled.")
    return cmd_deploy(args)


def cmd_doctor(args) -> int:
    failures = []
    public_key_id = ""
    try:
        from cryptography.hazmat.primitives import serialization
        private_key = load_private_key()
        public_raw = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)
        public_key_id = fleet_update.key_id(public_raw)
        print(f"PASS signing key {public_key_id} at {key_path()}")
    except (Exception, SystemExit) as exc:
        failures.append(f"signing key: {exc}")
        print(f"FAIL signing key: {exc}")

    resolved = _git("rev-parse", f"{args.ref}^{{commit}}")
    commit = resolved.stdout.strip() if resolved.returncode == 0 else ""
    if commit:
        print(f"PASS commit {commit[:12]} resolves from {args.ref}")
        remote = _git("branch", "-r", "--contains", commit)
        if remote.returncode == 0 and remote.stdout.strip():
            print("PASS commit is available on a remote branch")
        else:
            failures.append("commit is not on a remote branch")
            print("FAIL commit is not on a remote branch")
    else:
        failures.append(f"{args.ref!r} is not a commit")
        print(f"FAIL {args.ref!r} is not a commit")

    try:
        seed, token = _require_seed_credentials(args)
        status = _fetch_status(seed, token, timeout=args.timeout)
        print(f"PASS seed API {seed}")
        if status.get("group") == args.group:
            print(f"PASS seed fleet group {args.group}")
        else:
            failures.append(
                f"seed group is {status.get('group')!r}, expected {args.group!r}")
            print(f"FAIL seed fleet group is {status.get('group')!r}")
        if status.get("mode") != "off":
            print(f"PASS seed update mode {status.get('mode')}")
        else:
            failures.append("seed fleet updates are off")
            print("FAIL seed fleet updates are off")
        trusted = status.get("trusted_key_ids") or []
        if public_key_id and public_key_id in trusted:
            print(f"PASS seed trusts signing key {public_key_id}")
        else:
            failures.append("seed does not trust the local signing key")
            print("FAIL seed does not trust the local signing key")
        if status.get("config_error"):
            failures.append(str(status["config_error"]))
            print(f"FAIL seed configuration: {status['config_error']}")
    except ValueError as exc:
        failures.append(str(exc))
        print(f"FAIL seed API: {exc}")

    if failures:
        print(f"\nDoctor found {len(failures)} problem(s).", file=sys.stderr)
        return 1
    print("\nFleet is ready to deploy.")
    return 0


def cmd_token(args) -> int:
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    print("Add this to the seed node's [fleet] section:")
    print(f"api_token_hash = {digest}\n")
    print("Store this token on the admin machine only. It is shown once:")
    print(token)
    return 0


def cmd_enroll(args) -> int:
    parsed = fleet_update.parse_trusted_keys(args.public_key)
    if len(parsed) != 1:
        return _fail("Public key must be one valid fk...:... entry.")
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.is_file():
        return _fail(f"Config file does not exist: {config_path}")

    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf-8")
    if not config.has_section("fleet"):
        config.add_section("fleet")
    existing_group = config.get("fleet", "group", fallback="").strip()
    if existing_group and existing_group != args.group and not args.force:
        return _fail(
            f"This node is already enrolled in group {existing_group!r}. "
            "Use --force to change it.")

    existing = fleet_update.parse_trusted_keys(
        config.get("fleet", "trusted_keys", fallback=""))
    existing.update(parsed)
    entries = [fleet_update.public_key_entry(raw) for raw in existing.values()]
    config.set("fleet", "group", args.group)
    config.set("fleet", "trusted_keys", ",".join(entries))
    config.set("fleet", "updates", args.updates)
    if args.api_token_hash:
        digest = args.api_token_hash.strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            return _fail("--api-token-hash must be a 64-character SHA-256 hex digest.")
        config.set("fleet", "api_token_hash", digest)

    backup = config_path.with_suffix(config_path.suffix + ".fleet-backup")
    if not backup.exists():
        shutil.copy2(config_path, backup)
    temp = config_path.with_suffix(config_path.suffix + ".tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            config.write(handle)
        os.replace(temp, config_path)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass

    key_id = next(iter(parsed))
    print(f"Enrolled {config_path} in fleet {args.group} with key {key_id}.")
    print(f"Original configuration backup: {backup}")
    print("Restart mesh-bbs and bacon-web-admin for all settings to take effect.")
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

    deploy = sub.add_parser("deploy", help="sign and submit a commit to one seed node")
    deploy.add_argument("ref", nargs="?", default="HEAD")
    deploy.add_argument("--version", default="",
                        help="version string to advertise (default: derived)")
    deploy.add_argument("--allow-unpushed", action="store_true")
    deploy.add_argument("--seed", default=os.getenv("BBS_FLEET_SEED", ""),
                        help="seed web admin URL (or BBS_FLEET_SEED)")
    deploy.add_argument("--token", default=os.getenv("BBS_FLEET_API_TOKEN", ""),
                        help="seed API token (or BBS_FLEET_API_TOKEN)")
    deploy.add_argument("--timeout", type=int, default=30)
    deploy.set_defaults(func=cmd_deploy)

    status = sub.add_parser("status", help="show seed and peer convergence")
    status.add_argument("--seed", default=os.getenv("BBS_FLEET_SEED", ""),
                        help="seed web admin URL (or BBS_FLEET_SEED)")
    status.add_argument("--token", default=os.getenv("BBS_FLEET_API_TOKEN", ""),
                        help="seed API token (or BBS_FLEET_API_TOKEN)")
    status.add_argument("--timeout", type=int, default=30)
    status.set_defaults(func=cmd_status)

    rollback = sub.add_parser(
        "rollback", help="sign and deploy an older commit as a new target")
    rollback.add_argument(
        "ref", nargs="?", default="",
        help="known-good ref (default: previous distinct signed target)")
    rollback.add_argument("--version", default="",
                          help="version string to advertise (default: derived)")
    rollback.add_argument("--allow-unpushed", action="store_true")
    rollback.add_argument("--seed", default=os.getenv("BBS_FLEET_SEED", ""),
                          help="seed web admin URL (or BBS_FLEET_SEED)")
    rollback.add_argument("--token", default=os.getenv("BBS_FLEET_API_TOKEN", ""),
                          help="seed API token (or BBS_FLEET_API_TOKEN)")
    rollback.add_argument("--timeout", type=int, default=30)
    rollback.add_argument("--yes", action="store_true",
                          help="skip the rollback confirmation prompt")
    rollback.set_defaults(func=cmd_rollback)

    doctor = sub.add_parser("doctor", help="check fleet readiness before deploy")
    doctor.add_argument("ref", nargs="?", default="HEAD")
    doctor.add_argument("--seed", default=os.getenv("BBS_FLEET_SEED", ""),
                        help="seed web admin URL (or BBS_FLEET_SEED)")
    doctor.add_argument("--token", default=os.getenv("BBS_FLEET_API_TOKEN", ""),
                        help="seed API token (or BBS_FLEET_API_TOKEN)")
    doctor.add_argument("--timeout", type=int, default=30)
    doctor.set_defaults(func=cmd_doctor)

    token = sub.add_parser("token", help="create a seed API token and config hash")
    token.set_defaults(func=cmd_token)

    enroll = sub.add_parser(
        "enroll", help="enroll one node config with a fleet public key")
    enroll.add_argument("public_key", help="fk...:... public key entry")
    enroll.add_argument("--config", default="config.ini",
                        help="node config path (default: config.ini)")
    enroll.add_argument("--updates", choices=("auto", "notify", "off"),
                        default="auto")
    enroll.add_argument("--api-token-hash", default="",
                        help="optional seed API token SHA-256 hash")
    enroll.add_argument("--force", action="store_true",
                        help="allow changing an existing fleet group")
    enroll.set_defaults(func=cmd_enroll)

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
