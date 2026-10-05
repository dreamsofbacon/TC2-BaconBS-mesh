"""Run the web admin inside the mesh server, as one service.

The web admin used to be its own process, bacon-web-admin.service, beside
mesh-bbs.service. Two services meant two things to sandbox, two to restart
on every fleet update, and a status page reading the mesh server's state
second-hand from runtime_diagnostics.json. This runs the same Flask app on a
thread inside server.py instead.

It is opt-in, by BBS_WEBGUI_EMBEDDED=1, which only the mesh-bbs.service that
install_services.sh now writes sets. A fleet update changes the code but not
the installed unit files, so a node that has not re-run the installer keeps
its separate web admin and carries on exactly as before. Embedding by default
would have moved its admin page off the address its operator reaches it on:
the old mesh-bbs unit has none of the web admin's host or env settings.

The web admin starts FIRST, before the mesh server reads its config. It is
the only means of repairing a node, so a BBS that cannot start must not take
it down with it; see server.main and docker/entrypoint.sh, which keeps the
two separate processes for the same reason.
"""
import logging
import os
import threading

ENABLE_ENV = "BBS_WEBGUI_EMBEDDED"
# The unit the embedded web admin lives in: restarting it restarts the page.
SERVICE_UNIT = "mesh-bbs.service"

_started = False


def enabled() -> bool:
    return os.getenv(ENABLE_ENV, "").strip().casefold() in {"1", "true", "yes", "on"}


def running() -> bool:
    return _started


def start(runtime_interface=None) -> bool:
    """Serve the web admin on a daemon thread. True if it is now serving.

    Never raises: a web admin that cannot start (port 8081 still held by an
    old bacon-web-admin.service, say) is logged, and the BBS runs without it.
    """
    global _started
    if _started or not enabled():
        return _started
    host = os.getenv("BBS_WEBGUI_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("BBS_WEBGUI_PORT", "8081"))
    except ValueError:
        port = 8081
    try:
        import web_admin
        from werkzeug.serving import make_server
        web_admin.EMBEDDED_SERVICE_UNIT = SERVICE_UNIT
        app = web_admin.create_app(runtime_interface)
        # threaded=True for the reason web_admin.py's own __main__ gives: one
        # slow request must not take the whole page down for everyone.
        server = make_server(host, port, app, threaded=True)
    # SystemExit too: werkzeug answers a port already in use with
    # sys.exit(1), which would have taken the whole mesh server down.
    except (Exception, SystemExit):
        logging.exception(
            "Embedded web admin could not start on %s:%s; the BBS continues "
            "without it. If bacon-web-admin.service is still installed, "
            "re-run install_services.sh to retire it.", host, port)
        return False
    thread = threading.Thread(target=server.serve_forever, name="web-admin",
                              daemon=True)
    thread.start()
    _started = True
    logging.info("Web admin running inside the mesh server on %s:%s", host, port)
    return True
