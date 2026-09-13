"""The storage cap reclaims space. It never deletes anyone's posts.

It used to. Over the cap, it deleted the oldest bulletins, mail and channel
comments through the synced delete path, so one node's limit set retention for
the whole fleet. Measured on the live node, that design was worse than blunt:

  * it measured the FILE, and 63% of a 30 MB file was empty pages;
  * content held almost none of the bytes, so deleting it could not get the
    file under the cap -- it would keep going down to the 20-per-board floor;
  * every deletion propagated, so a small node's cap erased posts on nodes
    with gigabytes free.

These tests pin the replacement: reclaim empty pages, trim this node's own
diagnostic logs, and stop. Content survives however far over the cap a node
is, and no tombstone or delete frame is ever produced.

Over-cap is simulated by patching _db_total_bytes, since an in-memory
database has no file on disk.
"""
import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations

WAY_OVER = 500 * 1024 * 1024


class _Case(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def count(self, table):
        return int(db_operations.get_db_connection().execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def seed_content(self, n=30):
        for i in range(n):
            db_operations.add_bulletin(
                "General", "CALL", "subj%d" % i, "body" * 20, [], None,
                unique_id="b%03d" % i, date="2026-01-%02d 00:00" % (i % 28 + 1))

    def seed_log(self, table, n):
        conn = db_operations.get_db_connection()
        if table == "connection_events":
            conn.executemany(
                "INSERT INTO connection_events (event_time, message_type, event_text)"
                " VALUES (?, ?, ?)",
                [("2026-01-01T00:00:00", "rx", "event %d" % i) for i in range(n)])
        elif table == "sync_session_history":
            conn.executemany(
                "INSERT INTO sync_session_history (peer_node_id, started_at)"
                " VALUES (?, ?)", [("peer", "2026-01-01") for _ in range(n)])
        else:
            db_operations._ensure_sync_transmissions_table()
            conn.executemany(
                "INSERT INTO sync_transmissions (transmission_time, frame_type,"
                " destination_node_id, frame_size_bytes, direction, frame_text)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [("2026-01-01", "X", "peer", 10, "tx", "frame") for _ in range(n)])
        conn.commit()

    def over_cap(self, max_mb=1, size=WAY_OVER):
        with mock.patch.object(db_operations, "_db_total_bytes", lambda: size):
            return db_operations.enforce_db_size_cap(max_mb=max_mb)


class DisabledTests(_Case):
    def test_a_cap_of_zero_does_nothing(self):
        self.seed_content()
        summary = db_operations.enforce_db_size_cap(max_mb=0)
        self.assertFalse(summary["enabled"])
        self.assertEqual(self.count("bulletins"), 30)

    def test_under_the_cap_nothing_happens(self):
        self.seed_content()
        self.seed_log("connection_events", 2000)
        with mock.patch.object(db_operations, "_db_total_bytes", lambda: 1024):
            summary = db_operations.enforce_db_size_cap(max_mb=1)
        self.assertFalse(summary["over"])
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(self.count("connection_events"), 2000)


class ContentIsNeverDeletedTests(_Case):
    """The whole point of the change."""

    def test_bulletins_survive_any_amount_over_the_cap(self):
        self.seed_content(30)
        summary = self.over_cap()
        self.assertTrue(summary["over"])
        self.assertEqual(self.count("bulletins"), 30)
        self.assertEqual(summary["content_deleted"], 0)

    def test_mail_and_comments_survive_too(self):
        db_operations.add_mail("!a", "A", "!b", "subject", "body", [], None,
                               unique_id="m1")
        channel_id = db_operations.add_channel("Topic", "desc")
        db_operations.add_channel_comment(channel_id, "A", "a reply", [], None)
        mail, comments = self.count("mail"), self.count("channel_comments")
        self.over_cap()
        self.assertEqual(self.count("mail"), mail)
        self.assertEqual(self.count("channel_comments"), comments)

    def test_no_tombstone_is_ever_written(self):
        """A tombstone is what carries a deletion to every other node."""
        self.seed_content(30)
        self.over_cap()
        self.assertEqual(self.count("deleted_sync_tombstones"), 0)

    def test_nothing_is_sent_to_any_peer(self):
        """The old version took bbs_nodes and an interface precisely so it
        could broadcast deletions. The new one has nothing to say."""
        self.seed_content(30)
        with mock.patch("utils.send_message") as sent, \
             mock.patch("utils._send_one_sync") as synced:
            self.over_cap()
        sent.assert_not_called()
        synced.assert_not_called()


class ReclaimingSpaceTests(_Case):
    def test_it_vacuums_before_trimming_anything(self):
        """Empty pages are the cheapest bytes there are: reclaiming them
        deletes nothing."""
        self.seed_log("connection_events", 3000)
        order = []
        with mock.patch.object(db_operations, "vacuum_database",
                               side_effect=lambda: order.append("vacuum") or True), \
             mock.patch.object(db_operations, "_trim_log_table",
                               side_effect=lambda *a: order.append("trim") or 0):
            self.over_cap()
        self.assertEqual(order[0], "vacuum")

    def test_it_stops_as_soon_as_vacuum_is_enough(self):
        self.seed_log("connection_events", 3000)
        sizes = iter([WAY_OVER, WAY_OVER, 1024, 1024])
        with mock.patch.object(db_operations, "_db_total_bytes",
                               lambda: next(sizes, 1024)), \
             mock.patch.object(db_operations, "_db_live_bytes", lambda: 512):
            summary = db_operations.enforce_db_size_cap(max_mb=1)
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(self.count("connection_events"), 3000)

    def test_it_trims_diagnostic_logs_when_vacuum_is_not_enough(self):
        self.seed_log("connection_events", 4000)
        self.seed_log("sync_transmissions", 4000)
        summary = self.over_cap()
        self.assertGreater(summary["deleted"], 0)
        self.assertLess(self.count("connection_events"), 4000)
        self.assertLess(self.count("sync_transmissions"), 4000)

    def test_it_keeps_the_newest_log_rows_not_the_oldest(self):
        self.seed_log("connection_events", 4000)
        newest = db_operations.get_db_connection().execute(
            "SELECT MAX(rowid) FROM connection_events").fetchone()[0]
        self.over_cap()
        kept = {r[0] for r in db_operations.get_db_connection().execute(
            "SELECT rowid FROM connection_events")}
        self.assertIn(newest, kept)

    def test_a_log_is_never_trimmed_below_its_floor(self):
        """A cap small enough to demand it has stopped being about space and
        started erasing the evidence of why the node is full."""
        self.seed_log("connection_events", 800)
        for _ in range(5):
            self.over_cap()
        self.assertGreaterEqual(self.count("connection_events"),
                                db_operations._SIZE_CAP_LOG_FLOOR)

    def test_the_sync_op_log_is_left_alone(self):
        """Sync reconciliation reads it; trimming it is not a storage fix."""
        self.assertNotIn("op_log", db_operations._SIZE_CAP_LOG_TABLES)

    def test_still_over_afterwards_says_so(self):
        self.seed_content(30)
        with self.assertLogs(level="WARNING") as logs:
            self.over_cap()
        self.assertTrue(any("never deletes" in line for line in logs.output))


class MeasurementTests(_Case):
    def test_live_bytes_excludes_free_pages(self):
        """The file keeps its high-water mark after a delete. The live figure
        must not, or empty space is counted as data."""
        self.seed_log("connection_events", 20000)
        full = db_operations._db_live_bytes()
        db_operations.get_db_connection().execute("DELETE FROM connection_events")
        db_operations.get_db_connection().commit()
        self.assertLess(db_operations._db_live_bytes(), full)


if __name__ == "__main__":
    unittest.main()
