"""PR 4 — drop base64 on text fields (`nob64` capability) wire-format tests."""

import base64
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


# ── helpers: round-trip ──────────────────────────────────────────────────────

class HelperRoundTripTests(unittest.TestCase):
    def _rt(self, s):
        # Plain encoding then decoded must match original.
        enc = utils.encode_text(s, use_plain=True)
        self.assertTrue(enc.startswith('~') or enc == '')
        self.assertNotIn('|', enc)  # critical: no unescaped pipe in wire form
        dec = utils.decode_text(enc)
        self.assertEqual(dec, s if s is not None else '')

    def test_simple_ascii(self):
        self._rt('hello')

    def test_with_pipes(self):
        self._rt('a|b|c')

    def test_with_backslashes(self):
        self._rt(r'C:\path\to\file')

    def test_mixed_pipes_and_backslashes(self):
        self._rt(r'foo\|bar\\|baz')

    def test_unicode(self):
        self._rt('héllo 🌮 wörld')

    def test_empty_string(self):
        self.assertEqual(utils.encode_text('', use_plain=True), '')
        self.assertEqual(utils.decode_text(''), '')

    def test_none(self):
        self.assertEqual(utils.encode_text(None, use_plain=True), '')
        self.assertEqual(utils.decode_text(None), '')

    def test_starts_with_tilde(self):
        # User content beginning with '~' must still round-trip — the sentinel
        # is the leading char of the encoded wire form, not of the source.
        self._rt('~rooted')
        self._rt('~~~')

    def test_legacy_base64_still_decodes(self):
        # Old peers send base64; decoder must accept it.
        legacy = base64.b64encode('legacy text'.encode('utf-8')).decode('ascii')
        self.assertFalse(legacy.startswith('~'))
        self.assertEqual(utils.decode_text(legacy), 'legacy text')

    def test_legacy_empty_b64_is_empty(self):
        self.assertEqual(utils.decode_text(''), '')

    def test_plain_encoding_skipped_when_not_capable(self):
        out = utils.encode_text('hello', use_plain=False)
        self.assertFalse(out.startswith('~'))
        self.assertEqual(base64.b64decode(out.encode('ascii')).decode('utf-8'), 'hello')


# ── pipe_escape / pipe_unescape internals ────────────────────────────────────

class PipeEscapeTests(unittest.TestCase):
    def test_pipe_replaced_with_backslash_p(self):
        self.assertEqual(utils.pipe_escape('a|b'), r'a\pb')

    def test_backslash_doubled(self):
        self.assertEqual(utils.pipe_escape(r'a\b'), r'a\\b')

    def test_escape_order_pipe_in_backslash_first(self):
        # '\|' should not become '\\p' (backslash then literal pipe) — verify the
        # encoded form has no literal '|'.
        result = utils.pipe_escape(r'\|')
        self.assertNotIn('|', result)
        self.assertEqual(utils.pipe_unescape(result), r'\|')

    def test_unescape_unknown_escape_drops_backslash(self):
        # Forward-compat: '\x' decodes to 'x' so future codes don't break old peers.
        self.assertEqual(utils.pipe_unescape(r'a\xb'), 'axb')


# ── capability wiring ────────────────────────────────────────────────────────

class CapabilityWiringTests(unittest.TestCase):
    def test_nob64_in_wire_capabilities(self):
        self.assertIn('nob64', utils.WIRE_CAPABILITIES)


# ── sender behaviour (per-peer gating) ───────────────────────────────────────

class _CaptureSender(_DBFixtureMixin, unittest.TestCase):
    """Captures _send_one_sync calls so we can inspect emitted wire frames."""

    def _capture(self, fn, *args, **kwargs):
        sent = []

        def _fake(message, node_id, *_a, **_kw):
            sent.append((str(node_id), str(message)))

        with mock.patch.object(utils, '_send_one_sync', side_effect=_fake):
            fn(*args, **kwargs)
        return sent


class ProfileSyncWireTests(_CaptureSender):
    def test_plain_when_all_peers_capable(self):
        self._set_peer_caps('!aaa', 'nob64,epoch')
        self._set_peer_caps('!bbb', 'nob64,epoch')
        sent = self._capture(
            utils.send_profile_to_bbs_nodes,
            '!user', 'Shorty', 'Long Name', 1700000000, 1700000100, 5, 'My bio', ['!aaa', '!bbb'], None,
        )
        self.assertEqual(len(sent), 2)
        for _peer, msg in sent:
            parts = msg.split('|')
            # parts: PROFILESYNC | user_id | short | long | first | last | sent | bio
            self.assertTrue(parts[2].startswith('~'), f"short_name not plain: {parts[2]!r}")
            self.assertTrue(parts[3].startswith('~'), f"long_name not plain: {parts[3]!r}")
            self.assertTrue(parts[7].startswith('~'), f"bio not plain: {parts[7]!r}")
            self.assertEqual(utils.decode_text(parts[2]), 'Shorty')
            self.assertEqual(utils.decode_text(parts[7]), 'My bio')

    def test_base64_when_any_peer_not_capable(self):
        self._set_peer_caps('!aaa', 'nob64,epoch')
        self._set_peer_caps('!bbb', 'epoch')  # legacy: no nob64
        sent = self._capture(
            utils.send_profile_to_bbs_nodes,
            '!user', 'Shorty', 'Long Name', 1700000000, 1700000100, 5, 'My bio', ['!aaa', '!bbb'], None,
        )
        for _peer, msg in sent:
            parts = msg.split('|')
            self.assertFalse(parts[2].startswith('~'))
            self.assertEqual(base64.b64decode(parts[2].encode('ascii')).decode('utf-8'), 'Shorty')


