import sqlite3
import sys
import types
import unittest
from unittest.mock import patch

if "meshtastic" not in sys.modules:
    meshtastic_stub = types.ModuleType("meshtastic")
    setattr(meshtastic_stub, "BROADCAST_NUM", 0)
    sys.modules["meshtastic"] = meshtastic_stub

import db_operations
import message_processing
from utils import _send_sync_with_cont


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

        self.assertTrue(any(m.startswith("HASHZ|bulletins|") for m in iface.sent_texts))

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

    def test_hashend_pushes_local_only_records_to_peer(self):
        db_operations.add_bulletin(
            "General", "CALL", "Subject", "Body", [], None, unique_id="uid-local-only"
        )
        iface = _DummyInterface()

        # Remote manifest is empty, so peer is missing our local record.
        message_processing.process_message(
            sender_id=1,
            message="HASHEND|bulletins|0",
            interface=iface,
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        self.assertTrue(any(m.startswith("BULLETIN|General|CALL|Subject|Body|uid-local-only") for m in iface.sent_texts))

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

        self.assertTrue(any(m.startswith("HASHZ|channels|") for m in iface.sent_texts))
        self.assertFalse(any(m.startswith("HASHEND|channels|") for m in iface.sent_texts))

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

        self.assertTrue(any(m.startswith("HASHZ|tombstones|") for m in iface.sent_texts))
        self.assertFalse(any(m.startswith("HASHEND|tombstones|") for m in iface.sent_texts))

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

    def test_hashreq_falls_back_to_hashrec_when_compression_disabled(self):
        db_operations.add_bulletin("General", "CALL", "Subject", "Body", [], None, unique_id="uid-z-1")
        iface = _DummyInterface()
        with patch.dict("os.environ", {"BBS_HASH_MANIFEST_COMPRESSION": "0"}):
            message_processing.process_message(
                sender_id=1,
                message="HASHREQ|bulletins",
                interface=iface,
                is_sync_message=True,
                sender_node_id="!peer1",
            )

        self.assertTrue(any(m.startswith("HASHREC|bulletins|") for m in iface.sent_texts))
        self.assertFalse(any(m.startswith("HASHZ|bulletins|") for m in iface.sent_texts))

    def test_hashz_receive_triggers_hashmiss(self):
        import json
        import zlib
        import base64

        iface = _DummyInterface()
        manifest = {"uid-remote-z": "abc123"}
        payload = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
        b64 = base64.urlsafe_b64encode(zlib.compress(payload, level=6)).decode("ascii")
        msg = f"HASHZ|bulletins|mid1|0|1|{b64}"

        message_processing.process_message(
            sender_id=1,
            message=msg,
            interface=iface,
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        self.assertIn("HASHMISS|bulletins|uid-remote-z", iface.sent_texts)

    def test_syncstate_mismatch_triggers_immediate_hash_requests(self):
        iface = _DummyInterface()

        message_processing.process_message(
            sender_id=1,
            message="SYNCSTATE|1|0|0|0|0|0|peer-b-hash|||||",
            interface=iface,
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        self.assertIn("HASHREQ|bulletins", iface.sent_texts)
        self.assertIn("HASHREQ|tombstones", iface.sent_texts)

    def test_replayed_bulletin_continuations_can_heal_existing_partial_record(self):
        content = "X" * 600
        unique_id = "uid-repair-bulletin"
        outbound = _DummyInterface()
        _send_sync_with_cont(
            "BULLETIN|General|CALL|Subject|",
            f"|{unique_id}",
            content,
            unique_id,
            cont_prefix=f"BULLETINCONT|{unique_id}|",
            bbs_nodes=["!peer1"],
            interface=outbound,
            pause_seconds=0,
        )

        delivered = outbound.sent_texts
        for msg in delivered[:2]:
            message_processing.process_message(
                sender_id=1,
                message=msg,
                interface=_DummyInterface(),
                is_sync_message=True,
                sender_node_id="!peer1",
            )

        for msg in delivered:
            message_processing.process_message(
                sender_id=1,
                message=msg,
                interface=_DummyInterface(),
                is_sync_message=True,
                sender_node_id="!peer1",
            )

        row = db_operations.get_bulletin_by_unique_id(unique_id)
        self.assertIsNotNone(row)
        self.assertEqual(row[3], content)

    def test_hashmiss_ttl_can_be_disabled_for_repeated_repair_cycles(self):
        iface = _DummyInterface()

        with patch.dict("os.environ", {"BBS_HASHMISS_REQUEST_TTL_SECONDS": "0"}):
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

        self.assertEqual(iface.sent_texts.count("HASHMISS|bulletins|uid-remote-only"), 2)

    def test_out_of_order_bulletin_continuations_are_buffered_until_gap_closes(self):
        content = "0123456789" * 80
        unique_id = "uid-out-of-order-bulletin"
        outbound = _DummyInterface()
        _send_sync_with_cont(
            "BULLETIN|General|CALL|Subject|",
            f"|{unique_id}",
            content,
            unique_id,
            cont_prefix=f"BULLETINCONT|{unique_id}|",
            bbs_nodes=["!peer1"],
            interface=outbound,
            pause_seconds=0,
        )

        delivered = outbound.sent_texts
        self.assertGreaterEqual(len(delivered), 3)
        reordered = [delivered[0], delivered[2], delivered[1], *delivered[3:]]

        for msg in reordered:
            message_processing.process_message(
                sender_id=1,
                message=msg,
                interface=_DummyInterface(),
                is_sync_message=True,
                sender_node_id="!peer1",
            )

        row = db_operations.get_bulletin_by_unique_id(unique_id)
        self.assertIsNotNone(row)
        self.assertEqual(row[3], content)

    def test_bulletin_continuation_before_base_record_is_buffered(self):
        content = "ABCDEFGH" * 90
        unique_id = "uid-early-cont-bulletin"
        outbound = _DummyInterface()
        _send_sync_with_cont(
            "BULLETIN|General|CALL|Subject|",
            f"|{unique_id}",
            content,
            unique_id,
            cont_prefix=f"BULLETINCONT|{unique_id}|",
            bbs_nodes=["!peer1"],
            interface=outbound,
            pause_seconds=0,
        )

        delivered = outbound.sent_texts
        self.assertGreaterEqual(len(delivered), 2)
        reordered = [delivered[1], delivered[0], *delivered[2:]]

        for msg in reordered:
            message_processing.process_message(
                sender_id=1,
                message=msg,
                interface=_DummyInterface(),
                is_sync_message=True,
                sender_node_id="!peer1",
            )

        row = db_operations.get_bulletin_by_unique_id(unique_id)
        self.assertIsNotNone(row)
        self.assertEqual(row[3], content)

    def test_replayed_bulletin_base_packet_repairs_existing_short_prefix(self):
        content = "REPAIR" * 120
        unique_id = "uid-short-prefix-bulletin"
        outbound = _DummyInterface()
        _send_sync_with_cont(
            "BULLETIN|General|CALL|Subject|",
            f"|{unique_id}",
            content,
            unique_id,
            cont_prefix=f"BULLETINCONT|{unique_id}|",
            bbs_nodes=["!peer1"],
            interface=outbound,
            pause_seconds=0,
        )

        first_parts = outbound.sent_texts[0].split("|", 5)
        truncated_prefix = first_parts[4][: max(1, len(first_parts[4]) // 3)]
        db_operations.add_bulletin(
            "General", "CALL", "Subject", truncated_prefix, [], None, unique_id=unique_id
        )

        for msg in outbound.sent_texts:
            message_processing.process_message(
                sender_id=1,
                message=msg,
                interface=_DummyInterface(),
                is_sync_message=True,
                sender_node_id="!peer1",
            )

        row = db_operations.get_bulletin_by_unique_id(unique_id)
        self.assertIsNotNone(row)
        self.assertEqual(row[3], content)


if __name__ == "__main__":
    unittest.main()
