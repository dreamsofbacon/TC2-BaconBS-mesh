"""The cached connection must follow BBS_DB_PATH.

This is the mechanism behind the roaming tests/test_web_admin.py failures.
A thread caches one sqlite connection; before this was fixed the cache had
no idea which file it had opened, so the first connection a thread ever made
won for the rest of the process. A test file that left one open handed it to
every file that ran after it, pointed at a temporary directory that had
already been deleted -- which is why the failures moved between runs, why
they vanished when the file was run alone, and why they looked like a
Windows file-lock artifact rather than the aliasing bug they were.
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db_operations


class ConnectionFollowsThePathTests(unittest.TestCase):
    def setUp(self):
        self.first_dir = tempfile.TemporaryDirectory()
        self.second_dir = tempfile.TemporaryDirectory()
        self.first_db = Path(self.first_dir.name) / "bulletins.db"
        self.second_db = Path(self.second_dir.name) / "bulletins.db"
        # Cleanups run last-registered-first, and the directories cannot be
        # removed on Windows while a connection still holds a file open --
        # the very PermissionError this whole fix is about.
        self.addCleanup(self.second_dir.cleanup)
        self.addCleanup(self.first_dir.cleanup)
        self.addCleanup(self._close_connection)

    def _close_connection(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        if hasattr(db_operations.thread_local, "connection_origin"):
            del db_operations.thread_local.connection_origin

    def _use(self, path):
        return mock.patch.dict(os.environ, {"BBS_DB_PATH": str(path)}, clear=False)

    def test_a_new_path_gets_a_new_connection(self):
        with self._use(self.first_db):
            first = db_operations.get_db_connection()
            first.execute("CREATE TABLE marker (which TEXT)")
            first.execute("INSERT INTO marker VALUES ('first')")
            first.commit()

        with self._use(self.second_db):
            second = db_operations.get_db_connection()
            # The whole bug in one assertion: without the path check this is
            # the same object, still answering from the first database.
            self.assertIsNot(second, first)
            rows = second.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            self.assertEqual(rows, [], "the second database should be empty")

    def test_the_same_path_keeps_the_same_connection(self):
        with self._use(self.first_db):
            first = db_operations.get_db_connection()
            self.assertIs(db_operations.get_db_connection(), first)

    def test_the_old_connection_is_closed_not_abandoned(self):
        with self._use(self.first_db):
            first = db_operations.get_db_connection()
        with self._use(self.second_db):
            db_operations.get_db_connection()
        # An abandoned handle is what keeps a temporary directory from being
        # removed on Windows, so closing it is part of the fix rather than
        # tidiness.
        with self.assertRaises(sqlite3.ProgrammingError):
            first.execute("SELECT 1")

    def test_an_injected_connection_is_left_alone(self):
        # Many test files assign an in-memory connection here on purpose.
        # Reopening over that would be the same bug pointing the other way.
        injected = sqlite3.connect(":memory:")
        self.addCleanup(injected.close)
        db_operations.thread_local.connection = injected
        with self._use(self.first_db):
            self.assertIs(db_operations.get_db_connection(), injected)
        with self._use(self.second_db):
            self.assertIs(db_operations.get_db_connection(), injected)

    def test_a_path_that_differs_only_in_case_is_the_same_file_on_windows(self):
        # os.path.normcase, so a case difference is not read as a new
        # database and does not needlessly drop a live connection.
        with self._use(self.first_db):
            first = db_operations.get_db_connection()
        with self._use(str(self.first_db).upper() if os.name == "nt" else self.first_db):
            self.assertIs(db_operations.get_db_connection(), first)


if __name__ == "__main__":
    unittest.main()
