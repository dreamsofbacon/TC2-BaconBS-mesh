"""Lunar Lander: the physics, the landing grades, and one-packet screens."""
import copy
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import db_operations
import door_kit
import lander as game

# Found by search: coast, brake hard, then ease down. Lands softly with fuel
# to spare, which is what makes a score possible at all.
GOOD_PLAN = None


def _find_good_plan():
    best = None
    for coast in range(0, 8):
        for brake in range(20, 41):
            for factor in (0.8, 1.0, 1.2, 1.5):
                state, plan = game.new_game(), []
                for i in range(80):
                    if state['phase'] != 'play':
                        break
                    target = max(1.0, (state['alt'] ** 0.5) * factor)
                    burn = 0 if i < coast else (
                        brake if state['speed'] > target else game.HOVER_BURN - 2)
                    burn = max(0, min(game.MAX_BURN, burn))
                    plan.append(burn)
                    game.step(state, burn)
                if state['outcome'] == 'soft' and (
                        best is None or game.score(state) > best[0]):
                    best = (game.score(state), plan)
    return best[1]


class PhysicsTests(unittest.TestCase):
    def test_no_burn_gains_eight_metres_a_second_each_step(self):
        state = game.new_game()
        game.step(state, 0)
        self.assertEqual(48.0, state['speed'])
        self.assertEqual(1500 - (40 * 5 + 0.5 * 1.6 * 25), state['alt'])

    def test_the_hover_burn_holds_speed(self):
        state = game.new_game()
        game.step(state, game.HOVER_BURN)
        self.assertEqual(40.0, state['speed'])
        self.assertEqual(game.START['fuel'] - game.HOVER_BURN, state['fuel'])

    def test_a_big_burn_slows_the_fall(self):
        state = game.new_game()
        game.step(state, game.MAX_BURN)
        self.assertEqual(40 + 8 - game.MAX_BURN * game.THRUST, state['speed'])

    def test_you_cannot_burn_fuel_you_do_not_have(self):
        state = game.new_game()
        state['fuel'] = 10
        game.step(state, 40)
        self.assertEqual(0, state['fuel'])
        self.assertEqual(40 + 8 - 10 * game.THRUST, state['speed'])

    def test_the_same_burns_fly_the_same_descent(self):
        a, b = game.new_game(1), game.new_game(2)
        game.fly(a, [20, 20, 16])
        game.fly(b, [20, 20, 16])
        self.assertEqual((a['alt'], a['speed'], a['fuel']), (b['alt'], b['speed'], b['fuel']))

    def test_free_fall_makes_a_crater(self):
        state = game.new_game()
        while state['phase'] == 'play':
            game.step(state, 0)
        self.assertEqual('crash', state['outcome'])
        self.assertGreater(state['impact'], 70)
        self.assertEqual(0.0, state['alt'])
        self.assertIsNone(game.result(state))

    def test_touchdown_speed_is_taken_at_the_ground_not_the_end_of_the_step(self):
        """Ten metres up and barely moving: the craft lands a fraction of
        the way into the step, at the speed it has then."""
        state = game.new_game()
        state.update(alt=10.0, speed=1.0, fuel=100)
        game.step(state, 0)
        # v^2 = u^2 + 2as
        self.assertAlmostEqual((1 + 2 * 1.6 * 10) ** 0.5, state['impact'], places=1)
        self.assertEqual('hard', state['outcome'])

    def test_an_empty_tank_falls_the_rest_of_the_way(self):
        state = game.new_game()
        state['fuel'] = 30
        game.fly(state, [30])
        self.assertEqual('ended', state['phase'])

    def test_a_soft_landing_is_possible_and_scored(self):
        global GOOD_PLAN
        GOOD_PLAN = GOOD_PLAN or _find_good_plan()
        state = game.new_game()
        for burn in GOOD_PLAN:
            if state['phase'] == 'play':
                game.step(state, burn)
        self.assertEqual('soft', state['outcome'])
        self.assertGreater(state['fuel'], 0)
        self.assertEqual((game.score(state), state['steps']), game.result(state))

    def test_gentler_and_thriftier_scores_higher(self):
        def landed(impact, fuel):
            state = game.new_game()
            state.update(phase='ended', impact=impact, fuel=fuel,
                         outcome=game._outcome(impact))
            return game.score(state)
        self.assertGreater(landed(1.0, 50), landed(4.0, 50))
        self.assertGreater(landed(1.0, 80), landed(1.0, 50))
        self.assertEqual(0, landed(30.0, 200))


