"""Database adapter tests for Fragment Assembly integrity and repair state."""

import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations


class FragmentRecordIntegrityTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        db_operations._pending_bulletin_continuations.clear()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _add_bulletin(self, uid, content):
        db_operations.add_bulletin(
            "General", "CALL", "Subject", content, [], None, unique_id=uid,
        )

    def _row(self, uid):
        return db_operations.get_db_connection().execute(
            "SELECT content, expected_content_length, content_complete "
            "FROM bulletins WHERE unique_id = ?", (uid,),
        ).fetchone()

    def test_matching_overlap_extends_the_verified_prefix(self):
        self._add_bulletin("uid-match", "ABCDE")
        db_operations.apply_bulletin_expected_content_length("uid-match", 7)

        db_operations.append_bulletin_content("uid-match", 3, "DEFG")

        self.assertEqual(self._row("uid-match"), ("ABCDEFG", 7, 1))

    def test_conflicting_continuation_never_overwrites_and_marks_for_repair(self):
        self._add_bulletin("uid-conflict", "ABCDE")

        db_operations.append_bulletin_content("uid-conflict", 3, "XX")
        # A replayed META frame must not erase the conflict state merely
        # because the retained prefix happens to match the declared length.
        db_operations.apply_bulletin_expected_content_length("uid-conflict", 5)

        self.assertEqual(self._row("uid-conflict"), ("ABCDE", 5, 0))
        self.assertIn(
            "uid-conflict",
            db_operations.get_incomplete_record_uids()["bulletins"],
        )

    def test_conflicting_base_replay_never_replaces_accepted_content(self):
        self._add_bulletin("uid-base-conflict", "ABCDE")

        self._add_bulletin("uid-base-conflict", "ABXDE")

        self.assertEqual(self._row("uid-base-conflict"), ("ABCDE", 5, 0))

    def test_emoji_continuation_uses_unicode_character_offset(self):
        self._add_bulletin("uid-emoji", "A🙂B")
        db_operations.apply_bulletin_expected_content_length("uid-emoji", 4)

        db_operations.append_bulletin_content("uid-emoji", 3, "C")

        self.assertEqual(self._row("uid-emoji"), ("A🙂BC", 4, 1))

    def test_conflicting_buffered_fragment_cannot_replace_an_earlier_fragment(self):
        self.assertEqual(
            db_operations.append_bulletin_content("uid-buffered", 5, "FGH"),
            "gap",
        )
        self.assertEqual(
            db_operations.append_bulletin_content("uid-buffered", 5, "XYZ"),
            "conflict",
        )

        self._add_bulletin("uid-buffered", "ABCDE")
        db_operations.flush_pending_bulletin_continuations("uid-buffered")

        self.assertEqual(self._row("uid-buffered"), ("ABCDEFGH", 8, 0))


if __name__ == "__main__":
    unittest.main()
