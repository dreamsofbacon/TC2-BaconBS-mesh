"""The same save must hash the same on both nodes.

A save made locally is stored by upsert_zork_save as "%Y-%m-%d %H:%M:%S".
The same save received from a peer goes over the wire epoch-encoded and comes
back out of utils.decode_ts_second as "%Y-%m-%dT%H:%M:%S". Same instant, two
strings -- and get_record_hash_manifest hashes the string.

So two nodes holding one identical save disagreed about its hash. Each read
the other as missing it; the peer asked for the record, we sent it, its own
copy compared equal so it kept what it had, its manifest never changed, and
it asked again next cycle. On the live mesh that re-sent one 366-byte save
116 times in three hours and never converged.
"""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import utils


SPACE_FORM = "2026-03-23 21:26:38"
T_FORM = "2026-03-23T21:26:38"
SAVE = b"a zork save"


class TimestampNormalisationTests(unittest.TestCase):
    def test_both_spellings_of_one_instant_agree(self):
        self.assertEqual(db_operations._normalize_zork_timestamp(SPACE_FORM),
                         db_operations._normalize_zork_timestamp(T_FORM))

    def test_the_wire_round_trip_lands_on_the_canonical_form(self):
        """The exact path that produced the drift: encode for a peer that
        supports epoch, decode on the far side, store."""
        wire = utils.encode_ts_second(SPACE_FORM, True)
        self.assertTrue(wire.startswith("s"), wire)
        decoded = utils.decode_ts_second(wire)
        self.assertEqual(decoded, T_FORM, "decode still returns the T form")
        self.assertEqual(db_operations._normalize_zork_timestamp(decoded),
                         SPACE_FORM)

    def test_a_newer_save_wins_regardless_of_spelling(self):
        """Before this, the comparison was a raw string compare, so with
        equal dates the separator decided: 'T' (0x54) beat ' ' (0x20) and an
        older T-form save displaced a newer space-form one."""
        older_t = "2026-03-23T21:26:38"
        newer_space = "2026-03-23 22:00:00"
        self.assertFalse(db_operations._should_replace_zork_save(
            newer_space, SAVE, older_t, b"other"))
        self.assertTrue(db_operations._should_replace_zork_save(
            older_t, SAVE, newer_space, b"other"))

    def test_the_same_instant_is_not_treated_as_newer(self):
        self.assertFalse(db_operations._should_replace_zork_save(
            SPACE_FORM, SAVE, T_FORM, SAVE))

    def test_unparseable_values_are_left_alone(self):
        self.assertEqual(db_operations._normalize_zork_timestamp("garbage"), "garbage")
        self.assertEqual(db_operations._normalize_zork_timestamp(None), "")


class ManifestAgreementTests(unittest.TestCase):
    """Two nodes, one save, two spellings -- one hash."""

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

    def _store_raw(self, updated_at):
        conn = db_operations.get_db_connection()
        conn.execute("DELETE FROM zork_saves")
        conn.execute(
            "INSERT INTO zork_saves (user_id, game_id, save_data, updated_at)"
            " VALUES ('67472072', 'hhgttg', ?, ?)", (SAVE, updated_at))
        conn.commit()
        return db_operations.get_record_hash_manifest('zork_saves')

    def test_a_peer_storing_either_spelling_hashes_identically(self):
        """This is what breaks the loop without waiting for the peer to be
        updated: whichever spelling it holds, the hashes now match."""
        self.assertEqual(self._store_raw(SPACE_FORM), self._store_raw(T_FORM))

    def test_the_key_is_unchanged(self):
        self.assertEqual(list(self._store_raw(T_FORM)), ["67472072:hhgttg"])

    def test_a_synced_save_is_stored_in_the_canonical_form(self):
        db_operations.upsert_synced_zork_save("67472072", "hhgttg", SAVE, T_FORM)
        stored = db_operations.get_db_connection().execute(
            "SELECT updated_at FROM zork_saves").fetchone()[0]
        self.assertEqual(stored, SPACE_FORM)

    def test_a_save_does_not_ping_pong_between_two_nodes(self):
        """End to end: our copy arrived over the wire (T form), the peer made
        it locally (space form). Applying the peer's copy must be a no-op and
        must leave the hashes equal -- that is the loop, gone."""
        self._store_raw(T_FORM)
        ours = db_operations.get_record_hash_manifest('zork_saves')
        db_operations.upsert_synced_zork_save("67472072", "hhgttg", SAVE, SPACE_FORM)
        after = db_operations.get_record_hash_manifest('zork_saves')
        self.assertEqual(ours, after)

    def test_a_genuinely_newer_save_still_replaces(self):
        self._store_raw(T_FORM)
        db_operations.upsert_synced_zork_save(
            "67472072", "hhgttg", b"newer save", "2026-04-01 09:00:00")
        row = db_operations.get_db_connection().execute(
            "SELECT save_data, updated_at FROM zork_saves").fetchone()
        self.assertEqual(row[0], b"newer save")
        self.assertEqual(row[1], "2026-04-01 09:00:00")

    def test_a_delete_still_beats_an_equal_timestamp_in_either_spelling(self):
        self._store_raw(T_FORM)
        self.assertTrue(db_operations.apply_synced_zork_save_delete(
            "67472072", "hhgttg", SPACE_FORM))
        self.assertEqual(
            db_operations.get_db_connection().execute(
                "SELECT COUNT(*) FROM zork_saves").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
