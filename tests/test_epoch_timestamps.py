"""Tests for PR 2 — `epoch` capability: epoch-encoded timestamps.

Senders emit minute-precision dates as ``m<seconds>`` and second-precision
ISO timestamps as ``s<seconds>`` when every peer supports the ``epoch`` cap.
Receivers decode both forms back to the canonical ISO strings before storing.
Legacy peers (no ``epoch`` cap) keep receiving ISO byte-for-byte.
"""

import sqlite3
import unittest
from datetime import datetime
from unittest.mock import patch

import db_operations
import message_processing
import utils


class EncoderDecoderTests(unittest.TestCase):
    def test_encode_ts_minute_roundtrip(self):
        iso = "2026-05-17 18:42"
        token = utils.encode_ts_minute(iso, use_epoch=True)
        self.assertTrue(token.startswith("m"), token)
        self.assertTrue(token[1:].isdigit(), token)
        self.assertEqual(utils.decode_ts_minute(token), iso)

    def test_encode_ts_second_roundtrip(self):
        iso = "2026-05-17T18:42:31"
        token = utils.encode_ts_second(iso, use_epoch=True)
        self.assertTrue(token.startswith("s"), token)
        self.assertTrue(token[1:].isdigit(), token)
        self.assertEqual(utils.decode_ts_second(token), iso)

    def test_encode_passthrough_when_not_epoch(self):
        iso = "2026-05-17 18:42"
        self.assertEqual(utils.encode_ts_minute(iso, use_epoch=False), iso)
        self.assertEqual(utils.encode_ts_second("2026-05-17T18:42:31", False),
                         "2026-05-17T18:42:31")

    def test_decode_passes_through_legacy_iso(self):
        # Legacy ISO strings come through unchanged.
        self.assertEqual(utils.decode_ts_minute("2026-05-17 18:42"),
                         "2026-05-17 18:42")
        self.assertEqual(utils.decode_ts_second("2026-05-17T18:42:31"),
                         "2026-05-17T18:42:31")

    def test_decode_garbage_returns_original(self):
        self.assertEqual(utils.decode_ts_minute("mNotANumber"), "mNotANumber")
        self.assertEqual(utils.decode_ts_second("hello"), "hello")
        self.assertEqual(utils.decode_ts_minute(""), "")

    def test_encode_handles_none_and_empty(self):
        self.assertEqual(utils.encode_ts_minute(None, True), "")
        self.assertEqual(utils.encode_ts_minute("", True), "")

    def test_distinct_prefixes(self):
        # 'm' and 's' tokens for the same instant differ only in the prefix.
        sec = int(datetime(2026, 5, 17, 18, 42, 31).timestamp())
        # Minute token rounds down to the minute boundary.
        expected_minute_sec = (sec // 60) * 60
        self.assertEqual(utils.encode_ts_minute("2026-05-17 18:42", True),
                         f"m{int(datetime(2026, 5, 17, 18, 42).timestamp())}")
        self.assertEqual(utils.encode_ts_second("2026-05-17T18:42:31", True),
                         f"s{sec}")


class _DBFixtureMixin:
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


class PeersAllSupportTests(_DBFixtureMixin, unittest.TestCase):
    def test_empty_peer_set_is_false(self):
        self.assertFalse(utils.peers_all_support([], "epoch"))

    def test_all_supporting_peers(self):
        db_operations.upsert_peer_sync_state("!a", 0, 0, 0, 0, proto_v=2, caps="epoch")
        db_operations.upsert_peer_sync_state("!b", 0, 0, 0, 0, proto_v=2, caps="epoch,cck")
        self.assertTrue(utils.peers_all_support(["!a", "!b"], "epoch"))

    def test_one_missing_peer_blocks_all(self):
        db_operations.upsert_peer_sync_state("!a", 0, 0, 0, 0, proto_v=2, caps="epoch")
        db_operations.upsert_peer_sync_state("!b", 0, 0, 0, 0, proto_v=2, caps="cck")
        self.assertFalse(utils.peers_all_support(["!a", "!b"], "epoch"))


class WireRoundtripTests(_DBFixtureMixin, unittest.TestCase):
    def _capture_sends(self):
        sends = []

        def fake(message, node_id, interface, *_a, **_kw):
            sends.append((node_id, message))

        return sends, fake

    def test_bulletin_uses_epoch_when_all_peers_support(self):
        db_operations.upsert_peer_sync_state("!a", 0, 0, 0, 0, proto_v=2, caps="epoch")
        sends, fake_send = self._capture_sends()
        with patch.object(utils, "_send_one_sync", side_effect=fake_send):
            utils.send_bulletin_to_bbs_nodes(
                "General", "alice", "hello", "world", "uid-b1",
                ["!a"], interface=None, date="2026-05-17 18:42",
            )
        self.assertTrue(sends)
        _, msg = sends[0]
        # Date is the last field of the first packet.
        last_field = msg.rsplit("|", 1)[-1]
        self.assertTrue(last_field.startswith("m"), last_field)

    def test_bulletin_falls_back_to_iso_for_legacy_peer(self):
        db_operations.upsert_peer_sync_state("!a", 0, 0, 0, 0, proto_v=2, caps="")
        sends, fake_send = self._capture_sends()
        with patch.object(utils, "_send_one_sync", side_effect=fake_send):
            utils.send_bulletin_to_bbs_nodes(
                "General", "alice", "hello", "world", "uid-b2",
                ["!a"], interface=None, date="2026-05-17 18:42",
            )
        self.assertTrue(sends)
        _, msg = sends[0]
        self.assertTrue(msg.endswith("|2026-05-17 18:42"), msg)

    def test_bulletin_with_source_timestamp_epoch(self):
        db_operations.upsert_peer_sync_state("!a", 0, 0, 0, 0, proto_v=2, caps="epoch")
        sends, fake_send = self._capture_sends()
        with patch.object(utils, "_send_one_sync", side_effect=fake_send):
            utils.send_bulletin_to_bbs_nodes(
                "General", "alice", "hello", "world", "uid-b3",
                ["!a"], interface=None,
                date="2026-05-17 18:42",
                source_node_id="!srcnode",
                source_timestamp="2026-05-17T18:42:31",
            )
        self.assertTrue(sends)
        _, msg = sends[0]
        # Footer: |uid|m...|!srcnode|s...
        parts = msg.split("|")
        self.assertEqual(parts[-2], "!srcnode")
        self.assertTrue(parts[-1].startswith("s"), parts[-1])
        # The minute-precision date sits right before the source_node_id.
        self.assertTrue(parts[-3].startswith("m"), parts[-3])

    def test_pattern_matches_epoch_tokens(self):
        self.assertTrue(message_processing._SYNC_DATE_PATTERN.match("m1715973540"))
        self.assertTrue(message_processing._SYNC_DATE_PATTERN.match("2026-05-17 18:42"))
        self.assertFalse(message_processing._SYNC_DATE_PATTERN.match("s1715973540"))
        self.assertTrue(message_processing._SYNC_ISO_TIMESTAMP_PATTERN.match("s1715973540"))
        self.assertTrue(message_processing._SYNC_ISO_TIMESTAMP_PATTERN.match("2026-05-17T18:42:31"))
        self.assertFalse(message_processing._SYNC_ISO_TIMESTAMP_PATTERN.match("m1715973540"))


if __name__ == "__main__":
    unittest.main()
