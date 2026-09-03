import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import message_processing
import utils


class PublicChatterProtocolTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        db_operations.set_local_node_id("!origin")
        message_processing._pending_public_chatter.clear()
        self.interface = SimpleNamespace(
            bbs_nodes=[], max_text_bytes=80, protocol_name="Meshtastic"
        )

    def tearDown(self):
        message_processing.set_public_chatter_cross_link_relay(None)
        db_operations.set_local_node_id(None)
        db_operations.thread_local.connection.close()
        del db_operations.thread_local.connection

    def record(self, unique_id="pch:test"):
        now = datetime.now(timezone.utc)
        db_operations.add_public_chatter(
            unique_id, "meshtastic", 0, "LongFast", "!sender", "CALL",
            "A longer message with a | delimiter and enough text to require chunks.",
            now.isoformat().replace("+00:00", "Z"),
            now.isoformat().replace("+00:00", "Z"),
            "!origin", (now + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
        )
        return db_operations.get_public_chatter_by_unique_id(unique_id)

    def test_local_capture_writes_one_op_log_event(self):
        self.record()
        count = db_operations.get_db_connection().execute(
            "SELECT COUNT(*) FROM op_log WHERE scope='public_chatter' AND target_uid='pch:test'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_chunked_record_round_trip_does_not_create_new_event(self):
        row = self.record()
        frames = []
        with mock.patch.object(db_operations, "peer_supports", return_value=True), mock.patch.object(
            utils, "_send_one_sync", side_effect=lambda frame, *_args, **_kwargs: frames.append(frame)
        ):
            utils.send_public_chatter_to_bbs_nodes(row, ["!peer"], self.interface)
        self.assertTrue(frames[0].startswith("PCHAT|pch:test|"))
        self.assertTrue(any(frame.startswith("PCHATCONT|pch:test|") for frame in frames))

        conn = db_operations.get_db_connection()
        conn.execute("DELETE FROM public_chatter")
        conn.execute("DELETE FROM op_log")
        conn.commit()
        for frame in frames:
            message_processing.process_message(
                1, frame, self.interface, is_sync_message=True, sender_node_id="!peer"
            )
        restored = db_operations.get_public_chatter_by_unique_id("pch:test")
        self.assertIsNotNone(restored)
        self.assertIn("| delimiter", restored[6])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM op_log WHERE scope='public_chatter'").fetchone()[0],
            0,
        )

    def test_legacy_peer_receives_no_chatter_frames(self):
        frames = []
        with mock.patch.object(db_operations, "peer_supports", return_value=False), mock.patch.object(
            utils, "_send_one_sync", side_effect=lambda frame, *_args, **_kwargs: frames.append(frame)
        ):
            utils.send_public_chatter_to_bbs_nodes(self.record(), ["!legacy"], self.interface)
        self.assertEqual(frames, [])

    def test_mqtt_chatter_is_published_once_for_all_capable_peers(self):
        row = self.record()
        frames = []
        self.interface.protocol_name = "MQTT:test"
        self.interface.max_text_bytes = 32768
        with mock.patch.object(db_operations, "peer_supports", return_value=True), mock.patch.object(
            utils, "_send_one_sync", side_effect=lambda frame, destination, *_args, **_kwargs: frames.append((frame, destination))
        ):
            utils.send_public_chatter_to_bbs_nodes(
                row, ["mqtt:group:one", "mqtt:group:two"], self.interface)

        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0][0].startswith("PCHAT|pch:test|"))
        self.assertEqual(frames[0][1], 0)

    def test_new_mqtt_chatter_invokes_only_cross_link_relay(self):
        row = self.record()
        frames = []
        with mock.patch.object(db_operations, "peer_supports", return_value=True), mock.patch.object(
            utils, "_send_one_sync", side_effect=lambda frame, *_args, **_kwargs: frames.append(frame)
        ):
            utils.send_public_chatter_to_bbs_nodes(row, ["mqtt:group:source"], self.interface)
        conn = db_operations.get_db_connection()
        conn.execute("DELETE FROM public_chatter")
        conn.commit()
        self.interface.protocol_name = "MQTT:test"
        relayed = []
        message_processing.set_public_chatter_cross_link_relay(
            lambda stored, origin: relayed.append((stored[0], origin)))

        for frame in frames:
            message_processing.process_message(
                1, frame, self.interface, is_sync_message=True,
                sender_node_id="mqtt:group:source",
            )
        self.assertEqual(relayed, [("pch:test", self.interface)])

        for frame in frames:
            message_processing.process_message(
                1, frame, self.interface, is_sync_message=True,
                sender_node_id="mqtt:group:source",
            )
        self.assertEqual(relayed, [("pch:test", self.interface)])

    def test_partial_chunks_are_isolated_by_sender_and_expire(self):
        self.assertFalse(message_processing._add_public_chatter_chunk(
            "!peer-a", "pch:shared", 0, "abc", expected_length=6))
        self.assertFalse(message_processing._add_public_chatter_chunk(
            "!peer-b", "pch:shared", 0, "xyz", expected_length=6))
        self.assertEqual(
            set(message_processing._pending_public_chatter),
            {("!peer-a", "pch:shared"), ("!peer-b", "pch:shared")},
        )

        for entry in message_processing._pending_public_chatter.values():
            entry["updated_at"] = 1.0
        message_processing._prune_stale_public_chatter_buffers(
            now=1.0 + message_processing._PUBLIC_CHATTER_BUFFER_TTL_SECONDS + 1)
        self.assertEqual(message_processing._pending_public_chatter, {})

    def test_public_chatter_chunks_can_arrive_out_of_order(self):
        with mock.patch.object(message_processing, "_store_public_chatter_payload", return_value=True) as store:
            self.assertFalse(message_processing._add_public_chatter_chunk(
                "!peer", "pch:ordered", 3, "def"))
            self.assertTrue(message_processing._add_public_chatter_chunk(
                "!peer", "pch:ordered", 0, "abc", expected_length=6))
        store.assert_called_once_with("pch:ordered", "abcdef")

    def test_conflicting_duplicate_chunk_discards_buffer(self):
        self.assertFalse(message_processing._add_public_chatter_chunk(
            "!peer", "pch:conflict", 3, "def"))
        self.assertFalse(message_processing._add_public_chatter_chunk(
            "!peer", "pch:conflict", 3, "xyz"))
        self.assertNotIn(("!peer", "pch:conflict"), message_processing._pending_public_chatter)

    def test_oversized_public_chatter_payload_is_rejected_without_buffering(self):
        self.assertFalse(message_processing._add_public_chatter_chunk(
            "!peer", "pch:large", 0, "x",
            expected_length=message_processing._PUBLIC_CHATTER_MAX_PAYLOAD_LENGTH + 1,
        ))
        self.assertEqual(message_processing._pending_public_chatter, {})


if __name__ == "__main__":
    unittest.main()