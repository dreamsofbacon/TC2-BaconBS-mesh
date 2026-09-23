"""Channels reach the nodes they are told to, and their comments follow.

Boards got this first; channels could only be "this node only", and even
that leaked: a comment on a local-only channel was broadcast to every peer
unconditionally, because the send did not look at the channel it was on.

A comment has no audience of its own. It goes exactly as far as its channel,
everywhere -- the creation broadcast, the op log, the manifests and the
answer to a peer asking for it by name.
"""
import os
import sqlite3
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations
import message_processing as mp

LOCAL = "!self0000"
PEER_A = "!aaaa1111"
PEER_B = "!bbbb2222"


class _Iface:
    protocol_name = "meshtastic"
    max_text_bytes = 220
    nodes = {}
    bbs_nodes = [PEER_A, PEER_B]


class _Channels(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.iface = _Iface()
        patcher = mock.patch.object(db_operations, 'get_local_node_id',
                                    return_value=LOCAL)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def add_channel(self, name="News", url="https://example.invalid/feed", **kwargs):
        with mock.patch.object(db_operations, 'send_channel_to_bbs_nodes') as send:
            channel_id = db_operations.add_channel(
                name, url, [PEER_A, PEER_B], self.iface, **kwargs)
        return channel_id, send

    def comment(self, channel_id, content="a comment"):
        with mock.patch.object(db_operations, 'send_channel_comment_to_bbs_nodes') as send:
            unique_id = db_operations.add_channel_comment(
                channel_id, "caller", content, [PEER_A, PEER_B], self.iface)
        return unique_id, send


class ChannelAudienceTests(_Channels):
    def test_a_new_channel_goes_everywhere(self):
        _id, send = self.add_channel()
        self.assertEqual([PEER_A, PEER_B], list(send.call_args[0][2]))

    def test_chosen_nodes_only(self):
        channel_id, _ = self.add_channel()
        db_operations.set_channel_audience(
            "News", "https://example.invalid/feed", "peers", [PEER_A])
        self.assertEqual(('peers', [PEER_A]), db_operations.get_channel_audience(
            "News", "https://example.invalid/feed"))

    def test_choosing_no_nodes_means_this_node_only(self):
        self.add_channel()
        db_operations.set_channel_audience(
            "News", "https://example.invalid/feed", "peers", [])
        self.assertEqual(('local', []), db_operations.get_channel_audience(
            "News", "https://example.invalid/feed"))

    def test_a_peer_is_not_served_a_channel_it_may_not_have(self):
        self.add_channel()
        db_operations.set_channel_audience(
            "News", "https://example.invalid/feed", "peers", [PEER_A])
        key = db_operations.make_channel_manifest_key(
            "News", "https://example.invalid/feed")
        self.assertIsNotNone(db_operations.get_channel_by_manifest_key(key, peer_id=PEER_A))
        self.assertIsNone(db_operations.get_channel_by_manifest_key(key, peer_id=PEER_B))

    def test_the_manifest_and_counts_agree_per_peer(self):
        self.add_channel()
        self.add_channel(name="Open", url="https://example.invalid/open")
        db_operations.set_channel_audience(
            "News", "https://example.invalid/feed", "peers", [PEER_A])
        for peer, expected in ((PEER_A, 2), (PEER_B, 1)):
            with self.subTest(peer=peer):
                manifest = db_operations.get_record_hash_manifest('channels', peer_id=peer)
                self.assertEqual(expected, len(manifest))


class CommentsFollowTheirChannelTests(_Channels):
    def test_a_comment_on_a_local_only_channel_is_not_broadcast(self):
        """The leak: this send ignored the channel entirely."""
        channel_id, _ = self.add_channel(local_only=True)
        _unique_id, send = self.comment(channel_id)
        send.assert_not_called()

    def test_a_comment_on_a_local_only_channel_is_not_in_the_op_log(self):
        channel_id, _ = self.add_channel(local_only=True)
        unique_id, _send = self.comment(channel_id)
        rows = db_operations.get_db_connection().execute(
            "SELECT target_uid FROM op_log WHERE target_uid = ?",
            (unique_id,)).fetchall()
        self.assertEqual([], rows)

    def test_a_comment_goes_only_where_its_channel_goes(self):
        channel_id, _ = self.add_channel()
        db_operations.set_channel_audience(
            "News", "https://example.invalid/feed", "peers", [PEER_A])
        _unique_id, send = self.comment(channel_id)
        self.assertEqual([PEER_A], list(send.call_args[0][5]))

    def test_a_peer_asking_for_a_withheld_comment_gets_nothing(self):
        channel_id, _ = self.add_channel(local_only=True)
        unique_id, _send = self.comment(channel_id)
        self.assertIsNone(db_operations.get_channel_comment_by_unique_id(
            unique_id, peer_id=PEER_A))
        with mock.patch.object(mp, 'send_channel_comment_to_bbs_nodes') as send:
            mp._send_requested_record('channel_comments', unique_id, PEER_A, self.iface)
        send.assert_not_called()

    def test_it_is_still_readable_here(self):
        channel_id, _ = self.add_channel(local_only=True)
        unique_id, _send = self.comment(channel_id)
        self.assertIsNotNone(
            db_operations.get_channel_comment_by_unique_id(unique_id))


if __name__ == "__main__":
    unittest.main()