class ScoreSyncWireTests(_CaptureSender):
    def test_plain_short_name(self):
        self._set_peer_caps('!aaa', 'nob64')
        sent = self._capture(
            utils.send_game_score_to_bbs_nodes,
            '!user', 'zork1', 'Shorty', 100, 350, 42, 1700000000, ['!aaa'], None,
        )
        parts = sent[0][1].split('|')
        # SCORESYNC | user | game | short | score | max | moves | ts
        self.assertTrue(parts[3].startswith('~'))
        self.assertEqual(utils.decode_text(parts[3]), 'Shorty')


class DeleteZorkSaveWireTests(_CaptureSender):
    def test_plain_ids_when_capable(self):
        self._set_peer_caps('!aaa', 'nob64,epoch')
        with mock.patch.object(utils, 'is_zork_save_sync_enabled', return_value=True):
            sent = self._capture(
                utils.send_delete_zork_save_to_bbs_nodes,
                '!user', 'zork1', 1700000000, ['!aaa'], None,
            )
        parts = sent[0][1].split('|')
        self.assertTrue(parts[1].startswith('~'))
        self.assertTrue(parts[2].startswith('~'))
        self.assertEqual(utils.decode_text(parts[1]), '!user')
        self.assertEqual(utils.decode_text(parts[2]), 'zork1')


class ChannelCommentWireTests(_CaptureSender):
    def test_plain_sender_when_all_capable(self):
        self._set_peer_caps('!aaa', 'nob64,cck,epoch')
        captured = []

        def _fake_send(header, footer, content, unique_id, **kw):
            captured.append((header, footer, content, list(kw.get('bbs_nodes') or [])))

        with mock.patch.object(utils, '_send_sync_with_cont', side_effect=_fake_send):
            utils.send_channel_comment_to_bbs_nodes(
                'channelkey', 'Alice', '2024-01-01 12:00', 'hello', 'uid123',
                ['!aaa'], None,
            )
        self.assertTrue(captured)
        header = captured[0][0]
        # header: CHANNELCOMMENT|key|sender|date|
        sender_field = header.split('|')[2]
        self.assertTrue(sender_field.startswith('~'))
        self.assertEqual(utils.decode_text(sender_field), 'Alice')

    def test_base64_when_peer_not_capable(self):
        self._set_peer_caps('!aaa', 'cck,epoch')  # no nob64
        captured = []

        def _fake_send(header, footer, content, unique_id, **kw):
            captured.append(header)

        with mock.patch.object(utils, '_send_sync_with_cont', side_effect=_fake_send):
            utils.send_channel_comment_to_bbs_nodes(
                'channelkey', 'Alice', '2024-01-01 12:00', 'hello', 'uid123',
                ['!aaa'], None,
            )
        sender_field = captured[0].split('|')[2]
        self.assertFalse(sender_field.startswith('~'))
        self.assertEqual(base64.b64decode(sender_field.encode('ascii')).decode('utf-8'), 'Alice')


# ── decode_text on real PROFILESYNC frame ────────────────────────────────────

class FrameRoundTripTests(_CaptureSender):
    def test_profilesync_full_roundtrip(self):
        self._set_peer_caps('!peer', 'nob64,epoch')
        sent = self._capture(
            utils.send_profile_to_bbs_nodes,
            '!u1', 'Alice|2', r'Long\Name', 1700000000, 1700000100, 7, 'bio with | pipe', ['!peer'], None,
        )
        msg = sent[0][1]
        parts = msg.split('|', 7)
        # parts count: 8 (PROFILESYNC, user, short, long, first, last, sent, bio)
        self.assertEqual(parts[0], 'PROFILESYNC')
        self.assertEqual(parts[1], '!u1')
        self.assertEqual(utils.decode_text(parts[2]), 'Alice|2')
        self.assertEqual(utils.decode_text(parts[3]), r'Long\Name')
        self.assertEqual(int(parts[6]), 7)
        self.assertEqual(utils.decode_text(parts[7]), 'bio with | pipe')


if __name__ == '__main__':
    unittest.main()
