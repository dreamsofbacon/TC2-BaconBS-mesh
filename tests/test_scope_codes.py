"""PR 3 — single-char scope codes (`scc` capability) wire-format tests."""

import os
import sys
import sqlite3
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import utils
import db_operations


class _DBFixtureMixin:
    def setUp(self):
        conn = sqlite3.connect(":memory:")
        db_operations.thread_local.connection = conn
        db_operations.initialize_database()
        db_operations._clear_peer_caps_cache()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        db_operations._clear_peer_caps_cache()

    def _set_peer_caps(self, peer_id: str, caps_csv: str, proto_v: int = 2):
        db_operations.upsert_peer_sync_state(peer_id, 0, 0, 0, 0, proto_v=proto_v, caps=caps_csv)
        db_operations._clear_peer_caps_cache()


# ── Encoder/decoder ──────────────────────────────────────────────────────────

class EncoderDecoderTests(unittest.TestCase):
    def test_known_scopes_map_to_codes(self):
        self.assertEqual(utils.encode_scope('bulletins', True), 'b')
        self.assertEqual(utils.encode_scope('mail', True), 'm')
        self.assertEqual(utils.encode_scope('channels', True), 'c')
        self.assertEqual(utils.encode_scope('channel_comments', True), 'C')
        self.assertEqual(utils.encode_scope('profiles', True), 'p')
        self.assertEqual(utils.encode_scope('zork_saves', True), 'z')
        self.assertEqual(utils.encode_scope('game_scores', True), 'g')
        self.assertEqual(utils.encode_scope('tombstones', True), 't')

    def test_codes_map_back_to_scopes(self):
        for long_name in (
            'bulletins', 'mail', 'channels', 'channel_comments',
            'profiles', 'zork_saves', 'game_scores', 'tombstones',
        ):
            code = utils.encode_scope(long_name, True)
            self.assertEqual(utils.decode_scope(code), long_name)

    def test_encode_passthrough_when_use_codes_false(self):
        self.assertEqual(utils.encode_scope('bulletins', False), 'bulletins')
        self.assertEqual(utils.encode_scope('zork_saves', False), 'zork_saves')

    def test_decode_passthrough_for_long_names(self):
        # Legacy senders advertise the full word — must pass through unchanged.
        for long_name in (
            'bulletins', 'mail', 'channels', 'channel_comments',
            'profiles', 'zork_saves', 'game_scores', 'tombstones',
        ):
            self.assertEqual(utils.decode_scope(long_name), long_name)

    def test_unknown_token_passes_through(self):
        self.assertEqual(utils.encode_scope('unknown_thing', True), 'unknown_thing')
        self.assertEqual(utils.decode_scope('Q'), 'Q')

    def test_empty_handling(self):
        self.assertEqual(utils.encode_scope('', True), '')
        self.assertEqual(utils.encode_scope(None, True), '')
        self.assertEqual(utils.decode_scope(''), '')
        self.assertEqual(utils.decode_scope(None), '')

    def test_channels_and_channel_comments_disambiguate(self):
        # The two distinct scopes must NOT collide on the wire.
        self.assertNotEqual(
            utils.encode_scope('channels', True),
            utils.encode_scope('channel_comments', True),
        )


# ── Capability wiring ────────────────────────────────────────────────────────

class CapabilityWiringTests(unittest.TestCase):
    def test_scc_is_in_local_capabilities(self):
        self.assertIn('scc', utils.WIRE_CAPABILITIES)

    def test_local_capabilities_token_includes_scc(self):
        token = utils.local_capabilities_token()
        self.assertIn('scc', token)


# ── Per-peer gating ──────────────────────────────────────────────────────────

class PerPeerGatingTests(_DBFixtureMixin, unittest.TestCase):
    def test_peers_all_support_true_when_advertised(self):
        self._set_peer_caps('!peer1', 'cck,epoch,scc')
        self.assertTrue(utils.peers_all_support(['!peer1'], 'scc'))

    def test_peers_all_support_false_when_missing(self):
        self._set_peer_caps('!peer1', 'cck,epoch')  # no scc
        self.assertFalse(utils.peers_all_support(['!peer1'], 'scc'))

    def test_peers_all_support_false_for_empty_set(self):
        self.assertFalse(utils.peers_all_support([], 'scc'))

    def test_one_missing_blocks_use(self):
        self._set_peer_caps('!peer1', 'cck,epoch,scc')
        self._set_peer_caps('!peer2', 'cck,epoch')  # no scc
        self.assertFalse(utils.peers_all_support(['!peer1', '!peer2'], 'scc'))


# ── HASHREQ wire encoding ────────────────────────────────────────────────────

class HashReqWireTests(_DBFixtureMixin, unittest.TestCase):
    def _captured_sends(self):
        return [(call.args[0], call.args[1]) for call in self._sends.call_args_list]

    def setUp(self):
        super().setUp()
        self._sends = mock.MagicMock()
        self._sync_patch = mock.patch.object(utils, '_send_one_sync', self._sends)
        self._sync_patch.start()

    def tearDown(self):
        self._sync_patch.stop()
        super().tearDown()

    def test_scope_code_when_peer_supports_scc(self):
        self._set_peer_caps('!peer1', 'cck,epoch,scc')
        utils.send_hash_request_to_bbs_nodes(['!peer1'], interface=mock.MagicMock(), scope='bulletins')
        sent = self._captured_sends()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 'HASHREQ|b')
        self.assertEqual(sent[0][1], '!peer1')

    def test_scope_long_name_when_peer_lacks_scc(self):
        self._set_peer_caps('!peer1', 'cck,epoch')  # no scc
        utils.send_hash_request_to_bbs_nodes(['!peer1'], interface=mock.MagicMock(), scope='bulletins')
        sent = self._captured_sends()
        self.assertEqual(sent[0][0], 'HASHREQ|bulletins')

    def test_all_passes_through_for_either_peer_type(self):
        self._set_peer_caps('!peer1', 'cck,epoch,scc')
        self._set_peer_caps('!peer2', 'cck,epoch')
        utils.send_hash_request_to_bbs_nodes(['!peer1', '!peer2'], interface=mock.MagicMock(), scope='all')
        sent = self._captured_sends()
        # Both peers receive the literal HASHREQ|all — there is no code for 'all'
        for msg, _peer in sent:
            self.assertEqual(msg, 'HASHREQ|all')

    def test_per_peer_independent_encoding(self):
        # peer1 gets the code form, peer2 gets the long form, in the same call.
        self._set_peer_caps('!peer1', 'cck,epoch,scc')
        self._set_peer_caps('!peer2', 'cck,epoch')
        utils.send_hash_request_to_bbs_nodes(['!peer1', '!peer2'], interface=mock.MagicMock(), scope='zork_saves')
        sent = dict((peer, msg) for msg, peer in self._captured_sends())
        self.assertEqual(sent['!peer1'], 'HASHREQ|z')
        self.assertEqual(sent['!peer2'], 'HASHREQ|zork_saves')


if __name__ == '__main__':
    unittest.main()
