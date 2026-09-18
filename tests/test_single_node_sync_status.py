"""A node with no peers says so, instead of reading as broken.

Reported on the open list as "the single-node status icon behavior": with
no sync peers configured the nav pill sat at "no peer reports" and the
diagnostics page said "Unknown", which reads like a fault. There is simply
nothing to compare against.
"""
import configparser
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db_operations
from web_admin import create_app


class _Case(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp_dir.name)
        self.config_path = root / "config.ini"
        self.env = mock.patch.dict(os.environ, {
            "BBS_CONFIG_PATH": str(self.config_path),
            "BBS_DB_PATH": str(root / "bulletins.db"),
            "BBS_RUNTIME_DIAG_PATH": str(root / "runtime_diagnostics.json"),
            "BBS_WEBGUI_SECRET": "test-secret",
        }, clear=False)
        self.env.start()
        self.addCleanup(self._close)

    def _close(self):
        self.env.stop()
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    def write_config(self, peers):
        config = configparser.ConfigParser()
        config["admin"] = {"username": "admin", "password": "oldpass"}
        config["sync"] = {"bbs_nodes": peers, "sync_interval_minutes": "5"}
        with open(self.config_path, "w", encoding="utf-8") as handle:
            config.write(handle)
        db_operations.initialize_database()
        app = create_app()
        client = app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
        return client

    def status(self, peers):
        return self.write_config(peers).get("/api/sync/status").get_json()


class SyncPillTests(_Case):
    def test_a_lone_node_is_named_as_one(self):
        payload = self.status("")
        self.assertEqual(payload["peer_status_text"], "single node")
        self.assertFalse(payload["peer_mismatch"])

    def test_a_node_with_peers_still_reports_on_them(self):
        payload = self.status("!peer1,!peer2")
        self.assertNotEqual(payload["peer_status_text"], "single node")


class DiagnosticsTests(_Case):
    def test_the_page_says_there_is_nothing_to_compare(self):
        page = self.write_config("").get("/settings").get_data(as_text=True)
        self.assertIn("Single node: no sync peers configured", page)
        self.assertNotIn("No peer reports yet", page)

    def test_with_peers_it_still_waits_for_their_reports(self):
        page = self.write_config("!peer1").get("/settings").get_data(as_text=True)
        self.assertIn("No peer reports yet", page)


if __name__ == "__main__":
    unittest.main()
