import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import db_operations


class SyncStateHashingTests(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(":memory:")
        db_operations.thread_local.connection = conn
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_local_counts_include_compact_hashes(self):
        counts = db_operations.get_local_record_counts()
        for key in [
            "bulletins_hash",
            "mail_hash",
            "channels_hash",
            "zork_saves_hash",
            "profiles_hash",
            "game_scores_hash",
        ]:
            self.assertIn(key, counts)
            self.assertIsInstance(counts[key], str)
            self.assertGreaterEqual(len(counts[key]), 10)

    def test_hash_mismatch_is_detected_even_when_counts_match(self):
        counts = db_operations.get_local_record_counts()
        db_operations.upsert_peer_sync_state(
            peer_node_id="!peer1",
            bulletins=counts["bulletins"],
            mail=counts["mail"],
            channels=counts["channels"],
            zork_saves=counts["zork_saves"],
            profiles=counts["profiles"],
            game_scores=counts["game_scores"],
            bulletins_hash="tampered-hash",
            mail_hash=counts["mail_hash"],
            channels_hash=counts["channels_hash"],
            zork_saves_hash=counts["zork_saves_hash"],
            profiles_hash=counts["profiles_hash"],
            game_scores_hash=counts["game_scores_hash"],
        )

        mismatched = db_operations.get_mismatched_peer_nodes({"!peer1"})
        self.assertIn("!peer1", mismatched)

    def test_mismatch_scopes_reports_only_affected_scope(self):
        counts = db_operations.get_local_record_counts()
        db_operations.upsert_peer_sync_state(
            peer_node_id="!peer1",
            bulletins=counts["bulletins"],
            mail=counts["mail"],
            channels=counts["channels"],
            zork_saves=counts["zork_saves"],
            profiles=counts["profiles"],
            game_scores=counts["game_scores"],
            bulletins_hash=counts["bulletins_hash"],
            mail_hash="bad-mail-hash",
            channels_hash=counts["channels_hash"],
            zork_saves_hash=counts["zork_saves_hash"],
            profiles_hash=counts["profiles_hash"],
            game_scores_hash=counts["game_scores_hash"],
        )

        by_peer = db_operations.get_mismatched_peer_scopes({"!peer1"})
        self.assertIn("!peer1", by_peer)
        self.assertIn("mail", by_peer["!peer1"])
        self.assertIn("tombstones", by_peer["!peer1"])
        self.assertNotIn("channels", by_peer["!peer1"])

    def test_get_db_connection_uses_bbs_db_path_env(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "custom-bulletins.db")
            with mock.patch.dict(os.environ, {"BBS_DB_PATH": db_path}, clear=False):
                conn = db_operations.get_db_connection()
                conn.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY)")
                conn.commit()
                conn.close()
                del db_operations.thread_local.connection

            self.assertTrue(os.path.exists(db_path))

        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_game_score_equal_score_merge_is_deterministic(self):
        db_operations.upsert_synced_game_score(
            user_id="u1",
            game_id="zork1",
            short_name="ZED",
            score=100,
            max_score=100,
            moves=35,
            achieved_at="2026-03-31 12:00:00",
        )
        db_operations.upsert_synced_game_score(
            user_id="u1",
            game_id="zork1",
            short_name="ALF",
            score=100,
            max_score=120,
            moves=20,
            achieved_at="2026-03-31 11:30:00",
        )

        row = db_operations.get_game_score_by_user_and_game("u1", "zork1")
        self.assertIsNotNone(row)
        self.assertEqual(row[2], "ALF")
        self.assertEqual(row[3], 100)
        self.assertEqual(row[4], 120)
        self.assertEqual(row[5], 20)
        self.assertEqual(row[6], "2026-03-31 11:30:00")

    def test_stale_peer_syncstate_is_ignored_for_mismatch(self):
        counts = db_operations.get_local_record_counts()
        db_operations.upsert_peer_sync_state(
            peer_node_id="!peer1",
            bulletins=counts["bulletins"] + 99,
            mail=counts["mail"],
            channels=counts["channels"],
            zork_saves=counts["zork_saves"],
            profiles=counts["profiles"],
            game_scores=counts["game_scores"],
            bulletins_hash="stale-hash",
            mail_hash=counts["mail_hash"],
            channels_hash=counts["channels_hash"],
            zork_saves_hash=counts["zork_saves_hash"],
            profiles_hash=counts["profiles_hash"],
            game_scores_hash=counts["game_scores_hash"],
        )

        conn = db_operations.get_db_connection()
        conn.execute(
            "UPDATE peer_sync_state SET reported_at = ? WHERE peer_node_id = ?",
            ("2000-01-01 00:00:00", "!peer1"),
        )
        conn.commit()

        with mock.patch.dict(os.environ, {"BBS_SYNCSTATE_MAX_AGE_SECONDS": "60"}, clear=False):
            mismatched = db_operations.get_mismatched_peer_nodes({"!peer1"})
            self.assertNotIn("!peer1", mismatched)


if __name__ == "__main__":
    unittest.main()
