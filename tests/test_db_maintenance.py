"""Tests for Phase 0 DB retention / SD-card safety.

Verifies the periodic maintenance prunes unbounded diagnostic/log tables and
expired tombstones while NEVER touching content tables, and that op_log pruning
keeps the newest events (hash-repair remains the reconciliation safety net).
"""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock
from datetime import datetime, timedelta

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations


class DbMaintenanceTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        # Force a known maintenance config (small caps) regardless of config.ini.
        db_operations._cached_maintenance_cfg = {
            'interval_minutes': 60,
            'sync_transmissions_max_rows': 50,
            'op_log_max_rows': 50,
            'sync_session_history_max_rows': 20,
            'tombstone_max_age_days': 30,
            'vacuum_interval_hours': 24,
        }

    def tearDown(self):
        db_operations._cached_maintenance_cfg = None
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _count(self, table):
        return int(db_operations.get_db_connection().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_sync_transmissions_pruned_to_cap(self):
        for i in range(200):
            db_operations.log_sync_transmission(f"SYNCSTATE|{i}", "!peer", 10, direction='tx')
        self.assertGreater(self._count("sync_transmissions"), 50)
        db_operations.prune_old_sync_transmissions(50)
        self.assertEqual(self._count("sync_transmissions"), 50)

    def test_op_log_pruned_keeps_newest(self):
        c = db_operations.get_db_connection().cursor()
        for seq in range(1, 201):
            c.execute(
                "INSERT INTO op_log (origin_node_id, origin_seq, event_id, event_type, scope, "
                "target_uid, payload, prev_event_id, created_at, content_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("!self", seq, f"ev{seq}", "upsert", "bulletins", f"uid{seq}", "{}", "", "2026-01-01 00:00:00", f"h{seq}"),
            )
        db_operations.get_db_connection().commit()
        deleted = db_operations.prune_op_log(50)
        self.assertEqual(deleted, 150)
        self.assertEqual(self._count("op_log"), 50)
        # Newest events retained, oldest gone.
        rows = {r[0] for r in db_operations.get_db_connection().execute("SELECT origin_seq FROM op_log")}
        self.assertIn(200, rows)
        self.assertNotIn(1, rows)

    def test_op_log_state_untouched_by_prune(self):
        c = db_operations.get_db_connection().cursor()
        c.execute("INSERT INTO op_log_state (origin_node_id, next_seq) VALUES (?, ?)", ("!self", 999))
        for seq in range(1, 101):
            c.execute(
                "INSERT INTO op_log (origin_node_id, origin_seq, event_id, event_type, scope, "
                "target_uid, payload, prev_event_id, created_at, content_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("!self", seq, f"e{seq}", "upsert", "mail", f"u{seq}", "{}", "", "2026-01-01 00:00:00", f"h{seq}"),
            )
        db_operations.get_db_connection().commit()
        db_operations.prune_op_log(10)
        # Seq allocator must be preserved so future events keep increasing.
        nxt = db_operations.get_db_connection().execute(
            "SELECT next_seq FROM op_log_state WHERE origin_node_id='!self'").fetchone()[0]
        self.assertEqual(int(nxt), 999)

    def test_expired_tombstones_pruned_with_floor(self):
        conn = db_operations.get_db_connection()
        old = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d %H:%M:%S')
        recent = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute("INSERT INTO deleted_sync_tombstones (tombstone_key, deleted_at) VALUES (?,?)", ("bulletins:old", old))
        conn.execute("INSERT INTO deleted_sync_tombstones (tombstone_key, deleted_at) VALUES (?,?)", ("bulletins:new", recent))
        conn.commit()
        deleted = db_operations.prune_expired_tombstones(30)
        self.assertEqual(deleted, 1)
        keys = {r[0] for r in conn.execute("SELECT tombstone_key FROM deleted_sync_tombstones")}
        self.assertEqual(keys, {"bulletins:new"})  # recent delete still propagatable

    def test_maintenance_does_not_touch_content_tables(self):
        db_operations.add_bulletin("General", "CALL", "subj", "body", [], None, unique_id="keep-1")
        db_operations.add_mail("!a", "CALL", "!b", "subj", "body", [], None, unique_id="keep-2")
        for i in range(100):
            db_operations.log_sync_transmission(f"X|{i}", "!p", 5)
        db_operations.run_db_maintenance(do_vacuum=True)
        self.assertEqual(self._count("bulletins"), 1)
        self.assertEqual(self._count("mail"), 1)
        self.assertLessEqual(self._count("sync_transmissions"), 50)

    def test_run_db_maintenance_summary(self):
        for i in range(120):
            db_operations.log_sync_transmission(f"X|{i}", "!p", 5)
        summary = db_operations.run_db_maintenance(do_vacuum=False)
        self.assertGreaterEqual(summary['sync_transmissions_deleted'], 70)
        self.assertIn('op_log_deleted', summary)
        self.assertFalse(summary['vacuumed'])

    def test_file_connection_uses_wal_and_busy_timeout(self):
        self.tearDown()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "maintenance.db")
            with mock.patch.dict(os.environ, {"BBS_DB_PATH": db_path}, clear=False):
                db_operations.initialize_database()
                conn = db_operations.get_db_connection()
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(
                    conn.execute("PRAGMA busy_timeout").fetchone()[0],
                    db_operations.DB_BUSY_TIMEOUT_MS,
                )
                self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)
                conn.close()
                del db_operations.thread_local.connection

    def test_rollback_db_connection_releases_open_transaction(self):
        conn = db_operations.get_db_connection()
        conn.execute("INSERT INTO sync_transmissions "
                     "(transmission_time, frame_type, direction) VALUES (?, ?, ?)",
                     ("2026-01-01 00:00:00", "TEST", "rx"))
        self.assertTrue(conn.in_transaction)
        self.assertTrue(db_operations.rollback_db_connection())
        self.assertFalse(conn.in_transaction)
        self.assertFalse(db_operations.rollback_db_connection())
        self.assertEqual(self._count("sync_transmissions"), 0)

    def test_checkpoint_skips_current_open_transaction(self):
        conn = db_operations.get_db_connection()
        conn.execute("INSERT INTO sync_transmissions "
                     "(transmission_time, frame_type, direction) VALUES (?, ?, ?)",
                     ("2026-01-01 00:00:00", "TEST", "rx"))
        self.assertIsNone(db_operations.checkpoint_wal())
        self.assertTrue(conn.in_transaction)
        conn.rollback()

    def test_passive_checkpoint_coexists_with_file_writer(self):
        self.tearDown()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "checkpoint.db")
            with mock.patch.dict(os.environ, {"BBS_DB_PATH": db_path}, clear=False):
                db_operations.initialize_database()
                maintenance_conn = db_operations.get_db_connection()
                writer_conn = sqlite3.connect(db_path, timeout=1)
                try:
                    writer_conn.execute(
                        "INSERT INTO sync_transmissions "
                        "(transmission_time, frame_type, direction) VALUES (?, ?, ?)",
                        ("2026-01-01 00:00:00", "TEST", "rx"),
                    )
                    result = db_operations.checkpoint_wal()
                    self.assertIsNotNone(result)
                    self.assertEqual(len(result), 3)
                finally:
                    writer_conn.rollback()
                    writer_conn.close()
                    maintenance_conn.close()
                    del db_operations.thread_local.connection

    def test_sync_transmission_failure_rolls_back(self):
        failing_cursor = mock.Mock()
        failing_cursor.execute.side_effect = sqlite3.OperationalError("database is locked")
        failing_conn = mock.Mock()
        failing_conn.cursor.return_value = failing_cursor
        with mock.patch.object(db_operations, "_ensure_sync_transmissions_table"), \
                mock.patch.object(db_operations, "get_db_connection", return_value=failing_conn), \
                mock.patch.object(db_operations, "rollback_db_connection") as rollback:
            db_operations.log_sync_transmission("SYNCSTATE|1", "!peer", 11, direction="rx")
        rollback.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()


