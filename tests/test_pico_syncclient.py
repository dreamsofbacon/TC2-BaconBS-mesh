"""End-to-end host tests for the Pico SyncClient (discovery + records + cache)."""

import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pico_node"))

import store
import syncclient


class SyncClientTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = store.CacheStore(self.dir)
        self.client = syncclient.SyncClient(self.store)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_have_produces_want(self):
        out = self.client.handle_frame("HAVE|!gw|bulletins:3")
        self.assertEqual(out, ["WANT|bulletins|!gw|1"])

    def test_event_produces_hashmiss_then_record_stored(self):
        out = self.client.handle_frame("EVENT|bulletins|!gw|1|upsert|uidA")
        self.assertEqual(out, ["HASHMISS|bulletins|uidA"])
        self.assertEqual(self.store.get_watermark("!gw", "bulletins"), 1)
        # gateway replies with the record frame
        out2 = self.client.handle_frame("BULLETIN|General|Bob|Hi|hello world|uidA|2026-06-08 14:30")
        self.assertEqual(out2, [])
        rows = self.store.get("bulletins")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "hello world")
        self.assertEqual(rows[0]["subject"], "Hi")

    def test_multipacket_record_via_client(self):
        self.client.handle_frame("EVENT|bulletins|!gw|2|upsert|uidM")
        self.client.handle_frame("BULLETIN|General|Bob|Hi|AAAAA|uidM|2026-06-08 14:30")
        self.client.handle_frame("BULLETINMETA|uidM|15")
        self.client.handle_frame("BULLETINCONT|uidM|5|BBBBB")
        self.client.handle_frame("BULLETINCONT|uidM|10|CCCCC")
        rows = self.store.get("bulletins")
        self.assertEqual(rows[0]["content"], "AAAAABBBBBCCCCC")
        # once stored complete, it's no longer outstanding
        self.assertEqual(self.client.outstanding_hashmiss(), [])

    def test_conflicting_overlap_preserves_cache_and_requests_full_record(self):
        self.client.handle_frame("EVENT|bulletins|!gw|3|upsert|uidC")
        self.client.handle_frame(
            "BULLETIN|General|Bob|Hi|ABCDE|uidC|2026-06-08 14:30")
        self.client.handle_frame("BULLETINMETA|uidC|10")

        repair = self.client.handle_frame("BULLETINCONT|uidC|3|XX")

        self.assertEqual(repair, ["HASHMISS|bulletins|uidC"])
        self.assertEqual(self.store.get("bulletins")[0]["content"], "ABCDE")
        self.assertEqual(self.client.outstanding_hashmiss(),
                         ["HASHMISS|bulletins|uidC"])

        # The requested full replay starts a clean generation and clears the
        # repair only after continuous coverage reaches the declared length.
        self.client.handle_frame(
            "BULLETIN|General|Bob|Hi|VWXYZ|uidC|2026-06-08 14:30")
        self.assertEqual(self.client.outstanding_hashmiss(),
                         ["HASHMISS|bulletins|uidC"])
        self.client.handle_frame("BULLETINMETA|uidC|10")
        self.client.handle_frame("BULLETINCONT|uidC|5|12345")

        self.assertEqual(self.store.get("bulletins")[0]["content"], "VWXYZ12345")
        self.assertEqual(self.client.outstanding_hashmiss(), [])

    def test_delete_frame_removes_record(self):
        self.client.handle_frame("BULLETIN|General|Bob|Hi|body|uidD|2026-06-08 14:30")
        self.assertEqual(self.store.count("bulletins"), 1)
        self.client.handle_frame("DELETE_BULLETIN|uidD")
        self.assertEqual(self.store.count("bulletins"), 0)

    def test_outstanding_hashmiss_until_record_arrives(self):
        self.client.handle_frame("EVENT|mail|!gw|1|upsert|m1")
        self.assertEqual(self.client.outstanding_hashmiss(), ["HASHMISS|mail|m1"])
        self.client.handle_frame("MAIL|!s|Bob|!r|Subj|body|m1|2026-06-08 14:30")
        self.assertEqual(self.client.outstanding_hashmiss(), [])

    def test_channel_comment_roundtrip(self):
        import binascii
        sender = binascii.b2a_base64(b"Carol").decode().strip()
        self.client.handle_frame("EVENT|channel_comments|!gw|1|upsert|cc1")
        self.client.handle_frame(
            "CHANNELCOMMENT|chankey|%s|2026-06-08 14:30|nice|cc1" % sender)
        rows = self.store.get("channels")
        self.assertEqual(rows[0]["sender"], "Carol")
        self.assertEqual(rows[0]["content"], "nice")

    def test_event_for_present_record_no_hashmiss(self):
        self.store.upsert("bulletins", {"unique_id": "u1", "date": "2026-01-01"})
        out = self.client.handle_frame("EVENT|bulletins|!gw|5|upsert|u1")
        self.assertEqual(out, [])
        self.assertEqual(self.store.get_watermark("!gw", "bulletins"), 5)

    def test_persistence_after_sync(self):
        self.client.handle_frame("BULLETIN|General|Bob|Hi|body|uidP|2026-06-08 14:30")
        self.store.set_watermark("!gw", "bulletins", 9)
        self.store.save()
        reloaded = store.CacheStore(self.dir).load()
        self.assertEqual(reloaded.count("bulletins"), 1)
        self.assertEqual(reloaded.get_watermark("!gw", "bulletins"), 9)


if __name__ == "__main__":
    unittest.main()
