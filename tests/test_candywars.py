"""Candy Wars by default, Dope Wars in PG-13 mode, played by menu.

The first class is the one that matters most. Candy Wars is what a child on
the mesh sees, and "kid-friendly" is not something to hope the text is: this
drives every screen and every outcome in that theme and fails on any word
from the drug, police or violence vocabulary the engine was written in.
"""

import json
import os
import re
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dopewars as game
import dopewars_menu as menu
import dopewars_theme


BANNED = ('weed', 'hash', 'acid', 'cocaine', 'coke', 'drug', 'drugs', 'dope',
          'police', 'cop', 'cops', 'fight', 'fought', 'weapon', 'gun', 'shoot',
          'kill', 'beaten', 'dealer', 'deal', 'high', 'stoned', 'smoke', 'crack',
          'narc', 'shark', 'vest', 'medkit', 'confiscated')
_BANNED_RE = re.compile(r"\b(" + "|".join(BANNED) + r")\b", re.IGNORECASE)


class _DbCase(unittest.TestCase):
    """A fresh database per test, with runs written straight into it."""

    def setUp(self):
        folder = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ,
                                  {'BBS_DB_PATH': os.path.join(folder, 'cw.db')})
        patcher.start()
        self.addCleanup(patcher.stop)
        import db_operations
        self.db = db_operations
        self.db.initialize_database()
        self.addCleanup(self._close)
        self.outputs = []

    def _close(self):
        try:
            self.db.get_db_connection().close()
        except Exception:
            pass

    def seed(self, user, seed=7, **overrides):
        """Write a run in a given state, validated the way the door would."""
        state = game.new_game(seed)
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(state.get(key), dict):
                state[key].update(value)
            else:
                state[key] = value
        game.validate(state)
        from player_identity import player_key
        conn = self.db.get_db_connection()
        conn.execute('CREATE TABLE IF NOT EXISTS dopewars_runs '
                     '(user_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)')
        conn.execute('INSERT OR REPLACE INTO dopewars_runs VALUES (?, ?)',
                     (player_key(user), json.dumps(state)))
        conn.commit()
        return state

    def saved(self, user):
        from player_identity import player_key
        row = self.db.get_db_connection().execute(
            'SELECT state_json FROM dopewars_runs WHERE user_id = ?',
            (player_key(user),)).fetchone()
        return json.loads(row[0])

    def play(self, user, text, pg13=False, nav=None):
        reply, leave, nav = menu.handle(user, text, 'kid', pg13, nav)
        self.outputs.append(reply)
        return reply, leave, nav

    def script(self, user, words, pg13=False):
        """Play a sequence of replies from the entry screen; return the last."""
        reply, leave, nav = self.play(user, None, pg13)
        for word in words:
            reply, leave, nav = self.play(user, word, pg13, nav)
            if leave:
                break
        return reply, leave, nav


