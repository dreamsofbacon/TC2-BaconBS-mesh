"""Oregon Trail: the outfit, a fortnight's arithmetic, and every way it ends."""
import copy
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import db_operations
import door_kit
import oregon as game

KIT = (250, 220, 40, 90, 80)


def underway(seed=1, **fields):
    state = game.new_game(seed)
    game.outfit(state, *KIT)
    state.update(fields)
    return state


def quiet(state, low, high):
    """Draws that make nothing happen: no event, no mountains, full hunt."""
    return high


class OutfitTests(unittest.TestCase):
    def test_what_is_not_spent_stays_as_cash(self):
        state = underway()
        self.assertEqual(game.PURSE - sum(KIT), state['cash'])
        self.assertEqual(40 * game.BULLETS_PER_DOLLAR, state['ammo'])
        self.assertEqual(('play', 1), (state['phase'], state['turn']))

    def test_a_bad_outfit_is_refused_and_nothing_is_spent(self):
        for kit in ((100, 0, 0, 0, 0), (301, 0, 0, 0, 0), (250, 500, 0, 0, 0),
                    (250, -1, 0, 0, 0)):
            state = game.new_game(1)
            before = copy.deepcopy(state)
            with self.assertRaises(game.Refused):
                game.outfit(state, *kit)
            self.assertEqual(before, state)


