"""Lemonade Stand: the demand curve, the weather, and one-packet screens."""
import copy
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import db_operations
import door_kit
import lemonade as game


class DemandTests(unittest.TestCase):
    def test_cheaper_sells_more(self):
        sold = [game.demand(price, 0, 'sunny') for price in (2, 5, 9, 12, 20, 50)]
        self.assertEqual(sorted(sold, reverse=True), sold)
        self.assertGreater(sold[0], sold[-1])

    def test_signs_bring_people_with_less_from_each_one(self):
        none, one, two, ten = (game.demand(10, n, 'sunny') for n in (0, 1, 2, 10))
        self.assertGreater(one, none)
        self.assertGreater(two, one)
        self.assertGreater(one - none, ten - game.demand(10, 9, 'sunny'))
        self.assertLessEqual(ten, 2 * none)

    def test_heat_doubles_the_crowd_and_cloud_thins_it(self):
        sunny = game.demand(10, 0, 'sunny')
        self.assertEqual(2 * sunny, game.demand(10, 0, 'hot'))
        self.assertLess(game.demand(10, 0, 'cloudy'), sunny)

    def test_lemons_get_dearer(self):
        self.assertEqual([2, 2, 4, 4, 4, 4, 5, 5, 5, 5],
                         [game.glass_cost(day) for day in range(1, 11)])


class DayTests(unittest.TestCase):
    def test_you_sell_what_you_made_or_what_they_wanted_whichever_is_less(self):
        few = game.new_game(1)
        game.play_day(few, 5, 0, 10)
        self.assertEqual(5, few['last']['sold'])
        many = game.new_game(1)
        game.play_day(many, 100, 0, 10)
        self.assertEqual(game.demand(10, 0, 'sunny'), many['last']['sold'])

    def test_the_money_adds_up(self):
        state = game.new_game(1)
        game.play_day(state, 20, 1, 10)
        cost = 20 * 2 + 15
        self.assertEqual(game.START_CASH + state['last']['sold'] * 10 - cost, state['cash'])
        self.assertEqual(2, state['day'])

    def test_orders_that_cannot_be_met_change_nothing(self):
        state = game.new_game(1)
        before = copy.deepcopy(state)
        for orders in ((1000, 0, 10), (0, 100, 10), (10, 0, 0), (10, 0, 101),
                       (-1, 0, 10), (10, -1, 10)):
            with self.assertRaises(game.Refused):
                game.play_day(state, *orders)
            self.assertEqual(before, state)

    def test_the_first_two_days_are_always_fine(self):
        for seed in range(50):
            state = game.new_game(seed)
            self.assertEqual('sunny', state['weather'])
            game.play_day(state, 1, 0, 10)
            self.assertEqual('sunny', state['weather'])

    def test_a_storm_on_a_cloudy_day_sells_nothing(self):
        state = game.new_game(1)
        state.update(day=5, weather='cloudy')
        with mock.patch.object(door_kit, 'draw', lambda s, low, high: 1):
            game.play_day(state, 20, 0, 10)
        self.assertTrue(state['last']['storm'])
        self.assertEqual(0, state['last']['sold'])
        self.assertEqual(game.START_CASH - 20 * 4, state['cash'])

    def test_a_storm_never_comes_on_a_clear_day(self):
        state = game.new_game(1)
        state.update(day=5, weather='sunny')
        with mock.patch.object(door_kit, 'draw', lambda s, low, high: 1):
            game.play_day(state, 20, 0, 10)
        self.assertFalse(state['last']['storm'])

    def test_the_summer_is_ten_days_and_the_score_is_the_cash(self):
        state = game.new_game(3)
        for day in range(1, game.DAYS + 1):
            self.assertEqual(day, state['day'])
            game.play_day(state, 10, 0, 10)
        self.assertEqual(('ended', 'finished'), (state['phase'], state['outcome']))
        self.assertEqual((state['cash'], game.DAYS), game.result(state))
        with self.assertRaises(game.Refused):
            game.play_day(state, 1, 0, 10)

    def test_the_same_seed_has_the_same_summer(self):
        a, b = game.new_game(8), game.new_game(8)
        for _ in range(game.DAYS):
            game.play_day(a, 15, 1, 12)
            game.play_day(b, 15, 1, 12)
        self.assertEqual(a, b)


