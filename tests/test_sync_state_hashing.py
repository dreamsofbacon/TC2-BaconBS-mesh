import sqlite3
import unittest

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


if __name__ == "__main__":
    unittest.main()
