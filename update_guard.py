"""Roll a node back when a fleet update leaves it unable to run.

Imported as the FIRST statement of server.py and web_admin.py, before any
project module. That placement is the whole design: when the new code cannot
import, the thing that has to notice is the thing that already ran.

For the same reason this module imports nothing from the project and nothing
outside the standard library, and does as little as possible. Every line here
is a line that could itself fail on the version it is meant to rescue.

The layer in front of this -- the pre-switch smoke test in fleet_update.py --
catches code that cannot compile or import, and it catches it before the
running node is touched. This one exists for what a smoke test cannot see: a
version that loads perfectly and then dies on real hardware, a real database,
or a real radio.

A node is never left with nothing to run: three failed starts and it goes
back to the commit it came from, which by definition worked.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

MAX_ATTEMPTS = 3
_STATE_FILE = "update_state.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _state_path() -> Path:
    return Path(os.getenv("BBS_UPDATE_STATE_PATH") or (_repo_root() / _STATE_FILE))


def _read() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except Exception:
        # Unreadable state must not stop the BBS from booting. The worst case
        # is that a rollback does not happen; refusing to start would be a
        # self-inflicted outage.
        return {}


def _write(state: dict) -> None:
    try:
        path = _state_path()
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def _revert(commit: str) -> bool:
    root = _repo_root()
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={root}",
             "checkout", "--quiet", "--force", "--detach", commit],
            cwd=str(root), capture_output=True, text=True, timeout=120, check=False)
        return result.returncode == 0
    except Exception:
        return False


def check(process_name: str = "bbs") -> None:
    """Called at import. Counts starts during probation and reverts if needed.

    Deliberately silent and side-effect-free when no update is in flight,
    which is almost always.
    """
    state = _read()
    if state.get("state") != "probation":
        return

    target = str(state.get("target_commit") or "")
    previous = str(state.get("previous_commit") or "")
    attempts = int(state.get("attempts") or 0) + 1
    state["attempts"] = attempts
    _write(state)

    if attempts <= MAX_ATTEMPTS:
        print(f"[update_guard] {process_name}: start {attempts} of "
              f"{MAX_ATTEMPTS} on {target[:12]} (on probation)", file=sys.stderr)
        return

    if not previous:
        # Nothing recorded to go back to. Give up on the guard rather than
        # loop forever; leaving the state file would re-trigger every start.
        print("[update_guard] probation failed but no previous commit was "
              "recorded; clearing update state", file=sys.stderr)
        try:
            os.remove(_state_path())
        except Exception:
            pass
        return

    print(f"[update_guard] {target[:12]} failed to start {attempts} times; "
          f"reverting to {previous[:12]}", file=sys.stderr)
    if _revert(previous):
        _write({
            "state": "rolled_back",
            "failed_commit": target,
            "restored_commit": previous,
            "attempts": attempts,
        })
        print("[update_guard] reverted; exiting so systemd restarts on the "
              "previous version", file=sys.stderr)
        # Exit non-zero so the restart is visible as a failure in the journal
        # rather than looking like a clean shutdown.
        os._exit(1)
    else:
        print("[update_guard] revert FAILED; continuing on the new version. "
              "Manual recovery needed: git checkout " + previous, file=sys.stderr)
        _write({**state, "state": "rollback_failed"})


def confirm_healthy() -> bool:
    """Called once a node is demonstrably working. Ends probation.

    Until this runs, every restart counts against the attempt budget -- so it
    must only be called when the node is genuinely serving, not merely when
    main() has been entered.
    """
    state = _read()
    if state.get("state") != "probation":
        return False
    _write({
        "state": "confirmed",
        "commit": state.get("target_commit", ""),
        "version": state.get("target_version", ""),
        "attempts": state.get("attempts", 0),
    })
    return True
