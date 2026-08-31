"""Deleting a channel used to appear to do nothing.

The web admin's generic delete route special-cased bulletins and mail; every
other table fell through to a bare DELETE. So a channel was removed with no
tombstone and no delete frame, and the next sync pass saw a record the peer
had and this node did not -- and put it straight back. Deleting it again did
the same thing.

Tombstones now also carry a snapshot of the row, because a tombstone alone
records only THAT something was deleted: enough to stop it returning, but
nothing to bring back.
"""
import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations
import message_processing


class _Iface:
    def __init__(self):
        self.sent = []
        self.bbs_nodes = []
        self.allowed_nodes = []
        self.nodes = {}

    def sendText(self, text=None, destinationId=None, wantAck=True, wantResponse=False, **kwargs):
        self.sent.append((destinationId, text))
        return types.SimpleNamespace(id=len(self.sent))


class ChannelDeleteTests(unittest.TestCase):
    NAME = "VT Mesh"
    URL = "https://meshtastic.org/e/vt"

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.iface = _Iface()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _count(self):
        conn = db_operations.thread_local.connection
        return conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]

    def _key(self):
        return db_operations.make_channel_manifest_key(self.NAME, self.URL)

    def test_deleting_a_channel_records_a_tombstone(self):
        db_operations.add_channel(self.NAME, self.URL)
        self.assertTrue(db_operations.delete_channel(self.NAME, self.URL))
        self.assertEqual(self._count(), 0)
        self.assertTrue(db_operations.has_sync_tombstone("channels", self._key()))

    def test_a_peer_reoffering_the_channel_cannot_resurrect_it(self):
        """The reported bug, end to end."""
        db_operations.add_channel(self.NAME, self.URL)
        db_operations.delete_channel(self.NAME, self.URL)
        message_processing.process_message(
            1, "CHANNEL|" + self.NAME + "|" + self.URL, self.iface,
            is_sync_message=True, sender_node_id="!peer")
        self.assertEqual(self._count(), 0, "deleted channel came back from a peer")

    def test_deleting_notifies_peers(self):
        db_operations.add_channel(self.NAME, self.URL)
        db_operations.delete_channel(self.NAME, self.URL, ["!peer1"], self.iface)
        sent = [text for _dest, text in self.iface.sent if text.startswith("DELETE_CHANNEL|")]
        self.assertEqual(len(sent), 1, "expected one delete frame, got %r" % (self.iface.sent,))
        self.assertIn(self._key(), sent[0])

    def test_an_inbound_delete_frame_removes_the_channel(self):
        db_operations.add_channel(self.NAME, self.URL)
        message_processing.process_message(
            1, "DELETE_CHANNEL|" + self._key(), self.iface,
            is_sync_message=True, sender_node_id="!peer")
        self.assertEqual(self._count(), 0)
        self.assertTrue(db_operations.has_sync_tombstone("channels", self._key()))

    def test_applying_a_peers_delete_does_not_rebroadcast_it(self):
        """Two nodes echoing the same delete would loop forever."""
        db_operations.add_channel(self.NAME, self.URL)
        message_processing.process_message(
            1, "DELETE_CHANNEL|" + self._key(), self.iface,
            is_sync_message=True, sender_node_id="!peer")
        echoed = [t for _d, t in self.iface.sent if t.startswith("DELETE_CHANNEL|")]
        self.assertEqual(echoed, [])

    def test_tombstone_hash_ignores_node_local_receipt_time(self):
        db_operations.record_sync_tombstone_at(
            "channels", self._key(), "2026-01-01 00:00:00")
        first_hash = db_operations.get_record_hash_manifest("tombstones")[
            "channels:" + self._key()
        ]
        db_operations.record_sync_tombstone_at(
            "channels", self._key(), "2026-08-30 23:00:00")
        second_hash = db_operations.get_record_hash_manifest("tombstones")[
            "channels:" + self._key()
        ]
        self.assertEqual(first_hash, second_hash)

    def test_a_deliberate_local_re_add_clears_the_tombstone(self):
        """Otherwise the next sync pass would honour it and delete it again."""
        db_operations.add_channel(self.NAME, self.URL)
        db_operations.delete_channel(self.NAME, self.URL)
        db_operations.add_channel(self.NAME, self.URL)
        self.assertEqual(self._count(), 1)
        self.assertFalse(db_operations.has_sync_tombstone("channels", self._key()))

    def test_deleting_an_unknown_channel_reports_false(self):
        self.assertFalse(db_operations.delete_channel("nope", "nowhere"))

    def test_deleting_takes_the_channels_comments_with_it(self):
        db_operations.add_channel(self.NAME, self.URL)
        conn = db_operations.thread_local.connection
        cid = conn.execute("SELECT id FROM channels WHERE name = ?", (self.NAME,)).fetchone()[0]
        conn.execute(
            "INSERT INTO channel_comments (channel_id, sender_short_name, date, content, unique_id) "
            "VALUES (?, 'AAA', '2026-01-01', 'hi', 'c-1')", (cid,))
        conn.commit()
        db_operations.delete_channel(self.NAME, self.URL)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM channel_comments").fetchone()[0], 0)


