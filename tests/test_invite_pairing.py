"""An invite pairs both nodes, or says that it has not.

An invite used to fix one side. It gave the joining node the broker, the
credentials, the topic and every peer -- and told no peer about the joiner,
because the joiner picked its own name at import time and nobody was told.
A node only acts on sync frames from a sender it lists, so the result is a
node that asks for records several times a minute and is never answered.
That is not a slow link, it is no link, and it cost a day on the VPS node.

So the inviter names the guest and adds it to its own peers while writing
the bundle, and the import offers that name rather than inventing one. An
invite with no guest name still works -- an older bundle has none -- and
then the review page says plainly that the other side does not know this
node yet.

The other end of the same fault is covered here too: a node that keeps
asking us for records while absent from our peer list is recorded, so the
side doing the ignoring can see it. Ignoring it stays correct; being silent
about it does not.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_operations
import invite

TOPIC = "baconbbsvt"
LINK = {"index": 2, "host": "mqtt.example.invalid", "port": "8884",
        "topic_prefix": TOPIC, "local_id": "bbs-main", "username": "client1",
        "password": "secret", "tls": "true"}


class GuestNamingTests(unittest.TestCase):
    def test_the_bundle_carries_the_name(self):
        payload = invite.build_payload(LINK, inviter_local_id="bbs-main",
                                       guest_local_id="VPS-Public")
        self.assertEqual("VPS-Public", payload["guest_local_id"])
        self.assertEqual(f"mqtt:{TOPIC}:VPS-Public", invite.guest_node_id(payload))

    def test_an_unnamed_guest_yields_no_id(self):
        """Older bundles, and anyone who leaves the field blank."""
        payload = invite.build_payload(LINK, inviter_local_id="bbs-main")
        self.assertEqual("", payload.get("guest_local_id", ""))
        self.assertEqual("", invite.guest_node_id(payload))

    def test_a_bundle_without_the_field_at_all_is_handled(self):
        payload = invite.build_payload(LINK, inviter_local_id="bbs-main")
        payload.pop("guest_local_id", None)
        self.assertEqual("", invite.guest_node_id(payload))

    def test_the_guest_id_uses_the_link_topic(self):
        payload = invite.build_payload(dict(LINK, topic_prefix="other-topic"),
                                       inviter_local_id="bbs-main",
                                       guest_local_id="VPS-Public")
        self.assertEqual("mqtt:other-topic:VPS-Public", invite.guest_node_id(payload))

    def test_the_guest_still_learns_every_peer(self):
        payload = invite.build_payload(
            LINK, inviter_local_id="bbs-main", guest_local_id="VPS-Public",
            sync_nodes=[f"mqtt:{TOPIC}:VT2"])
        peers = invite.peer_ids_for_importer(payload)
        self.assertIn(f"mqtt:{TOPIC}:bbs-main", peers)
        self.assertIn(f"mqtt:{TOPIC}:VT2", peers)


class ExportAddsTheGuestTests(unittest.TestCase):
    """Naming the guest has to change this node's own config, or it is just
    a label."""

    def setUp(self):
        folder = tempfile.mkdtemp()
        self.config_path = os.path.join(folder, "config.ini")
        self.db_path = os.path.join(folder, "bulletins.db")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(
                "[bbs]\nname = Test\n"
                "[mqtt2]\nenabled = true\nhost = mqtt.example.invalid\n"
                f"port = 8884\ntopic_prefix = {TOPIC}\nlocal_id = bbs-main\n"
                f"[sync_mqtt2]\nbbs_nodes = mqtt:{TOPIC}:VT2\n")
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

    def _export(self, guest):
        with mock.patch.dict(os.environ, {"BBS_CONFIG_PATH": self.config_path}):
            app = self.web_admin.create_app()
            app.config["CONFIG_PATH"] = self.config_path
            app.config["DB_PATH"] = self.db_path
            client = app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True
            token = client.get("/api/csrf-token").get_json()["csrf_token"]
            return client.post("/invite/export", data={
                "csrf_token": token, "link_index": "2",
                "passphrase": "a-long-enough-passphrase",
                "guest_local_id": guest}, follow_redirects=True)

    def _peers(self):
        import configparser
        config = configparser.ConfigParser()
        config.read(self.config_path)
        return [p.strip() for p in
                config.get("sync_mqtt2", "bbs_nodes", fallback="").split(",")
                if p.strip()]

    def test_the_named_guest_is_added_to_our_peers(self):
        self._export("VPS-Public")
        self.assertIn(f"mqtt:{TOPIC}:VPS-Public", self._peers())

    def test_the_existing_peers_are_left_alone(self):
        self._export("VPS-Public")
        self.assertIn(f"mqtt:{TOPIC}:VT2", self._peers())

    def test_no_name_changes_nothing(self):
        before = self._peers()
        self._export("")
        self.assertEqual(before, self._peers())

    def test_our_own_name_is_refused(self):
        """Two nodes sharing a name read each other's traffic as their own."""
        self._export("bbs-main")
        self.assertNotIn(f"mqtt:{TOPIC}:bbs-main", self._peers())
        self.assertEqual([f"mqtt:{TOPIC}:VT2"], self._peers())

    def test_a_junk_name_is_refused_before_anything_is_written(self):
        self._export("not a valid name!")
        self.assertEqual([f"mqtt:{TOPIC}:VT2"], self._peers())


class IgnoredPeersAreRecordedTests(unittest.TestCase):
    """The view from the side doing the ignoring."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_one_stray_frame_is_not_reported(self):
        db_operations.record_unlisted_sync_sender("mqtt:baconbbsvt:bbs", "HASHREQ")
        self.assertEqual([], db_operations.unlisted_sync_senders())

    def test_persistent_asking_is(self):
        for _ in range(25):
            db_operations.record_unlisted_sync_sender("mqtt:baconbbsvt:bbs", "HASHREQ")
        rows = db_operations.unlisted_sync_senders()
        self.assertEqual(1, len(rows))
        self.assertEqual("mqtt:baconbbsvt:bbs", rows[0]["node_id"])
        self.assertEqual(25, rows[0]["frames"])

    def test_adding_the_node_as_a_peer_clears_it(self):
        for _ in range(25):
            db_operations.record_unlisted_sync_sender("mqtt:baconbbsvt:bbs", "HASHREQ")
        db_operations.forget_unlisted_sync_sender("mqtt:baconbbsvt:bbs")
        self.assertEqual([], db_operations.unlisted_sync_senders())

    def test_a_blank_sender_is_ignored(self):
        self.assertEqual(0, db_operations.record_unlisted_sync_sender("", "HASHREQ"))


if __name__ == "__main__":
    unittest.main()
