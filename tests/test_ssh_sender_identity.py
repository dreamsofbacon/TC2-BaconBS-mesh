"""An SSH account's local number has to outlive the connection.

user_profiles, game_scores and zork_saves are keyed by the numeric sender
id, not the node id. bbs_emulator used to mint a fresh number for every SSH
connection, so a player's Zork save was written under a number nothing would
ever present again: it synced to every peer perfectly and could never be
resumed by the person who made it. The live database still holds two of
those orphans, under 0xE0000003 and 0xE0000006.

A radio has one number for its whole life. An SSH account needs the same.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations


class SenderNumberTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env_patch = mock.patch.dict(
            os.environ,
            {"BBS_DB_PATH": str(Path(self.temp_dir.name) / "bulletins.db")},
            clear=False)
        self.env_patch.start()
        db_operations.initialize_database()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        if hasattr(db_operations.thread_local, "connection_origin"):
            del db_operations.thread_local.connection_origin

    def _account(self, alias):
        account_id = db_operations.create_ssh_account(alias, "hash", "salt")
        self.assertIsNotNone(account_id)
        return account_id

    def test_the_same_account_always_gets_the_same_number(self):
        account_id = self._account("player")
        first = db_operations.get_account_sender_num(account_id)
        self.assertIsNotNone(first)
        for _ in range(5):
            self.assertEqual(db_operations.get_account_sender_num(account_id), first)

    def test_the_stored_number_is_read_back_not_recomputed(self):
        account_id = self._account("player")
        first = db_operations.get_account_sender_num(account_id)
        self._close()
        self.assertEqual(db_operations.get_account_sender_num(account_id), first)

    def test_the_derivation_is_the_same_in_a_fresh_process(self):
        """Deriving with Python's hash() would pass every test above -- it is
        stable within one process and salted between them, so an account
        would get a new number on each boot and its saves would be orphaned
        exactly as before. Only a separate interpreter shows that."""
        import json
        import subprocess
        import textwrap

        script = textwrap.dedent("""
            import os, sys, tempfile, types
            sys.path.insert(0, %r)
            sys.modules.setdefault("meshtastic", types.SimpleNamespace(BROADCAST_NUM=0))
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
                os.environ["BBS_DB_PATH"] = os.path.join(folder, "bulletins.db")
                import db_operations as db
                db.initialize_database()
                db.get_db_connection().execute(
                    "INSERT INTO accounts (account_id, alias, alias_normalized, created_at)"
                    " VALUES ('%s', 'fixed', 'fixed', '2026-01-01')")
                print(db.get_account_sender_num('%s'))
                db.get_db_connection().close()
        """) % (str(Path(__file__).resolve().parent.parent), "a" * 32, "a" * 32)

        numbers = []
        for seed in ("0", "12345"):
            environment = dict(os.environ, PYTHONHASHSEED=seed)
            environment.pop("BBS_DB_PATH", None)
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True,
                text=True, env=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            numbers.append(int(result.stdout.strip().splitlines()[-1]))
        self.assertEqual(numbers[0], numbers[1],
                         "the same account got different numbers in two processes")

    def test_two_accounts_never_share_a_number(self):
        numbers = {db_operations.get_account_sender_num(self._account(f"user{n}"))
                   for n in range(25)}
        self.assertEqual(len(numbers), 25)
        self.assertNotIn(None, numbers)

    def test_a_derivation_collision_still_yields_distinct_numbers(self):
        """Sharing a number would mean sharing a Zork save with a stranger.
        The probe and the unique index both defend this; the index is what
        makes it a guarantee rather than an optimisation."""
        first = self._account("first")
        second = self._account("second")
        taken = db_operations.get_account_sender_num(first)
        # Force the derivation to land on the number already in use.
        with mock.patch.object(
                db_operations.hashlib, "blake2b",
                return_value=types.SimpleNamespace(
                    digest=lambda: (taken - db_operations.SSH_SENDER_NUM_BASE
                                    ).to_bytes(8, "big"))):
            other = db_operations.get_account_sender_num(second)
        self.assertIsNotNone(other)
        self.assertNotEqual(other, taken)

    def test_the_database_refuses_a_duplicate_number(self):
        first = self._account("first")
        second = self._account("second")
        taken = db_operations.get_account_sender_num(first)
        conn = db_operations.get_db_connection()
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE accounts SET sender_num = ? WHERE account_id = ?",
                         (taken, second))

    def test_the_number_avoids_the_emulator_and_broadcast_ranges(self):
        for n in range(10):
            number = db_operations.get_account_sender_num(self._account(f"user{n}"))
            with self.subTest(n=n):
                # Above the emulator's synthetic band, below broadcast.
                self.assertGreaterEqual(number, db_operations.SSH_SENDER_NUM_BASE)
                self.assertLess(
                    number,
                    db_operations.SSH_SENDER_NUM_BASE + db_operations.SSH_SENDER_NUM_SPAN)
                self.assertNotEqual(number, 0xFFFFFFFF)

    def test_an_unknown_account_gets_nothing(self):
        self.assertIsNone(db_operations.get_account_sender_num("no-such-account"))
        self.assertIsNone(db_operations.get_account_sender_num(""))

    def test_a_save_written_in_one_session_is_found_in_the_next(self):
        """The whole point, end to end."""
        account_id = self._account("player")
        first = db_operations.get_account_sender_num(account_id)
        db_operations.upsert_zork_save(first, b"save-bytes", "zork1")

        # A second login, as if the process had restarted in between.
        self._close()
        second = db_operations.get_account_sender_num(account_id)
        self.assertEqual(second, first)
        self.assertEqual(db_operations.get_zork_save(second, "zork1"), b"save-bytes")


class SSHSessionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env_patch = mock.patch.dict(
            os.environ,
            {"BBS_DB_PATH": str(Path(self.temp_dir.name) / "bulletins.db")},
            clear=False)
        self.env_patch.start()
        db_operations.initialize_database()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.env_patch.stop)

    def test_reconnecting_keeps_the_same_sender_id(self):
        import bbs_emulator

        account_id = db_operations.create_ssh_account("player", "hash", "salt")
        first = bbs_emulator.start_ssh_session(account_id, "player")
        second_id = first.sender_id
        bbs_emulator.end_session(first.token)

        again = bbs_emulator.start_ssh_session(account_id, "player")
        self.addCleanup(bbs_emulator.end_session, again.token)
        self.assertEqual(again.sender_id, second_id)
        self.assertEqual(again.sender_node_id, f"ssh:{account_id}")

    def test_different_accounts_get_different_sender_ids(self):
        import bbs_emulator

        one = db_operations.create_ssh_account("alpha", "hash", "salt")
        two = db_operations.create_ssh_account("bravo", "hash", "salt")
        first = bbs_emulator.start_ssh_session(one, "alpha")
        second = bbs_emulator.start_ssh_session(two, "bravo")
        self.addCleanup(bbs_emulator.end_session, first.token)
        self.addCleanup(bbs_emulator.end_session, second.token)
        self.assertNotEqual(first.sender_id, second.sender_id)


if __name__ == "__main__":
    unittest.main()