class VacuumScheduleSurvivesRestartsTests(unittest.TestCase):
    """VACUUM has to actually happen on a node that gets redeployed.

    The schedule was measured from process start, and a fleet deploy restarts
    the process. The live node had been redeployed well inside every 24-hour
    window, logged zero VACUUMs in 14 days, and was carrying a file that was
    63% empty pages. The last successful VACUUM is stored in the database now,
    so the schedule outlives the process.
    """

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_a_node_that_has_never_vacuumed_does_so_soon(self):
        now = 1_000_000.0
        due = db_operations.next_vacuum_due(24, now=now, settle_seconds=600)
        self.assertEqual(due, now + 600)

    def test_a_recent_vacuum_is_remembered_across_a_restart(self):
        """The regression itself: a restart must not reset the clock."""
        db_operations.record_vacuum(epoch=1_000_000.0)
        due = db_operations.next_vacuum_due(24, now=1_000_000.0 + 3600)
        self.assertEqual(due, 1_000_000.0 + 24 * 3600)

    def test_an_overdue_vacuum_is_not_deferred_another_full_day(self):
        db_operations.record_vacuum(epoch=1_000_000.0)
        now = 1_000_000.0 + 30 * 3600
        due = db_operations.next_vacuum_due(24, now=now, settle_seconds=600)
        self.assertEqual(due, now + 600)

    def test_a_successful_vacuum_is_recorded(self):
        self.assertIsNone(db_operations.get_last_vacuum_epoch())
        self.assertTrue(db_operations.vacuum_database())
        self.assertIsNotNone(db_operations.get_last_vacuum_epoch())

    def test_a_failed_vacuum_reports_failure_and_records_nothing(self):
        """It used to report vacuumed=True whether or not VACUUM worked, so a
        node failing every day would have looked healthy in its logs."""
        conn = db_operations.get_db_connection()
        conn.execute("BEGIN")  # VACUUM cannot run inside a transaction
        try:
            self.assertFalse(db_operations.vacuum_database())
        finally:
            conn.rollback()
        self.assertIsNone(db_operations.get_last_vacuum_epoch())

    def test_maintenance_reports_what_vacuum_actually_did(self):
        with mock.patch.object(db_operations, "vacuum_database", return_value=False):
            summary = db_operations.run_db_maintenance(do_vacuum=True)
        self.assertFalse(summary["vacuumed"])


class OverdueVacuumReachesTheFirstPassTests(unittest.TestCase):
    """VACUUM is only considered on a maintenance pass, and the first pass is
    five minutes after start. An overdue VACUUM therefore has to be due before
    that pass, or it slips to the next one an hour later -- and a node that is
    redeployed more often than hourly never vacuums at all, which is the bug
    this whole change exists to fix.

    Read from server.py's source because the scheduling lives inside its main
    loop rather than a function a test can call.
    """

    def test_an_overdue_vacuum_is_due_before_the_first_maintenance_pass(self):
        import re
        from pathlib import Path
        source = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
        first_pass = float(re.search(
            r"next_maintenance = time\.time\(\) \+ ([\d.]+)", source).group(1))
        settle = float(re.search(r"settle_seconds=([\d.]+)", source).group(1))
        self.assertLess(settle, first_pass)