class TurnTests(unittest.TestCase):
    def test_a_quiet_fortnight_is_miles_and_meals(self):
        state = underway()
        with mock.patch.object(door_kit, 'draw', quiet):
            events = game.play_turn(state, 1, 2)
        self.assertEqual([], events)
        self.assertEqual(200 + (250 - 220) // 5 + 10, state['miles'])
        self.assertEqual(220 - game.MEALS[2], state['food'])
        self.assertEqual(2, state['turn'])

    def test_better_oxen_go_further(self):
        slow, fast = game.new_game(1), game.new_game(1)
        game.outfit(slow, 200, 200, 40, 80, 80)
        game.outfit(fast, 300, 200, 40, 80, 80)
        with mock.patch.object(door_kit, 'draw', quiet):
            game.play_turn(slow, 1, 2)
            game.play_turn(fast, 1, 2)
        self.assertGreater(fast['miles'], slow['miles'])

    def test_eating_well_costs_more_food(self):
        eaten = []
        for meal in (1, 2, 3):
            state = underway()
            with mock.patch.object(door_kit, 'draw', quiet):
                game.play_turn(state, 1, meal)
            eaten.append(220 - state['food'])
        self.assertEqual([13, 18, 23], eaten)

    def test_hunting_trades_bullets_and_miles_for_meat(self):
        state = underway()
        with mock.patch.object(door_kit, 'draw', quiet):
            events = game.play_turn(state, 2, 2)
        self.assertIn("meat", events[0])
        self.assertGreater(state['food'], 220)
        self.assertLess(state['ammo'], 2000)
        self.assertEqual(200 + 6 + 10 - 45, state['miles'])

    def test_hunting_needs_bullets(self):
        state = underway(ammo=10)
        with self.assertRaises(game.Refused):
            game.play_turn(state, 2, 2)

    def test_a_fort_is_every_other_stop_and_charges_half_as_much_again(self):
        state = underway()
        self.assertFalse(game.at_fort(state))
        with self.assertRaises(game.Refused) as refused:
            game.play_turn(state, 3, 2, (30, 0, 0, 0))
        self.assertIn("next turn", str(refused.exception))
        state['turn'] = 2
        cash = state['cash']
        with mock.patch.object(door_kit, 'draw', quiet):
            game.play_turn(state, 3, 2, (15, 0, 3, 0))
        self.assertEqual(cash - 18, state['cash'])
        self.assertEqual(220 + 10 - 18, state['food'])
        self.assertEqual(90 + 2, state['clothes'])

    def test_a_fort_will_not_sell_on_credit(self):
        state = underway(turn=2)
        before = copy.deepcopy(state)
        with self.assertRaises(game.Refused):
            game.play_turn(state, 3, 2, (9999, 0, 0, 0))
        self.assertEqual(before, state)

    def test_running_out_of_food_ends_it(self):
        state = underway(food=5)
        events = game.play_turn(state, 1, 2)
        self.assertEqual(('ended', 'starved'), (state['phase'], state['outcome']))
        self.assertIn("food is gone", " ".join(events))
        self.assertIsNone(game.result(state))

    def test_serious_illness_needs_a_doctor_next_turn(self):
        paid = underway(sick=True, cash=50)
        with mock.patch.object(door_kit, 'draw', quiet):
            events = game.play_turn(paid, 1, 2)
        self.assertEqual(30, paid['cash'])
        self.assertIn("doctor is paid", events[0])
        broke = underway(sick=True, cash=5)
        game.play_turn(broke, 1, 2)
        self.assertEqual('untreated', broke['outcome'])

    def test_illness_with_no_supplies_ends_it(self):
        state = underway(supplies=0, meal=1)
        events = []
        with mock.patch.object(door_kit, 'draw', lambda s, low, high: low):
            game._illness(state, events)
        self.assertEqual('medicine', state['outcome'])

    def test_eating_poorly_makes_illness_worse(self):
        def severity(meal, roll):
            state = underway(meal=meal, supplies=50)
            with mock.patch.object(door_kit, 'draw', lambda s, low, high: roll):
                game._illness(state, [])
            return state['sick']
        self.assertTrue(severity(1, 70))     # serious on poor rations
        self.assertFalse(severity(3, 70))    # shrugged off when eating well

    def test_reaching_the_end_wins_and_scores(self):
        state = underway(miles=game.TRAIL_MILES - 50)
        with mock.patch.object(door_kit, 'draw', quiet):
            events = game.play_turn(state, 1, 2)
        self.assertEqual('arrived', state['outcome'])
        self.assertEqual(game.TRAIL_MILES, state['miles'])
        self.assertIn("Oregon!", events[-1])
        self.assertEqual((game.score(state), state['turn']), game.result(state))

    def test_arriving_sooner_scores_higher(self):
        early = underway(miles=game.TRAIL_MILES, turn=10, phase='ended', outcome='arrived')
        late = underway(miles=game.TRAIL_MILES, turn=17, phase='ended', outcome='arrived')
        self.assertGreater(game.score(early), game.score(late))

    def test_winter_ends_a_journey_that_takes_too_long(self):
        state = underway(turn=game.MAX_TURNS, miles=100)
        with mock.patch.object(door_kit, 'draw', quiet):
            game.play_turn(state, 1, 2)
        self.assertEqual('winter', state['outcome'])

    def test_stores_never_show_below_nothing(self):
        for seed in range(300):
            state = underway(seed, food=400)
            for _ in range(game.MAX_TURNS):
                if state['phase'] != 'play':
                    break
                game.play_turn(state, 1, 2)
                for key in ('food', 'ammo', 'clothes', 'supplies', 'oxen', 'miles'):
                    self.assertGreaterEqual(state[key], 0, (seed, key))

    def test_the_same_seed_is_the_same_journey(self):
        a, b = underway(77), underway(77)
        for _ in range(6):
            if a['phase'] == 'play':
                self.assertEqual(game.play_turn(a, 1, 2), game.play_turn(b, 1, 2))
        self.assertEqual(a, b)

    def test_a_careful_outfit_usually_arrives(self):
        arrived = 0
        for seed in range(300):
            state = underway(seed)
            while state['phase'] == 'play':
                if state['food'] < 60 and state['ammo'] >= 40:
                    game.play_turn(state, 2, 2)
                else:
                    game.play_turn(state, 1, 2)
            arrived += state['outcome'] == 'arrived'
        self.assertGreater(arrived, 200)
        self.assertLess(arrived, 300)


class StoreTests(unittest.TestCase):
    def test_an_amount_replaces_the_last_one_for_that_item(self):
        state = game.new_game(1)
        game.allocate(state, 'food', 300)
        game.allocate(state, 'food', 120)
        self.assertEqual(120, state['cart']['food'])
        self.assertEqual(game.PURSE - 120, game.left_to_spend(state))

    def test_the_store_will_not_overspend_or_sell_odd_oxen(self):
        state = game.new_game(1)
        game.allocate(state, 'food', 600)
        with self.assertRaises(game.Refused) as refused:
            game.allocate(state, 'supplies', 200)
        self.assertIn("You have $100", str(refused.exception))
        for oxen in (50, 301, -1):
            with self.assertRaises(game.Refused):
                game.allocate(state, 'oxen', oxen)

    def test_no_wagon_leaves_without_oxen(self):
        state = game.new_game(1)
        with self.assertRaises(game.Refused) as refused:
            game.set_out(state)
        self.assertIn("oxen", str(refused.exception))
        self.assertEqual('outfit', state['phase'])

    def test_the_ready_made_outfit_sets_out_with_change(self):
        state = game.new_game(1)
        state['cart'] = dict(game.READY_MADE)
        game.set_out(state)
        self.assertEqual(('play', 1), (state['phase'], state['turn']))
        self.assertEqual(game.PURSE - sum(game.READY_MADE.values()), state['cash'])

    def test_a_fort_sells_at_half_again_and_charges_the_miles_on_leaving(self):
        state = underway(turn=2, cash=90)
        got = game.fort_buy(state, 'food', 30)
        self.assertEqual(20, got)
        self.assertEqual(60, state['cash'])
        self.assertTrue(state['fort_stop'])
        with mock.patch.object(door_kit, 'draw', quiet):
            game.play_turn(state, 1, 2)
        self.assertEqual(200 + 6 + 10 - game.FORT_STOP_MILES, state['miles'])
        self.assertFalse(state['fort_stop'])

    def test_a_fort_only_where_there_is_one_and_never_on_credit(self):
        with self.assertRaises(game.Refused):
            game.fort_buy(underway(turn=1), 'food', 10)
        with self.assertRaises(game.Refused):
            game.fort_buy(underway(turn=2, cash=5), 'food', 10)
        with self.assertRaises(game.Refused):
            game.fort_buy(underway(turn=2), 'oxen', 10)

    def test_bullets_are_bought_by_the_dollar(self):
        state = underway(turn=2, cash=90, ammo=0)
        self.assertEqual(20 * game.BULLETS_PER_DOLLAR, game.fort_buy(state, 'ammo', 30))

    def test_a_save_from_before_the_menus_still_loads(self):
        state = underway()
        del state['cart'], state['fort_stop']
        self.assertIs(state, game.validate(state))
        self.assertFalse(state['fort_stop'])


class ScreenTests(unittest.TestCase):
    def _every_screen(self):
        rich = dict(turn=18, miles=2039, food=9999, ammo=99999, clothes=999,
                    supplies=999, cash=700)
        yield game.new_game(1), {}
        store = game.new_game(1)
        store['cart'] = dict(game.READY_MADE)
        yield store, {}
        for item in game.ITEMS:
            yield store, {'menu': 'spend', 'item': item}
        for turn in (1, 2, game.MAX_TURNS):
            state = underway(**{**rich, 'turn': turn})
            yield state, {}
            yield state, {'menu': 'rations'}
        fort = underway(**{**rich, 'turn': 16})
        yield fort, {'menu': 'fort'}
        for item in game.ITEMS[1:]:
            yield fort, {'menu': 'fort_spend', 'item': item}
        for outcome in ('arrived', 'starved', 'untreated', 'medicine', 'winter'):
            yield underway(phase='ended', outcome=outcome, **rich), {}

    def test_every_screen_fits_one_packet(self):
        for state, nav in self._every_screen():
            screen = game.render(state, '', nav)
            with self.subTest(menu=nav.get('menu'), phase=state['phase']):
                self.assertTrue(door_kit.fits(screen), screen)

    def test_every_help_text_fits_one_packet(self):
        for text in list(game.SCREEN_HELP.values()) + list(game.ITEM_HELP.values()):
            self.assertTrue(door_kit.fits(text), text)

    def test_every_choice_is_a_numbered_menu_item(self):
        """No screen asks for a line of several numbers any more."""
        for state, nav in self._every_screen():
            screen = game.render(state, '', nav)
            self.assertRegex(screen, r"\[[0B]\]")
            self.assertNotIn("e.g. 1 2", screen)

    def test_the_fort_is_offered_only_where_there_is_one(self):
        self.assertNotIn("Fort", game.render(underway(turn=1)))
        screen = game.render(underway(turn=2))
        self.assertIn("[4]Fort", screen)
        self.assertIn("Fort Kearney is here", screen)

    def test_a_long_run_of_events_goes_ahead_whole(self):
        note = ("The doctor is paid $20. Good hunting: 102 lb of meat. Bandits! "
                "They take a third of your cash and leave a wound. A doctor will "
                "cost $20. A blizzard in the pass. Someone is seriously ill. A "
                "doctor will cost $20.")
        parts = door_kit.messages(game.render(underway(turn=2), note))
        for part in parts:
            self.assertTrue(door_kit.fits(part), part)
        self.assertEqual(note.split(), " ".join(parts[:-1]).split())
        self.assertIn("[1]Travel on", parts[-1])

    def test_the_rules_are_one_packet(self):
        self.assertTrue(door_kit.fits(game.RULES), len(game.RULES.encode()))


class DoorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        path = os.path.join(self.folder.name, "oregon.db")
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

    def say(self, text, user=31):
        reply, leave, nav = game.handle(user, text, "Pioneer", self.nav.get(user))
        self.nav[user] = nav
        return reply, leave

    def test_the_store_is_a_menu_that_explains_each_item(self):
        reply, leave = self.say(None)
        self.assertIn("General store", reply)
        self.assertIn("[1]Oxen [2]Food", reply)
        reply, _ = self.say("2")
        self.assertIn("$1 buys 1 lb", reply)
        self.assertIn("Spend how much on food?", reply)
        reply, _ = self.say("lots")
        self.assertIn("Send an amount in dollars", reply)
        reply, _ = self.say("$150")
        self.assertIn("Food: $150.", reply)
        self.assertIn("$550 of $700 left", reply)

    def test_b_goes_back_from_an_amount_and_zero_leaves_from_the_store(self):
        self.say(None)
        self.say("6")
        self.say("3")
        reply, leave = self.say("b")
        self.assertFalse(leave)
        self.assertIn("Ammo $40", reply)
        self.say("3")
        reply, _ = self.say("0")           # an amount: no ammo at all
        self.assertIn("Ammo $0", reply)
        reply, leave = self.say("0")
        self.assertTrue(leave)
        self.assertIn("saved", reply)

    def test_help_explains_the_screen_you_are_on(self):
        self.say(None)
        reply, _ = self.say("?")
        parts = door_kit.messages(reply)
        self.assertEqual([game.RULES, game.SCREEN_HELP['outfit']], parts[:2])
        self.assertIn("General store", parts[-1])

    def test_ready_made_set_out_travel_and_arrive_on_the_scoreboard(self):
        self.say(None)
        reply, _ = self.say("6")
        self.assertIn("balanced outfit", reply)
        reply, _ = self.say("7")
        self.assertIn("You set out", reply)
        self.assertIn("Turn 1 of 18", reply)
        with mock.patch.object(door_kit, 'draw', quiet):
            for _ in range(12):
                reply, _ = self.say("1")
                if "reached Oregon" in reply:
                    break
        self.assertIn("reached Oregon", reply)
        board = db_operations.get_game_scoreboard(game.GAME_ID, limit=5)
        self.assertEqual("Pioneer", board[0][0])
        reply, _ = self.say("1")
        self.assertIn("General store", reply)

    def test_rations_are_a_menu_and_stick(self):
        self.say(None)
        self.say("6")
        self.say("7")
        reply, _ = self.say("3")
        self.assertIn("[1]Poorly: 13 lb", reply)
        reply, _ = self.say("3")
        self.assertIn("Everyone eats well", reply)
        with mock.patch.object(door_kit, 'draw', quiet):
            reply, _ = self.say("1")
        self.assertIn(f"Food {220 - 23}lb", reply)

    def test_trading_at_a_fort_then_travelling_on(self):
        self.say(None)
        self.say("6")
        self.say("7")
        with mock.patch.object(door_kit, 'draw', quiet):
            reply, _ = self.say("1")
        self.assertIn("[4]Fort", reply)
        reply, _ = self.say("4")
        self.assertIn("Fort Kearney trading post", reply)
        reply, _ = self.say("1")
        self.assertIn("$3 buys what $2 did at home", reply)
        reply, _ = self.say("15")
        self.assertIn("Bought 10 lb of food", reply)
        self.assertIn("trading post", reply)
        with mock.patch.object(door_kit, 'draw', quiet):
            reply, _ = self.say("5")
        self.assertIn("Turn 3 of 18", reply)

    def test_a_lost_journey_is_not_scored(self):
        self.say(None)
        self.say("1")
        self.say("250")
        self.say("2")
        self.say("5")
        self.say("7")
        reply, _ = self.say("1")
        self.assertIn("food is gone", reply)
        self.assertIn("[1]Set out again", reply)
        self.assertEqual([], db_operations.get_game_scoreboard(game.GAME_ID, limit=5))

    def test_a_choice_that_is_not_on_the_screen_says_what_is(self):
        self.say(None)
        self.say("6")
        self.say("7")
        reply, _ = self.say("4")          # no fort on turn 1
        self.assertIn("Choose 1-3", reply)
        self.assertIn("Turn 1 of 18", reply)


if __name__ == "__main__":
    unittest.main()
