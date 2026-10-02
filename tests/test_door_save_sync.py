"""The new games' saves follow a player between nodes.

They are kept in the same table the adventure games' saves use, so the
fleet's existing save sync carries them with no new frames. These play a
game on one node, hand the save across the way the sync does, and carry on
at another node -- and check the rule that settles two copies.
"""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import db_operations
import door_kit
import hamurabi
import skilletkeep
import wordday

USER = 4242


def steady(state, low, high):
    """Draws with no rats and no plague, so a fed city reaches the next year."""
    return high if high == 100 else 3


class TwoNodes(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.paths = {name: os.path.join(self.folder.name, f"{name}.db")
                      for name in ("a", "b")}
        self.env = None
        self.on("a")
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        if self.env is not None:
            self.env.stop()

    def on(self, node):
        """Become this node: its own database, nothing shared."""
        self._close()
        self.env = mock.patch.dict(os.environ, {"BBS_DB_PATH": self.paths[node]})
        self.env.start()
        db_operations.thread_local.connection = sqlite3.connect(self.paths[node])
        db_operations.initialize_database()

    def saves(self):
        """What the save sync would offer a peer: every row, as it stands."""
        return db_operations.get_db_connection().execute(
            "SELECT user_id, game_id, save_data, updated_at FROM zork_saves").fetchall()

    def deliver(self, rows):
        """Apply rows on this node exactly as a received save is applied."""
        for user_id, game_id, save_data, updated_at in rows:
            db_operations.upsert_synced_zork_save(user_id, game_id, bytes(save_data), updated_at)


class SaveFollowsThePlayerTests(TwoNodes):
    def test_a_reign_started_on_one_node_continues_on_another(self):
        with mock.patch.object(hamurabi, '_draw', steady):
            hamurabi.handle(USER, None, "Ruler")
            hamurabi.handle(USER, "0 2000 500", "Ruler")
            reply, _, _ = hamurabi.handle(USER, "0 2050 0", "Ruler")
        self.assertIn("Yr 3/10", reply)
        rows = self.saves()

        self.on("b")
        self.assertIsNone(door_kit.load_save(hamurabi.GAME_ID, USER))
        self.deliver(rows)
        reply, _, _ = hamurabi.handle(USER, None, "Ruler")
        self.assertIn("Yr 3/10", reply)

    def test_the_save_is_in_the_scope_the_fleet_already_syncs(self):
        hamurabi.handle(USER, None, "Ruler")
        with mock.patch.object(db_operations, "is_zork_save_sync_enabled", return_value=True):
            manifest = db_operations.get_record_hash_manifest('zork_saves')
        self.assertTrue(any(hamurabi.GAME_ID in str(key) for key in manifest), manifest)

    def test_a_node_that_does_not_sync_saves_offers_none(self):
        hamurabi.handle(USER, None, "Ruler")
        with mock.patch.object(db_operations, "is_zork_save_sync_enabled", return_value=False):
            self.assertEqual({}, db_operations.get_record_hash_manifest('zork_saves'))

    def test_the_newer_copy_wins_whichever_way_it_travels(self):
        with mock.patch.object(hamurabi, '_draw', steady):
            hamurabi.handle(USER, None, "Ruler")
            hamurabi.handle(USER, "0 2000 500", "Ruler")             # year 2
            older = [(u, g, d, "2026-10-01 10:00:00") for u, g, d, _ in self.saves()]
            hamurabi.handle(USER, "0 2050 0", "Ruler")               # year 3
            newer = [(u, g, d, "2026-10-01 11:00:00") for u, g, d, _ in self.saves()]

        self.on("b")
        self.deliver(newer)
        self.deliver(older)          # arrives late; must not undo progress
        self.assertEqual(3, door_kit.load_save(hamurabi.GAME_ID, USER)['year'])

    def test_a_daily_puzzle_already_solved_stays_solved_elsewhere(self):
        with mock.patch.object(door_kit, 'fleet_day', lambda now=None: "2026-10-01"):
            wordday.handle(USER, wordday.answer("2026-10-01"), "Solver")
            rows = self.saves()
            self.on("b")
            self.deliver(rows)
            reply, _, _ = wordday.handle(USER, None, "Solver")
        self.assertIn("Solved in 1! Streak 1.", reply)

    def test_the_keeps_board_lists_players_from_other_nodes(self):
        with mock.patch.object(door_kit, 'fleet_day', lambda now=None: "2026-10-01"):
            skilletkeep.handle(1, None, "Crispy")
            rows = self.saves()
            self.on("b")
            skilletkeep.handle(2, None, "Maple")
            self.deliver(rows)
            reply, _, _ = skilletkeep.handle(2, "6", "Maple", {'menu': 'town'})
        self.assertIn("Crispy Lv1", reply)
        self.assertIn("Maple Lv1", reply)

    def test_games_and_adventures_share_the_table_without_colliding(self):
        db_operations.upsert_zork_save(USER, b"\x00zmachine-bytes", 'zork1')
        hamurabi.handle(USER, None, "Ruler")
        self.assertEqual(b"\x00zmachine-bytes", bytes(db_operations.get_zork_save(USER, 'zork1')))
        self.assertEqual(1, door_kit.load_save(hamurabi.GAME_ID, USER)['year'])
        # A save that is not JSON (an adventure's) is skipped, not fatal.
        self.assertEqual([], door_kit.all_saves('zork1'))


class RunsThatWereLocalTests(TwoNodes):
    """Candy Wars and Baconfall kept saves in tables of their own, which
    never left the node. A run in progress has to survive the move."""

    def _legacy(self, table, user, state):
        import json
        from player_identity import player_key
        conn = db_operations.get_db_connection()
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} "
                     "(user_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)")
        conn.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, ?)",
                     (player_key(user), json.dumps(state)))
        conn.commit()

    def _legacy_rows(self, table):
        return db_operations.get_db_connection().execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_a_dope_wars_run_in_the_old_table_is_carried_over(self):
        import dopewars
        import dopewars_door
        run = dopewars.new_game(19)
        run['cash'] = 77777
        self._legacy('dopewars_runs', USER, run)

        _before, state, _reply, _leave, _result = dopewars_door.play_state(USER, None, "Trader")
        self.assertEqual(77777, state['cash'])
        self.assertEqual(77777, door_kit.load_save(dopewars.GAME_ID, USER)['cash'])
        self.assertEqual(0, self._legacy_rows('dopewars_runs'))

    def test_a_baconfall_run_in_the_old_table_is_carried_over(self):
        import baconfall
        import baconfall_port
        baconfall_port.play(USER)                      # makes a valid run
        run = door_kit.load_save(baconfall.GAME_ID, USER)
        run['gold'] = 4321
        db_operations.get_db_connection().execute("DELETE FROM zork_saves")
        self._legacy('baconfall_runs', USER, run)

        baconfall_port.play(USER)
        self.assertEqual(4321, door_kit.load_save(baconfall.GAME_ID, USER)['gold'])
        self.assertEqual(0, self._legacy_rows('baconfall_runs'))

    def test_a_turn_that_fails_mid_move_leaves_the_old_run_where_it_was(self):
        import dopewars
        import dopewars_door
        run = dopewars.new_game(19)
        self._legacy('dopewars_runs', USER, run)
        with mock.patch.object(dopewars_door, 'write_game_save',
                               side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                dopewars_door.play(USER)
        self.assertEqual(1, self._legacy_rows('dopewars_runs'))
        self.assertIsNone(door_kit.load_save(dopewars.GAME_ID, USER))

    def test_a_save_that_arrived_by_sync_wins_over_an_old_local_one(self):
        """The player carried on elsewhere; that is the run they mean."""
        import dopewars
        import dopewars_door
        local = dopewars.new_game(19)
        local['cash'] = 1
        self._legacy('dopewars_runs', USER, local)
        synced = dopewars.new_game(19)
        synced['cash'] = 999
        door_kit.store_save(dopewars.GAME_ID, USER, synced)

        _before, state, *_ = dopewars_door.play_state(USER, None, "Trader")
        self.assertEqual(999, state['cash'])

    def test_a_node_with_no_old_table_just_starts_a_game(self):
        import dopewars_door
        reply, leave, _result = dopewars_door.play(USER)
        self.assertFalse(leave)
        self.assertTrue(reply)

    def test_both_games_now_travel_between_nodes(self):
        import baconfall
        import baconfall_port
        import dopewars
        import dopewars_door
        dopewars_door.play(USER)
        baconfall_port.play(USER)
        state = door_kit.load_save(dopewars.GAME_ID, USER)
        state['cash'] = 31337
        door_kit.store_save(dopewars.GAME_ID, USER, state)
        rows = self.saves()
        self.assertEqual({dopewars.GAME_ID, baconfall.GAME_ID}, {row[1] for row in rows})

        self.on("b")
        self.deliver(rows)
        _before, arrived, *_ = dopewars_door.play_state(USER, None, "Trader")
        self.assertEqual(31337, arrived['cash'])
        self.assertTrue(baconfall_port.play(USER)[0])


if __name__ == "__main__":
    unittest.main()
