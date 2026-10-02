"""Hunt the Wumpus: every rule of the cave, and every screen in one packet."""
import copy
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import db_operations
import door_kit
import wumpus as game


def cave(room=1, wumpus=20, pits=(18, 19), bats=(16, 17), arrows=5):
    """A hunt with everything placed by hand."""
    state = game.new_game(1)
    state.update(room=room, wumpus=wumpus, pits=list(pits), bats=list(bats),
                 arrows=arrows, moves=0)
    return state


class CaveTests(unittest.TestCase):
    def test_every_tunnel_runs_both_ways_and_every_room_has_three(self):
        for room, tunnels in game.CAVE.items():
            self.assertEqual(3, len(set(tunnels)))
            for other in tunnels:
                self.assertIn(room, game.CAVE[other])

    def test_nobody_starts_on_top_of_anything(self):
        for seed in range(200):
            state = game.new_game(seed)
            placed = [state['room'], state['wumpus']] + state['pits'] + state['bats']
            self.assertEqual(6, len(set(placed)), seed)

    def test_the_same_seed_is_the_same_cave(self):
        self.assertEqual(game.new_game(9), game.new_game(9))


class MoveTests(unittest.TestCase):
    def test_you_can_only_walk_down_a_tunnel(self):
        state = cave(room=1)
        before = copy.deepcopy(state)
        with self.assertRaises(ValueError) as refused:
            game.move(state, 12)
        self.assertIn("2 5 8", str(refused.exception))
        self.assertEqual(before, state)

    def test_a_safe_step_just_moves_you(self):
        state = cave(room=1)
        self.assertEqual([], game.move(state, 2))
        self.assertEqual((2, 1, 'play'), (state['room'], state['moves'], state['phase']))

    def test_a_pit_ends_the_hunt(self):
        state = cave(room=1, pits=(2, 19))
        game.move(state, 2)
        self.assertEqual(('ended', 'pit'), (state['phase'], state['outcome']))
        self.assertIsNone(game.result(state))

    def test_bats_carry_you_somewhere_else(self):
        state = cave(room=1, bats=(2, 17))
        with mock.patch.object(door_kit, 'draw', lambda s, low, high: 11):
            events = game.move(state, 2)
        self.assertEqual(11, state['room'])
        self.assertIn("room 11", events[0])
        self.assertEqual('play', state['phase'])

    def test_walking_into_the_wumpus_is_a_coin_with_three_good_sides(self):
        stays = cave(room=1, wumpus=2)
        with mock.patch.object(door_kit, 'draw', lambda s, low, high: 1):
            game.move(stays, 2)
        self.assertEqual('eaten', stays['outcome'])

        leaves = cave(room=1, wumpus=2)
        with mock.patch.object(door_kit, 'draw', lambda s, low, high: high):
            game.move(leaves, 2)
        self.assertEqual('play', leaves['phase'])
        self.assertNotEqual(2, leaves['wumpus'])


class SensesTests(unittest.TestCase):
    def test_each_danger_next_door_has_its_own_sign(self):
        self.assertEqual([], game.senses(cave(room=1)))
        self.assertIn("foul", " ".join(game.senses(cave(room=1, wumpus=2))))
        self.assertIn("draft", " ".join(game.senses(cave(room=1, pits=(5, 19)))))
        self.assertIn("rustle", " ".join(game.senses(cave(room=1, bats=(8, 17)))))

    def test_a_sign_never_says_which_room(self):
        signs = " ".join(game.senses(cave(room=1, wumpus=2, pits=(5, 19), bats=(8, 17))))
        self.assertFalse(any(ch.isdigit() for ch in signs), signs)


