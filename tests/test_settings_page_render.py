"""The settings and edit pages actually render.

A panel was added with tests for its loader and its save, and none that
asked the page for itself. The markup used `loop.parent`, which Jinja does
not have, so the whole Settings page -- every panel on it, not just the new
one -- returned 500 as soon as a node had both a channel and a peer. Loading
data and saving a form are not evidence that the page renders.

Each case here is a shape that changes which branches of the template run:
nothing configured, boards but no peers, and the full case that broke.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_operations

PEER_A = "!aaaa1111"
PEER_B = "!bbbb2222"


class _Page(unittest.TestCase):
    peers_line = "[sync]\nbbs_nodes = " + PEER_A + ", " + PEER_B + "\n"

    def setUp(self):
        folder = tempfile.mkdtemp()
        self.config_path = os.path.join(folder, "config.ini")
        self.db_path = os.path.join(folder, "bulletins.db")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("[bbs]\nname = Test\n"
                         "[boards]\nbulletin_boards = General, Ops\n"
                         + self.peers_line)
        db_operations.thread_local.connection = sqlite3.connect(self.db_path)
        db_operations.initialize_database()
        import web_admin
        self.web_admin = web_admin
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _get(self, path):
        with mock.patch.dict(os.environ, {"BBS_CONFIG_PATH": self.config_path}):
            app = self.web_admin.create_app()
            app.config["CONFIG_PATH"] = self.config_path
            app.config["DB_PATH"] = self.db_path
            app.config["BULLETIN_BOARDS"] = ["General", "Ops"]
            client = app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True
            return client.get(path)

    def add_channel(self, name="News", url="https://example.invalid/feed"):
        with mock.patch.object(db_operations, "send_channel_to_bbs_nodes"):
            db_operations.add_channel(name, url, [], None)


class SettingsPageTests(_Page):
    def test_it_renders_on_a_fresh_node(self):
        response = self._get("/settings")
        self.assertEqual(200, response.status_code)
        self.assertIn("Board Sync", response.get_data(as_text=True))

    def test_it_renders_with_a_channel_and_peers(self):
        """The exact shape that returned 500: two nested loops."""
        self.add_channel()
        db_operations.set_channel_audience(
            "News", "https://example.invalid/feed", "peers", [PEER_A])
        db_operations.set_board_audience("Ops", "peers", [PEER_B])
        response = self._get("/settings")
        self.assertEqual(200, response.status_code)
        body = response.get_data(as_text=True)
        self.assertIn("example.invalid", body)
        self.assertIn('name="channel_peers_0"', body)
        self.assertIn('name="channel_audience_0"', body)

    def test_each_channel_gets_its_own_field_names(self):
        """Two channels sharing one index would save the first over the
        second -- silently, since the form still posts."""
        self.add_channel()
        self.add_channel(name="Weather", url="https://example.invalid/wx")
        body = self._get("/settings").get_data(as_text=True)
        for index in (0, 1):
            with self.subTest(index=index):
                self.assertIn(f'name="channel_audience_{index}"', body)
                self.assertIn(f'name="channel_peers_{index}"', body)

    def test_it_renders_with_a_channel_and_no_peers_configured(self):
        self.peers_line = ""
        self.setUp()
        self.add_channel()
        self.assertEqual(200, self._get("/settings").status_code)


class MaintenancePanelRendersTests(_Page):
    def test_the_three_actions_are_on_the_page(self):
        body = self._get("/settings").get_data(as_text=True)
        self.assertIn("Maintenance", body)
        for section in ("apply_update", "restart_services", "reboot_node"):
            with self.subTest(section=section):
                self.assertIn(f'value="{section}"', body)

    def test_reboot_asks_for_the_typed_word(self):
        body = self._get("/settings").get_data(as_text=True)
        self.assertIn('name="confirm"', body)


class OneWayPeerWarningTests(_Page):
    """The operator has to be able to see it without reading a journal."""

    def test_a_silent_peer_is_named_on_the_page(self):
        from datetime import datetime, timedelta, timezone
        for _ in range(3):
            db_operations.record_peer_request(PEER_A)
        # Back-date the silence so it is a misconfiguration, not a burst.
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn = db_operations.get_db_connection()
        conn.execute("UPDATE peer_link_health SET first_request_at = ?", (old,))
        conn.commit()
        body = self._get("/settings").get_data(as_text=True)
        self.assertIn("One-way peering", body)
        self.assertIn(PEER_A, body)
        self.assertIn("bbs_nodes", body)

    def test_a_healthy_node_shows_no_warning(self):
        body = self._get("/settings").get_data(as_text=True)
        self.assertNotIn("One-way peering", body)


class BulletinEditPageTests(_Page):
    def _post_bulletin(self):
        with mock.patch.object(db_operations, "send_bulletin_to_bbs_nodes"):
            unique_id = db_operations.add_bulletin(
                "General", "caller", "a subject", "some text", [], None)
        row = db_operations.get_db_connection().execute(
            "SELECT id FROM bulletins WHERE unique_id = ?", (unique_id,)).fetchone()
        return int(row[0])

    def test_the_edit_form_renders(self):
        row_id = self._post_bulletin()
        response = self._get(f"/bulletins/{row_id}/edit")
        self.assertEqual(200, response.status_code)
        body = response.get_data(as_text=True)
        self.assertIn("Who this post reaches", body)
        self.assertIn('name="audience"', body)

    def test_the_stored_audience_is_preselected(self):
        row_id = self._post_bulletin()
        db_operations.get_db_connection().execute(
            "UPDATE bulletins SET sync_peers = ? WHERE id = ?", (PEER_A, row_id))
        db_operations.get_db_connection().commit()
        body = self._get(f"/bulletins/{row_id}/edit").get_data(as_text=True)
        self.assertIn('value="peers" selected', body)


if __name__ == "__main__":
    unittest.main()
