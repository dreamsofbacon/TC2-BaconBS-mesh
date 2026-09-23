"""Editing a post from the web admin.

The old form wrote the row with a direct UPDATE, which peers never hear
about: they reconcile by unique_id, so the next repair pass pushed their
copy of the original back and the edit vanished. The form now goes through
edit_bulletin, which retracts the post and republishes it.
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
LOCAL = "!self0000"


class WebEditTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp()
        self.config_path = os.path.join(folder, 'config.ini')
        with open(self.config_path, 'w', encoding='utf-8') as handle:
            handle.write('[bbs]\nname = Test\n'
                         '[boards]\nbulletin_boards = General, Ops\n'
                         '[sync]\nbbs_nodes = ' + PEER_A + '\n')
        # A file, not :memory:. The row editor opens the database itself
        # through app.config['DB_PATH'], so both halves have to be looking at
        # the same one.
        self.db_path = os.path.join(folder, 'bulletins.db')
        db_operations.thread_local.connection = sqlite3.connect(self.db_path)
        db_operations.initialize_database()
        patcher = mock.patch.object(db_operations, 'get_local_node_id',
                                    return_value=LOCAL)
        patcher.start()
        self.addCleanup(patcher.stop)
        import web_admin
        self.web_admin = web_admin
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _post_bulletin(self, content="the original text", board="General"):
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'):
            unique_id = db_operations.add_bulletin(
                board, "caller", "a subject", content, [], None)
        row = db_operations.get_db_connection().execute(
            "SELECT id FROM bulletins WHERE unique_id = ?", (unique_id,)).fetchone()
        return unique_id, int(row[0])

    def _client(self):
        app = self.web_admin.create_app()
        app.config['CONFIG_PATH'] = self.config_path
        app.config['DB_PATH'] = self.db_path
        app.config['BULLETIN_BOARDS'] = ['General', 'Ops']
        client = app.test_client()
        with client.session_transaction() as session:
            session['logged_in'] = True
        return client

    def _edit(self, row_id, **fields):
        with mock.patch.dict(os.environ, {'BBS_CONFIG_PATH': self.config_path}), \
                mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'), \
                mock.patch.object(db_operations, 'send_delete_bulletin_to_bbs_nodes') as retract:
            client = self._client()
            token = client.get('/api/csrf-token').get_json()['csrf_token']
            data = {'csrf_token': token, 'board': 'General',
                    'sender_short_name': 'caller', 'date': '2026-09-01 10:00',
                    'subject': 'a subject', 'content': 'the original text',
                    'audience': 'all'}
            data.update(fields)
            response = client.post(f'/bulletins/{row_id}/edit', data=data,
                                   follow_redirects=True)
        return response, retract

    def _stored(self):
        return db_operations.get_db_connection().execute(
            "SELECT unique_id, subject, content, local_only, sync_peers"
            " FROM bulletins").fetchall()

    def test_editing_the_text_republishes_the_post(self):
        _unique_id, row_id = self._post_bulletin()
        _response, retract = self._edit(row_id, content='a much shorter text')
        retract.assert_called_once()
        rows = self._stored()
        self.assertEqual(1, len(rows))
        self.assertEqual('a much shorter text', rows[0][2])

    def test_the_post_gets_a_new_id_so_peers_take_the_new_version(self):
        unique_id, row_id = self._post_bulletin()
        self._edit(row_id, content='changed')
        self.assertNotEqual(unique_id, self._stored()[0][0])

    def test_the_audience_can_be_set_on_one_post(self):
        _unique_id, row_id = self._post_bulletin()
        self._edit(row_id, content='changed', audience='peers', peers=[PEER_A])
        self.assertEqual(PEER_A, self._stored()[0][4])

    def test_this_node_only_is_stored_on_the_post(self):
        _unique_id, row_id = self._post_bulletin()
        self._edit(row_id, content='changed', audience='local')
        self.assertEqual(1, self._stored()[0][3])

    def test_a_post_from_another_node_is_refused_with_a_reason(self):
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'):
            db_operations.add_bulletin(
                "General", "someone", "theirs", "their text", [], None,
                unique_id="from-elsewhere", source_node_id="!peer9999",
                source_timestamp="2026-09-01T00:00:00Z")
        row_id = int(db_operations.get_db_connection().execute(
            "SELECT id FROM bulletins WHERE unique_id = 'from-elsewhere'").fetchone()[0])
        response, retract = self._edit(row_id, subject='theirs', content='rewritten',
                                       sender_short_name='someone')
        retract.assert_not_called()
        self.assertIn("another node", response.get_data(as_text=True))
        self.assertEqual('their text', self._stored()[0][2])


if __name__ == "__main__":
    unittest.main()
