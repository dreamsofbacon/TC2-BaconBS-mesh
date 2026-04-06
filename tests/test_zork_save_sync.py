import sqlite3
import sys
import types
import unittest
import base64

if "meshtastic" not in sys.modules:
    meshtastic_stub = types.ModuleType("meshtastic")
    setattr(meshtastic_stub, "BROADCAST_NUM", 0)
    sys.modules["meshtastic"] = meshtastic_stub

import db_operations
import message_processing
from utils import send_zork_save_to_bbs_nodes, _MESHTASTIC_MAX_BYTES


class _DummyInterface:
    def __init__(self):
        self.sent_texts = []
        self.bbs_nodes = []

    def sendText(self, text, destinationId, wantAck, wantResponse):
        self.sent_texts.append(text)


class ZorkSaveSyncTests(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(":memory:")
        db_operations.thread_local.connection = conn
        db_operations.initialize_database()
        message_processing._zork_save_chunk_buffers.clear()
        message_processing._peer_hash_manifest_buffers.clear()
        message_processing._peer_hash_compressed_buffers.clear()
        message_processing._recent_hashmiss_requests.clear()
        message_processing._recent_syncstate_repairs.clear()
        message_processing._pending_hashreq.clear()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_chunked_zork_save_sync_reassembles_and_persists(self):
        sender_interface = _DummyInterface()
        payload = bytes([x % 256 for x in range(2000)])

        send_zork_save_to_bbs_nodes(
            user_id="1234",
            game_id="zork1",
            save_data=payload,
            updated_at="2026-03-30 12:00:00",
            bbs_nodes=["!peer1"],
            interface=sender_interface,
            pause_seconds=0,
        )

        self.assertGreater(len(sender_interface.sent_texts), 1)
        self.assertTrue(all(m.startswith("ZORKSAVE|") for m in sender_interface.sent_texts))
        self.assertTrue(all(len(m.encode("utf-8")) <= _MESHTASTIC_MAX_BYTES for m in sender_interface.sent_texts))

        recv_interface = _DummyInterface()
        for msg in sender_interface.sent_texts:
            message_processing.process_message(
                sender_id=1,
                message=msg,
                interface=recv_interface,
                is_sync_message=True,
                sender_node_id="!peer1",
            )

        restored = db_operations.get_zork_save(1234, "zork1")
        self.assertEqual(restored, payload)

    def test_tampered_zork_chunk_is_rejected_by_payload_hash(self):
        sender_interface = _DummyInterface()
        payload = bytes([x % 256 for x in range(1500)])

        send_zork_save_to_bbs_nodes(
            user_id="1234",
            game_id="zork1",
            save_data=payload,
            updated_at="2026-03-30 12:01:00",
            bbs_nodes=["!peer1"],
            interface=sender_interface,
            pause_seconds=0,
        )

        tampered = list(sender_interface.sent_texts)
        self.assertGreater(len(tampered), 1)
        parts = tampered[-1].split("|")
        self.assertTrue(parts[-1])
        parts[-1] = ("A" if parts[-1][0] != "A" else "B") + parts[-1][1:]
        tampered[-1] = "|".join(parts)

        recv_interface = _DummyInterface()
        for msg in tampered:
            message_processing.process_message(
                sender_id=1,
                message=msg,
                interface=recv_interface,
                is_sync_message=True,
                sender_node_id="!peer1",
            )

        restored = db_operations.get_zork_save(1234, "zork1")
        self.assertIsNone(restored)

    def test_delete_zork_save_frame_removes_older_local_save(self):
        db_operations.upsert_synced_zork_save("1234", "zork1", b"save-payload", "2026-03-30 12:00:00")
        msg = (
            "DELETE_ZORKSAVE|"
            f"{base64.b64encode(b'1234').decode('ascii')}|"
            f"{base64.b64encode(b'zork1').decode('ascii')}|"
            "2026-03-30 12:05:00"
        )

        message_processing.process_message(
            sender_id=1,
            message=msg,
            interface=_DummyInterface(),
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        self.assertIsNone(db_operations.get_zork_save(1234, "zork1"))

    def test_stale_delete_zork_save_frame_does_not_remove_newer_local_save(self):
        db_operations.upsert_synced_zork_save("1234", "zork1", b"save-payload", "2026-03-30 12:05:00")
        msg = (
            "DELETE_ZORKSAVE|"
            f"{base64.b64encode(b'1234').decode('ascii')}|"
            f"{base64.b64encode(b'zork1').decode('ascii')}|"
            "2026-03-30 12:00:00"
        )

        message_processing.process_message(
            sender_id=1,
            message=msg,
            interface=_DummyInterface(),
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        self.assertEqual(db_operations.get_zork_save(1234, "zork1"), b"save-payload")

    def test_zork_save_equal_timestamp_prefers_larger_payload(self):
        db_operations.upsert_synced_zork_save("1234", "zork1", b"abc", "2026-03-30 12:00:00")
        db_operations.upsert_synced_zork_save("1234", "zork1", b"abcdef", "2026-03-30 12:00:00")

        self.assertEqual(db_operations.get_zork_save(1234, "zork1"), b"abcdef")

    def test_zork_save_tombstone_blocks_stale_restore(self):
        db_operations.record_sync_tombstone_at("zork_saves", "1234:zork1", "2026-03-30 12:05:00")
        db_operations.upsert_synced_zork_save("1234", "zork1", b"older-save", "2026-03-30 12:00:00")

        self.assertIsNone(db_operations.get_zork_save(1234, "zork1"))


if __name__ == "__main__":
    unittest.main()