class KidSafeTests(_DbCase):
    """Every Candy Wars screen and outcome, against the banned vocabulary."""

    def assert_kid_safe(self):
        for text in self.outputs:
            found = _BANNED_RE.findall(text)
            self.assertFalse(found, f"{found} in Candy Wars output:\n{text}")

    def test_the_whole_menu_tree(self):
        self.seed(1, cash=5000, inventory={'weed': 3, 'acid': 1})
        for words in (['1'], ['1', '2'], ['1', '2', '1'], ['2'], ['2', '1', '1'],
                      ['3'], ['3', '1'], ['4'], ['5'], ['5', '1'], ['5', '2'],
                      ['5', '3'], ['5', '4'], ['6'], ['6', '1'], ['6', '1', '100'],
                      ['6', '2'], ['6', '2', '50'], ['7'], ['7', '0'],
                      ['9'], ['zz'], ['1', '9'], ['?'], ['']):
            self.script(1, words)
        self.assert_kid_safe()

    def test_every_encounter_outcome(self):
        # Talked your way out: one point of patience left.
        self.seed(2, phase='police', enemy_hp=1)
        self.script(2, ['1'])
        # A lecture that empties your energy: sent to the office.
        self.seed(3, phase='police', enemy_hp=45, hp=1)
        self.script(3, ['1'])
        # Handing over your candy.
        self.seed(4, phase='police', enemy_hp=45, inventory={'hash': 2})
        self.script(4, ['3'])
        # Running, over enough seeds to see both getting away and not.
        for seed in range(20):
            self.seed(10 + seed, seed=seed, phase='police', enemy_hp=45)
            self.script(10 + seed, ['2'])
        self.assert_kid_safe()

    def test_the_endings(self):
        # Missing the payback deadline.
        self.seed(5, days=365, day=30, loan_due=30)
        self.script(5, ['3', '1'])
        # Ending on purpose, and the screen after.
        self.seed(6, inventory={'cocaine': 1})
        self.script(6, ['7', 'y'])
        self.script(6, ['1'])
        # The last day of a run.
        self.seed(7, day=29)
        self.script(7, ['3', '1'])
        self.assert_kid_safe()

    def test_exit_and_refusals(self):
        self.seed(8, cash=0)
        for words in (['x'], ['5', '1'], ['1', '1'], ['2', '1'], ['6', '2']):
            self.script(8, words)
        self.seed(9, capacity=70, armor=1, weapon=1)
        for words in (['5', '1'], ['5', '2'], ['5', '3'], ['5', '4']):
            self.script(9, words)
        self.assert_kid_safe()

    def test_the_title_everywhere_a_game_is_listed(self):
        import command_handlers as ch
        sent = []
        iface = types.SimpleNamespace(bbs_nodes=[], nodes={})
        with mock.patch.object(ch, 'send_message',
                               side_effect=lambda text, *a, **k: sent.append(text)), \
                mock.patch.object(ch, 'get_node_id_from_num', return_value='!kid00001'), \
                mock.patch.object(ch, 'effective_pg13', return_value=False), \
                mock.patch.object(ch, 'handle_help_command'):
            ch.handle_games_command(77, iface)
            ch.handle_scoreboard_command(77, iface)
            ch.handle_hall_of_fame_command(77, iface)
        self.addCleanup(ch.update_user_state, 77, None)
        joined = "\n".join(sent)
        self.assertIn('Candy Wars', joined)
        self.assertNotIn('Dope', joined)


class PacketBudgetTests(_DbCase):
    """Every screen fits one Meshtastic packet, in both themes -- the point of
    moving to menus was a turn costing one packet instead of two."""

    BUDGET = 200

    def test_every_screen_in_both_themes(self):
        big = dict(cash=9_999_999, debt=9_999, loan_due=394, day=365, days=365,
                   capacity=70, inventory={'weed': 20, 'hash': 20, 'acid': 15,
                                           'cocaine': 15},
                   armor=1, weapon=1)
        for pg13 in (False, True):
            self.outputs = []
            self.seed(20, **big)
            for words in (['1'], ['1', '1'], ['2'], ['2', '1'], ['3'], ['4'],
                          ['5'], ['6'], ['6', '1'], ['6', '2'], ['7'], ['9']):
                self.script(20, words, pg13=pg13)
            self.seed(21, phase='police', enemy_hp=45)
            self.script(21, [], pg13=pg13)
            self.seed(22, phase='ended', outcome='Completed', enemy_hp=0)
            self.script(22, [], pg13=pg13)
            for text in self.outputs:
                self.assertLessEqual(len(text.encode('utf-8')), self.BUDGET,
                                     f"{len(text.encode())} bytes:\n{text}")


