"""A device move, and an unlink, are true on every node -- not just the one.

Reported 2026-09-14: a peer never accepted a device moving between accounts,
so a move made on one node was invisible to the others. They kept routing
that person's mail by the old link, and re-advertised it back. An unlink had
no way to travel at all.

Both now carry a timestamp and the newest wins. An unlink leaves a tombstone
(ACCTUNLINK, gated on the 'acctmv' capability) so a peer still holding the
old link cannot resurrect it. An `ssh:` login is still never moved: it is its
own account, and moving it would strand the password.
"""
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import utils

HERE = "a" * 32
THERE = "b" * 32
RADIO = "!04058ac8"


def stamp(offset_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


class _Case(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        enabled = mock.patch.object(utils, "is_account_sync_enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.addCleanup(self._close)
        for account_id, alias in ((HERE, "bacon"), (THERE, "materva")):
            db_operations.apply_synced_account_identity(account_id, alias, stamp(-3600))

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def link(self, node_id, account_id, when=-3600):
        conn = db_operations.get_db_connection()
        conn.execute("DELETE FROM linked_nodes WHERE node_id = ?", (node_id,))
        conn.execute("INSERT INTO linked_nodes (node_id, account_id, network, linked_at)"
                     " VALUES (?, ?, 'meshtastic', ?)", (node_id, account_id, stamp(when)))
        conn.commit()

    def owner(self, node_id=RADIO):
        return db_operations.get_account_id_for_node(node_id)


class MoveTests(_Case):
    def test_a_newer_move_is_accepted_and_clears_an_old_removal(self):
        db_operations.record_node_unlink(RADIO, stamp(-600))
        self.assertTrue(db_operations.apply_synced_account_link(
            RADIO, THERE, "meshtastic", stamp()))
        self.assertEqual(self.owner(), THERE)
        self.assertEqual(db_operations.get_node_unlink_time(RADIO), "")

    def test_a_link_older_than_a_removal_is_not_resurrected(self):
        db_operations.record_node_unlink(RADIO, stamp(-60))
        self.assertFalse(db_operations.apply_synced_account_link(
            RADIO, THERE, "meshtastic", stamp(-600)))
        self.assertIsNone(self.owner())

    def test_an_ssh_login_is_never_moved(self):
        ssh_id = f"ssh:{HERE}"
        self.link(ssh_id, HERE)
        self.assertFalse(db_operations.apply_synced_account_link(
            ssh_id, THERE, "ssh", stamp()))
        self.assertEqual(self.owner(ssh_id), HERE)

    def test_the_same_account_again_changes_nothing(self):
        self.link(RADIO, HERE)
        self.assertFalse(db_operations.apply_synced_account_link(
            RADIO, HERE, "meshtastic", stamp()))
        self.assertEqual(self.owner(), HERE)

    def test_a_local_move_is_stamped_so_peers_accept_it(self):
        """The move a user makes with a code has to beat the link the other
        node already holds, or it would be refused there as older."""
        self.link(RADIO, HERE, when=-60)
        code = db_operations.create_link_code(THERE, "!aaaaaaaa")
        ok, message = db_operations.move_node_with_link_code(code, RADIO, "meshtastic")
        self.assertTrue(ok, message)
        row = db_operations.get_db_connection().execute(
            "SELECT account_id, linked_at FROM linked_nodes WHERE node_id = ?",
            (RADIO,)).fetchone()
        self.assertEqual(row[0], THERE)
        self.assertGreater(row[1], stamp(-60))


class UnlinkTests(_Case):
    def test_unlinking_leaves_a_tombstone(self):
        self.link(RADIO, HERE)
        self.link("!ffffffff", HERE)
        self.assertTrue(db_operations.unlink_node(RADIO))
        self.assertNotEqual(db_operations.get_node_unlink_time(RADIO), "")

    def test_a_peers_unlink_removes_the_local_link(self):
        self.link(RADIO, HERE, when=-600)
        self.assertTrue(db_operations.apply_synced_account_unlink(RADIO, stamp()))
        self.assertIsNone(self.owner())

    def test_an_older_unlink_does_not_undo_a_newer_link(self):
        self.link(RADIO, HERE, when=-60)
        self.assertFalse(db_operations.apply_synced_account_unlink(RADIO, stamp(-600)))
        self.assertEqual(self.owner(), HERE)

    def test_an_unlink_for_an_ssh_login_is_refused(self):
        ssh_id = f"ssh:{HERE}"
        self.link(ssh_id, HERE)
        self.assertFalse(db_operations.apply_synced_account_unlink(ssh_id, stamp()))
        self.assertEqual(self.owner(ssh_id), HERE)

    def test_a_malformed_unlink_is_ignored(self):
        self.assertFalse(db_operations.apply_synced_account_unlink("", stamp()))
        self.assertFalse(db_operations.apply_synced_account_unlink(RADIO, ""))

    def test_old_tombstones_are_dropped(self):
        db_operations.record_node_unlink(RADIO, stamp(-1))
        db_operations.record_node_unlink(
            "!eeeeeeee",
            (datetime.now(timezone.utc)
             - timedelta(days=db_operations.UNLINK_TOMBSTONE_MAX_AGE_DAYS + 1)).isoformat())
        listed = [row["node_id"] for row in db_operations.get_node_unlinks_for_sync()]
        self.assertEqual(listed, [RADIO])


class WireTests(_Case):
    def test_the_frame_goes_only_to_peers_that_understand_it(self):
        caps = {"!new": {"acct", "acctmv"}, "!half": {"acct"}, "!old": set()}
        sent = []
        with mock.patch.object(db_operations, "peer_supports",
                               side_effect=lambda peer, cap: cap in caps.get(peer, set())), \
             mock.patch.object(utils, "_send_one_sync",
                               side_effect=lambda msg, dest, *a, **k: sent.append((dest, msg))):
            count = utils.send_account_unlink_to_bbs_nodes(
                RADIO, "2026-09-17T00:00:00Z", ["!new", "!half", "!old"], object())
        self.assertEqual(count, 1)
        self.assertEqual(sent, [("!new", f"ACCTUNLINK|{RADIO}|2026-09-17T00:00:00Z")])

    def test_the_capability_is_advertised(self):
        self.assertIn("acctmv", utils.WIRE_CAPABILITIES)

    def test_a_received_frame_is_applied(self):
        import message_processing
        self.link(RADIO, HERE, when=-600)
        message_processing.process_message(
            sender_id=1, message=f"ACCTUNLINK|{RADIO}|{stamp()}",
            interface=types.SimpleNamespace(sent_texts=[], bbs_nodes=[]),
            is_sync_message=True, sender_node_id="!peer1")
        self.assertIsNone(self.owner())


if __name__ == "__main__":
    unittest.main()
