import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
from public_chatter import normalize_broadcast


class PublicChatterTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.interface = SimpleNamespace(
            protocol_name="Meshtastic",
            public_chatter_channels=[0],
            public_chatter_capture_node_id="!capture-a",
        )
        self.now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        db_operations.thread_local.connection.close()
        del db_operations.thread_local.connection

    def packet(self, **changes):
        packet = {
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"Hello mesh"},
            "fromId": "!sender",
            "to": 0,
            "channel": 0,
            "id": 12345,
            "rxTime": int(self.now.timestamp()),
            "sender_name": "CALL",
            "channel_name": "LongFast",
        }
        packet.update(changes)
        return packet

    def test_duplicate_rf_packet_keeps_one_row_and_original_lifetime(self):
        first = normalize_broadcast(self.packet(), self.interface, captured_at=self.now)
        self.assertIsNotNone(first)
        self.assertTrue(db_operations.add_public_chatter(**first))

        second_interface = SimpleNamespace(
            protocol_name="Meshtastic",
            public_chatter_channels=[0],
            public_chatter_capture_node_id="!capture-b",
        )
        second = normalize_broadcast(self.packet(), second_interface, captured_at=self.now)
        self.assertFalse(db_operations.add_public_chatter(**second))

        conn = db_operations.get_db_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM public_chatter").fetchone()[0], 1)
        row = conn.execute(
            "SELECT capture_node_id, expires_at FROM public_chatter"
        ).fetchone()
        self.assertEqual(row[0], "!capture-a")
        self.assertEqual(row[1], "2026-09-06T12:00:00Z")

    def test_rejects_dm_and_channel_outside_allowlist(self):
        self.assertIsNone(normalize_broadcast(self.packet(to=42), self.interface, captured_at=self.now))
        self.assertIsNone(normalize_broadcast(self.packet(channel=1), self.interface, captured_at=self.now))

    def test_rejects_bbs_control_frames(self):
        packet = self.packet()
        packet["decoded"]["payload"] = b"SYNCSTATE|1|2|3"
        self.assertIsNone(normalize_broadcast(packet, self.interface, captured_at=self.now))

    def test_meshtastic_channel_zero_defaults_to_longfast(self):
        observation = normalize_broadcast(
            self.packet(channel_name=""), self.interface, captured_at=self.now
        )
        self.assertEqual(observation["channel_name"], "LongFast")

    def test_schema_has_query_and_expiry_indexes(self):
        indexes = {
            row[1] for row in db_operations.get_db_connection().execute(
                "PRAGMA index_list(public_chatter)"
            )
        }
        self.assertIn("idx_public_chatter_unique_id", indexes)
        self.assertIn("idx_public_chatter_time", indexes)
        self.assertIn("idx_public_chatter_expiry", indexes)

    def test_database_initialization_removes_mqtt_misclassified_chatter(self):
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat().replace("+00:00", "Z")
        db_operations.add_public_chatter(
            "pch:mqtt-misclassified", "mqtt:mqtt1", 0, "LongFast",
            "mqtt:bridge:peer", "peer", "sync topic text", timestamp, timestamp,
            "mqtt:bridge:local",
            (now + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
            sync_received=True,
        )

        db_operations.initialize_database()

        self.assertIsNone(
            db_operations.get_public_chatter_by_unique_id("pch:mqtt-misclassified")
        )

    def test_history_clamps_window_and_filters_network(self):
        observation = normalize_broadcast(self.packet(), self.interface)
        self.assertIsNotNone(observation)
        db_operations.add_public_chatter(**observation)
        result = db_operations.get_public_chatter_history(
            hours=999, network="meshtastic", limit=500
        )
        self.assertEqual(result["hours"], 168)
        self.assertEqual(result["limit"], 200)
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["content"], "Hello mesh")

    def test_history_can_return_complete_window_without_page_limit(self):
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat().replace("+00:00", "Z")
        expires_at = (now + timedelta(days=7)).isoformat().replace("+00:00", "Z")
        db_operations.get_db_connection().executemany(
            '''INSERT INTO public_chatter
                   (unique_id, network, channel_index, content, message_timestamp,
                    captured_at, expires_at)
               VALUES (?, 'meshtastic', 0, 'message', ?, ?, ?)''',
            [(f"pch:{index}", timestamp, timestamp, expires_at) for index in range(201)],
        )

        result = db_operations.get_public_chatter_history(hours=24, limit=None)

        self.assertEqual(len(result["entries"]), 201)
        self.assertIsNone(result["limit"])
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_cursor"])

    def test_expiry_prunes_record_and_discovery_event_without_tombstone(self):
        db_operations.set_local_node_id("!capture-a")
        expired = self.now - timedelta(minutes=1)
        db_operations.add_public_chatter(
            "pch:expired", "meshtastic", 0, "LongFast", "!sender", "CALL",
            "old", (expired - timedelta(days=7)).isoformat().replace("+00:00", "Z"),
            (expired - timedelta(days=7)).isoformat().replace("+00:00", "Z"),
            "!capture-a", expired.isoformat().replace("+00:00", "Z"),
        )
        self.assertEqual(db_operations.prune_expired_public_chatter(), 1)
        conn = db_operations.get_db_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM public_chatter").fetchone()[0], 0)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM op_log WHERE scope='public_chatter'").fetchone()[0],
            0,
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM deleted_sync_tombstones").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()