class OrdersTests(unittest.TestCase):
    def test_burns_outside_the_throttle_are_refused(self):
        for burns in ([], [41], [-1], [10] * 7):
            with self.assertRaises(ValueError):
                game.check(burns)

    def test_bad_orders_change_nothing(self):
        state = game.new_game()
        before = copy.deepcopy(state)
        self.assertIn("0 to 40", game.respond(state, "99"))
        self.assertIn("number", game.respond(state, "full"))
        self.assertEqual(before, state)

    def test_several_burns_in_one_message_fly_several_steps(self):
        state = game.new_game()
        self.assertEqual('', game.respond(state, "20 20, 16"))
        self.assertEqual(3, state['steps'])


class ScreenTests(unittest.TestCase):
    def test_every_screen_fits_one_packet(self):
        states = []
        for alt, speed, fuel in ((1500.0, 40.0, 400), (9999.9, -123.4, 400),
                                 (0.1, 199.9, 0)):
            state = game.new_game()
            state.update(alt=alt, speed=speed, fuel=fuel)
            states.append(state)
        for outcome, impact in (('soft', 1.9), ('firm', 4.9), ('hard', 9.9),
                                ('crash', 199.9)):
            state = game.new_game()
            state.update(phase='ended', outcome=outcome, impact=impact, steps=99, fuel=400)
            states.append(state)
        for state in states:
            for note in ("", "A burn is 0 to 40."):
                screen = game.render(state, note)
                self.assertTrue(door_kit.fits(screen), screen)

    def test_rising_is_said_as_rising(self):
        state = game.new_game()
        state['speed'] = -3.5
        self.assertIn("3.5 m/s up", game.render(state))

    def test_the_rules_are_one_packet(self):
        self.assertTrue(door_kit.fits(game.RULES), len(game.RULES.encode()))


class DoorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        path = os.path.join(self.folder.name, "lander.db")
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

    def test_zero_is_a_burn_here_not_the_way_out(self):
        game.handle(70, None, "Pilot")
        reply, leave, _ = game.handle(70, "0", "Pilot")
        self.assertFalse(leave)
        self.assertIn("48 m/s down", reply)
        reply, leave, _ = game.handle(70, "x", "Pilot")
        self.assertTrue(leave)

    def test_a_landing_is_scored_and_a_crash_is_not(self):
        global GOOD_PLAN
        GOOD_PLAN = GOOD_PLAN or _find_good_plan()
        game.handle(71, None, "Crasher")
        reply, _, _ = game.handle(71, "0 0 0 0 0", "Crasher")
        self.assertIn("crater", reply)
        self.assertEqual([], db_operations.get_game_scoreboard(game.GAME_ID, limit=5))

        game.handle(72, None, "Ace")
        for start in range(0, len(GOOD_PLAN), game.MAX_BURNS_PER_MESSAGE):
            chunk = GOOD_PLAN[start:start + game.MAX_BURNS_PER_MESSAGE]
            reply, _, _ = game.handle(72, " ".join(map(str, chunk)), "Ace")
        self.assertIn("perfect landing", reply)
        board = db_operations.get_game_scoreboard(game.GAME_ID, limit=5)
        self.assertEqual("Ace", board[0][0])
        reply, _, _ = game.handle(72, "1", "Ace")
        self.assertIn("Alt 1500 m", reply)


if __name__ == "__main__":
    unittest.main()
