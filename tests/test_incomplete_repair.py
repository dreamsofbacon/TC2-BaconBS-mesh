"""Tests for last-resort reset of stuck incomplete records.

A record whose continuations arrive with misaligned chunk boundaries can be
left with a permanent middle gap that no single peer's frames ever fill. After
several failed repair cycles the server resets such a record so a fresh,
self-consistent full resend rebuilds it. These tests cover the reset primitive.
"""

import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations


class ResetIncompleteRecordTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        db_operations._pending_bulletin_continuations.clear()
        db_operations._pending_mail_continuations.clear()
        db_operations._pending_channel_comment_continuations.clear()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _insert_incomplete_bulletin(self, uid, content, expected_len):
        conn = db_operations.get_db_connection()
        c = conn.cursor()
        c.execute(
            """INSERT INTO bulletins
               (subject, sender_short_name, date, content, board, unique_id,
                expected_content_length, content_complete)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            ("Subj", "abc", "2026-01-01", content, "General", uid, expected_len),
        )
        conn.commit()

    def test_reset_clears_partial_content_keeps_expected_len(self):
        self._insert_incomplete_bulletin("u1", "partial", 100)
        db_operations._pending_bulletin_continuations["u1"] = [(50, "stuck")]
        ok = db_operations.reset_incomplete_record("bulletins", "u1")
        self.assertTrue(ok)
        conn = db_operations.get_db_connection()
        row = conn.execute(
            "SELECT content, expected_content_length, content_complete "
            "FROM bulletins WHERE unique_id='u1'"
        ).fetchone()
        self.assertEqual(row[0], "")
        self.assertEqual(row[1], 100)
        self.assertEqual(row[2], 0)
        self.assertNotIn("u1", db_operations._pending_bulletin_continuations)

    def test_reset_skips_complete_records(self):
        conn = db_operations.get_db_connection()
        conn.execute(
            """INSERT INTO bulletins
               (subject, sender_short_name, date, content, board, unique_id,
                expected_content_length, content_complete)
               VALUES ('S','a','2026-01-01','full','General','u2',4,1)"""
        )
        conn.commit()
        ok = db_operations.reset_incomplete_record("bulletins", "u2")
        self.assertFalse(ok)
        row = conn.execute("SELECT content FROM bulletins WHERE unique_id='u2'").fetchone()
        self.assertEqual(row[0], "full")

    def test_reset_channel_comment_strips_prefix(self):
        conn = db_operations.get_db_connection()
        conn.execute(
            """INSERT INTO channel_comments
               (channel_id, sender_short_name, date, content, unique_id,
                expected_content_length, content_complete)
               VALUES (1,'a','2026-01-01','part','cc1',80,0)"""
        )
        conn.commit()
        db_operations._pending_channel_comment_continuations["cc1"] = [(10, "x")]
        ok = db_operations.reset_incomplete_record("channels", "comment:cc1")
        self.assertTrue(ok)
        row = conn.execute(
            "SELECT content, content_complete FROM channel_comments WHERE unique_id='cc1'"
        ).fetchone()
        self.assertEqual(row[0], "")
        self.assertEqual(row[1], 0)
        self.assertNotIn("cc1", db_operations._pending_channel_comment_continuations)

    def test_unknown_scope_returns_false(self):
        self.assertFalse(db_operations.reset_incomplete_record("nope", "x"))


if __name__ == "__main__":
    unittest.main()
