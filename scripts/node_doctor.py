"""Check a node the way this fleet has actually gone wrong.

Run it on the node, after installing it and after enrolling it:

    venv/bin/python3 scripts/node_doctor.py

Every check here exists because something failed silently once. None of them
talk to another node or need a password: they read this node's own config,
database and service state, and say what is wrong in the words of the fix.

    FAIL  something is broken and will not fix itself
    WARN  works today, will bite later
    OK    checked, fine

Exit status is 1 if anything failed, so it can gate a deploy script.
"""
import configparser
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAIL, WARN, OK = "FAIL", "WARN", "OK"
# ASCII only: this runs over SSH, in journals and on Windows consoles,
# and a check that dies encoding its own tick mark is worse than useless.
_ICON = {FAIL: "[x]", WARN: "[!]", OK: "[ok]"}


class Report:
    def __init__(self):
        self.rows = []

    def add(self, level: str, title: str, detail: str = "") -> None:
        self.rows.append((level, title, detail))

    def failed(self) -> bool:
        return any(level == FAIL for level, _t, _d in self.rows)

    def print(self) -> None:
        for level, title, detail in self.rows:
            print(f"{_ICON[level]:<4} {level:<4} {title}")
            if detail:
                for line in str(detail).splitlines():
                    print(f"          {line}")
        failures = sum(1 for level, _t, _d in self.rows if level == FAIL)
        warnings = sum(1 for level, _t, _d in self.rows if level == WARN)
        print()
        if failures:
            print(f"{failures} problem(s) to fix, {warnings} warning(s).")
        elif warnings:
            print(f"Nothing broken, {warnings} warning(s).")
        else:
            print("This node looks healthy.")


def _config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(os.getenv("BBS_CONFIG_PATH") or str(ROOT / "config.ini"))
    return parser


def check_config(report: Report, config) -> None:
    if not config.sections():
        report.add(FAIL, "No config.ini",
                   "Copy example_config.ini to config.ini, or run install.sh.")
        return
    interface = config.get("interface", "type", fallback="").strip() or "(unset)"
    report.add(OK, f"Radio interface: {interface}")


def check_fleet(report: Report, config) -> None:
    """The settings that decide whether this node can be updated at all."""
    group = config.get("fleet", "group", fallback="").strip()
    updates = config.get("fleet", "updates", fallback="off").strip().lower()
    keys = [k for k in config.get("fleet", "trusted_keys", fallback="").split(",")
            if k.strip()]
    pinned = config.get("fleet", "pin_commit", fallback="").strip()

    if not group:
        report.add(WARN, "Not in a fleet",
                   "No [fleet] group, so signed updates are ignored.")
        return
    if not keys:
        report.add(FAIL, f"Fleet {group}: no trusted key",
                   "Every instruction is ignored. Enrol the public key:\n"
                   "  python scripts/fleet_sign.py --group " + group +
                   ' enroll "fk...:..."')
    else:
        report.add(OK, f"Fleet {group}: {len(keys)} trusted key(s)")

    if updates != "auto":
        report.add(WARN, f"Updates are '{updates}', not 'auto'",
                   "Targets are recorded and not applied.")
    if pinned:
        report.add(WARN, f"Pinned to {pinned[:12]}",
                   "This node ignores new targets until pin_commit is cleared.")


def check_update_state(report: Report) -> None:
    """What this node did with the last target it was given."""
    try:
        import fleet_update
        state = fleet_update.read_update_state() or {}
        current = fleet_update.current_commit()
    except Exception as exc:
        report.add(WARN, "Update state unreadable", str(exc))
        return
    if not state:
        report.add(OK, f"Running {current[:12] if current else 'unknown'}",
                   "No update has been applied yet on this node.")
        return
    name = str(state.get("state", "")).lower()
    target = str(state.get("target_commit", ""))[:12]
    version = state.get("target_version", "")
    if name == "healthy":
        report.add(OK, f"Running {target} ({version})")
    elif name == "held":
        report.add(FAIL, f"Target {target} ({version}) accepted but NOT applied",
                   f"{state.get('detail', '')}\n"
                   "This will never apply itself. Fix the config, then restart "
                   "mesh-bbs.")
    elif name == "pinned":
        report.add(WARN, f"Target {target} recorded; this node is pinned")
    else:
        report.add(WARN, f"Update state: {name} (target {target})")


