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


class ScreenTests(unittest.TestCase):
    def test_every_screen_fits_one_packet(self):
        screens = [game.render(game.new_game(1))]
        for turn in (1, 2, game.MAX_TURNS):
            screens.append(game.render(underway(
                turn=turn, miles=2039, food=9999, ammo=99999, clothes=999,
                supplies=999, cash=700)))
        for outcome in ('arrived', 'starved', 'untreated', 'medicine', 'winter'):
            screens.append(game.render(underway(
                phase='ended', outcome=outcome, turn=18, miles=2040, food=9999,
                ammo=99999, clothes=999, supplies=999, cash=700)))
        for screen in screens:
            self.assertTrue(door_kit.fits(screen), screen)

    def test_the_fort_is_offered_only_where_there_is_one(self):
        self.assertNotIn("[3]Fort", game.render(underway(turn=1)))
        self.assertIn("[3]Fort", game.render(underway(turn=2)))

    def test_a_long_run_of_events_goes_ahead_whole(self):
        note = ("The doctor is paid $20. Good hunting: 102 lb of meat. Bandits! "
                "They take a third of your cash and leave a wound. A doctor will "
                "cost $20. A blizzard in the pass. Someone is seriously ill. A "
                "doctor will cost $20.")
        parts = door_kit.messages(game.render(underway(turn=2), note))
        for part in parts:
            self.assertTrue(door_kit.fits(part), part)
        self.assertEqual(note.split(), " ".join(parts[:-1]).split())
        self.assertIn("[1]Travel", parts[-1])

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

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_outfit_then_travel_then_arrive_on_the_scoreboard(self):
        reply, _, _ = game.handle(31, None, "Pioneer")
        self.assertIn("Send 5 amounts", reply)
        reply, _, _ = game.handle(31, "250 200", "Pioneer")
        self.assertIn("five amounts", reply)
        reply, _, _ = game.handle(31, "250 900 0 0 0", "Pioneer")
        self.assertIn("you have $700", reply)
        reply, _, _ = game.handle(31, "$250, 220, 40, 90, 80", "Pioneer")
        self.assertIn("Independence", reply)
        self.assertIn("Turn 1/18", reply)
        reply, _, _ = game.handle(31, "3 10 0 0 0 2", "Pioneer")
        self.assertIn("No fort here", reply)
        with mock.patch.object(door_kit, 'draw', quiet):
            for _ in range(12):
                reply, _, _ = game.handle(31, "1 2", "Pioneer")
                if "Oregon!" in reply:
                    break
        self.assertIn("Oregon!", reply)
        board = db_operations.get_game_scoreboard(game.GAME_ID, limit=5)
        self.assertEqual("Pioneer", board[0][0])
        reply, _, _ = game.handle(31, "1", "Pioneer")
        self.assertIn("Send 5 amounts", reply)

    def test_a_lost_journey_is_not_scored(self):
        game.handle(32, None, "Lost")
        game.handle(32, "250 5 40 90 80", "Lost")
        reply, _, _ = game.handle(32, "1 3", "Lost")
        self.assertIn("food is gone", reply)
        self.assertEqual([], db_operations.get_game_scoreboard(game.GAME_ID, limit=5))

    def test_the_meal_defaults_to_moderate(self):
        game.handle(33, None, "Plain")
        game.handle(33, "250 220 40 90 80", "Plain")
        with mock.patch.object(door_kit, 'draw', quiet):
            reply, _, _ = game.handle(33, "1", "Plain")
        self.assertIn("Food 202", reply)


if __name__ == "__main__":
    unittest.main()
