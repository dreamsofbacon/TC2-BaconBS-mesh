import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

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


if __name__ == "__main__":
    unittest.main()
