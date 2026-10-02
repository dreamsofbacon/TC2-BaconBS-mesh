"""Hamurabi: ten years, one packet a year.

The engine is pure and seeded, so each rule is tested directly. The screens
are measured at the worst the numbers can reach, because a report that
spills into a second packet doubles the airtime of every year.
"""
import copy
import os
import sqlite3
import tempfile
import types
import unittest
from unittest import mock

import command_handlers as ch
import db_operations
import door_games
import door_kit
import hamurabi as game
import utils


def fed(state):
    """Orders that feed everyone and plant what the grain allows."""
    feed = state['pop'] * game.BUSHELS_PER_PERSON
    plant = min(state['land'], state['pop'] * game.ACRES_PER_PERSON,
                max(0, (state['grain'] - feed) * 2))
    return 0, min(feed, state['grain']), plant


class EngineTests(unittest.TestCase):
    def test_the_same_seed_plays_the_same_reign(self):
        a, b = game.new_game(42), game.new_game(42)
        for _ in range(3):
            game.play_year(a, *fed(a))
            game.play_year(b, *fed(b))
        self.assertEqual(a, b)

    def test_it_opens_as_the_classic_does(self):
        state = game.new_game(1)
        self.assertEqual((1, 100, 2800, 1000),
                         (state['year'], state['pop'], state['grain'], state['land']))
        self.assertTrue(17 <= state['price'] <= 26)

    def test_buying_land_costs_grain_and_selling_returns_it(self):
        state = game.new_game(3)
        price, before = state['price'], state['grain']
        game.check(state, 10, 0, 0)
        bought = copy.deepcopy(state)
        game.play_year(bought, 10, 2000, 0)
        self.assertEqual(1010, bought['land'])
        sold = copy.deepcopy(state)
        game.play_year(sold, -10, 2000, 0)
        self.assertEqual(990, sold['land'])
        # Same seed, same rats and harvest: the difference is the trade alone,
        # give or take the rats' share of it.
        self.assertGreater(sold['grain'], bought['grain'])
        self.assertEqual(before, state['grain'])
        self.assertEqual(price, state['price'])

    def test_orders_that_cannot_be_met_change_nothing(self):
        state = game.new_game(5)
        untouched = copy.deepcopy(state)
        for orders in ((5000, 0, 0),        # cannot afford
                       (-2000, 0, 0),       # do not own
                       (0, 99999, 0),       # not that much grain
                       (0, 0, 5000),        # not that much land
                       (0, 0, 1001),        # more than the land held
                       (0, 2800, 100),      # nothing left for seed
                       (0, -1, 0), (0, 0, -1)):
            with self.subTest(orders=orders):
                with self.assertRaises(game.Refused):
                    game.play_year(state, *orders)
                self.assertEqual(untouched, state)

    def test_people_can_farm_only_ten_acres_each(self):
        state = game.new_game(5)
        state.update(land=5000, grain=99999)
        with self.assertRaises(game.Refused) as refused:
            game.play_year(state, 0, 2000, 1001)
        self.assertIn("1000 acres", str(refused.exception))

    def test_starving_most_of_the_people_ends_the_reign_at_once(self):
        state = game.new_game(7)
        game.play_year(state, 0, 0, 0)
        self.assertEqual(('ended', 'deposed'), (state['phase'], state['outcome']))
        self.assertEqual(0, game.score(state))
        self.assertIn("deposed", game.verdict(state))

    def test_starving_a_few_is_survivable(self):
        state = game.new_game(7)
        game.play_year(state, 0, 1600, 0)   # feeds 80 of 100
        self.assertEqual('play', state['phase'])
        self.assertEqual(20, state['last']['starved'])
        self.assertEqual(20, state['deaths'])

    def test_the_reign_ends_after_ten_years(self):
        state = game.new_game(11)
        state.update(grain=10 ** 6)
        for year in range(1, game.YEARS + 1):
            self.assertEqual('play', state['phase'])
            self.assertEqual(year, state['year'])
            game.play_year(state, 0, state['pop'] * game.BUSHELS_PER_PERSON, 0)
        self.assertEqual(('ended', 'finished'), (state['phase'], state['outcome']))
        self.assertEqual(game.YEARS, game.years_played(state))
        self.assertGreater(game.score(state), 0)
        with self.assertRaises(game.Refused):
            game.play_year(state, 0, 0, 0)

    def test_a_save_from_another_version_is_refused(self):
        state = game.new_game(1)
        state['v'] = 99
        with self.assertRaises(ValueError):
            game.validate(state)


