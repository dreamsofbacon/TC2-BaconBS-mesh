"""Skillet Keep: twelve turns a day, ten Wardens, one packet a turn."""
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import db_operations
import door_kit
import skilletkeep as game

DAY1, DAY2 = "2026-10-01", "2026-10-02"


def hero(seed=1, **fields):
    state = game.new_game(seed)
    state['name'] = 'Crispy'
    game.new_day(state, DAY1)
    state.update(fields)
    return state


def strong(**fields):
    """A hero nothing at their level can hurt."""
    return hero(**{'level': 5, 'blade': 5, 'armor': 5, 'hp': 90, **fields})


class NumbersTests(unittest.TestCase):
    def test_levels_and_gear_make_you_stronger(self):
        base = hero()
        self.assertGreater(game.attack(hero(blade=1)), game.attack(base))
        self.assertGreater(game.defense(hero(armor=1)), game.defense(base))
        self.assertGreater(game.max_hp(hero(level=2)), game.max_hp(base))
        self.assertGreater(game.attack(hero(level=2)), game.attack(base))

    def test_each_level_asks_for_more_experience(self):
        needs = [game.xp_needed(level) for level in range(1, game.MAX_LEVEL + 1)]
        self.assertEqual(sorted(needs), needs)
        self.assertEqual(len(set(needs)), len(needs))

    def test_every_tier_of_gear_has_a_name_and_a_price(self):
        self.assertEqual(len(game.BLADES), len(game.GEAR_PRICES))
        self.assertEqual(len(game.ARMORS), len(game.GEAR_PRICES))
        self.assertEqual(game.MAX_LEVEL, len(game.WARDENS))
        self.assertEqual(sorted(game.GEAR_PRICES), list(game.GEAR_PRICES))

    def test_deeper_wilds_send_stronger_foes(self):
        near = game.monster(hero(level=5), 1)
        far = game.monster(hero(level=5), 3)
        self.assertGreater(far['attack'], near['attack'])
        self.assertGreater(far['xp'], near['xp'])

    def test_a_fight_always_ends_even_against_a_wall(self):
        state = hero()
        outcome = game.fight(state, {'name': 'Wall', 'hp': 500, 'attack': 1, 'defense': 999})
        self.assertFalse(outcome['won'])
        self.assertEqual(0, state['hp'])

    def test_the_same_seed_fights_the_same_fight(self):
        a, b = hero(9), hero(9)
        game.explore(a, 2, [], [])
        game.explore(b, 2, [], [])
        self.assertEqual(a, b)


class DayTests(unittest.TestCase):
    def test_a_new_day_gives_twelve_turns_and_a_night_of_rest(self):
        state = hero(turns=0, hp=3, down=True, dueled=True)
        self.assertFalse(game.new_day(state, DAY1))
        self.assertEqual(0, state['turns'])
        self.assertTrue(game.new_day(state, DAY2))
        self.assertEqual((game.TURNS_PER_DAY, game.max_hp(state), False, False),
                         (state['turns'], state['hp'], state['down'], state['dueled']))
        self.assertEqual(2, state['days'])

    def test_turns_do_not_carry_over(self):
        state = hero(turns=7)
        game.new_day(state, DAY2)
        self.assertEqual(game.TURNS_PER_DAY, state['turns'])


class WildsTests(unittest.TestCase):
    def test_a_won_fight_pays_and_costs_a_turn(self):
        state = strong()
        events, news = [], []
        game.explore(state, 1, events, news)
        self.assertEqual(game.TURNS_PER_DAY - 1, state['turns'])
        self.assertGreater(state['gold'], 30)
        self.assertGreater(state['xp'], 0)
        self.assertEqual(state['xp'], state['renown'])
        self.assertEqual(1, state['kills'])
        self.assertIn("You beat a", events[0])
        self.assertEqual([], news)

    def test_falling_costs_half_the_gold_carried_and_the_rest_of_the_day(self):
        state = hero(hp=1, gold=101, bank=500)
        events, news = [], []
        game.explore(state, 3, events, news)
        self.assertEqual((51, 500), (state['gold'], state['bank']))
        self.assertEqual((0, True, 1), (state['turns'], state['down'], state['falls']))
        self.assertIn("50 gold lighter", events[0])
        self.assertIn("Crispy fell to", news[0])

    def test_no_turns_no_fight(self):
        for fields in ({'turns': 0}, {'down': True}):
            state = strong(**fields)
            before = copy.deepcopy(state)
            with self.assertRaises(ValueError):
                game.explore(state, 2, [], [])
            self.assertEqual(before, state)

    def test_only_three_depths(self):
        with self.assertRaises(ValueError):
            game.explore(strong(), 4, [], [])


