"""Container health: is the BBS actually doing its job?

Two things have to be true, and checking only the first is how a container
reports itself healthy while doing half its work:

  * the web admin answers, so the node can be managed at all
  * server.py is running, so mail, bulletins and sync are actually moving

The entrypoint deliberately keeps the web admin alive when server.py crashes,
because it is the only way to fix whatever caused the crash. This is what
makes that state visible instead of silent.
"""
import os
import sys
import urllib.error
import urllib.request

PORT = os.getenv("BBS_WEBGUI_PORT", "8081")


def web_admin_answers() -> bool:
    # /login is the one route that answers without a session; anything
    # authenticated would report unhealthy forever.
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/login", timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def bbs_is_running() -> bool:
    """True when a server.py process exists.

    Read from /proc rather than shelling out, so the image needs no extra
    packages for this.
    """
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                argv = handle.read().split(b"\0")
        except OSError:
            continue  # the process exited while we were looking at it
        if any(arg.endswith(b"server.py") for arg in argv):
            return True
    return False


def main() -> int:
    problems = []
    if not web_admin_answers():
        problems.append(f"web admin is not answering on port {PORT}")
    if not bbs_is_running():
        problems.append("server.py is not running (check the log for why it "
                        "exited; the web admin is up so it can be fixed)")

    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
