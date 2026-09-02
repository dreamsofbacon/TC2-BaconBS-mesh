"""The emulator's web layer.

Two things here are load-bearing beyond "the route returns 200". The first is
that acting as a real node needs an explicit confirmation: from there, writes
are attributed to that node for real and there is no undo. The second is the
poll endpoint, which is the only way an Ask Nomad answer arriving on a worker
thread a minute later ever reaches the page.
"""
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import bbs_emulator
import db_operations
import utils
import web_admin


class _Admin:
    """A logged-in web admin against a scratch config and database."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="emuweb-")
        self.config = os.path.join(self.dir, "config.ini")
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write("[admin]\nusername = admin\npassword = pw\n\n"
                         "[allow_list]\nallowed_nodes = !allowed01\n")
        self.env = mock.patch.dict(os.environ, {
            "BBS_CONFIG_PATH": self.config,
            # Without this create_app resolves the repo's real bulletins.db.
            # The thread-local :memory: connection below means nothing is
            # actually written there, but pointing it at scratch keeps the
            # test from touching a live database at all.
            "BBS_DB_PATH": os.path.join(self.dir, "bulletins.db"),
            "BBS_WEBGUI_SECRET": "test-secret",
        })
        self.env.start()
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.app = web_admin.create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self._login()
        return self

    def __exit__(self, *exc):
        for token in list(bbs_emulator._sessions):
            bbs_emulator.end_session(token)
        self.env.stop()
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        return False

    def _login(self):
        page = self.client.get("/login")
        html = page.get_data(as_text=True)
        marker = 'name="csrf_token" value="'
        start = html.index(marker) + len(marker)
        token = html[start:html.index('"', start)]
        self.client.post("/login", data={
            "username": "admin", "password": "pw", "csrf_token": token})

    def token(self):
        return self.client.get("/api/csrf-token").get_json()["csrf_token"]

    def post(self, path, payload, csrf=True):
        headers = {"X-CSRF-Token": self.token()} if csrf else {}
        return self.client.post(path, json=payload, headers=headers)

    def start(self, **payload):
        return self.post("/api/emulator/session", payload).get_json()


class PageTests(unittest.TestCase):
    def test_the_page_renders_and_warns_about_live_writes(self):
        with _Admin() as admin:
            html = admin.client.get("/emulator").get_data(as_text=True)
            self.assertIn("Emulator", html)
            self.assertIn("live database", html)

    def test_the_roster_is_offered_as_identities(self):
        with _Admin() as admin:
            db_operations.thread_local.connection.execute(
                "INSERT INTO mesh_clients (link_name, node_id, node_num, "
                "short_name, long_name, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("primary", "!1bbecf78", 464464760, "zrk", "Zorak",
                 "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
            html = admin.client.get("/emulator").get_data(as_text=True)
            self.assertIn("!1bbecf78", html)


class AccessTests(unittest.TestCase):
    def test_the_page_is_closed_to_anonymous_visitors(self):
        with _Admin() as admin:
            admin.client.get("/logout")
            response = admin.client.get("/emulator")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login", response.headers["Location"])

    def test_sending_is_closed_to_anonymous_visitors(self):
        with _Admin() as admin:
            started = admin.start()
            admin.client.get("/logout")
            response = admin.post("/api/emulator/send",
                                  {"token": started["session"]["token"],
                                   "text": "?"})
            self.assertIn(response.status_code, (302, 401, 403))

    def test_a_post_without_a_csrf_token_is_refused(self):
        with _Admin() as admin:
            response = admin.post("/api/emulator/session", {}, csrf=False)
            self.assertEqual(response.status_code, 403)
            self.assertFalse(response.get_json()["ok"])


class SessionTests(unittest.TestCase):
    def test_a_synthetic_session_starts(self):
        with _Admin() as admin:
            data = admin.start(short_name="tester")
            self.assertTrue(data["ok"])
            self.assertTrue(data["session"]["node_id"].startswith("emu:"))
            self.assertFalse(data["session"]["acting_as_real"])

    def test_acting_as_a_real_node_is_refused_without_confirmation(self):
        """The guard that keeps a stray click from posting as someone else."""
        with _Admin() as admin:
            data = admin.start(node_id="!1bbecf78")
            self.assertFalse(data["ok"])
            self.assertIn("confirmation", data["error"])

    def test_acting_as_a_real_node_works_once_confirmed(self):
        with _Admin() as admin:
            data = admin.start(node_id="!1bbecf78", confirm_act_as=True)
            self.assertTrue(data["ok"])
            self.assertEqual(data["session"]["node_id"], "!1bbecf78")
            self.assertTrue(data["session"]["acting_as_real"])

    def test_the_packet_limit_is_honoured_and_bounded(self):
        with _Admin() as admin:
            self.assertEqual(
                admin.start(max_text_bytes=64)["session"]["max_text_bytes"], 64)
            self.assertEqual(
                admin.start(max_text_bytes=99999)["session"]["max_text_bytes"],
                1024)

    def test_an_expired_token_says_so_rather_than_failing_opaquely(self):
        with _Admin() as admin:
            response = admin.post("/api/emulator/send",
                                  {"token": "gone", "text": "?"})
            self.assertEqual(response.status_code, 410)
            self.assertIn("expired", response.get_json()["error"])

    def test_ending_a_session_clears_its_menu_state(self):
        with _Admin() as admin:
            token = admin.start()["session"]["token"]
            admin.post("/api/emulator/send", {"token": token, "text": "?"})
            sender_id = bbs_emulator.get_session(token).sender_id
            admin.post("/api/emulator/end", {"token": token})
            self.assertNotIn(sender_id, utils.user_states)


class ExchangeTests(unittest.TestCase):
    def test_sending_returns_the_real_menu_in_packets(self):
        with _Admin() as admin:
            token = admin.start()["session"]["token"]
            data = admin.post("/api/emulator/send",
                              {"token": token, "text": "?"}).get_json()
            self.assertTrue(data["ok"])
            body = "".join(chunk["text"] for chunk in data["chunks"])
            self.assertIn("[1] Quick Commands", body)

    def test_a_small_packet_limit_splits_the_reply(self):
        with _Admin() as admin:
            token = admin.start(max_text_bytes=64)["session"]["token"]
            data = admin.post("/api/emulator/send",
                              {"token": token, "text": "?"}).get_json()
            self.assertGreater(len(data["chunks"]), 1)
            for chunk in data["chunks"]:
                self.assertLessEqual(chunk["bytes"], 64)

    def test_menu_state_is_reported_back_to_the_page(self):
        with _Admin() as admin:
            token = admin.start()["session"]["token"]
            admin.post("/api/emulator/send", {"token": token, "text": "?"})
            data = admin.post("/api/emulator/send",
                              {"token": token, "text": "B"}).get_json()
            self.assertIsNotNone(data["session"]["menu"]["command"])

    def test_an_empty_message_is_refused(self):
        with _Admin() as admin:
            token = admin.start()["session"]["token"]
            response = admin.post("/api/emulator/send",
                                  {"token": token, "text": "   "})
            self.assertEqual(response.status_code, 400)

    def test_a_late_reply_is_collected_by_polling(self):
        """Stands in for the gateway worker answering after send returned."""
        with _Admin() as admin:
            token = admin.start()["session"]["token"]
            admin.post("/api/emulator/send", {"token": token, "text": "?"})
            session = bbs_emulator.get_session(token)
            utils.send_message("the slow answer", session.sender_id,
                               session.interface)
            data = admin.client.get(
                "/api/emulator/poll?token=" + token).get_json()
            body = "".join(chunk["text"] for chunk in data["chunks"])
            self.assertIn("the slow answer", body)

    def test_polling_twice_does_not_repeat_a_reply(self):
        with _Admin() as admin:
            token = admin.start()["session"]["token"]
            admin.post("/api/emulator/send", {"token": token, "text": "?"})
            admin.client.get("/api/emulator/poll?token=" + token)
            second = admin.client.get(
                "/api/emulator/poll?token=" + token).get_json()
            self.assertEqual(second["chunks"], [])

    def test_reset_clears_the_menu_but_keeps_the_identity(self):
        with _Admin() as admin:
            started = admin.start()["session"]
            token = started["token"]
            admin.post("/api/emulator/send", {"token": token, "text": "?"})
            admin.post("/api/emulator/send", {"token": token, "text": "B"})
            data = admin.post("/api/emulator/reset", {"token": token}).get_json()
            self.assertTrue(data["ok"])
            self.assertIsNone(data["session"]["menu"]["command"])
            self.assertEqual(data["session"]["node_id"], started["node_id"])


if __name__ == "__main__":
    unittest.main()