class TownTests(unittest.TestCase):
    def test_the_inn_heals_for_a_price(self):
        state = hero(hp=5, gold=100)
        game.rest(state, [])
        self.assertEqual((game.max_hp(state), 100 - game.inn_price(state)),
                         (state['hp'], state['gold']))

    def test_the_inn_turns_away_the_rested_and_the_broke(self):
        with self.assertRaises(ValueError):
            game.rest(hero(), [])
        with self.assertRaises(ValueError) as refused:
            game.rest(hero(hp=5, gold=0), [])
        self.assertIn("A bed costs", str(refused.exception))

    def test_the_smith_sells_the_next_tier_only(self):
        state = hero(gold=10000)
        game.buy(state, 1, [])
        game.buy(state, 2, [])
        self.assertEqual((1, 1), (state['blade'], state['armor']))
        self.assertEqual(10000 - 2 * game.GEAR_PRICES[1], state['gold'])

    def test_the_smith_gives_no_credit_and_has_a_best(self):
        with self.assertRaises(ValueError) as refused:
            game.buy(hero(gold=5), 1, [])
        self.assertIn("costs 120 gold", str(refused.exception))
        with self.assertRaises(ValueError):
            game.buy(hero(gold=99999, blade=5), 1, [])

    def test_the_bank_moves_all_of_it(self):
        state = hero(gold=70, bank=30)
        game.bank(state, 1, [])
        self.assertEqual((0, 100), (state['gold'], state['bank']))
        game.bank(state, 2, [])
        self.assertEqual((100, 0), (state['gold'], state['bank']))
        with self.assertRaises(ValueError):
            game.bank(state, 2, [])


class WardenTests(unittest.TestCase):
    def test_the_warden_wants_experience_first(self):
        state = strong(xp=0)
        with self.assertRaises(ValueError) as refused:
            game.challenge(state, [], [])
        self.assertIn(str(game.xp_needed(5)), str(refused.exception))
        self.assertEqual(game.TURNS_PER_DAY, state['turns'])

    def test_beating_the_warden_is_a_level(self):
        state = strong(xp=game.xp_needed(5), hp=40)
        events, news = [], []
        game.challenge(state, events, news)
        self.assertEqual((6, 0, game.max_hp(state)), (state['level'], state['xp'], state['hp']))
        self.assertIn("Level 6", events[0])
        self.assertIn("reached level 6", news[0])

    def test_losing_to_the_warden_is_a_fall(self):
        state = hero(xp=game.xp_needed(1), hp=1, gold=40)
        game.challenge(state, [], [])
        self.assertEqual((1, True, 20), (state['level'], state['down'], state['gold']))
        self.assertEqual(game.xp_needed(1), state['xp'])

    def test_the_ash_king_is_a_crown_and_a_fresh_start(self):
        state = hero(level=10, blade=5, armor=5, hp=150, xp=game.xp_needed(10),
                     renown=5000, bank=900)
        events, news = [], []
        game.challenge(state, events, news)
        self.assertEqual((1, 1, 0, 0), (state['crowns'], state['level'],
                                        state['blade'], state['armor']))
        self.assertEqual(900, state['bank'])
        self.assertEqual(5500, game.score(state))
        self.assertIn("Ash King", news[0])

    def test_a_careful_player_can_finish_and_a_reckless_one_falls(self):
        """The balance the game was tuned to: weeks, not days or never."""
        state = game.new_game(3)
        state['name'] = 'Bot'
        for day in range(1, 61):
            game.new_day(state, f"day-{day}")
            while state['turns'] > 0 and not state['down'] and not state['crowns']:
                for slot, key in ((1, 'blade'), (2, 'armor')):
                    tier = state[key] + 1
                    if tier < 6 and state['gold'] >= game.GEAR_PRICES[tier]:
                        game.buy(state, slot, [])
                if state['hp'] < game.max_hp(state) * 0.6:
                    if state['gold'] < game.inn_price(state):
                        break
                    game.rest(state, [])
                elif state['xp'] >= game.xp_needed(state['level']):
                    game.challenge(state, [], [])
                else:
                    game.explore(state, 2, [], [])
            if state['crowns']:
                break
        self.assertEqual(1, state['crowns'])
        self.assertGreater(day, 14)
        self.assertLess(day, 45)


