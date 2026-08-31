import gc
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import db_operations


class ConnectionEventLatencyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "events.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE connection_events (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       event_time TEXT NOT NULL,
                       sender_num TEXT,
                       sender_node_id TEXT,
                       sender_short_name TEXT,
                       to_id TEXT,
                       message_type TEXT NOT NULL,
                       event_text TEXT NOT NULL
                   )"""
            )

    def test_connection_event_does_not_wait_for_busy_database(self):
        blocker = sqlite3.connect(self.db_path)
        try:
            blocker.execute("BEGIN EXCLUSIVE")
            blocker.execute(
                "INSERT INTO connection_events (event_time, message_type, event_text) VALUES ('now', 'test', 'lock')"
            )

            started = time.monotonic()
            with patch.object(db_operations, "get_database_path", return_value=str(self.db_path)):
                db_operations.log_connection_event(1, "!node", "NODE", 2, "user", "received")

            self.assertLess(time.monotonic() - started, 0.1)
        finally:
            blocker.rollback()
            blocker.close()
            gc.collect()


if __name__ == "__main__":
    unittest.main()