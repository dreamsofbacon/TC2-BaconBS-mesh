import sqlite3
import unittest

import db_operations


class ChannelDedupeTests(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(":memory:")
        db_operations.thread_local.connection = conn

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_add_channel_is_idempotent_by_name_and_url(self):
        db_operations.initialize_database()

        db_operations.add_channel("General", "http://example.com")
        db_operations.add_channel("General", "http://example.com")

        conn = db_operations.get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM channels WHERE name = ? AND url = ?", ("General", "http://example.com"))
        self.assertEqual(c.fetchone()[0], 1)

    def test_initialize_database_cleans_existing_duplicate_channels(self):
        conn = db_operations.get_db_connection()
        c = conn.cursor()

        c.execute(
            """
            CREATE TABLE channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE channel_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                sender_short_name TEXT NOT NULL,
                date TEXT NOT NULL,
                content TEXT NOT NULL
            )
            """
        )
        c.execute("INSERT INTO channels (name, url) VALUES (?, ?)", ("Tech", "mesh://room"))
        keeper_id = c.lastrowid
        c.execute("INSERT INTO channels (name, url) VALUES (?, ?)", ("Tech", "mesh://room"))
        dup_id = c.lastrowid
        c.execute(
            "INSERT INTO channel_comments (channel_id, sender_short_name, date, content) VALUES (?, ?, ?, ?)",
            (dup_id, "N1", "2026-01-01", "hello"),
        )
        conn.commit()

        db_operations.initialize_database()

        c.execute("SELECT id FROM channels WHERE name = ? AND url = ?", ("Tech", "mesh://room"))
        rows = c.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], keeper_id)

        c.execute("SELECT channel_id FROM channel_comments")
        comment_channel_ids = [row[0] for row in c.fetchall()]
        self.assertEqual(comment_channel_ids, [keeper_id])


if __name__ == "__main__":
    unittest.main()