class DuelTests(unittest.TestCase):
    def test_a_duel_pays_a_bounty_and_leaves_the_rival_alone(self):
        rival = hero(level=2)
        rival['name'] = 'Rival'
        before = copy.deepcopy(rival)
        state = strong()
        events, news = [], []
        game.duel(state, rival, events, news)
        self.assertEqual(30 + 15 * 2, state['gold'])
        self.assertEqual(before, rival)
        self.assertIn("Crispy beat Rival", news[0])
        with self.assertRaises(ValueError) as again:
            game.duel(state, rival, [], [])
        self.assertIn("One duel a day", str(again.exception))

    def test_losing_a_duel_costs_pride_not_gold(self):
        rival = strong()
        rival['name'] = 'Champion'
        state = hero(gold=200)
        game.duel(state, rival, [], [])
        self.assertEqual((1, 200, False), (state['hp'], state['gold'], state['down']))

    def test_nobody_to_duel(self):
        with self.assertRaises(ValueError):
            game.duel(strong(), None, [], [])


class ScreenTests(unittest.TestCase):
    def test_every_screen_fits_one_packet_at_its_largest(self):
        state = hero(level=10, xp=99999, hp=150, gold=9999999, bank=9999999,
                     blade=5, armor=5)
        state['name'] = 'W' * 16
        for menu in ('town', 'wilds', 'smith', 'bank'):
            for candidate in (state, hero()):
                screen = game.render(candidate, menu)
                self.assertTrue(door_kit.fits(screen), screen)

    def test_a_long_report_goes_ahead_and_the_screen_stays_whole(self):
        note = game.board(
            [{'name': 'W' * 16, 'level': 10, 'crowns': 3}] * 5,
            ["W" * 16 + " beat the Smoke Tyrant and reached level 9."] * 3)
        parts = door_kit.messages(game.render(hero(), 'town', note))
        for part in parts:
            self.assertTrue(door_kit.fits(part), part)
        self.assertIn("[1]Wilds", parts[-1])
        self.assertEqual(note.split(), " ".join(parts[:-1]).split())

    def test_the_rules_are_one_packet(self):
        self.assertTrue(door_kit.fits(game.RULES), len(game.RULES.encode()))


class DoorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        path = os.path.join(self.folder.name, "keep.db")
        env = mock.patch.dict(os.environ, {"BBS_DB_PATH": path})
        env.start()
        self.addCleanup(env.stop)
        db_operations.thread_local.connection = sqlite3.connect(path)
        db_operations.initialize_database()
        self.addCleanup(self._close)
        self.day = DAY1
        clock = mock.patch.object(door_kit, 'fleet_day', lambda now=None: self.day)
        clock.start()
        self.addCleanup(clock.stop)
        self.nav = {}

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def say(self, text, user=1, name="Crispy"):
        reply, leave, nav = game.handle(user, text, name, self.nav.get(user))
        self.nav[user] = nav
        return reply, leave

    def _set(self, user_name, **fields):
        for user_id in (1, 2, 3):
            state = door_kit.load_save(game.GAME_ID, user_id)
            if state and state['name'] == user_name:
                state.update(fields)
                door_kit.store_save(game.GAME_ID, user_id, state)

    def test_arriving_shows_the_town_and_the_day(self):
        reply, leave = self.say(None)
        self.assertFalse(leave)
        self.assertIn("Day 1 in the keep", reply)
        self.assertIn("Crispy Lv1", reply)
        self.assertIn("Turns 12/12", reply)

    def test_into_the_wilds_a_fight_and_back(self):
        self.say(None)
        reply, _ = self.say("1")
        self.assertIn("The Wilds.", reply)
        reply, _ = self.say("1")
        self.assertIn("11 turns left", reply)
        reply, _ = self.say("0")
        self.assertIn("[1]Wilds", reply)
        self.assertIn("Turns 11/12", reply)

    def test_zero_goes_back_and_x_leaves(self):
        self.say(None)
        reply, leave = self.say("0")
        self.assertFalse(leave)
        reply, leave = self.say("x")
        self.assertTrue(leave)
        self.assertIn("saved", reply)

    def test_opening_the_game_again_lands_in_town(self):
        self.say(None)
        self.say("2")
        self.say("x")
        reply, _leave, nav = game.handle(1, None, "Crispy", self.nav.get(1))
        self.assertIn("[1]Wilds", reply)
        self.assertEqual({'menu': 'town'}, nav)

    def test_tomorrow_brings_new_turns(self):
        self.say(None)
        self._set("Crispy", turns=0, hp=3)
        self.day = DAY2
        reply, _ = self.say(None)
        self.assertIn("Day 2 in the keep", reply)
        self.assertIn("Turns 12/12", reply)

    def test_the_board_lists_the_keep_and_its_news(self):
        self.say(None, user=1, name="Crispy")
        self.say(None, user=2, name="Maple")
        self._set("Maple", level=4)
        self._set("Crispy", hp=1, gold=10)
        self.say("1")
        self.say("3")                      # Far, on 1 HP: a fall
        reply, _ = self.say("6", user=2, name="Maple")
        self.assertIn("1. Maple Lv4", reply)
        self.assertIn("2. Crispy Lv1", reply)
        self.assertIn("Crispy fell to", reply)
        for part in door_kit.messages(reply):
            self.assertTrue(door_kit.fits(part), part)

    def test_a_duel_picks_the_nearest_rival(self):
        self.say(None, user=1, name="Crispy")
        self.say(None, user=2, name="Maple")
        self.say(None, user=3, name="Rind")
        self._set("Maple", level=10)       # five levels away
        self._set("Rind", level=3)         # two levels away: the nearer rival
        self._set("Crispy", level=5, blade=5, armor=5, hp=90)
        reply, _ = self.say("7")
        self.assertIn("You beat Rind", reply)
        reply, _ = self.say("7")
        self.assertIn("One duel a day", reply)

    def test_the_scoreboard_hears_about_levels_not_every_fight(self):
        self.say(None)
        self._set("Crispy", level=5, blade=5, armor=5, hp=90)
        self.say("1")
        self.say("1")
        self.assertEqual([], db_operations.get_game_scoreboard(game.GAME_ID, limit=5))
        self.say("0")
        self._set("Crispy", xp=game.xp_needed(5))
        reply, _ = self.say("5")
        self.assertIn("Level 6", reply)
        board = db_operations.get_game_scoreboard(game.GAME_ID, limit=5)
        self.assertEqual("Crispy", board[0][0])

    def test_nonsense_explains_itself_and_keeps_the_turn(self):
        self.say(None)
        reply, _ = self.say("dance")
        self.assertIn("number of your choice", reply)
        reply, _ = self.say("9")
        self.assertIn("Choose 1 to 7", reply)
        self.assertIn("Turns 12/12", reply)

    def test_every_reply_in_a_day_of_play_fits_a_packet(self):
        self.say(None)
        for text in ("?", "6", "7", "5", "3", "2", "1", "2", "0", "4", "1", "2", "0",
                     "1", "1", "2", "3", "1", "2", "0"):
            reply, _ = self.say(text)
            for part in door_kit.messages(reply):
                self.assertTrue(door_kit.fits(part), (text, part))


if __name__ == "__main__":
    unittest.main()
