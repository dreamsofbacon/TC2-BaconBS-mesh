"""Contract tests: radio owner, local selection, restart safety and web authority."""
import configparser
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import radio_admin as admin
from radio_link import RadioLink


class FakeRadio:
    def __init__(self, network='meshtastic'):
        self.protocol_name = network
        self.channel_names = {0: 'Custom', 2: '#Public'}
        self.is_connected = True
        self.max_text_bytes = 160 if network == 'meshcore' else 228
        self.nodes = {'peer': {'user': {'longName': 'A peer'}}}
        self.sent = []
        self.localNode = SimpleNamespace(setOwner=lambda **kw: None)
    def getMyNodeInfo(self):
        return {'user': {'longName': 'Local'}}
    def sendText(self, **kw):
        self.sent.append(kw)


class RadioAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'BBS_RADIO_ADMIN_PATH': self.tmp.name + '/queue.db',
                                          'BBS_RUNTIME_DIAG_PATH': self.tmp.name + '/runtime.json'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.pacing = patch('utils.get_user_message_pause_seconds', return_value=0)
        self.pacing.start(); self.addCleanup(self.pacing.stop)
        self.link = RadioLink('primary', FakeRadio())
    def request(self, link=None, **overrides):
        link = link or self.link
        channel = admin.describe(link)['channels'][1]
        return dict(id=str(uuid.uuid4()), radio=link.name, radio_token=admin.describe(link)['token'], action='send', channel=channel['index'],
                    channel_token=channel['token'], text='Hello', **overrides)
    def test_each_network_uses_explicit_channel_without_changing_default(self):
        for network in ['meshtastic', 'meshcore']:
            link = RadioLink('primary', FakeRadio(network))
            self.assertEqual(admin.execute(link, self.request(link))[0], 'submitted')
            self.assertEqual(link.interface.sent[0]['channelIndex'], 2)
            self.assertFalse(link.interface.sent[0]['wantAck'])
    def test_retry_and_new_worker_cannot_duplicate_a_send(self):
        request = self.request()
        admin.submit(request)
        admin.RadioAdminWorker().process_one(self.link)
        admin.submit(request)
        admin.RadioAdminWorker().process_one(self.link)
        self.assertEqual(len(self.link.interface.sent), 1)
        self.assertEqual(admin.operation(request['id'])['status'], 'submitted')
        request['text'] = 'Different'
        with self.assertRaises(ValueError): admin.submit(request)
    def test_invalid_unicode_limit_channel_and_remote_target_never_send(self):
        for change in ({'text': '🌍' * 58}, {'text': ''}, {'channel': 9}, {'channel_token': 'remote'}):
            request = self.request(); request.update(change)
            with self.assertRaises(ValueError): admin.execute(self.link, request)
        request = self.request(); request['radio'] = 'mqtt1'
        with self.assertRaises(ValueError): admin.submit(request)
        self.assertEqual(self.link.interface.sent, [])
    def test_reconnect_invalidates_old_selection_and_does_not_fallback(self):
        request = self.request()
        self.link.interface = FakeRadio()
        with self.assertRaises(ValueError): admin.execute(self.link, request)
        self.link.reconnecting = True
        with self.assertRaises(ValueError): admin.execute(self.link, self.request())
        self.assertEqual(self.link.interface.sent, [])
    def test_channel_rename_invalidates_selection(self):
        request = self.request(); self.link.interface.channel_names[2] = 'Changed'
        with self.assertRaises(ValueError): admin.execute(self.link, request)
    def test_claimed_operation_is_not_replayed_after_restart(self):
        request = self.request(); admin.submit(request)
        with admin.mailbox() as conn:
            conn.execute("UPDATE operations SET status='dispatching', created=?", (time.time()-120,))
        admin.RadioAdminWorker().process_one(self.link)
        self.assertEqual(admin.operation(request['id'])['status'], 'unknown')
        self.assertEqual(self.link.interface.sent, [])
    def test_expired_operation_is_not_sent(self):
        request = self.request(); admin.submit(request)
        with admin.mailbox() as conn: conn.execute('UPDATE operations SET created=?', (time.time()-70,))
        admin.RadioAdminWorker().process_one(self.link)
        self.assertEqual(admin.operation(request['id'])['status'], 'expired')
        self.assertEqual(self.link.interface.sent, [])
    def test_stalled_primary_leaves_secondary_usable(self):
        release = threading.Event(); self.addCleanup(release.set)
        self.link.interface.sendText = lambda **kw: release.wait(3)
        other = RadioLink('secondary', FakeRadio('meshcore'))
        a, b = self.request(), self.request(other)
        admin.submit(a); admin.submit(b)
        worker = admin.RadioAdminWorker(); worker.tick([self.link, other])
        worker.workers['secondary'].join(2)
        self.assertEqual(admin.operation(b['id'])['status'], 'submitted')
        release.set(); worker.workers['primary'].join(2)
    def test_mqtt_only_and_both_primary_placements(self):
        config = configparser.ConfigParser(); config.read_dict({'interface': {'type': 'none'}})
        self.assertEqual(admin.read_radios(config), [])
        for primary, secondary in [('meshcore_serial','serial'), ('serial','meshcore_tcp'), ('meshcore_ble','tcp')]:
            config.read_dict({'interface': {'type': primary}, 'interface2': {'type': secondary}})
            radios = admin.read_radios(config)
            self.assertEqual({r['network'] for r in radios}, {'meshcore','meshtastic'})
            self.assertTrue(all(r['state'] == 'configured' for r in radios))
    def test_stale_snapshot_never_offers_live_controls(self):
        config = configparser.ConfigParser(); config.read_dict({'interface': {'type': 'serial'}})
        path = os.environ['BBS_RUNTIME_DIAG_PATH']
        with open(path, 'w') as out: json.dump({'radio_admin':[admin.describe(self.link)]}, out)
        os.utime(path, (0,0))
        self.assertEqual(admin.read_radios(config)[0]['state'], 'configured')
    def test_missing_names_are_honest(self):
        self.assertEqual(admin.channel_label('meshcore', '', 0), 'Channel 0 (name unknown)')
        self.assertEqual(admin.channel_label('meshcore', '#Public', 0), '#Public')
        self.assertEqual(admin.channel_label('meshcore', 'Custom', 0), 'Custom')

