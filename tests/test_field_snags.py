"""The small snags left over from the 2026-09-20 field report.

Each is minor on its own; together they were most of what made the tester
stop and re-read a screen.
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import command_handlers as ch
import trivia_port


class TriviaEmptyRunTests(unittest.TestCase):
    """Opening Trivia King and leaving put a zero on the scoreboard and said
    "Your score was saved." for a run in which nothing was answered."""

    def setUp(self):
        self.addCleanup(ch.update_user_state, 6060, None)
        self.addCleanup(trivia_port._sessions.pop, 6060, None)

    def test_the_exit_says_nothing_was_saved(self):
        trivia_port._sessions[6060] = {"score": 0, "moves": 0}
        reply = trivia_port.command(6060, "x")
        self.assertIn("nothing was saved", reply)
        self.assertNotIn("Your score was saved", reply)

    def test_an_empty_run_is_not_scored(self):
        ch.update_user_state(6060, {'command': 'TRIVIA', 'step': 1,
                                    'game_id': trivia_port.GAME_ID})
        trivia_port._sessions[6060] = {"score": 0, "moves": 0}
        with mock.patch.object(ch, 'send_message', lambda *a, **k: True), \
                mock.patch.object(ch, 'handle_games_command'), \
                mock.patch.object(ch, 'upsert_game_score') as saved:
            ch.handle_trivia_steps(6060, 'x', types.SimpleNamespace())
        saved.assert_not_called()

    def test_a_real_run_still_is(self):
        ch.update_user_state(6060, {'command': 'TRIVIA', 'step': 1,
                                    'game_id': trivia_port.GAME_ID})
        trivia_port._sessions[6060] = {"score": 300, "moves": 2}
        with mock.patch.object(ch, 'send_message', lambda *a, **k: True), \
                mock.patch.object(ch, 'handle_games_command'), \
                mock.patch.object(ch, 'get_node_id_from_num', return_value='!a'), \
                mock.patch.object(ch, 'get_node_short_name', return_value='a'), \
                mock.patch.object(ch, 'upsert_game_score') as saved:
            ch.handle_trivia_steps(6060, 'x', types.SimpleNamespace())
        saved.assert_called_once()


class HardwareLabelTests(unittest.TestCase):
    """Stats listed "133: 1" and "254: 1" beside real hardware names."""

    def test_a_name_is_left_alone(self):
        self.assertEqual('HELTEC_V3', ch._hardware_label('HELTEC_V3'))

    def test_a_bare_number_is_never_shown_bare(self):
        label = ch._hardware_label(254)
        self.assertNotEqual('254', label)
        self.assertTrue(label)

    def test_a_numeric_string_is_treated_as_a_number(self):
        self.assertNotEqual('133', ch._hardware_label('133'))

    def test_nothing_useful_says_unknown(self):
        self.assertEqual('Unknown', ch._hardware_label(None))


class WordingTests(unittest.TestCase):

    def test_the_main_tip_no_longer_promises_zero_goes_back(self):
        """On the main menu [0] disconnects, as the banner above says."""
        self.assertNotIn('[0] always goes back', ch.HELP_TIPS['main'])

    def test_no_table_flip_on_success(self):
        """The tester re-read it to check it was not an error."""
        source = open(ch.__file__, encoding='utf-8').read()
        self.assertNotIn('╯°□°', source)

    def test_linked_devices_names_a_wrong_key(self):
        sent = []
        ch.update_user_state(6061, {'command': 'ACCOUNT', 'step': 1})
        self.addCleanup(ch.update_user_state, 6061, None)
        with mock.patch.object(ch, 'send_message',
                               side_effect=lambda text, *a, **k: sent.append(text)):
            ch.handle_account_steps(6061, '9', types.SimpleNamespace(),
                                    sender_node_id='!abcd1234')
        self.assertTrue(sent[-1].startswith('Invalid choice.'))


if __name__ == '__main__':
    unittest.main()
