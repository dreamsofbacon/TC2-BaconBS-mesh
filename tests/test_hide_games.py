"""An operator can take games off this node's menu from the web admin.

Hiding is per node and read live from [games] hidden, so every screen that
lists games -- the menu, the Scoreboard, the Hall of Fame -- has to agree on
what is shown and on what number each title carries.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import command_handlers as ch


def _hidden(value):
    """Patch [games] hidden to *value* for the duration of a test."""
    return mock.patch.object(
        ch, '_config_raw',
        lambda section, option: value if (section, option) == ('games', 'hidden') else None)


class VisibleGamesTests(unittest.TestCase):

    def test_nothing_hidden_by_default(self):
        with _hidden(None):
            self.assertEqual(len(ch.GAME_LIST), len(ch.visible_games()))

    def test_a_hidden_game_leaves_the_list(self):
        with _hidden('dopewars'):
            ids = [game_id for game_id, _ in ch.visible_games()]
        self.assertNotIn('dopewars', ids)
        self.assertIn('trivia', ids)

    def test_the_setting_forgives_spaces_and_case(self):
        with _hidden(' DopeWars , zork3 '):
            ids = [game_id for game_id, _ in ch.visible_games()]
        self.assertNotIn('dopewars', ids)
        self.assertNotIn('zork3', ids)

    def test_order_is_kept(self):
        with _hidden('zork2'):
            ids = [game_id for game_id, _ in ch.visible_games()]
        full = [game_id for game_id, _ in ch.GAME_LIST if game_id != 'zork2']
        self.assertEqual(full, ids)


class MenuTests(unittest.TestCase):
    """Every screen that numbers games has to number them the same way."""

    def setUp(self):
        self.sent = []
        patcher = mock.patch.object(
            ch, 'send_message',
            side_effect=lambda text, *a, **k: self.sent.append(text) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(ch.update_user_state, 5150, None)
        self.iface = types.SimpleNamespace(bbs_nodes=[], nodes={})

    def test_the_menu_renumbers_around_a_hidden_game(self):
        with _hidden('trivia'):
            ch.handle_games_command(5150, self.iface)
        first_line = self.sent[-1].splitlines()[1]
        self.assertTrue(first_line.startswith('[1] Zork I'), first_line)
        self.assertNotIn('Trivia', self.sent[-1])

    def test_a_number_opens_the_title_shown_beside_it(self):
        """The number a player reads must launch that game, not whatever
        sat at that position before the list was filtered."""
        launched = []
        with _hidden('trivia'), \
                mock.patch.object(ch, '_launch_game',
                                  side_effect=lambda s, i, gid, name: launched.append(gid)):
            ch.handle_games_steps(5150, '1', self.iface)
        self.assertEqual(['zork1'], launched)

    def test_a_number_past_the_shorter_list_is_refused(self):
        with _hidden('trivia'):
            ch.handle_games_steps(5150, str(len(ch.GAME_LIST)), self.iface)
        self.assertIn('Invalid choice', self.sent[-1])
        self.assertIn(f'1-{len(ch.GAME_LIST) - 1}', self.sent[-1])

    def test_the_scoreboard_numbers_match_the_menu(self):
        with _hidden('trivia'):
            ch.handle_scoreboard_command(5150, self.iface)
        self.assertNotIn('Trivia', self.sent[-1])
        self.assertIn('[1] Zork I', self.sent[-1])

    def test_every_game_hidden_says_so(self):
        """Not a menu of nothing but a scoreboard for games nobody can play."""
        every = ','.join(game_id for game_id, _ in ch.GAME_LIST)
        with _hidden(every), \
                mock.patch.object(ch, 'handle_help_command'):
            ch.handle_games_command(5150, self.iface)
        self.assertIn('No games are available', self.sent[-1])


class WebAdminTests(unittest.TestCase):
    """The Games panel writes the complement of what is ticked."""

    def setUp(self):
        folder = tempfile.mkdtemp()
        self.config_path = os.path.join(folder, 'config.ini')
        with open(self.config_path, 'w', encoding='utf-8') as handle:
            handle.write('[bbs]\nname = Test\n')
        import web_admin
        self.web_admin = web_admin

    def test_everything_is_shown_on_a_fresh_config(self):
        settings = self.web_admin.load_games_settings(self.config_path)
        self.assertTrue(all(game['shown'] for game in settings['games']))

    def test_a_hidden_game_loads_unticked(self):
        with open(self.config_path, 'a', encoding='utf-8') as handle:
            handle.write('\n[games]\nhidden = dopewars\n')
        shown = {g['id']: g['shown'] for g in
                 self.web_admin.load_games_settings(self.config_path)['games']}
        self.assertFalse(shown['dopewars'])
        self.assertTrue(shown['trivia'])

    def test_saving_stores_the_unticked_games(self):
        with mock.patch.dict(os.environ, {'BBS_CONFIG_PATH': self.config_path}):
            app = self.web_admin.create_app()
            app.config['CONFIG_PATH'] = self.config_path
            client = app.test_client()
            with client.session_transaction() as session:
                session['logged_in'] = True
            token = client.get('/api/csrf-token').get_json()['csrf_token']
            import zork_port
            ticked = [g for g in zork_port.GAMES if g not in ('dopewars', 'zork3')]
            client.post('/settings', data={
                'csrf_token': token, 'settings_section': 'games',
                'game_shown': ticked})
        shown = {g['id']: g['shown'] for g in
                 self.web_admin.load_games_settings(self.config_path)['games']}
        self.assertFalse(shown['dopewars'])
        self.assertFalse(shown['zork3'])
        self.assertTrue(shown['trivia'])


if __name__ == '__main__':
    unittest.main()