class ScreenTests(unittest.TestCase):
    def _states(self, pop, grain, land):
        """A reign in progress, finished and deposed, at these sizes."""
        for seed in range(20):
            yield game.new_game(seed)
            big = game.new_game(seed)
            big.update(year=10, pop=pop, grain=grain, land=land, price=26,
                       last={'yield': 5, 'rats': grain, 'starved': pop,
                             'came': pop, 'plague': True})
            yield big
            for outcome in ('finished', 'deposed'):
                ended = copy.deepcopy(big)
                ended.update(phase='ended', outcome=outcome, deaths=pop * 10)
                yield ended

    def test_every_screen_a_reign_can_reach_fits_one_packet(self):
        """Ten years cannot grow a city past a few thousand people; this is
        ten times that, with a plague and the rats both in the report."""
        for state in self._states(pop=9999, grain=999999, land=99999):
            screen = game.render(state)
            with self.subTest(phase=state['phase'], outcome=state['outcome'],
                              size=len(screen.encode('utf-8'))):
                self.assertNotIn(door_kit.MESSAGE_SEPARATOR, screen)
                self.assertTrue(door_kit.fits(screen), screen)

    def test_absurd_numbers_take_more_messages_never_a_cut(self):
        for state in self._states(pop=10 ** 9, grain=10 ** 12, land=10 ** 10):
            for note in ("", "Planting 999999 acres needs 500000 bushels; 12345 would be left."):
                parts = door_kit.messages(game.render(state, note))
                with self.subTest(phase=state['phase'], note=bool(note)):
                    for part in parts:
                        self.assertTrue(door_kit.fits(part), part)
                    joined = " ".join(parts)
                    self.assertIn(str(state['pop']), joined)
                    if note:
                        self.assertIn(note, joined)
                    if state['phase'] == 'play':
                        self.assertIn("Send: buy feed plant", parts[-1])
                        self.assertIn("[0]Exit", parts[-1])

    def test_a_refusal_goes_above_the_report_or_ahead_of_it(self):
        state = game.new_game(1)
        short = game.render(state, "Too few.")
        self.assertEqual("Too few.", short.splitlines()[0])
        self.assertNotIn(door_kit.MESSAGE_SEPARATOR, short)
        long_note = "Planting 999999 acres needs 500000 bushels; 12345 would be left."
        parts = door_kit.messages(game.render(state, long_note))
        self.assertEqual(long_note, parts[0])
        self.assertIn("Yr 1/10", parts[1])

    def test_the_rules_are_one_packet(self):
        self.assertTrue(door_kit.fits(game.RULES), len(game.RULES.encode('utf-8')))


class DoorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        path = os.path.join(self.folder.name, "hamurabi.db")
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

    def play(self, text):
        reply, leave, _nav = game.handle(606, text, "Ruler")
        return reply, leave

    def test_opening_shows_year_one_and_leaving_keeps_the_reign(self):
        reply, leave = self.play(None)
        self.assertIn("Yr 1/10", reply)
        self.assertFalse(leave)
        self.play("0 2000 500")
        reply, leave = self.play("0")
        self.assertTrue(leave)
        self.assertIn("saved", reply)
        reply, _ = self.play(None)
        self.assertIn("Yr 2/10", reply)

    def test_three_numbers_play_a_year_and_anything_else_explains(self):
        self.play(None)
        reply, _ = self.play("hello")
        self.assertIn("three numbers", reply)
        self.assertIn("Yr 1/10", reply)
        reply, _ = self.play("0, 2000, 500")
        self.assertIn("Yr 2/10", reply)

    def test_a_refusal_names_the_limit_and_keeps_the_year(self):
        self.play(None)
        reply, _ = self.play("5000 0 0")
        self.assertIn("you have 2800 bushels", reply)
        self.assertIn("Yr 1/10", reply)

    def test_a_finished_reign_is_scored_once_and_a_deposed_one_is_not(self):
        self.play(None)
        reply, _ = self.play("0 0 0")
        self.assertIn("deposed", reply)
        self.assertEqual([], db_operations.get_game_scoreboard(game.GAME_ID, limit=5))

        self.play("1")
        # A full granary and no plague, so ten years of feeding everyone
        # is certain to finish.
        state = door_kit.load_save(game.GAME_ID, 606)
        state['grain'] = 1000000
        door_kit.store_save(game.GAME_ID, 606, state)
        with mock.patch.object(game, '_draw',
                               lambda state, low, high: high if high == 100 else low):
            for _ in range(game.YEARS):
                reply, _ = self.play("0 40000 0")
        self.assertIn("Score", reply)
        board = db_operations.get_game_scoreboard(game.GAME_ID, limit=5)
        self.assertEqual(1, len(board))
        self.assertEqual("Ruler", board[0][0])
        self.assertEqual(game.YEARS, board[0][3])

    def test_the_rules_come_with_the_screen(self):
        self.play(None)
        reply, _ = self.play("?")
        parts = door_kit.messages(reply)
        self.assertEqual(game.RULES, parts[0])
        self.assertIn("Yr 1/10", parts[-1])


class WiringTests(unittest.TestCase):
    def test_it_is_a_door_and_on_the_menu(self):
        self.assertTrue(door_games.is_door(game.COMMAND))
        self.assertEqual('classic', ch.GAMES[game.GAME_ID]['group'])
        self.assertEqual(2, len(ch.games_menu_keys(game.GAME_ID)))

    def test_the_menu_opens_it_and_the_router_keeps_its_keys(self):
        """Its numbers are its own: "0 2000 500" is a year's orders, and a
        bare N is not Ask Nomad."""
        import message_processing as mp
        iface = types.SimpleNamespace(nodes={}, bbs_nodes=[], allowed_nodes=[])
        seen = []
        self.addCleanup(utils.user_states.pop, 4141, None)
        with mock.patch.object(game, 'handle',
                               lambda user, text, name, nav: seen.append(text) or ("ok", False, None)), \
                mock.patch.object(ch, 'send_message'), \
                mock.patch.object(ch, 'get_node_id_from_num', return_value='!abc'), \
                mock.patch.object(ch, 'get_node_short_name', return_value='Ruler'), \
                mock.patch.object(mp, '_auto_update_profile', lambda *a, **k: None):
            ch.handle_games_command(4141, iface)
            for key in ch.games_menu_keys(game.GAME_ID):
                ch.handle_games_steps(4141, key, iface)
            self.assertEqual(game.COMMAND, ch.get_user_state(4141)['command'])
            for text in ("0 2000 500", "n", "!CM"):
                mp.process_message(4141, text, iface, is_sync_message=False,
                                   sender_node_id='!abc')
        self.assertEqual([None, "0 2000 500", "n", "!CM"], seen)


if __name__ == "__main__":
    unittest.main()
