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


if __name__ == "__main__":
    unittest.main()
