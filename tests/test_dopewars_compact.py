"""A Dope Wars save is stored without the market the seed can rebuild.

Two thirds of a save was sixteen prices and stocks that market() builds from
the seed and a draw count alone. The stored form keeps the draw and the
stock that has changed, and rebuilds the rest.

That is only safe if it is exact. So the property tested here is not "it is
smaller" but "expanding it gives back precisely the state that was stored",
after every move of long runs across many seeds -- and that anything which
cannot be rebuilt exactly is stored whole instead.
"""
import copy
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import db_operations
import door_kit
import dopewars as game
import dopewars_door as door


def play_a_run(seed, moves=120):
    """Yield the state after each move of a busy run: travel, trade, fight."""
    state = game.new_game(seed)
    yield state
    for step in range(moves):
        if state['phase'] == 'ended':
            return
        if state['phase'] == 'police':
            commands = ['fight', 'run']
        else:
            places = [p for p in game.CITY_PLACES[game.city(state['place'])]
                      if p != state['place']]
            stocked = [i for i in game.GOODS if state['market'][i]['stock']]
            held = [i for i in game.GOODS if state['inventory'][i]]
            # Travel on most moves -- that is what makes markets and brings
            # the police -- and trade in between.
            travel = f"travel {places[step % len(places)]}"
            if step % 3 == 1 and stocked:
                commands = [f"buy {stocked[step % len(stocked)]} 1", travel]
            elif step % 3 == 2 and held:
                commands = [f"sell {held[0]} 1", travel]
            else:
                commands = [travel]
        for text in commands:
            try:
                state, _reply, _leave = game.command(state, text)
                break
            except ValueError:
                continue
        yield state


class ExactnessTests(unittest.TestCase):
    def test_every_state_of_many_runs_comes_back_exactly(self):
        compacted = total = 0
        for seed in range(40):
            hint = 0
            for state in play_a_run(seed):
                stored = door.compact(state, hint)
                rebuilt, hint = door.expand(json.loads(json.dumps(stored)))
                self.assertEqual(state, rebuilt, (seed, state['draw']))
                total += 1
                compacted += stored is not state
        # Every market in these runs was made by today's rules, so every
        # one of them should have been found.
        self.assertEqual(total, compacted)

    def test_the_stored_bytes_load_as_the_same_validated_state(self):
        for seed in range(15):
            for state in play_a_run(seed, moves=30):
                self.assertEqual(state, door._load(door._store(state)))

    def test_a_run_through_fights_is_found_without_the_hint(self):
        """A fight spends draws after the market was built, so the search
        has to reach back past them."""
        for seed in range(60):
            for state in play_a_run(seed, moves=60):
                if state['phase'] == 'police':
                    stored = door.compact(state)
                    self.assertIsNot(stored, state)
                    self.assertEqual(state, door.expand(stored)[0])

    def test_trading_is_kept_as_what_changed(self):
        state = game.new_game(19)
        item = next(i for i in game.GOODS if state['market'][i]['stock'] > 2)
        state, _, _ = game.command(state, f"buy {item} 2")
        stored = door.compact(state)
        self.assertEqual({item: -2}, stored['market']['d'])
        self.assertEqual({item: 2}, stored['inventory'])
        self.assertEqual(state, door.expand(stored)[0])


class FallbackTests(unittest.TestCase):
    def test_a_market_the_seed_cannot_rebuild_is_stored_whole(self):
        state = game.new_game(19)
        state['market']['weed']['price'] += 1      # not what market() makes
        self.assertIs(state, door.compact(state))
        self.assertEqual(state, door._load(door._store(state)))

    def test_a_save_stored_whole_before_this_still_loads(self):
        state = game.new_game(19)
        whole = json.dumps(state)
        self.assertEqual(state, door._load(whole))
        self.assertEqual(state, door._load(whole.encode()))

    def test_an_older_version_save_migrates_as_it_always_did(self):
        old = {'version': 1, 'seed': 7, 'draw': 9, 'day': 3, 'days': 30, 'loan_due': 30,
               'place': 'docks', 'cash': 500, 'debt': 1200, 'hp': 90, 'capacity': 40,
               'weapon': 0, 'armor': 0, 'phase': 'market', 'enemy_hp': 0, 'moves': 4,
               'outcome': '',
               'inventory': {'weed': 1, 'hash': 0, 'acid': 0, 'cocaine': 0},
               'market': {k: {'price': 100, 'stock': 5}
                          for k in ('weed', 'hash', 'acid', 'cocaine')}}
        migrated = door._load(json.dumps(old))
        self.assertEqual(game.VERSION, migrated['version'])
        self.assertEqual(500, migrated['cash'])
        # Its market was not made by today's rules, so it stays whole.
        self.assertEqual(migrated, door._load(door._store(migrated)))

    def test_nonsense_in_the_compact_form_is_refused(self):
        state = game.new_game(19)
        for market in ({'@': 'x', 'd': {}}, {'@': -1, 'd': {}},
                       {'@': state['draw'] + 5, 'd': {}}, {'@': 0, 'd': {'weed': 9999}}):
            stored = dict(door.compact(state), market=market)
            with self.assertRaises(door.SaveUnavailable):
                door._load(json.dumps(stored))

    def test_changed_market_rules_are_logged_not_silent(self):
        """If market() is ever edited, a compact save rebuilds a different
        market. It still loads -- refusing it would cost the whole run --
        but it says so."""
        state = game.new_game(19)
        stored = door._store(state)
        real = game.market

        def new_rules(s):
            real(s)
            for offer in s['market'].values():
                offer['price'] += 1

        with mock.patch.object(game, 'market', new_rules), \
                self.assertLogs(level=logging.WARNING) as logs:
            loaded = door._load(stored)
        self.assertIn("market rules have changed", "\n".join(logs.output))
        self.assertEqual(state['cash'], loaded['cash'])

    def test_a_compact_form_that_does_not_round_trip_is_never_written(self):
        state = game.new_game(19)
        with mock.patch.object(door, 'expand', lambda stored: ({}, None)), \
                self.assertLogs(level=logging.ERROR):
            written = door._store(state)
        self.assertEqual(state, door_kit.decode_save(written))


class SizeTests(unittest.TestCase):
    def test_the_stored_save_is_a_fraction_of_the_whole_one(self):
        sizes = []
        for seed in range(20):
            for state in play_a_run(seed, moves=40):
                whole = len(json.dumps(state, separators=(',', ':')))
                sizes.append((len(door._store(state)), whole))
        stored = sum(s for s, _ in sizes) / len(sizes)
        whole = sum(w for _, w in sizes) / len(sizes)
        self.assertLess(stored, whole * 0.3, (stored, whole))
        self.assertLess(max(s for s, _ in sizes), 330)


class DoorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        path = os.path.join(self.folder.name, "dw.db")
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

    def test_turn_after_turn_through_the_door_keeps_the_run_exact(self):
        _before, expected, *_ = door.play_state(5, None, "Trader")
        for text in ("travel brooklyn", "buy weed 1", "travel manhattan",
                     "sell weed 1", "travel queens", "m"):
            try:
                expected = game.command(copy.deepcopy(expected), text)[0]
            except ValueError:
                continue
            _before, after, *_ = door.play_state(5, text, "Trader")
            self.assertEqual(expected, after)
            _b, reopened, *_ = door.play_state(5, None, "Trader")
            self.assertEqual(expected, reopened)
        stored = door_kit.load_save(game.GAME_ID, 5)
        self.assertIn('@', stored['market'])


if __name__ == "__main__":
    unittest.main()
