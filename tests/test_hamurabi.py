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


class PlanTests(unittest.TestCase):
    def test_each_year_opens_with_a_plan_that_feeds_everyone(self):
        state = game.new_game(1)
        self.assertEqual({'buy': 0, 'feed': 2000, 'plant': 1000}, state['plan'])
        self.assertEqual(300, game.grain_left(state))
        game.play_year(state, **state['plan'])
        self.assertEqual(state['pop'] * game.BUSHELS_PER_PERSON
                         if state['grain'] >= state['pop'] * 20 else state['grain'],
                         state['plan']['feed'])
        self.assertEqual(0, state['plan']['buy'])

    def test_a_save_from_before_the_menus_gets_a_plan(self):
        state = game.new_game(1)
        del state['plan']
        self.assertIs(state, game.validate(state))
        self.assertIn('plant', state['plan'])


class ScreenTests(unittest.TestCase):
    def _states(self, pop, grain, land, plague=False):
        """A reign in progress, its three planning screens, and its ends."""
        for seed in range(20):
            yield game.new_game(seed), {}
            big = game.new_game(seed)
            big.update(year=10, pop=pop, grain=grain, land=land, price=26,
                       last={'yield': 5, 'rats': grain // 2, 'starved': pop // 10,
                             'came': pop // 10, 'plague': plague})
            big['plan'] = game.default_plan(big)
            for menu in (None, 'land', 'feed', 'plant'):
                yield big, {'menu': menu}
            for outcome in ('finished', 'deposed'):
                ended = copy.deepcopy(big)
                ended.update(phase='ended', outcome=outcome, deaths=pop * 10)
                yield ended, {}

    def test_every_screen_a_reign_reaches_fits_one_packet(self):
        """A big city for ten years: a thousand people, a granary of 49999."""
        for state, nav in self._states(pop=999, grain=49999, land=4999):
            screen = game.render(state, '', nav)
            with self.subTest(phase=state['phase'], menu=nav.get('menu')):
                self.assertNotIn(door_kit.MESSAGE_SEPARATOR, screen)
                self.assertTrue(door_kit.fits(screen), screen)

    def test_absurd_numbers_take_more_messages_never_a_cut(self):
        for state, nav in self._states(pop=10 ** 9, grain=10 ** 12, land=10 ** 10,
                                       plague=True):
            for note in ("", "Planting 999999 acres needs 500000 bushels; 12345 would be left."):
                parts = door_kit.messages(game.render(state, note, nav))
                with self.subTest(phase=state['phase'], note=bool(note)):
                    for part in parts:
                        self.assertTrue(door_kit.fits(part), part)
                    joined = " ".join(parts)
                    if note:
                        self.assertIn(note, joined)
                    if state['phase'] == 'play':
                        self.assertRegex(parts[-1], r"\[[0B]\]")
                    if state['year'] == 10 and state['phase'] == 'play' \
                            and not nav.get('menu'):
                        self.assertIn(str(state['pop']), joined)
                        self.assertIn("Plague", joined)
                        self.assertIn("[4]End year", parts[-1])

    def test_the_plan_says_what_it_leaves_or_lacks(self):
        state = game.new_game(1)
        self.assertIn("300 left", game.render(state))
        state['plan']['buy'] = 100
        self.assertIn(f"{100 * state['price'] - 300} short", game.render(state))

    def test_the_rules_are_one_packet(self):
        self.assertTrue(door_kit.fits(game.RULES), len(game.RULES.encode('utf-8')))


class DoorTests(unittest.TestCase):
    NAME = "Ruler"

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

    def test_the_first_visit_brings_the_rules_and_later_ones_do_not(self):
        reply, leave = self.say(None)
        parts = door_kit.messages(reply)
        self.assertEqual(game.RULES, parts[0])
        self.assertIn("Yr 1/10", parts[-1])
        self.say("x")
        reply, _ = self.say(None)
        self.assertNotIn(game.RULES, reply)
        self.assertIn("Yr 1/10", reply)

    def test_end_year_plays_the_plan_and_leaving_keeps_the_reign(self):
        self.say(None)
        reply, _ = self.say("4")
        self.assertIn("Yr 2/10", reply)
        reply, leave = self.say("0")
        self.assertTrue(leave)
        self.assertIn("saved", reply)
        reply, _ = self.say(None)
        self.assertIn("Yr 2/10", reply)

    def test_each_part_of_the_plan_is_its_own_screen(self):
        self.say(None)
        reply, _ = self.say("2")
        self.assertIn("2000 feeds all 100", reply)
        reply, _ = self.say("lots")
        self.assertIn("Send a whole number", reply)
        reply, _ = self.say("1500")
        self.assertIn("feed 1500", reply)
        self.assertIn("800 left", reply)
        self.say("1")
        reply, _ = self.say("-50")
        self.assertIn("buy -50", reply)
        self.say("3")
        reply, leave = self.say("b")
        self.assertFalse(leave)
        self.assertIn("plant 1000", reply)
        self.assertIn("[4]End year", reply)

    def test_a_plan_that_cannot_be_met_names_the_limit_and_keeps_the_year(self):
        self.say(None)
        self.say("1")
        self.say("5000")
        reply, _ = self.say("4")
        self.assertIn("you have 2800 bushels", reply)
        self.assertIn("Yr 1/10", reply)

    def test_a_finished_reign_is_scored_once_and_a_deposed_one_is_not(self):
        self.say(None)
        self.say("2")
        self.say("0")
        self.say("3")
        self.say("0")
        reply, _ = self.say("4")
        self.assertIn("deposed", reply)
        self.assertEqual([], db_operations.get_game_scoreboard(game.GAME_ID, limit=5))

        reply, _ = self.say("1")
        self.assertIn("Yr 1/10", reply)
        # A full granary and no plague, so ten years of feeding everyone
        # is certain to finish.
        state = door_kit.load_save(game.GAME_ID, 606)
        state['grain'] = 1000000
        door_kit.store_save(game.GAME_ID, 606, state)
        with mock.patch.object(game, '_draw',
                               lambda state, low, high: high if high == 100 else low):
            for _ in range(game.YEARS):
                reply, _ = self.say("4")
        self.assertIn("Score", reply)
        board = db_operations.get_game_scoreboard(game.GAME_ID, limit=5)
        self.assertEqual(1, len(board))
        self.assertEqual("Ruler", board[0][0])
        self.assertEqual(game.YEARS, board[0][3])

    def test_help_brings_the_rules_back(self):
        self.say(None)
        for word in ("?", "help"):
            parts = door_kit.messages(self.say(word)[0])
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
