"""Tests for the GUI-set DB size cap (enforce_db_size_cap).

Over-cap is simulated by patching _db_total_bytes (in-memory DBs have no file).
Verifies the oldest content is tombstone-deleted, the newest keep_floor is kept,
and a cap of 0 disables it.
"""

import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations


class DbSizeCapTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self._orig_total = db_operations._db_total_bytes

    def tearDown(self):
        db_operations._db_total_bytes = self._orig_total
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _count(self, table):
        return int(db_operations.get_db_connection().execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _seed_bulletins(self, n):
        for i in range(n):
            db_operations.add_bulletin(
                "General", "CALL", "subj%d" % i, "body" * 20, [], None,
                unique_id="b%03d" % i, date="2026-01-%02d 00:00" % (i + 1))

    def test_disabled_when_zero(self):
        self._seed_bulletins(30)
        summary = db_operations.enforce_db_size_cap([], None, max_mb=0)
        self.assertFalse(summary["enabled"])
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(self._count("bulletins"), 30)

    def test_under_cap_no_deletes(self):
        self._seed_bulletins(30)
        db_operations._db_total_bytes = lambda: 1024  # tiny, well under 1 MB
        summary = db_operations.enforce_db_size_cap([], None, max_mb=1)
        self.assertTrue(summary["enabled"])
        self.assertFalse(summary["over"])
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(self._count("bulletins"), 30)

    def test_over_cap_deletes_oldest_keeps_floor(self):
        self._seed_bulletins(30)
        db_operations._db_total_bytes = lambda: 100 * 1024 * 1024  # 100 MB, way over
        summary = db_operations.enforce_db_size_cap([], None, max_mb=1, keep_floor=20)
        self.assertTrue(summary["over"])
        # 30 rows, keep newest 20 -> at most 10 are deletable; want is large.
        self.assertEqual(summary["deleted"], 10)
        self.assertEqual(self._count("bulletins"), 20)
        # The 20 newest (b010..b029) survive; the 10 oldest are gone + tombstoned.
        rows = {r[0] for r in db_operations.get_db_connection().execute(
            "SELECT unique_id FROM bulletins")}
        self.assertIn("b029", rows)
        self.assertNotIn("b000", rows)
        self.assertTrue(db_operations.has_sync_tombstone("bulletins", "b000"))

    def test_keep_floor_prevents_emptying(self):
        self._seed_bulletins(5)
        db_operations._db_total_bytes = lambda: 100 * 1024 * 1024
        summary = db_operations.enforce_db_size_cap([], None, max_mb=1, keep_floor=20)
        # Fewer rows than the floor -> nothing deleted.
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(self._count("bulletins"), 5)

    def test_bounded_per_pass(self):
        self._seed_bulletins(200)
        db_operations._db_total_bytes = lambda: 500 * 1024 * 1024
        summary = db_operations.enforce_db_size_cap(
            [], None, max_mb=1, keep_floor=20, max_deletes=25)
        self.assertEqual(summary["deleted"], 25)  # capped per pass
        self.assertEqual(self._count("bulletins"), 175)


if __name__ == "__main__":
    unittest.main()
