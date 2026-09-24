"""Exit when the code on disk changes, so systemd starts us on it.

A fleet update switches the working tree and then restarts the two services
that do not exit by themselves -- the web admin and the SSH front end --
with `sudo -n systemctl restart`. That needs `/etc/sudoers.d/baconbbs-fleet`,
which `install_services.sh` writes. A node installed before that file
existed, or one where the rule never landed, keeps running the OLD code in
both services after every update: the SSH front end serves an old BBS, and
the web admin reports the version it started with, for ever, because
DISPLAY_VERSION is read once at startup. That has now bitten twice.

So neither service waits to be restarted any more. Each watches the commit
the working tree is on and exits when it changes, and systemd starts it
again on the new code -- exactly what mesh-bbs already does for itself. No
privilege is involved, so there is nothing left to be missing.

The sudo restart stays: where it works, the refresh is immediate instead of
within one poll.
"""
import logging
import os
import threading
import time
from pathlib import Path

# Non-zero on purpose. bacon-web-admin is Restart=always, but bacon-ssh
# shipped as Restart=on-failure for a long time and a clean exit there would
# simply stop the service -- which is the failure this exists to prevent, in
# a worse form. Exit non-zero and both policies bring it back.
EXIT_CODE = 3

_DEFAULT_INTERVAL = 30.0


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def current_code_id() -> str:
    """What the working tree is checked out at, read straight from git.

    A file read, not a `git` subprocess: this runs every poll in two
    long-lived services, and `.git/HEAD` is the file git itself writes when
    a checkout lands. A detached HEAD (what a fleet update produces) holds
    the commit directly; on a branch it names the ref, which is then read.
    """
    git_dir = repo_root() / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not head.startswith("ref:"):
        return head
    ref = head.split(":", 1)[1].strip()
    try:
        return (git_dir / ref).read_text(encoding="utf-8").strip()
    except OSError:
        # A packed ref, or a ref file that has not been written yet. The
        # packed-refs mtime is enough to notice a change without parsing it.
        try:
            packed = git_dir / "packed-refs"
            return f"{ref}@{packed.stat().st_mtime_ns}"
        except OSError:
            return ref


def watch(service_label: str, interval: float = _DEFAULT_INTERVAL,
          exit_hook=None, code_id=current_code_id) -> threading.Thread:
    """Start watching in the background. Returns the thread, for tests."""
    started_at = code_id()
    if not started_at:
        # Not a git checkout (a container image, a tarball install). There is
        # nothing to watch, and exiting on a reading we cannot take would be
        # a restart loop.
        logging.info("%s: no git checkout to watch for updates", service_label)
        return None

    def _loop():
        while True:
            time.sleep(interval)
            try:
                now = code_id()
            except Exception:
                logging.debug("%s: could not read the current commit",
                              service_label, exc_info=True)
                continue
            if not now or now == started_at:
                continue
            logging.warning(
                "%s: the code changed under us (%s -> %s); exiting so systemd "
                "restarts on it.", service_label, started_at[:12], now[:12])
            (exit_hook or _exit)()
            return

    thread = threading.Thread(target=_loop, name=f"{service_label}-update-watch",
                              daemon=True)
    thread.start()
    return thread


def _exit() -> None:
    # os._exit, not sys.exit: this runs on a daemon thread, where SystemExit
    # would be swallowed and the stale process would keep serving.
    logging.shutdown()
    os._exit(EXIT_CODE)
