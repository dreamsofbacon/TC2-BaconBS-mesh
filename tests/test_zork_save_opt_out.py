"""A peer that does not sync Zork saves is not a peer that is behind on them.

A node with saves disabled advertises zork_saves = 0 and a sentinel hash
meaning "I do not participate in this scope". The mismatch check only ever
consulted the LOCAL node's setting, so a node with saves enabled compared its
own count against that 0, flagged zork_saves out of sync, and requested a
repair the peer drops on arrival. The gap could never close: the dashboard
read "4 behind" indefinitely, and every sync pass dragged the tombstones
scope along with it.

The distinction that matters is between a peer that has opted out and a peer
that syncs saves but has none yet. The second one still has to report a gap
-- that is the case where sending ours across actually works.
"""
import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations
import utils


PEER = "mqtt:baconbbsvt:Chattanooga"


class ZorkSaveOptOutTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        db_operations._ensure_zork_saves_table()
        conn = db_operations.thread_local.connection
        for game in ("zork1", "hhgttg", "planetfall", "deadline"):
            conn.execute(
                "INSERT INTO zork_saves (user_id, game_id, save_data, updated_at) "
                "VALUES (?, ?, 'x', '2026-01-01T00:00:00')", ("67472072", game))
        conn.commit()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _advertise(self, zork_count, zork_hash):
        """Record a peer's SYNCSTATE, matching us on every scope but saves."""
        local = db_operations.get_local_record_counts()
        db_operations.upsert_peer_sync_state(
            PEER,
            int(local["bulletins"]), int(local["mail"]), int(local["channels"]),
            int(zork_count), int(local["profiles"]), int(local["game_scores"]),
            bulletins_hash=local["bulletins_hash"], mail_hash=local["mail_hash"],
            channels_hash=local["channels_hash"], zork_saves_hash=zork_hash,
            profiles_hash=local["profiles_hash"],
            game_scores_hash=local["game_scores_hash"],
            tombstones=int(local.get("tombstones", 0)),
        )

    def _scopes(self):
        with mock.patch.object(utils, "is_zork_save_sync_enabled", return_value=True), \
             mock.patch.object(db_operations, "is_zork_save_sync_enabled", return_value=True):
            return db_operations.get_mismatched_peer_scopes().get(PEER, [])

    def _mismatched(self):
        with mock.patch.object(utils, "is_zork_save_sync_enabled", return_value=True), \
             mock.patch.object(db_operations, "is_zork_save_sync_enabled", return_value=True):
            return db_operations.get_mismatched_peer_nodes()

    def test_we_hold_saves_the_test_peer_does_not(self):
        """Guards the fixture itself: without saves there is nothing to
        mis-report."""
        self.assertEqual(
            db_operations.get_local_record_counts()["zork_saves"], 4)

    def test_an_opted_out_peer_is_not_reported_out_of_sync(self):
        self._advertise(0, db_operations.zork_saves_disabled_hash())
        self.assertNotIn("zork_saves", self._scopes())

    def test_an_opted_out_peer_does_not_drag_in_tombstones(self):
        """zork_saves pulled the tombstones scope into every repair pass."""
        self._advertise(0, db_operations.zork_saves_disabled_hash())
        self.assertEqual(self._scopes(), [])

    def test_an_opted_out_peer_is_not_a_mismatched_node(self):
        self._advertise(0, db_operations.zork_saves_disabled_hash())
        self.assertNotIn(PEER, self._mismatched())

    def test_a_peer_that_syncs_saves_but_has_none_still_reports_a_gap(self):
        """The case that must keep working: a real hash over zero rows means
        the peer accepts saves and simply has none, so ours should go."""
        empty_but_enabled = "AAAAAAAAAAA"
        self._advertise(0, empty_but_enabled)
        self.assertIn("zork_saves", self._scopes())

    def test_a_peer_that_says_nothing_still_reports_a_gap(self):
        """An empty hash means the peer did not tell us, not that it opted
        out -- older builds send no hash at all."""
        self._advertise(0, "")
        self.assertIn("zork_saves", self._scopes())

    def test_a_peer_with_matching_saves_is_in_sync(self):
        local = db_operations.get_local_record_counts()
        self._advertise(4, local["zork_saves_hash"])
        self.assertNotIn("zork_saves", self._scopes())

    def test_the_sentinel_is_what_a_disabled_node_actually_advertises(self):
        """Ties the check to the value produced on the other side; if these
        ever drift the opt-out silently stops being recognised."""
        with mock.patch.object(utils, "is_zork_save_sync_enabled", return_value=False), \
             mock.patch.object(db_operations, "is_zork_save_sync_enabled", return_value=False):
            advertised = db_operations.get_local_record_counts()["zork_saves_hash"]
        self.assertEqual(advertised, db_operations.zork_saves_disabled_hash())
        self.assertTrue(db_operations.peer_opts_out_of_zork_saves(advertised))

    def test_a_disabled_node_advertises_no_saves(self):
        with mock.patch.object(utils, "is_zork_save_sync_enabled", return_value=False), \
             mock.patch.object(db_operations, "is_zork_save_sync_enabled", return_value=False):
            self.assertEqual(
                db_operations.get_local_record_counts()["zork_saves"], 0)

    def test_an_empty_hash_is_not_an_opt_out(self):
        self.assertFalse(db_operations.peer_opts_out_of_zork_saves(""))
        self.assertFalse(db_operations.peer_opts_out_of_zork_saves(None))


