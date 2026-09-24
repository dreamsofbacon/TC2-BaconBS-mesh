"""Join a fleet from an invite file, without a browser.

    venv/bin/python3 scripts/join_fleet.py invite.bbsinvite

The web admin has done this for a while; a new node often has no browser
pointed at it yet, and hand-copying a broker host, TLS material, credentials,
a topic, a peer list and a key into config.ini is the step where people give
up -- and where this fleet's worst deployment went wrong.

Two things are deliberate, and match the web path exactly:

**Updates are never armed unless asked.** The bundle carries the public half
of a signing key, and whoever holds the private half can make this node run
any commit they choose. So `--arm-updates` is a separate, explicit flag.

**The name comes from the invite when it has one.** The inviter names the
guest and has already added that id to its own peers; using a different name
here leaves this node asking peers that have never heard of it.
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import invite as invite_mod  # noqa: E402


def _cert_writer(config_dir: Path):
    def write(index: int, role: str, text: str) -> Path:
        folder = config_dir / "data" / "mqtt-certs" / f"mqtt{int(index)}"
        folder.mkdir(parents=True, exist_ok=True)
        name = {"tls_ca_certs": "ca.pem", "tls_certfile": "client-cert.pem",
                "tls_keyfile": "client-key.pem"}.get(role, f"{role}.pem")
        path = folder / name
        path.write_text(text, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path
    return write


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("invite", help="the .bbsinvite file")
    parser.add_argument("--passphrase", default=os.getenv("BBS_INVITE_PASSPHRASE", ""),
                        help="the passphrase it was encrypted with "
                             "(or BBS_INVITE_PASSPHRASE; asked for if absent)")
    parser.add_argument("--name", default="",
                        help="this node's name on the link. Defaults to the "
                             "name the invite gives it.")
    parser.add_argument("--config", default=str(ROOT / "config.ini"))
    parser.add_argument("--arm-updates", action="store_true",
                        help="also trust the signing key the invite carries, "
                             "so this fleet can update this node's code")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"No config at {config_path}. Run install.sh first.")
        return 1
    try:
        blob = Path(args.invite).read_bytes()
    except OSError as exc:
        print(f"Cannot read the invite: {exc}")
        return 1

    passphrase = args.passphrase or getpass.getpass("Invite passphrase: ")
    try:
        payload = invite_mod.decrypt_payload(blob, passphrase)
    except invite_mod.InviteError as exc:
        print(f"Refused: {exc}")
        return 1

    import configparser
    config = configparser.ConfigParser()
    config.read(config_path)

    existing = [int(s[4:]) for s in config.sections()
                if s.startswith("mqtt") and s[4:].isdigit()]
    index = invite_mod.next_free_index(existing)

    assigned = str(payload.get("guest_local_id", "")).strip()
    name = args.name.strip() or assigned
    if not name:
        print("This invite does not name your node, so tell me what to call "
              "it: --name <name>")
        print("Whatever you choose, every node in the fleet has to add "
              f"mqtt:{payload.get('link', {}).get('topic_prefix', '')}:<name> "
              "to its peers, or nothing they hold can reach you.")
        return 1
    if assigned and name != assigned:
        print(f"Note: the invite names this node '{assigned}', and the node "
              f"that sent it has already added that id to its peers. Using "
              f"'{name}' means telling them the new name.")

    summary = invite_mod.summarize(payload)
    try:
        changed = invite_mod.apply_invite(
            config, payload, local_id=name, index=index,
            cert_writer=_cert_writer(config_path.parent),
            arm_updates=args.arm_updates, summary=summary)
    except invite_mod.InviteError as exc:
        print(f"Refused: {exc}")
        return 1

    backup = config_path.with_suffix(config_path.suffix + ".invite-backup")
    if not backup.exists():
        backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    with open(config_path, "w", encoding="utf-8") as handle:
        config.write(handle)

    topic = payload.get("link", {}).get("topic_prefix", "")
    print(f"Joined as mqtt:{topic}:{name}  (wrote [{changed['section']}])")
    if changed.get("certs"):
        print(f"  TLS material written: {', '.join(changed['certs'])}")
    if changed.get("armed_updates"):
        print(f"  Signed updates armed for group {summary.get('fleet_group', '')}")
    elif summary.get("fleet_group"):
        print("  The invite carries a signing key; it was NOT armed. Pass "
              "--arm-updates if this fleet should update this node's code.")
    print(f"  A copy of the old config is at {backup.name}")
    print()
    print("Restart the BBS so the new link is opened:")
    print("  sudo systemctl restart mesh-bbs")
    print("Then check it:")
    print("  venv/bin/python3 scripts/node_doctor.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
