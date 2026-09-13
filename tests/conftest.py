"""Every test run sees the same world: an empty one.

CI runs in a fresh clone with no config.ini, no bulletins.db, no runtime
snapshot and no trigger files. A developer's checkout has all of them. Tests
that quietly read those files measured a different system in each place, so
they passed locally and failed in CI -- fifteen commits in a row, deployed
anyway, because the local run said green.

It was also worse than divergence. Test output was printing "Database schema
initialized at ...\\TC2-BaconBS-mesh\\bulletins.db": tests were writing to the
developer's real database.

So before any test module is imported -- which matters, because
command_handlers and zork_port read config.ini at import time -- this points
every runtime path the application honours at a throwaway directory, and
removes any BBS_* setting inherited from the developer's shell. The file is in
the repository, so CI loads exactly the same sandbox. Local and CI now match
by construction rather than by luck.

A test that needs a real file sets its own env var, as ~20 already do; those
overrides still win inside their own scope.
"""
import atexit
import os
import shutil
import tempfile

# Anything a developer exported (BBS_SYNC_ZORK_SAVES, BBS_BULLETIN_BOARDS...)
# changes behaviour, and CI sets none of them.
for _name in [name for name in os.environ if name.startswith("BBS_")]:
    del os.environ[_name]

_SANDBOX = tempfile.mkdtemp(prefix="bbs-tests-")


def _remove_sandbox():
    # Windows will not delete a file SQLite still holds open, and rmtree with
    # ignore_errors says nothing about it -- the sandbox just stays behind in
    # %TEMP% after every run. Close the shared connection first.
    try:
        import db_operations
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
    except Exception:
        pass
    shutil.rmtree(_SANDBOX, ignore_errors=True)


atexit.register(_remove_sandbox)

# Every path the application resolves from an env var. Paths that do not
# exist yet are deliberate: an absent config.ini is exactly what CI has.
_SANDBOXED_PATHS = {
    "BBS_CONFIG_PATH": "config.ini",
    "BBS_DB_PATH": "bulletins.db",
    "BBS_TRIVIA_DB": "trivia.db",
    "BBS_RADIO_ADMIN_PATH": "radio_admin.db",
    "BBS_RUNTIME_DIAG_PATH": "runtime_diagnostics.json",
    "BBS_UPDATE_STATE_PATH": "update_state.json",
    "BBS_FLEET_APPLY_TRIGGER_PATH": "apply_update.trigger",
    "BBS_FORCE_CHECK_TRIGGER_PATH": "force_check.trigger",
    "BBS_LINKS_RELOAD_TRIGGER_PATH": "reload_links.trigger",
    "BBS_LINK_RECONNECT_TRIGGER_PATH": "reconnect_link.trigger",
    "BBS_MANUAL_SYNC_TRIGGER_PATH": "manual_sync.trigger",
    "BBS_PEER_RESYNC_TRIGGER_PATH": "peer_resync.trigger",
    "BBS_RECORD_RESOLVE_TRIGGER_PATH": "record_resolve.trigger",
    "BBS_ZORK_SAVE_RESOLVE_TRIGGER_PATH": "zork_save_resolve.trigger",
    # Never the operator's real fleet signing key.
    "BBS_FLEET_KEY_PATH": "fleet-key",
    "BBS_MQTT_CERT_DIR": "mqtt-certs",
}
for _var, _filename in _SANDBOXED_PATHS.items():
    os.environ[_var] = os.path.join(_SANDBOX, _filename)

TEST_SANDBOX = _SANDBOX
