"""The Fleet page and its paste box.

The box is the one place a human hands the node an instruction, and the only
thing in front of it is the web admin password -- which is plaintext in
config.ini and defaults to "change-me". So the paste is NOT trusted because
it arrived through an authenticated session: it is verified exactly as if it
had come off the air. This endpoint can relay authority, never mint it.
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

import db_operations
import fleet_update
import web_admin

GROUP = "baconbbsvt"
COMMIT = "1234abcd" * 5


class _Node:
    """A web admin whose node trusts one key, with a scratch database."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="fleetweb-")
        self.config = os.path.join(self.dir, "config.ini")
        self.trigger = os.path.join(self.dir, "apply_update.trigger")
        self.private, self.public, self.key_id = fleet_update.generate_keypair()
        self.entry = fleet_update.public_key_entry(self.public)
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(
                "[admin]\nusername = admin\npassword = pw\n\n"
                f"[fleet]\ngroup = {GROUP}\ntrusted_keys = {self.entry}\n"
                "updates = auto\n")
        self.env = mock.patch.dict(os.environ, {
            "BBS_FLEET_APPLY_TRIGGER_PATH": self.trigger,
            "BBS_CONFIG_PATH": self.config,
            "BBS_WEBGUI_SECRET": "test-secret",
        })
        self.env.start()
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        # create_app reads BBS_CONFIG_PATH from the environment, which the
        # patch above has already pointed at this scratch config.
        self.app = web_admin.create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self._login()
        return self

    def __exit__(self, *exc):
        self.env.stop()
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        return False

    def _login(self):
        page = self.client.get("/login")
        token = self._token(page.get_data(as_text=True))
        self.client.post("/login", data={
            "username": "admin", "password": "pw", "csrf_token": token})

    @staticmethod
    def _token(html):
        marker = 'name="csrf_token" value="'
        start = html.index(marker) + len(marker)
        return html[start:html.index('"', start)]

    def instruction(self, commit=COMMIT, group=GROUP, private=None):
        payload = fleet_update.build_payload(
            group, commit, "0.1.999", self.key_id)
        return fleet_update.encode_instruction(
            payload, fleet_update.sign_payload(payload, private or self.private))

    def paste(self, blob):
        page = self.client.get("/fleet")
        token = self._token(page.get_data(as_text=True))
        return self.client.post("/fleet/apply", data={
            "instruction": blob, "csrf_token": token}, follow_redirects=True)


class PageTests(unittest.TestCase):
    def test_the_page_renders(self):
        with _Node() as node:
            page = node.client.get("/fleet")
            self.assertEqual(page.status_code, 200)
            body = page.get_data(as_text=True)
            self.assertIn("Fleet", body)
            self.assertIn(node.key_id, body)

    def test_it_requires_a_login(self):
        with _Node() as node:
            node.client.get("/logout")
            page = node.client.get("/fleet", follow_redirects=False)
            self.assertIn(page.status_code, (301, 302))


class PasteBoxTests(unittest.TestCase):
    def test_a_genuine_instruction_is_stored(self):
        with _Node() as node:
            node.paste(node.instruction())
            target = db_operations.get_fleet_target(GROUP)
            self.assertIsNotNone(target)
            self.assertEqual(target["commit"], COMMIT)

    def test_storing_one_asks_the_server_to_apply_it(self):
        with _Node() as node:
            node.paste(node.instruction())
            self.assertTrue(os.path.exists(node.trigger))

    def test_a_forged_instruction_is_refused(self):
        """An authenticated session does not make a paste trustworthy."""
        with _Node() as node:
            attacker, _, _ = fleet_update.generate_keypair()
            page = node.paste(node.instruction(private=attacker))
            self.assertIsNone(db_operations.get_fleet_target(GROUP))
            self.assertIn("rejected", page.get_data(as_text=True).lower())

    def test_a_tampered_commit_is_refused(self):
        with _Node() as node:
            payload, signature = fleet_update.decode_instruction(node.instruction())
            payload["c"] = "f" * 40
            node.paste(fleet_update.encode_instruction(payload, signature))
            self.assertIsNone(db_operations.get_fleet_target(GROUP))

    def test_an_instruction_for_another_group_is_refused(self):
        with _Node() as node:
            node.paste(node.instruction(group="not-my-fleet"))
            self.assertIsNone(db_operations.get_fleet_target(GROUP))

    def test_an_empty_paste_says_so_rather_than_failing(self):
        with _Node() as node:
            page = node.paste("   ")
            self.assertIn("paste a signed instruction",
                          page.get_data(as_text=True).lower())

    def test_garbage_does_not_raise(self):
        with _Node() as node:
            page = node.paste("this is not a blob")
            self.assertEqual(page.status_code, 200)
            self.assertIsNone(db_operations.get_fleet_target(GROUP))

    def test_a_replay_is_refused(self):
        with _Node() as node:
            blob = node.instruction()
            node.paste(blob)
            first = db_operations.get_fleet_target(GROUP)["issued_at"]
            node.paste(blob)
            self.assertEqual(
                db_operations.get_fleet_target(GROUP)["issued_at"], first)

    def test_the_post_is_csrf_protected(self):
        """It is a remote-code-execution endpoint reachable from a browser."""
        with _Node() as node:
            response = node.client.post(
                "/fleet/apply", data={"instruction": node.instruction()})
            self.assertEqual(response.status_code, 403)
            self.assertIsNone(db_operations.get_fleet_target(GROUP))


class LocalOptOutTests(unittest.TestCase):
    def test_updates_off_refuses_even_a_genuine_paste(self):
        """The local override has to hold against the GUI too, or a node
        being debugged can be yanked by whoever is logged in."""
        with _Node() as node:
            with open(node.config, "w", encoding="utf-8") as handle:
                handle.write(
                    "[admin]\nusername = admin\npassword = pw\n\n"
                    f"[fleet]\ngroup = {GROUP}\ntrusted_keys = {node.entry}\n"
                    "updates = off\n")
            page = node.paste(node.instruction())
            self.assertIsNone(db_operations.get_fleet_target(GROUP))
            self.assertIn("off", page.get_data(as_text=True).lower())


if __name__ == "__main__":
    unittest.main()
