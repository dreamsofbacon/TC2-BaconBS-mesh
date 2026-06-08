"""Host tests for the Pico op_log discovery client (HAVE/WANT/EVENT/HASHMISS)."""

import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pico_node"))

import store
import opsync


class OpSyncTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = store.CacheStore(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_parse_have(self):
        origin, heads = opsync.parse_have("HAVE|!gw|bulletins:5|mail:2|channel_comments:9")
        self.assertEqual(origin, "!gw")
        self.assertEqual(heads, {"bulletins": 5, "mail": 2, "channel_comments": 9})

    def test_parse_have_decodes_scope_codes(self):
        origin, heads = opsync.parse_have("HAVE|!gw|b:5|m:2|C:1")
        self.assertEqual(heads, {"bulletins": 5, "mail": 2, "channel_comments": 1})

    def test_wants_for_have_only_for_behind_scopes(self):
        self.store.set_watermark("!gw", "bulletins", 5)   # caught up
        self.store.set_watermark("!gw", "mail", 1)        # behind (peer has 2)
        wants = opsync.wants_for_have("HAVE|!gw|bulletins:5|mail:2", self.store)
        self.assertEqual(wants, ["WANT|mail|!gw|2"])

    def test_wants_from_zero(self):
        wants = opsync.wants_for_have("HAVE|!gw|bulletins:3", self.store)
        self.assertEqual(wants, ["WANT|bulletins|!gw|1"])

    def test_event_upsert_missing_emits_hashmiss(self):
        hm = opsync.apply_event("EVENT|bulletins|!gw|7|upsert|abc123", self.store)
        self.assertEqual(hm, "HASHMISS|bulletins|abc123")
        self.assertEqual(self.store.get_watermark("!gw", "bulletins"), 7)

    def test_event_upsert_present_no_hashmiss(self):
        self.store.upsert("bulletins", {"unique_id": "abc123", "date": "2026-01-01"})
        hm = opsync.apply_event("EVENT|bulletins|!gw|7|upsert|abc123", self.store)
        self.assertIsNone(hm)
        self.assertEqual(self.store.get_watermark("!gw", "bulletins"), 7)

    def test_event_delete_removes_from_cache(self):
        self.store.upsert("mail", {"unique_id": "m1", "date": "2026-01-01"})
        hm = opsync.apply_event("EVENT|mail|!gw|4|delete|m1", self.store)
        self.assertIsNone(hm)
        self.assertEqual(self.store.count("mail"), 0)
        self.assertEqual(self.store.get_watermark("!gw", "mail"), 4)

    def test_channel_comment_event_maps_to_channels_cache(self):
        hm = opsync.apply_event("EVENT|channel_comments|!gw|2|upsert|cc1", self.store)
        self.assertEqual(hm, "HASHMISS|channel_comments|cc1")

    def test_malformed_event_ignored(self):
        self.assertIsNone(opsync.apply_event("EVENT|bulletins|!gw|notanint|upsert|x", self.store))
        self.assertIsNone(opsync.apply_event("EVENT|bulletins|!gw|1|upsert", self.store))


if __name__ == "__main__":
    unittest.main()
