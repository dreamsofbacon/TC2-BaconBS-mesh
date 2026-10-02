"""Door games register once, and the router asks the registry.

The router used to carry four copies of ('ZORK', 'TRIVIA', 'BACONFALL',
'DOPEWARS') and an if/elif chain beside two of them, so a new game meant
finding every one. These hold down what the copies were protecting: a door
session owns its input, every title on the Games menu opens, and replacing a
handler still takes effect.
"""
import types
import unittest
from unittest import mock

import command_handlers as ch
import door_games
import message_processing as mp
import utils


class DoorRegistryTests(unittest.TestCase):
    def test_the_four_original_doors_are_registered(self):
        self.assertEqual({'ZORK', 'TRIVIA', 'BACONFALL', 'DOPEWARS'},
                         set(door_games.commands()) & {'ZORK', 'TRIVIA', 'BACONFALL', 'DOPEWARS'})

    def test_a_menu_is_not_a_door(self):
        for command in ('GAMES_MENU', 'MAIN_MENU', 'MAIL', None):
            with self.subTest(command=command):
                self.assertFalse(door_games.in_session({'command': command}))
        self.assertFalse(door_games.in_session(None))

    def test_every_door_title_on_the_menu_has_a_way_in(self):
        """A door-flagged title with no launcher would fall through to the
        Z-machine path and try to run a story file it does not have."""
        for game_id, info in ch.GAME_LIST:
            if info.get('door'):
                with self.subTest(game=game_id):
                    self.assertIn(game_id, door_games._LAUNCHERS)

    def test_a_replaced_handler_is_the_one_called(self):
        calls = []
        with mock.patch.object(ch, 'handle_zork_steps',
                               lambda *args: calls.append(args)):
            self.assertTrue(door_games.step({'command': 'ZORK'}, 7, 'north', None))
        self.assertEqual([(7, 'north', None)], calls)

    def test_step_refuses_what_is_not_a_door(self):
        self.assertFalse(door_games.step({'command': 'MAIL'}, 7, 'x', None))


class DoorOwnsItsInputTests(unittest.TestCase):
    """Inside any door, keys that mean something at the main menu -- N for
    Ask Nomad, a bang command, a blank line -- go to the game."""

    def setUp(self):
        self.iface = types.SimpleNamespace(nodes={}, bbs_nodes=[], allowed_nodes=[])
        self.addCleanup(utils.user_states.pop, 9191, None)

    def test_every_registered_door_keeps_its_keys(self):
        for command in door_games.commands():
            for text in ('n', '!CM', 's', '?', ''):
                with self.subTest(door=command, text=text):
                    utils.update_user_state(9191, {'command': command, 'step': 1})
                    seen = []
                    with mock.patch.object(door_games, '_current',
                                           lambda handler: lambda *a: seen.append(a[1])), \
                            mock.patch.object(mp, '_auto_update_profile', lambda *a, **k: None):
                        mp.process_message(9191, text, self.iface, is_sync_message=False,
                                           sender_node_id='!abcd9191')
                    self.assertEqual([text], seen)


if __name__ == "__main__":
    unittest.main()
