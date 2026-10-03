"""Every menu-driven door game, held to the same contract.

A new game is a module, a registry line and an entry in the games list.
These run over all of them at once, so the next one cannot ship without a
way in, a way out, rules that fit a packet, or a place on the menu.
"""
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
import utils


class ContractTests(unittest.TestCase):
    def test_each_game_supplies_what_the_door_needs(self):
        for module in ch.MENU_DOORS:
            with self.subTest(game=module.__name__):
                for name in ('GAME_ID', 'COMMAND', 'NAME', 'RULES'):
                    self.assertTrue(getattr(module, name))
                for name in ('new_game', 'validate', 'render', 'respond', 'result', 'handle'):
                    self.assertTrue(callable(getattr(module, name)))

    def test_ids_and_commands_are_unique(self):
        ids = [m.GAME_ID for m in ch.MENU_DOORS]
        commands = [m.COMMAND for m in ch.MENU_DOORS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(commands), len(set(commands)))

    def test_each_game_is_a_door_on_the_menu_under_its_own_name(self):
        for module in ch.MENU_DOORS:
            with self.subTest(game=module.__name__):
                self.assertTrue(door_games.is_door(module.COMMAND))
                self.assertEqual(module.NAME, ch.GAMES[module.GAME_ID]['name'])
                self.assertTrue(ch.GAMES[module.GAME_ID].get('door'))
                self.assertTrue(ch.games_menu_keys(module.GAME_ID))

    def test_rules_and_the_opening_screen_fit_one_packet(self):
        for module in ch.MENU_DOORS:
            with self.subTest(game=module.__name__):
                self.assertTrue(door_kit.fits(module.RULES), len(module.RULES.encode()))
                for seed in range(25):
                    screen = module.render(module.new_game(seed))
                    self.assertNotIn(door_kit.MESSAGE_SEPARATOR, screen)
                    self.assertTrue(door_kit.fits(screen), screen)

    def test_a_new_game_passes_its_own_validation(self):
        for module in ch.MENU_DOORS:
            with self.subTest(game=module.__name__):
                state = module.new_game(7)
                self.assertIs(state, module.validate(state))
                self.assertIsNone(module.result(state))

    def test_the_classics_group_fits_the_menu_budget(self):
        titles = next(t for key, _label, t in ch.game_groups() if key == 'classic')
        screen = "\n".join(["Classics"] + ch._title_lines(titles, None, None) + ["[0] Back"])
        self.assertLessEqual(len(screen.encode('utf-8')), 320, screen)


class PlayThroughTheRouterTests(unittest.TestCase):
    """Each game opened from the Games menu and played through the real
    router: it opens, answers '?', saves on the way out and resumes."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        path = os.path.join(self.folder.name, "doors.db")
        env = mock.patch.dict(os.environ, {"BBS_DB_PATH": path})
        env.start()
        self.addCleanup(env.stop)
        db_operations.thread_local.connection = sqlite3.connect(path)
        db_operations.initialize_database()
        self.addCleanup(self._close)
        self.addCleanup(utils.user_states.pop, 7373, None)
        self.sent = []
        self.iface = types.SimpleNamespace(nodes={}, bbs_nodes=[], allowed_nodes=[])

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def say(self, text):
        import message_processing as mp
        del self.sent[:]
        mp.process_message(7373, text, self.iface, is_sync_message=False,
                           sender_node_id='!door7373')
        return list(self.sent)

    def test_open_ask_leave_and_resume(self):
        import message_processing as mp
        patches = [
            mock.patch.object(ch, 'send_message',
                              side_effect=lambda text, *a, **k: self.sent.append(text) or True),
            mock.patch.object(ch, 'get_node_id_from_num', return_value='!door7373'),
            mock.patch.object(ch, 'get_node_short_name', return_value='Tester'),
            mock.patch.object(ch, 'handle_help_command'),
            mock.patch.object(mp, '_auto_update_profile', lambda *a, **k: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        for module in ch.MENU_DOORS:
            with self.subTest(game=module.__name__):
                ch.handle_games_command(7373, self.iface)
                for key in ch.games_menu_keys(module.GAME_ID):
                    opening = self.say(key)
                self.assertEqual(module.COMMAND, ch.get_user_state(7373)['command'])
                self.assertTrue(opening)

                # '?' explains -- the game's rules, or help for the screen
                # the player is on -- and then shows that screen again.
                asked = self.say("?")
                self.assertGreaterEqual(len(asked), 2)
                self.assertNotEqual(opening[-1], asked[0])
                self.assertEqual(opening[-1], asked[-1])
                for message in asked:
                    self.assertTrue(door_kit.fits(message), message)

                exit_word = sorted(getattr(module, 'EXIT_WORDS', door_kit.EXIT_WORDS))[0]
                left = self.say(exit_word)
                self.assertIn("saved", left[0])
                self.assertEqual('GAMES_MENU', ch.get_user_state(7373)['command'])

                for key in ch.games_menu_keys(module.GAME_ID):
                    resumed = self.say(key)
                self.assertEqual(opening[-1], resumed[-1])
                self.say(exit_word)

                # The rules came with the very first screen, and only then.
                self.assertEqual(module.RULES, opening[0])
                self.assertNotIn(module.RULES, resumed)

    def test_every_game_sends_its_rules_on_the_first_visit_only(self):
        """Puzzles and the keep too: a newcomer is told how to play once,
        then goes straight to the game, with [?] there to ask again."""
        patches = [
            mock.patch.object(ch, 'send_message',
                              side_effect=lambda text, *a, **k: self.sent.append(text) or True),
            mock.patch.object(ch, 'get_node_id_from_num', return_value='!door7373'),
            mock.patch.object(ch, 'get_node_short_name', return_value='Tester'),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for module in ch.MENU_DOORS + ch.DAILY_DOORS + (ch.skilletkeep,):
            with self.subTest(game=module.__name__):
                for visit in range(2):
                    del self.sent[:]
                    door_games.launch(module.GAME_ID, 7373, self.iface)
                    if visit == 0:
                        self.assertEqual(module.RULES, self.sent[0])
                        self.assertGreaterEqual(len(self.sent), 2)
                    else:
                        self.assertNotIn(module.RULES, self.sent)
                del self.sent[:]
                door_games.step(ch.get_user_state(7373), 7373, "help", self.iface)
                self.assertEqual(module.RULES, self.sent[0])


if __name__ == "__main__":
    unittest.main()