class TombstoneRestoreTests(unittest.TestCase):
    NAME = "VT Mesh"
    URL = "https://meshtastic.org/e/vt"

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_a_deleted_channel_can_be_restored(self):
        db_operations.add_channel(self.NAME, self.URL)
        db_operations.delete_channel(self.NAME, self.URL)
        entry = db_operations.get_sync_tombstones()[0]
        self.assertTrue(entry["restorable"])
        self.assertTrue(db_operations.restore_sync_tombstone(entry["tombstone_key"]))
        conn = db_operations.thread_local.connection
        self.assertEqual(
            conn.execute("SELECT name, url FROM channels").fetchall(),
            [(self.NAME, self.URL)])

    def test_restoring_clears_the_tombstone(self):
        """Left in place, the next sync pass would delete it again."""
        db_operations.add_channel(self.NAME, self.URL)
        db_operations.delete_channel(self.NAME, self.URL)
        key = db_operations.get_sync_tombstones()[0]["tombstone_key"]
        db_operations.restore_sync_tombstone(key)
        self.assertFalse(db_operations.has_sync_tombstone(
            "channels", db_operations.make_channel_manifest_key(self.NAME, self.URL)))

    def test_a_tombstone_with_no_snapshot_is_not_restorable(self):
        """Deletes predating snapshots, and those replayed from a peer, have
        nothing stored to bring back."""
        db_operations.record_sync_tombstone("channels", "somekey")
        entry = db_operations.get_sync_tombstones()[0]
        self.assertFalse(entry["restorable"])
        self.assertFalse(db_operations.restore_sync_tombstone(entry["tombstone_key"]))

    def test_a_replayed_delete_does_not_erase_the_local_snapshot(self):
        """A peer's delete carries no payload; letting it blank the copy we
        made would silently make the record unrestorable."""
        db_operations.add_channel(self.NAME, self.URL)
        db_operations.delete_channel(self.NAME, self.URL)
        key = db_operations.make_channel_manifest_key(self.NAME, self.URL)
        db_operations.record_sync_tombstone("channels", key)  # replay, no payload
        self.assertTrue(db_operations.get_sync_tombstones()[0]["restorable"])

    def test_forgetting_leaves_the_record_deleted(self):
        db_operations.add_channel(self.NAME, self.URL)
        db_operations.delete_channel(self.NAME, self.URL)
        key = db_operations.get_sync_tombstones()[0]["tombstone_key"]
        self.assertTrue(db_operations.forget_sync_tombstone(key))
        self.assertEqual(db_operations.get_sync_tombstones(), [])
        conn = db_operations.thread_local.connection
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0], 0)

    def test_legacy_tombstones_still_load(self):
        """Rows written before the payload column existed."""
        conn = db_operations.thread_local.connection
        conn.execute("INSERT INTO deleted_sync_tombstones (tombstone_key, deleted_at) "
                     "VALUES ('bulletins:old-uid', '2026-01-01 00:00:00')")
        conn.commit()
        entry = [t for t in db_operations.get_sync_tombstones()
                 if t["record_key"] == "old-uid"][0]
        self.assertEqual(entry["scope"], "bulletins")
        self.assertFalse(entry["restorable"])


if __name__ == "__main__":
    unittest.main()


class DeletePropagationTests(unittest.TestCase):
    """Reconciliation had suppression but no propagation.

    When this node had deleted a record and the peer had not, we stopped
    ourselves re-pulling it and left it there. The peer kept offering it and
    we kept refusing -- both holding their ground, forever. That is the
    stalemate two live nodes ended up in over channels.
    """

    NAME = "VT Mesh"
    URL = "https://meshtastic.org/e/vt"

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.iface = _Iface()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _sent(self, prefix):
        return [t for _d, t in self.iface.sent if t.startswith(prefix)]

    def test_a_held_tombstone_is_pushed_to_the_peer_that_still_has_it(self):
        key = db_operations.make_channel_manifest_key(self.NAME, self.URL)
        message_processing._push_delete_to_peer("channels", key, "!peer", self.iface)
        sent = self._sent("DELETE_CHANNEL|")
        self.assertEqual(len(sent), 1)
        self.assertIn(key, sent[0])

    def test_bulletin_and_mail_deletes_push_too(self):
        message_processing._push_delete_to_peer("bulletins", "uid-1", "!peer", self.iface)
        message_processing._push_delete_to_peer("mail", "uid-2", "!peer", self.iface)
        self.assertEqual(len(self._sent("DELETE_BULLETIN|")), 1)
        self.assertEqual(len(self._sent("DELETE_MAIL|")), 1)

    def test_a_comment_tombstone_pushes_the_comment_frame_not_the_channel_one(self):
        message_processing._push_delete_to_peer("channels", "comment:c-1", "!peer", self.iface)
        self.assertEqual(len(self._sent("DELETE_CHANNELCOMMENT|")), 1)
        self.assertEqual(self._sent("DELETE_CHANNEL|"), [])

    def test_a_scope_with_no_delete_frame_is_left_alone(self):
        """Half-handling it would be worse than not handling it."""
        message_processing._push_delete_to_peer("profiles", "!node", "!peer", self.iface)
        self.assertEqual(self.iface.sent, [])

    def test_replaying_a_channel_delete_answers_instead_of_going_silent(self):
        """The responder had branches for bulletins, mail, comments and zork
        saves, but none for a channel entry -- so the request was answered
        with nothing."""
        key = db_operations.make_channel_manifest_key(self.NAME, self.URL)
        message_processing._send_requested_record(
            "tombstones", "channels:" + key, "!peer", self.iface)
        sent = self._sent("DELETE_CHANNEL|")
        self.assertEqual(len(sent), 1, "channel delete replay produced nothing")