class MenuTests(_DbCase):

    def test_one_reply_buys_a_chosen_quantity(self):
        state = self.seed(30, cash=5000)
        price = state['market']['hash']['price']
        reply, _, _ = self.script(30, ['1 2 3'])
        self.assertEqual(3, self.saved(30)['inventory']['hash'])
        self.assertEqual(5000 - 3 * price, self.saved(30)['cash'])
        self.assertIn('Bought 3 Jelly beans', reply)

    def test_m_buys_as_many_as_you_can(self):
        state = self.seed(31, cash=5000)
        self.script(31, ['1 1 m'])
        expected = menu._max_buy(state, 'weed')
        self.assertEqual(expected, self.saved(31)['inventory']['weed'])

    def test_a_step_at_a_time_works_too(self):
        self.seed(32, cash=5000)
        self.script(32, ['1', '2', '2'])
        self.assertEqual(2, self.saved(32)['inventory']['hash'])

    def test_too_many_is_refused_and_nothing_changes(self):
        before = self.seed(33, cash=5000)
        reply, _, nav = self.script(33, ['1 2 999'])
        self.assertEqual(before['inventory'], self.saved(33)['inventory'])
        self.assertIn('Pick a number', reply)
        self.assertEqual('buy_qty', nav['menu'])

    def test_zero_goes_back_a_level_and_exits_from_the_top(self):
        self.seed(34)
        _, leave, nav = self.script(34, ['1', '2', '0'])
        self.assertEqual('buy', nav['menu'])
        _, leave, nav = self.script(34, ['1', '0'])
        self.assertEqual('main', nav['menu'])
        _, leave, _ = self.script(34, ['0'])
        self.assertTrue(leave)

    def test_moving_costs_a_day(self):
        before = self.seed(35)
        self.script(35, ['3 1'])
        after = self.saved(35)
        self.assertTrue(after['day'] == before['day'] + 1 or after['phase'] == 'police')

    def test_the_gear_prices_are_the_engines(self):
        """The menu checks gear before asking, so its prices must be the ones
        the engine actually charges."""
        for key, price in menu.GEAR:
            state = game.new_game(1)
            state['cash'], state['hp'] = 5000, 50
            after, _reply, _ = game.command(state, f"equipment {key}")
            self.assertEqual(5000 - price, after['cash'], key)

    def test_switching_pg13_mid_run_changes_only_the_words(self):
        self.seed(36, cash=5000)
        self.script(36, ['1 1 2'])
        before = self.saved(36)
        candy, _, _ = self.play(36, None, pg13=False)
        dope, _, _ = self.play(36, None, pg13=True)
        self.assertEqual(before, self.saved(36))
        self.assertIn('Candy Wars', candy)
        self.assertIn('Dope Wars', dope)
        self.assertIn(f"${before['cash']}", candy)
        self.assertIn(f"${before['cash']}", dope)


class Pg13SettingTests(_DbCase):
    """Off by default, a person's own choice, and lockable per node."""

    NODE = '!aaaa0001'

    def test_off_for_a_newcomer(self):
        self.assertFalse(self.db.effective_pg13(self.NODE))

    def test_a_person_can_turn_it_on(self):
        self.db.set_pg13_for_node(self.NODE, True, 'meshtastic')
        self.assertTrue(self.db.effective_pg13(self.NODE))

    def test_a_lock_decides_for_everyone_both_ways(self):
        self.db.set_pg13_for_node(self.NODE, True, 'meshtastic')
        with mock.patch.object(self.db, 'pg13_user_control', return_value=False), \
                mock.patch.object(self.db, 'pg13_node_mode', return_value=False):
            self.assertFalse(self.db.effective_pg13(self.NODE))
        with mock.patch.object(self.db, 'pg13_user_control', return_value=False), \
                mock.patch.object(self.db, 'pg13_node_mode', return_value=True):
            self.assertTrue(self.db.effective_pg13('!bbbb0002'))

    def test_a_lock_never_erases_anyones_choice(self):
        self.db.set_pg13_for_node(self.NODE, True, 'meshtastic')
        with mock.patch.object(self.db, 'pg13_user_control', return_value=False), \
                mock.patch.object(self.db, 'pg13_node_mode', return_value=False):
            self.db.effective_pg13(self.NODE)
        self.assertTrue(self.db.effective_pg13(self.NODE))

    def test_a_newer_synced_choice_wins_and_an_older_one_does_not(self):
        records = self.db.set_pg13_for_node(self.NODE, True, 'meshtastic')
        stamp = records[0][2]
        self.assertFalse(self.db.apply_synced_pg13_preference(
            self.NODE, False, '2000-01-01T00:00:00+00:00'))
        self.assertTrue(self.db.effective_pg13(self.NODE))
        self.assertTrue(self.db.apply_synced_pg13_preference(
            self.NODE, False, '2099-01-01T00:00:00+00:00'))
        self.assertFalse(self.db.effective_pg13(self.NODE))
        self.assertTrue(stamp)

    def test_an_unknown_device_is_ignored(self):
        self.assertFalse(self.db.apply_synced_pg13_preference(
            '!nobody00', True, '2099-01-01T00:00:00+00:00'))


