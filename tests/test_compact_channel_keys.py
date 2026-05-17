"""Tests for PR 1 — `cck` capability: compact channel-comment keys.

When a peer advertises the ``cck`` cap, ``send_channel_comment_to_bbs_nodes``
must emit the short ``~hash`` key instead of the full base64(name+url) key,
saving 40-90 bytes per frame.  Legacy peers (no ``cck``) keep receiving the
full key unless it would crowd the single-packet content budget.
"""

import sqlite3
import unittest
from unittest.mock import patch

import db_operations
import utils


class CompactChannelKeyTests(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(":memory:")
        db_operations.thread_local.connection = conn
        db_operations.initialize_database()
        db_operations._clear_peer_caps_cache()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        db_operations._clear_peer_caps_cache()

    def _capture_sends(self):
        sends = []

        def fake(message, node_id, interface, *_a, **_kw):
            sends.append((node_id, message))

        return sends, fake

    def test_cck_peer_receives_compact_key(self):
        db_operations.upsert_peer_sync_state(
            "!aaaaaaaa", 0, 0, 0, 0, proto_v=2, caps="cck",
        )
        # Use a short channel key to make sure cck is the *active* choice, not
        # a forced fallback for over-large frames.
        full_key = "U2hvcnRDaGFu"  # base64('ShortChan') — well within size limit
        sends, fake_send = self._capture_sends()
        with patch.object(utils, "_send_one_sync", side_effect=fake_send):
            utils.send_channel_comment_to_bbs_nodes(
                full_key, "alice", "2026-05-17", "hi there", "uid-1",
                ["!aaaaaaaa"], interface=None,
            )

        self.assertTrue(sends, "expected at least one send")
        node, msg = sends[0]
        self.assertEqual(node, "!aaaaaaaa")
        parts = msg.split("|")
        self.assertEqual(parts[0], "CHANNELCOMMENT")
        # Compact keys are prefixed with '~'.
        self.assertTrue(parts[1].startswith("~"),
                        f"cck peer should receive compact key, got {parts[1]!r}")

    def test_legacy_peer_receives_full_key_when_it_fits(self):
        # Peer is in the DB but has not advertised cck.
        db_operations.upsert_peer_sync_state(
            "!bbbbbbbb", 0, 0, 0, 0, proto_v=2, caps="",
        )
        full_key = "U2hvcnRDaGFu"  # short — full key fits comfortably
        sends, fake_send = self._capture_sends()
        with patch.object(utils, "_send_one_sync", side_effect=fake_send):
            utils.send_channel_comment_to_bbs_nodes(
                full_key, "alice", "2026-05-17", "hi", "uid-2",
                ["!bbbbbbbb"], interface=None,
            )

        self.assertTrue(sends)
        _, msg = sends[0]
        parts = msg.split("|")
        self.assertEqual(parts[1], full_key,
                         "legacy peer should receive the full base64 key")

    def test_mixed_peers_get_their_appropriate_key(self):
        db_operations.upsert_peer_sync_state(
            "!cap00001", 0, 0, 0, 0, proto_v=2, caps="cck",
        )
        db_operations.upsert_peer_sync_state(
            "!leg00002", 0, 0, 0, 0, proto_v=2, caps="",
        )
        full_key = "U2hvcnRDaGFu"
        sends, fake_send = self._capture_sends()
        with patch.object(utils, "_send_one_sync", side_effect=fake_send):
            utils.send_channel_comment_to_bbs_nodes(
                full_key, "alice", "2026-05-17", "hi", "uid-3",
                ["!cap00001", "!leg00002"], interface=None,
            )

        by_node = {node: msg for node, msg in sends}
        self.assertIn("!cap00001", by_node)
        self.assertIn("!leg00002", by_node)
        self.assertTrue(by_node["!cap00001"].split("|")[1].startswith("~"))
        self.assertEqual(by_node["!leg00002"].split("|")[1], full_key)

    def test_compact_key_saves_bytes(self):
        # A realistic full key (base64 of "RadioTalk,https://example.com/lora").
        # The compact form should be much shorter.
        import base64 as _b64
        full_key = _b64.b64encode(b"RadioTalk,https://example.com/lora").decode("ascii")
        short_key = utils.compact_channel_manifest_key(full_key)
        self.assertLess(len(short_key), len(full_key))
        # 6-byte blake2b digest urlsafe-b64 (no padding) = 8 chars + '~' prefix.
        self.assertEqual(len(short_key), 9)


if __name__ == "__main__":
    unittest.main()