class ShootTests(unittest.TestCase):
    def test_an_arrow_into_the_wumpus_wins(self):
        state = cave(room=1, wumpus=2)
        events = game.shoot(state, [2])
        self.assertEqual('won', state['outcome'])
        self.assertIn("dead", events[0])
        self.assertEqual((game.score(state), 1), game.result(state))
        self.assertEqual(4, state['arrows'])

    def test_a_crooked_arrow_follows_the_rooms_named(self):
        state = cave(room=1, wumpus=3)
        game.shoot(state, [2, 3])
        self.assertEqual('won', state['outcome'])

    def test_a_room_it_cannot_reach_sends_it_down_a_random_tunnel(self):
        state = cave(room=1, wumpus=8)
        with mock.patch.object(door_kit, 'draw', lambda s, low, high: 2):
            game.shoot(state, [13])       # no tunnel 1->13; tunnel index 2 is room 8
        self.assertEqual('won', state['outcome'])

    def test_an_arrow_that_comes_back_ends_the_hunt(self):
        state = cave(room=1)
        game.shoot(state, [2, 1])
        self.assertEqual('arrow', state['outcome'])

    def test_a_miss_costs_an_arrow_and_may_wake_it(self):
        state = cave(room=1, wumpus=20)
        with mock.patch.object(door_kit, 'draw', lambda s, low, high: 1):
            events = game.shoot(state, [2])
        self.assertEqual((4, 'play', 20), (state['arrows'], state['phase'], state['wumpus']))
        self.assertIn("miss", " ".join(events))

    def test_a_woken_wumpus_that_reaches_you_eats_you(self):
        state = cave(room=1, wumpus=2)
        with mock.patch.object(game, 'CAVE', {**game.CAVE, 2: (1, 1, 1)}), \
                mock.patch.object(door_kit, 'draw', lambda s, low, high: high):
            game.shoot(state, [5])
        self.assertEqual('eaten', state['outcome'])

    def test_the_last_arrow_missed_ends_the_hunt(self):
        state = cave(room=1, arrows=1)
        with mock.patch.object(door_kit, 'draw', lambda s, low, high: 1):
            game.shoot(state, [2])
        self.assertEqual('unarmed', state['outcome'])

    def test_bad_orders_change_nothing(self):
        state = cave(room=1)
        before = copy.deepcopy(state)
        for path in ([], [1, 2, 3, 4, 5, 6], [99]):
            with self.assertRaises(ValueError):
                game.shoot(state, path)
        self.assertEqual(before, state)


class ScreenTests(unittest.TestCase):
    def test_every_room_fits_one_packet_with_every_warning(self):
        for room in game.CAVE:
            near = game.CAVE[room]
            state = cave(room=room, wumpus=near[0], pits=(near[1], near[1]),
                         bats=(near[2], near[2]), arrows=5)
            screen = game.render(state)
            self.assertEqual(3, len(game.senses(state)))
            self.assertTrue(door_kit.fits(screen), screen)

    def test_the_end_screens_fit_and_a_long_note_goes_ahead(self):
        for outcome in ('won', 'pit', 'eaten', 'arrow', 'unarmed'):
            state = cave()
            state.update(phase='ended', outcome=outcome, moves=999)
            self.assertTrue(door_kit.fits(game.render(state)))
        note = ("Bats lift you and drop you in room 11. Bats lift you and drop you "
                "in room 4. You walk into the Wumpus! The Wumpus shuffles off in "
                "the dark.")
        near = game.CAVE[1]
        state = cave(room=1, wumpus=near[0], pits=(near[1], 19), bats=(near[2], 17))
        parts = door_kit.messages(game.render(state, note))
        for part in parts:
            self.assertTrue(door_kit.fits(part), part)
        self.assertIn("shuffles off", " ".join(parts))
        self.assertIn("Room 1.", parts[-1])

    def test_the_rules_are_one_packet(self):
        self.assertTrue(door_kit.fits(game.RULES), len(game.RULES.encode()))


class DoorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        path = os.path.join(self.folder.name, "wumpus.db")
        env = mock.patch.dict(os.environ, {"BBS_DB_PATH": path})
        env.start()
        self.addCleanup(env.stop)
        db_operations.thread_local.connection = sqlite3.connect(path)
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _place(self, user, **fields):
        state = door_kit.load_save(game.GAME_ID, user)
        state.update(fields)
        door_kit.store_save(game.GAME_ID, user, state)

    def test_a_hunt_from_the_menu_to_the_scoreboard(self):
        reply, leave, _ = game.handle(88, None, "Hunter")
        self.assertIn("Tunnels:", reply)
        self._place(88, room=1, wumpus=3, pits=[18, 19], bats=[16, 17])
        reply, _, _ = game.handle(88, "12", "Hunter")
        self.assertIn("No tunnel to 12", reply)
        reply, _, _ = game.handle(88, "2", "Hunter")
        self.assertIn("Room 2.", reply)
        self.assertIn("foul", reply)
        reply, _, _ = game.handle(88, "s 3", "Hunter")
        self.assertIn("dead", reply)
        self.assertIn("[1]Hunt again", reply)
        board = db_operations.get_game_scoreboard(game.GAME_ID, limit=5)
        self.assertEqual("Hunter", board[0][0])
        reply, _, _ = game.handle(88, "1", "Hunter")
        self.assertIn("Arrows 5", reply)

    def test_a_lost_hunt_is_not_scored(self):
        game.handle(89, None, "Lost")
        self._place(89, room=1, wumpus=20, pits=[2, 19], bats=[16, 17])
        reply, _, _ = game.handle(89, "2", "Lost")
        self.assertIn("fall", reply)
        self.assertEqual([], db_operations.get_game_scoreboard(game.GAME_ID, limit=5))

    def test_nonsense_explains_itself(self):
        game.handle(90, None, "New")
        reply, _, _ = game.handle(90, "north", "New")
        self.assertIn("room number", reply)
        reply, leave, _ = game.handle(90, "0", "New")
        self.assertTrue(leave)


if __name__ == "__main__":
    unittest.main()
