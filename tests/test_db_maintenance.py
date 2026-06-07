"""Tests for Phase 0 DB retention / SD-card safety.

Verifies the periodic maintenance prunes unbounded diagnostic/log tables and
expired tombstones while NEVER touching content tables, and that op_log pruning
keeps the newest events (hash-repair remains the reconciliation safety net).
"""

import sqlite3
import sys
import types
import unittest
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


if __name__ == "__main__":
    unittest.main()
