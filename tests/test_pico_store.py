"""Host tests for the Pico node's bounded local cache (Option B)."""

import os
import sys
import json
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pico_node"))

import store


def _bulletin(uid, date, board="General", subject="s"):
    return {"unique_id": uid, "subject": subject, "sender": "a",
            "date": date, "content": "body", "board": board}


class CacheStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_upsert_get_newest_first(self):
        s = store.CacheStore(self.dir)
        s.upsert("bulletins", _bulletin("u1", "2026-01-01"))
        s.upsert("bulletins", _bulletin("u2", "2026-02-01"))
        rows = s.get("bulletins")
        self.assertEqual([r["unique_id"] for r in rows], ["u2", "u1"])

    def test_upsert_replaces_same_uid(self):
        s = store.CacheStore(self.dir)
        s.upsert("bulletins", _bulletin("u1", "2026-01-01", subject="old"))
        s.upsert("bulletins", _bulletin("u1", "2026-01-01", subject="new"))
        self.assertEqual(s.count("bulletins"), 1)
        self.assertEqual(s.get("bulletins")[0]["subject"], "new")

    def test_board_filter(self):
        s = store.CacheStore(self.dir)
        s.upsert("bulletins", _bulletin("u1", "2026-01-01", board="General"))
        s.upsert("bulletins", _bulletin("u2", "2026-01-02", board="Urgent"))
        self.assertEqual([r["unique_id"] for r in s.get("bulletins", board="Urgent")], ["u2"])

    def test_prune_keeps_newest(self):
        s = store.CacheStore(self.dir, max_records=3)
        for i in range(6):
            s.upsert("bulletins", _bulletin("u%d" % i, "2026-01-%02d" % (i + 1)))
        self.assertEqual(s.count("bulletins"), 3)
        kept = [r["unique_id"] for r in s.get("bulletins")]
        self.assertEqual(kept, ["u5", "u4", "u3"])  # newest three

    def test_delete(self):
        s = store.CacheStore(self.dir)
        s.upsert("mail", {"unique_id": "m1", "date": "2026-01-01"})
        s.delete("mail", "m1")
        self.assertEqual(s.count("mail"), 0)

    def test_persistence_roundtrip(self):
        s = store.CacheStore(self.dir)
        s.upsert("bulletins", _bulletin("u1", "2026-01-01"))
        s.upsert("mail", {"unique_id": "m1", "date": "2026-01-01", "subject": "hi"})
        s.set_watermark("!gw", "bulletins", 42)
        s.save()

        s2 = store.CacheStore(self.dir).load()
        self.assertEqual(s2.count("bulletins"), 1)
        self.assertEqual(s2.count("mail"), 1)
        self.assertEqual(s2.get_watermark("!gw", "bulletins"), 42)

    def test_watermark_monotonic_per_scope(self):
        s = store.CacheStore(self.dir)
        s.set_watermark("!gw", "bulletins", 10)
        s.set_watermark("!gw", "bulletins", 5)  # ignored (older)
        self.assertEqual(s.get_watermark("!gw", "bulletins"), 10)
        s.set_watermark("!gw", "bulletins", 11)
        self.assertEqual(s.get_watermark("!gw", "bulletins"), 11)
        # Different scope is tracked independently.
        self.assertEqual(s.get_watermark("!gw", "mail"), 0)

    def test_load_missing_files_is_empty(self):
        s = store.CacheStore(self.dir).load()
        self.assertEqual(s.count("bulletins"), 0)
        self.assertEqual(s.get_watermark("!x", "bulletins"), 0)

    def test_save_only_writes_dirty_and_creates_dir(self):
        nested = os.path.join(self.dir, "sd", "bbs")
        s = store.CacheStore(nested)
        s.upsert("bulletins", _bulletin("u1", "2026-01-01"))
        s.save()
        self.assertTrue(os.path.exists(os.path.join(nested, "bulletins.json")))
        with open(os.path.join(nested, "bulletins.json")) as f:
            self.assertIn("u1", json.load(f))


if __name__ == "__main__":
    unittest.main()
