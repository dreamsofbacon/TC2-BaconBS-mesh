import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    meshtastic_stub = types.ModuleType("meshtastic")
    setattr(meshtastic_stub, "BROADCAST_NUM", 0)
    sys.modules["meshtastic"] = meshtastic_stub

import db_operations
import message_processing


class _DummyInterface:
    def __init__(self):
        self.sent_texts = []
        self.bbs_nodes = []

    def sendText(self, text, destinationId, wantAck, wantResponse):
        self.sent_texts.append(text)


class HashRepairProtocolTests(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(":memory:")
        db_operations.thread_local.connection = conn
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_hashreq_emits_manifest_records_and_end_marker(self):
        unique_id = db_operations.add_bulletin(
            "General", "CALL", "Subject", "Body", [], None, unique_id="uid-hash-req"
        )
        iface = _DummyInterface()

        message_processing.process_message(
            sender_id=1,
            message="HASHREQ|bulletins",
            interface=iface,
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        self.assertTrue(any(m.startswith(f"HASHREC|bulletins|{unique_id}|") for m in iface.sent_texts))
        self.assertIn("HASHEND|bulletins|1", iface.sent_texts)

    def test_hashmiss_resends_requested_bulletin_record(self):
        unique_id = db_operations.add_bulletin(
            "General", "CALL", "Subject", "Body", [], None, unique_id="uid-hash-miss"
        )
        iface = _DummyInterface()

        message_processing.process_message(
            sender_id=1,
            message=f"HASHMISS|bulletins|{unique_id}",
            interface=iface,
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        self.assertTrue(any(m.startswith("BULLETIN|General|CALL|Subject|Body|") for m in iface.sent_texts))

    def test_hashend_requests_missing_records(self):
        iface = _DummyInterface()

        message_processing.process_message(
            sender_id=1,
            message="HASHREC|bulletins|uid-remote-only|abc123",
            interface=iface,
            is_sync_message=True,
            sender_node_id="!peer1",
        )
        message_processing.process_message(
            sender_id=1,
            message="HASHEND|bulletins|1",
            interface=iface,
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        self.assertIn("HASHMISS|bulletins|uid-remote-only", iface.sent_texts)

    def test_hashreq_channels_emits_manifest_and_end(self):
        db_operations.add_channel("Tech", "mesh://tech")
        iface = _DummyInterface()

        message_processing.process_message(
            sender_id=1,
            message="HASHREQ|channels",
            interface=iface,
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        self.assertTrue(any(m.startswith("HASHREC|channels|") for m in iface.sent_texts))
        self.assertIn("HASHEND|channels|1", iface.sent_texts)

    def test_hashmiss_channels_resends_requested_channel(self):
        db_operations.add_channel("Tech", "mesh://tech")
        manifest = db_operations.get_record_hash_manifest("channels")
        self.assertEqual(len(manifest), 1)
        channel_key = next(iter(manifest.keys()))
        iface = _DummyInterface()

        message_processing.process_message(
            sender_id=1,
            message=f"HASHMISS|channels|{channel_key}",
            interface=iface,
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        self.assertIn("CHANNEL|Tech|mesh://tech", iface.sent_texts)

    def test_hashreq_tombstones_emits_manifest_and_end(self):
        db_operations.add_bulletin("General", "CALL", "Subject", "Body", [], None, unique_id="uid-del-a")
        db_operations.delete_bulletin("uid-del-a", [], None)
        iface = _DummyInterface()

        message_processing.process_message(
            sender_id=1,
            message="HASHREQ|tombstones",
            interface=iface,
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        self.assertTrue(any(m.startswith("HASHREC|tombstones|bulletins:uid-del-a|") for m in iface.sent_texts))
        self.assertIn("HASHEND|tombstones|1", iface.sent_texts)

    def test_hashmiss_tombstone_replays_delete(self):
        db_operations.record_sync_tombstone("mail", "uid-del-mail")
        iface = _DummyInterface()

        message_processing.process_message(
            sender_id=1,
            message="HASHMISS|tombstones|mail:uid-del-mail",
            interface=iface,
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        self.assertIn("DELETE_MAIL|uid-del-mail", iface.sent_texts)

    def test_hashend_prefers_tombstone_for_deleted_local_record(self):
        db_operations.record_sync_tombstone("bulletins", "uid-del-b")
        iface = _DummyInterface()

        message_processing.process_message(
            sender_id=1,
            message="HASHREC|bulletins|uid-del-b|abc123",
            interface=iface,
            is_sync_message=True,
            sender_node_id="!peer1",
        )
        message_processing.process_message(
            sender_id=1,
            message="HASHEND|bulletins|1",
            interface=iface,
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        self.assertIn("HASHMISS|tombstones|bulletins:uid-del-b", iface.sent_texts)


if __name__ == "__main__":
    unittest.main()