class RadioAdminWebTests(unittest.TestCase):
    def setUp(self):
        import db_operations
        import web_admin
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        config = self.tmp.name + '/config.ini'
        with open(config, 'w') as out: out.write('[admin]\nusername=admin\npassword=test\n[interface]\ntype=none\n')
        env = patch.dict(os.environ, {'BBS_DB_PATH': self.tmp.name + '/bbs.db',
                                    'BBS_CONFIG_PATH': config, 'BBS_RADIO_ADMIN_PATH': self.tmp.name + '/radio.db'})
        env.start(); self.addCleanup(env.stop)
        self.app = web_admin.create_app(); self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        def close():
            conn = getattr(db_operations.thread_local, 'connection', None)
            if conn: conn.close(); del db_operations.thread_local.connection
        self.addCleanup(close)
    def test_stale_status_is_not_reported_connected(self):
        with self.client.session_transaction() as session: session['logged_in'] = True
        path = self.tmp.name + '/runtime.json'
        with open(path, 'w') as out:
            json.dump({'radios':[{'name':'primary', 'radio_protocol':'Meshtastic', 'connected':True}]}, out)
        os.utime(path, (0,0))
        with patch.dict(os.environ, {'BBS_RUNTIME_DIAG_PATH':path}):
            link = self.client.get('/api/status/links').json['links'][0]
        self.assertFalse(link['connected'])
        self.assertTrue(link['stale'])

    def test_read_and_write_require_admin_session(self):
        self.assertEqual(self.client.get('/api/radios').status_code, 302)
        token = self.client.get('/api/csrf-token').json['csrf_token']
        response = self.client.post('/api/radios/operations', json={}, headers={'X-CSRF-Token': token})
        self.assertEqual(response.status_code, 302)
    def test_csrf_is_required_even_with_admin_session(self):
        with self.client.session_transaction() as session: session['logged_in'] = True
        self.assertEqual(self.client.post('/api/radios/operations', json={}).status_code, 403)
    def test_radios_legacy_link_and_mqtt_empty_state(self):
        with self.client.session_transaction() as session: session['logged_in'] = True
        for path in ['/radios', '/system/meshtastic', '/chatter']:
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get('/api/radios').json, {'radios': []})
    def test_authorized_submission_and_repeat_return_same_operation(self):
        with self.client.session_transaction() as session: session['logged_in'] = True
        token = self.client.get('/api/csrf-token').json['csrf_token']
        request = dict(id=str(uuid.uuid4()), action='refresh', radio='primary')
        for _ in range(2):
            response = self.client.post('/api/radios/operations', json=request, headers={'X-CSRF-Token': token})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json['id'], request['id'])

class CaptureNameScopeTests(unittest.TestCase):
    def test_backfill_does_not_rename_another_receiving_radios_channel(self):
        import db_operations as db
        import sqlite3
        previous = getattr(db.thread_local, 'connection', None)
        db.thread_local.connection = sqlite3.connect(':memory:')
        try:
            db.initialize_database()
            for receiver in ['local', 'remote']:
                db.thread_local.connection.execute('''INSERT INTO public_chatter
                    (unique_id, network, channel_index, channel_name, content,
                     message_timestamp, captured_at, capture_node_id, expires_at)
                    VALUES (?, 'meshcore', 0, '', 'hello', '2026-09-09', '2026-09-09', ?, '2026-09-16')''',
                    (receiver, receiver))
            db.thread_local.connection.commit()
            db.backfill_channel_names('meshcore', {0:'Custom'}, capture_node_id='local')
            rows = dict(db.thread_local.connection.execute('SELECT capture_node_id, channel_name FROM public_chatter'))
            self.assertEqual(rows, {'local':'Custom', 'remote':''})
        finally:
            db.thread_local.connection.close()
            if previous is not None: db.thread_local.connection = previous
            else: del db.thread_local.connection