def check_peers(report: Report, config) -> None:
    """Peering is per node and has to be configured on both sides."""
    sections = [s for s in config.sections() if s.startswith("sync")]
    peers = []
    for section in sections:
        peers.extend([p.strip() for p in
                      config.get(section, "bbs_nodes", fallback="").split(",")
                      if p.strip()])
    if not peers:
        report.add(WARN, "No sync peers configured",
                   "This node syncs with nobody.")
        return
    report.add(OK, f"{len(peers)} sync peer(s) configured")

    try:
        import db_operations
        db_operations.initialize_database()
        silent = [row for row in db_operations.peer_link_health()
                  if row.get("one_way")]
        strangers = db_operations.unlisted_sync_senders()
    except Exception as exc:
        report.add(WARN, "Peer health unavailable", str(exc))
        return

    for row in silent:
        report.add(
            FAIL, f"{row['peer_node_id']} never answers this node",
            f"{row['requests']} requests, silent for "
            f"{int(row.get('silent_for', 0) // 60)} minutes. This node is "
            "almost certainly missing from that peer's [sync*] bbs_nodes "
            "list. Peering has to be configured on BOTH sides.")
    for row in strangers:
        report.add(
            WARN, f"{row['node_id']} is being ignored by this node",
            f"{row['frames']} sync requests from a node that is not in any "
            "peer list. Add it under Sync > Add a peer if it belongs here.")
    if not silent and not strangers:
        report.add(OK, "Peering looks two-way")


def check_companion_restarts(report: Report) -> None:
    """A fleet update restarts the web admin and SSH service with sudo -n."""
    if os.name != "posix" or not shutil.which("systemctl"):
        return
    if os.geteuid() == 0:
        report.add(OK, "Running as root: companion restarts need no rule")
        return
    if not shutil.which("sudo"):
        report.add(WARN, "No sudo", "Fleet updates cannot restart the web "
                                    "admin or SSH service.")
        return
    for unit in ("bacon-web-admin.service", "bacon-ssh.service"):
        installed = subprocess.run(["systemctl", "list-unit-files", unit],
                                   capture_output=True, text=True, check=False)
        if unit not in (installed.stdout or ""):
            continue
        probe = subprocess.run(
            ["sudo", "-n", "systemctl", "show", unit, "-p", "Id"],
            capture_output=True, text=True, check=False)
        if probe.returncode == 0:
            report.add(OK, f"Fleet updates may restart {unit}")
        else:
            report.add(
                FAIL, f"Fleet updates cannot restart {unit}",
                "It will keep running the OLD code after every update. Fix:\n"
                "  bash install_services.sh --yes --user $(id -un) --dir "
                + str(ROOT))


def check_services(report: Report) -> None:
    if os.name != "posix" or not shutil.which("systemctl"):
        return
    for unit in ("mesh-bbs.service", "bacon-web-admin.service", "bacon-ssh.service"):
        installed = subprocess.run(["systemctl", "list-unit-files", unit],
                                   capture_output=True, text=True, check=False)
        if unit not in (installed.stdout or ""):
            continue
        active = subprocess.run(["systemctl", "is-active", unit],
                                capture_output=True, text=True, check=False)
        state = (active.stdout or "").strip()
        if state == "active":
            report.add(OK, f"{unit} is running")
        elif unit == "bacon-ssh.service":
            report.add(OK, f"{unit} is {state}")
        else:
            report.add(FAIL, f"{unit} is {state}",
                       f"sudo journalctl -u {unit} -n 50")


def check_content(report: Report) -> None:
    """A node holding nothing while its peers hold plenty is the shape of a
    link that is configured on one side only."""
    try:
        import db_operations
        db_operations.initialize_database()
        conn = db_operations.get_db_connection()
        local = conn.execute("SELECT COUNT(*) FROM bulletins").fetchone()[0]
        peers = conn.execute(
            "SELECT peer_node_id, bulletins FROM peer_sync_state").fetchall()
    except Exception:
        return
    best = max([int(row[1] or 0) for row in peers], default=0)
    if peers and best and int(local or 0) == 0:
        report.add(FAIL, f"This node holds no bulletins; a peer reports {best}",
                   "Nothing has ever been received. Check the peer warnings "
                   "above.")
    else:
        report.add(OK, f"{int(local or 0)} bulletin(s) stored")


def main() -> int:
    report = Report()
    config = _config()
    print(f"Bacon BBS node check -- {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"{ROOT}")
    print()
    check_config(report, config)
    check_services(report)
    check_fleet(report, config)
    check_update_state(report)
    check_companion_restarts(report)
    check_peers(report, config)
    check_content(report)
    report.print()
    return 1 if report.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