class ScreenTests(unittest.TestCase):
    def test_money_reads_as_money(self):
        self.assertEqual("$2.00", game.money(200))
        self.assertEqual("$0.05", game.money(5))
        self.assertEqual("-$1.50", game.money(-150))

    def test_every_screen_fits_one_packet(self):
        lasts = [None,
                 {'made': 99999, 'sold': 99999, 'price': 100, 'profit': 9999999, 'storm': False},
                 {'made': 99999, 'sold': 0, 'price': 100, 'profit': -999999, 'storm': False},
                 {'made': 99999, 'sold': 0, 'price': 100, 'profit': -999999, 'storm': True}]
        for last in lasts:
            for weather in game.WEATHER:
                state = game.new_game(1)
                state.update(day=10, cash=99999, weather=weather, last=last,
                             plan={'glasses': 9999, 'signs': 99, 'price': 100})
                for menu in (None, 'glasses', 'signs', 'price'):
                    screen = game.render(state, '', {'menu': menu})
                    self.assertTrue(door_kit.fits(screen), screen)
                if last is not None:
                    state.update(phase='ended', outcome='finished')
                    self.assertTrue(door_kit.fits(game.render(state)), game.render(state))

    def test_absurd_numbers_take_more_messages_never_a_cut(self):
        state = game.new_game(1)
        state.update(cash=10 ** 12, weather='hot',
                     plan={'glasses': 10 ** 9, 'signs': 10 ** 6, 'price': 100})
        parts = door_kit.messages(game.render(state))
        for part in parts:
            self.assertTrue(door_kit.fits(part), part)
        self.assertIn("1000000000 glasses", " ".join(parts))
        self.assertIn("[4]Open stand", parts[-1])

    def test_a_refusal_keeps_the_day_whole(self):
        state = game.new_game(1)
        note = "That costs $2000.00; you have $2.00."
        parts = door_kit.messages(game.render(state, note))
        for part in parts:
            self.assertTrue(door_kit.fits(part), part)
        self.assertIn(note, " ".join(parts))
        self.assertIn("[4]Open stand", parts[-1])

    def test_the_rules_are_one_packet(self):
        self.assertTrue(door_kit.fits(game.RULES), len(game.RULES.encode()))

    def test_a_save_from_before_the_menus_gets_a_plan(self):
        state = game.new_game(1)
        del state['plan']
        self.assertEqual(game.FIRST_PLAN, game.validate(state)['plan'])


class DoorTests(unittest.TestCase):
    NAME = "Kid"

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        path = os.path.join(self.folder.name, "door.db")
        env = mock.patch.dict(os.environ, {"BBS_DB_PATH": path})
        env.start()
        self.addCleanup(env.stop)
        db_operations.thread_local.connection = sqlite3.connect(path)
        db_operations.initialize_database()
        self.addCleanup(self._close)
        self.nav = {}

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def say(self, text, user=606):
        reply, leave, nav = game.handle(user, text, self.NAME, self.nav.get(user))
        self.nav[user] = nav
        return reply, leave

    def test_the_first_visit_brings_the_rules(self):
        parts = door_kit.messages(self.say(None)[0])
        self.assertEqual(game.RULES, parts[0])
        self.assertIn("Day 1/10", parts[-1])

    def test_a_whole_summer_ends_on_the_scoreboard(self):
        reply, _ = self.say(None)
        self.assertIn("Plan: 20 glasses, 1 sign, 10c each", reply)
        reply, _ = self.say("lots")
        self.assertIn("Choose 1-4", reply)
        self.say("1")
        reply, _ = self.say("9999")
        self.assertIn("you have $2.00", reply)
        reply, _ = self.say("4")
        self.assertIn("you have $2.00", reply)
        self.assertIn("Day 1/10", reply)
        self.say("1")
        self.say("20")
        self.say("3")
        reply, _ = self.say("12c")
        self.assertIn("12c each", reply)
        for _ in range(game.DAYS):
            reply, _ = self.say("4")
        self.assertIn("Summer's over", reply)
        board = db_operations.get_game_scoreboard(game.GAME_ID, limit=5)
        self.assertEqual(("Kid", game.DAYS), (board[0][0], board[0][3]))
        reply, leave = self.say("0")
        self.assertTrue(leave)

    def test_each_part_of_the_plan_explains_itself(self):
        self.say(None)
        self.assertIn("enough for 100", self.say("1")[0])
        self.say("b")
        self.assertIn("each one adds less", self.say("2")[0])
        self.assertIn("0 signs", self.say("0")[0])
        reply, _ = self.say("3")
        self.assertIn("Cheap sells more", reply)
        reply, _ = self.say("500")
        self.assertIn("Charge 1 to 100", reply)

    def test_leaving_mid_summer_keeps_your_place_and_your_plan(self):
        self.say(None, user=52)
        self.say("2", user=52)
        self.say("3", user=52)
        self.say("4", user=52)
        self.say("0", user=52)
        reply, _ = self.say(None, user=52)
        self.assertIn("Day 2/10", reply)
        self.assertIn("3 signs", reply)


if __name__ == "__main__":
    unittest.main()
