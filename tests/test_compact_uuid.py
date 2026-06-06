"""Tests for the 'cuid' wire capability — compact UUID encoding (PR 6).

The unique_id is the primary dedup / manifest / tombstone key, so the
encode→wire→decode round-trip MUST be lossless and deterministic, returning
the exact canonical lowercase-hyphenated UUID form. Non-UUID ids must pass
through verbatim in both directions.
"""

import base64
import sqlite3
import sys
import types
import unittest
import uuid

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import utils
import db_operations
import message_processing


class CompactUuidRoundTripTests(unittest.TestCase):
    def test_roundtrip_lossless_for_many_random_uuids(self):
        for _ in range(5000):
            canonical = str(uuid.uuid4())
            wire = utils.encode_uid(canonical, use_cuid=True)
            self.assertTrue(wire.startswith('*'))
            self.assertEqual(len(wire), 23)  # '*' + 22 base64 chars
            self.assertNotIn('|', wire)
            self.assertEqual(utils.decode_uid(wire), canonical)

    def test_uuid5_roundtrip(self):
        # Channel-comment ids are uuid5; make sure those round-trip too.
        canonical = str(uuid.uuid5(uuid.NAMESPACE_URL, "chan|sender|date|content|1"))
        wire = utils.encode_uid(canonical, use_cuid=True)
        self.assertEqual(utils.decode_uid(wire), canonical)

    def test_disabled_capability_passes_through(self):
        canonical = str(uuid.uuid4())
        self.assertEqual(utils.encode_uid(canonical, use_cuid=False), canonical)

    def test_non_uuid_passes_through_verbatim(self):
        for bad in ("not-a-uuid", "12345", "comment:xyz", "", "hello world"):
            self.assertEqual(utils.encode_uid(bad, use_cuid=True), bad)

    def test_decode_passes_through_full_uuid(self):
        # A legacy sender sends the full 36-char UUID; decode must not touch it.
        canonical = str(uuid.uuid4())
        self.assertEqual(utils.decode_uid(canonical), canonical)

    def test_decode_passes_through_non_uuid(self):
        for tok in ("comment:abc", "12345", "", "plain"):
            self.assertEqual(utils.decode_uid(tok), tok)

    def test_decode_malformed_sentinel_left_intact(self):
        # A '*' followed by garbage must not raise or corrupt — return as-is.
        self.assertEqual(utils.decode_uid("*!!!notbase64!!!"), "*!!!notbase64!!!")

    def test_canonical_form_is_lowercase_hyphenated(self):
        # Even if an uppercase UUID is encoded, decode yields canonical lowercase.
        up = str(uuid.uuid4()).upper()
        wire = utils.encode_uid(up, use_cuid=True)
        self.assertEqual(utils.decode_uid(wire), up.lower())

    def test_savings(self):
        canonical = str(uuid.uuid4())
        wire = utils.encode_uid(canonical, use_cuid=True)
        self.assertEqual(len(canonical) - len(wire), 13)


class CompactUuidReceivePathTests(unittest.TestCase):
    """End-to-end: a BULLETINCONT carrying a compact uid must append to the
    record stored under the canonical uid."""

    def setUp(self):
        conn = sqlite3.connect(":memory:")
        db_operations.thread_local.connection = conn
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_bulletincont_with_compact_uid_appends_to_canonical_record(self):
        canonical = str(uuid.uuid4())
        # Base record stored under canonical uid, with a known expected length so
        # the continuation has somewhere to land.
        full_text = "AAAA" + "B" * 50
        db_operations.add_bulletin(
            "General", "CALL", "Subj", "AAAA", [], None, unique_id=canonical
        )
        db_operations.apply_bulletin_expected_content_length(canonical, len(full_text))

        compact = utils.encode_uid(canonical, use_cuid=True)
        self.assertTrue(compact.startswith("*"))

        # Feed a compact-uid continuation through the real receive handler.
        message_processing.process_message(
            sender_id=1,
            message=f"BULLETINCONT|{compact}|4|{'B' * 50}",
            interface=types.SimpleNamespace(sent_texts=[], bbs_nodes=[]),
            is_sync_message=True,
            sender_node_id="!peer1",
        )

        row = db_operations.get_bulletin_by_unique_id(canonical)
        self.assertIsNotNone(row)
        self.assertEqual(row[4], full_text)  # index 4 = content


if __name__ == "__main__":
    unittest.main()