class RelayPreservesHashesTests(unittest.TestCase):
    """A gossiped relay must not blank what first-hand SYNCSTATE established.

    PEERGOSSIP carries counts and nothing else. It was writing empty strings
    over every stored hash, which looks like new information and is not --
    so a peer's opt-out sentinel survived only until the next relay about it,
    and the phantom gap reappeared on a loop.
    """

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        db_operations._ensure_zork_saves_table()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _stored_hashes(self):
        row = db_operations.thread_local.connection.execute(
            "SELECT bulletins_hash, zork_saves_hash FROM peer_sync_state "
            "WHERE peer_node_id = ?", (PEER,)).fetchone()
        return row

    def _first_hand(self):
        db_operations.upsert_peer_sync_state(
            PEER, 9, 0, 13, 0, 13, 0,
            bulletins_hash="realBulletinH",
            zork_saves_hash=db_operations.zork_saves_disabled_hash())
        # Backdate it so the relay below counts as strictly fresher.
        db_operations.thread_local.connection.execute(
            "UPDATE peer_sync_state SET reported_at = '2020-01-01 00:00:00' "
            "WHERE peer_node_id = ?", (PEER,))
        db_operations.thread_local.connection.commit()

    def test_a_relay_keeps_the_opt_out_sentinel(self):
        self._first_hand()
        self.assertTrue(db_operations.merge_relayed_peer_state(
            PEER, {"bulletins": 9, "mail": 0, "channels": 13, "zork_saves": 0,
                   "profiles": 13, "game_scores": 0}, age_seconds=5))
        self.assertEqual(self._stored_hashes()[1],
                         db_operations.zork_saves_disabled_hash())

    def test_a_relay_keeps_the_other_hashes_too(self):
        self._first_hand()
        db_operations.merge_relayed_peer_state(
            PEER, {"bulletins": 9, "mail": 0, "channels": 13, "zork_saves": 0,
                   "profiles": 13, "game_scores": 0}, age_seconds=5)
        self.assertEqual(self._stored_hashes()[0], "realBulletinH")

    def test_a_relay_still_updates_the_counts(self):
        """Preserving hashes must not turn the relay into a no-op."""
        self._first_hand()
        db_operations.merge_relayed_peer_state(
            PEER, {"bulletins": 11, "mail": 0, "channels": 13, "zork_saves": 0,
                   "profiles": 13, "game_scores": 0}, age_seconds=5)
        row = db_operations.thread_local.connection.execute(
            "SELECT bulletins FROM peer_sync_state WHERE peer_node_id = ?",
            (PEER,)).fetchone()
        self.assertEqual(row[0], 11)

    def test_a_relay_about_an_unknown_peer_still_works(self):
        """No stored row to preserve anything from."""
        self.assertTrue(db_operations.merge_relayed_peer_state(
            "mqtt:x:brand-new", {"bulletins": 3, "mail": 0, "channels": 0,
                                 "zork_saves": 0, "profiles": 0,
                                 "game_scores": 0}, age_seconds=5))


class DashboardTests(unittest.TestCase):
    """The page showed "we are behind by 4" against a peer that will never
    accept one of them."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        db_operations._ensure_zork_saves_table()
        conn = db_operations.thread_local.connection
        conn.execute(
            "INSERT INTO zork_saves (user_id, game_id, save_data, updated_at) "
            "VALUES ('u', 'zork1', 'x', '2026-01-01T00:00:00')")
        conn.commit()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _peer_entry(self, zork_hash):
        local = db_operations.get_local_record_counts()
        db_operations.upsert_peer_sync_state(
            PEER, int(local["bulletins"]), int(local["mail"]),
            int(local["channels"]), 0, int(local["profiles"]),
            int(local["game_scores"]), zork_saves_hash=zork_hash)
        with mock.patch.object(utils, "is_zork_save_sync_enabled", return_value=True), \
             mock.patch.object(db_operations, "is_zork_save_sync_enabled", return_value=True):
            data = db_operations.get_sync_progress_data()
        return next(p for p in data["peers"] if p["peer_node_id"] == PEER)

    def test_an_opted_out_peer_shows_no_gap(self):
        entry = self._peer_entry(db_operations.zork_saves_disabled_hash())
        self.assertTrue(entry["skips_zork_saves"])
        self.assertEqual([g["scope"] for g in entry["gaps"]], [])

    def test_a_peer_that_simply_lags_still_shows_the_gap(self):
        entry = self._peer_entry("AAAAAAAAAAA")
        self.assertFalse(entry["skips_zork_saves"])
        self.assertIn("zork_saves", [g["scope"] for g in entry["gaps"]])


if __name__ == "__main__":
    unittest.main()
