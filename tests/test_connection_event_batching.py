"""Connection events are written in batches, not one fsync per log line.

Reported 2026-09-14: both nodes were writing roughly 25 GB a day to their SD
cards. The cause was this log -- every line opened a connection, committed
its own transaction, and ran a prune that rewrote the table for one row.

Events now wait in memory for a full batch or a few seconds, whichever comes
first, and the prune runs once every few hundred rows. The log is the only
thing that trades durability for the card: content keeps synchronous=FULL.
"""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import db_operations


class _Case(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / "events.db")
        patcher = mock.patch.object(db_operations, "get_database_path",
                                    return_value=self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        db_operations.flush_connection_events()
        db_operations._connection_event_buffer.clear()
        db_operations._connection_events_since_prune = 0
        self.addCleanup(self._drain)

    def _drain(self):
        db_operations._connection_event_buffer.clear()
        db_operations._connection_events_since_prune = 0
        db_operations.flush_connection_events()

    def log(self, count=1, text="received"):
        for n in range(count):
            db_operations.log_connection_event(1, "!node", "NODE", 2, "user", f"{text} {n}")

    def rows(self):
        if not Path(self.db_path).exists():
            return 0
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM connection_events").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()


class BatchingTests(_Case):
    def test_a_single_event_waits_for_the_batch(self):
        self.log()
        self.assertEqual(self.rows(), 0, "wrote to disk for one log line")
        self.assertEqual(db_operations.flush_connection_events(), 1)
        self.assertEqual(self.rows(), 1)

    def test_a_full_batch_goes_out_without_waiting(self):
        self.log(db_operations.CONNECTION_EVENT_FLUSH_ROWS)
        self.assertEqual(self.rows(), db_operations.CONNECTION_EVENT_FLUSH_ROWS)

    def test_a_quiet_node_still_writes_within_the_window(self):
        with mock.patch.object(db_operations, "CONNECTION_EVENT_FLUSH_SECONDS", 0.05):
            self.log()
            deadline = time.monotonic() + 5
            while self.rows() == 0 and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertEqual(self.rows(), 1, "a lone event never reached the database")

    def test_nothing_queued_writes_nothing(self):
        self.assertEqual(db_operations.flush_connection_events(), 0)

    def test_the_text_and_fields_survive_the_trip(self):
        db_operations.log_connection_event(7, "!abcd1234", "NODE", 9, "direct", "Accepted direct message")
        db_operations.flush_connection_events()
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT sender_num, sender_node_id, sender_short_name, to_id,"
                " message_type, event_text FROM connection_events").fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("7", "!abcd1234", "NODE", "9", "direct", "Accepted direct message"))

    def test_an_unwritable_database_is_dropped_not_retried(self):
        with mock.patch.object(db_operations, "get_database_path",
                               return_value=str(Path(self.temp_dir.name) / "no" / "such.db")):
            self.log(3)
            db_operations.flush_connection_events()
        self.assertEqual(db_operations._connection_event_buffer, [])

    def test_the_buffer_cannot_grow_without_limit(self):
        with mock.patch.object(db_operations, "CONNECTION_EVENT_MAX_BUFFER", 10), \
             mock.patch.object(db_operations, "CONNECTION_EVENT_FLUSH_ROWS", 10000):
            self.log(25, text="line")
            buffered = list(db_operations._connection_event_buffer)
        self.assertEqual(len(buffered), 10)
        self.assertIn("line 24", buffered[-1][1][-1], "the newest line was dropped")


class PruningTests(_Case):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(db_operations, "_get_max_connection_log_rows",
                                    return_value=10)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_prune_does_not_run_for_every_row(self):
        with mock.patch.object(db_operations, "CONNECTION_EVENT_PRUNE_EVERY", 100):
            self.log(60)
            db_operations.flush_connection_events()
        self.assertEqual(self.rows(), 60, "pruned before the interval was reached")

    def test_it_runs_once_the_interval_is_reached(self):
        """At 100 rows the table is cut back to the cap; the 20 rows written
        after that prune are simply the overshoot the interval buys."""
        with mock.patch.object(db_operations, "CONNECTION_EVENT_PRUNE_EVERY", 100):
            self.log(120)
            db_operations.flush_connection_events()
        self.assertEqual(self.rows(), 30)

    def test_the_cap_is_still_honoured_over_time(self):
        with mock.patch.object(db_operations, "CONNECTION_EVENT_PRUNE_EVERY", 50):
            for _ in range(6):
                self.log(50)
                db_operations.flush_connection_events()
        self.assertLessEqual(self.rows(), 10)


class ShutdownTests(_Case):
    def test_removing_the_handler_writes_what_is_queued(self):
        self.log(2)
        db_operations.remove_connection_log_handler()
        self.assertEqual(self.rows(), 2)


if __name__ == "__main__":
    unittest.main()