class Pg13WireTests(unittest.TestCase):

    def test_the_capability_is_advertised(self):
        import utils
        self.assertIn('pg13', utils.WIRE_CAPABILITIES)

    def test_the_frame(self):
        import utils
        sent = []
        with mock.patch.object(utils, '_send_one_sync',
                               side_effect=lambda msg, *a, **k: sent.append(msg)), \
                mock.patch('db_operations.peer_supports', return_value=True):
            utils.send_pg13_preference_to_bbs_nodes('!a', True, 'stamp', ['!p'], object())
        self.assertEqual(['CONTENTPREF|!a|1|stamp'], sent)

    def test_an_old_peer_is_not_sent_one(self):
        import utils
        with mock.patch('db_operations.peer_supports', return_value=False), \
                mock.patch.object(utils, '_send_one_sync') as send:
            utils.send_pg13_preference_to_bbs_nodes('!a', True, 's', ['!old'], object())
        send.assert_not_called()

    def test_the_frame_is_classified_as_sync_traffic(self):
        """Both halves: handled, and on the allow-list that decides what
        counts as sync traffic -- the list that silently dropped BBSID."""
        source = open('message_processing.py', encoding='utf-8').read()
        self.assertIn('message.startswith("CONTENTPREF|")', source)
        start = source.index('"SCORESYNC|", "ROLE|", "BBSID|"')
        self.assertIn('"CONTENTPREF|"', source[start:start + 250])


class SettingsScreenTests(_DbCase):

    def setUp(self):
        super().setUp()
        import command_handlers as ch
        self.ch = ch
        self.sent = []
        patcher = mock.patch.object(
            ch, 'send_message',
            side_effect=lambda text, *a, **k: self.sent.append(text) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(ch.update_user_state, 88, None)
        self.iface = types.SimpleNamespace(bbs_nodes=[], nodes={})

    def test_the_setting_is_on_the_screen(self):
        with mock.patch.object(self.ch, 'effective_pg13', return_value=False):
            text = self.ch._settings_menu_text(88, self.iface, '!kid00001')
        self.assertIn('[8] PG-13 mode: Off', text)

    def test_a_lock_is_visible_and_refuses_the_change(self):
        with mock.patch('db_operations.pg13_user_control', return_value=False), \
                mock.patch.object(self.ch, 'effective_pg13', return_value=False), \
                mock.patch.object(self.ch, 'handle_settings_command'):
            self.assertIn('set by this node', self.ch._pg13_label('!kid00001'))
            self.ch.handle_settings_steps(88, '8', self.iface, '!kid00001')
        self.assertIn("operator sets PG-13 mode", self.sent[-1])

    def test_turning_it_on_asks_first(self):
        with mock.patch('db_operations.pg13_user_control', return_value=True), \
                mock.patch.object(self.ch, 'effective_pg13', return_value=False):
            self.ch.handle_settings_steps(88, '8', self.iface, '!kid00001')
        self.assertIn('[Y/N]', self.sent[-1])
        self.assertTrue(self.ch.get_user_state(88)['pg13'])


if __name__ == '__main__':
    unittest.main()
