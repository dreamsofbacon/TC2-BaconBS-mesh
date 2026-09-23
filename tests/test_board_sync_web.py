"""The Board Sync panel in the web admin.

The audience lives in the database, not the config file, because a radio
admin sets the same thing from the Bulletin Menu. These tests pin the panel
to that store rather than to a config key, so the two cannot drift apart.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_operations

PEER_A = "!aaaa1111"


class BoardSyncPanelTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp()
        self.config_path = os.path.join(folder, 'config.ini')
        with open(self.config_path, 'w', encoding='utf-8') as handle:
            handle.write('[bbs]\nname = Test\n'
                         '[boards]\nbulletin_boards = General, Ops\n'
                         '[sync]\nbbs_nodes = ' + PEER_A + '\n')
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        import web_admin
        self.web_admin = web_admin
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _post(self, data):
        with mock.patch.dict(os.environ, {'BBS_CONFIG_PATH': self.config_path}):
            app = self.web_admin.create_app()
            app.config['CONFIG_PATH'] = self.config_path
            client = app.test_client()
            with client.session_transaction() as session:
                session['logged_in'] = True
            token = client.get('/api/csrf-token').get_json()['csrf_token']
            payload = {'csrf_token': token, 'settings_section': 'board_sync'}
            payload.update(data)
            return client.post('/settings', data=payload)

    def test_every_board_starts_as_everywhere(self):
        settings = self.web_admin.load_board_sync_settings(self.config_path)
        self.assertEqual(['General', 'Ops'], [row['board'] for row in settings['boards']])
        self.assertTrue(all(row['audience'] == 'all' for row in settings['boards']))

    def test_the_configured_peers_are_offered(self):
        settings = self.web_admin.load_board_sync_settings(self.config_path)
        self.assertIn(PEER_A, [peer['id'] for peer in settings['peers']])

    def test_saving_this_node_only(self):
        self._post({'audience_General': 'all', 'audience_Ops': 'local'})
        self.assertEqual(('local', []), db_operations.get_board_audience('Ops'))
        self.assertEqual(('all', []), db_operations.get_board_audience('General'))

    def test_saving_chosen_nodes(self):
        self._post({'audience_General': 'all', 'audience_Ops': 'peers',
                    'peers_Ops': [PEER_A]})
        self.assertEqual(('peers', [PEER_A]), db_operations.get_board_audience('Ops'))

    def test_what_a_radio_admin_set_is_what_the_panel_shows(self):
        db_operations.set_board_audience('Ops', 'peers', [PEER_A])
        rows = {row['board']: row for row in
                self.web_admin.load_board_sync_settings(self.config_path)['boards']}
        self.assertEqual('peers', rows['Ops']['audience'])
        self.assertEqual([PEER_A], rows['Ops']['peers'])

    def test_channels_are_listed_with_their_own_audience(self):
        with mock.patch.object(db_operations, 'send_channel_to_bbs_nodes'):
            db_operations.add_channel("News", "https://example.invalid/feed", [], None)
        db_operations.set_channel_audience(
            "News", "https://example.invalid/feed", "local", [])
        channels = self.web_admin.load_board_sync_settings(self.config_path)["channels"]
        self.assertEqual(1, len(channels))
        self.assertEqual("local", channels[0]["audience"])

    def test_saving_a_channel_audience(self):
        with mock.patch.object(db_operations, 'send_channel_to_bbs_nodes'):
            db_operations.add_channel("News", "https://example.invalid/feed", [], None)
        self._post({'audience_General': 'all', 'audience_Ops': 'all',
                    'channel_audience_0': 'peers', 'channel_peers_0': [PEER_A]})
        self.assertEqual(('peers', [PEER_A]), db_operations.get_channel_audience(
            "News", "https://example.invalid/feed"))

    def test_chosen_nodes_with_nothing_ticked_is_stored_as_this_node_only(self):
        self._post({'audience_General': 'all', 'audience_Ops': 'peers'})
        self.assertEqual(('local', []), db_operations.get_board_audience('Ops'))


if __name__ == "__main__":
    unittest.main()
