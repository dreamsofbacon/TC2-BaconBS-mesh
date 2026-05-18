"""PR 5 — bitmap-base85 gap-fill (`bmgap` capability) wire-format tests."""

import base64
import os
import sys
import sqlite3
import unittest

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


class CapabilityWiringTests(unittest.TestCase):
    def test_bmgap_in_wire_capabilities(self):
        self.assertIn('bmgap', utils.WIRE_CAPABILITIES)


class PackMissingTests(unittest.TestCase):
    def test_legacy_bare_csv_no_prefix(self):
        out = utils.pack_missing([1, 3, 5], total=10, prefer_bitmap=False)
        self.assertEqual(out, "1,3,5")
        self.assertFalse(out.startswith('csv:'))
        self.assertFalse(out.startswith('bm:'))

    def test_legacy_empty_is_empty_string(self):
        self.assertEqual(utils.pack_missing([], total=10, prefer_bitmap=False), "")

    def test_capable_prefixed(self):
        out = utils.pack_missing([1, 3, 5], total=10, prefer_bitmap=True)
        self.assertTrue(out.startswith('csv:') or out.startswith('bm:'))

    def test_dense_missing_prefers_bitmap(self):
        # All of 200 indices missing — CSV would be ~700 bytes; bitmap is ~25 bytes b85.
        missing = list(range(200))
        out = utils.pack_missing(missing, total=200, prefer_bitmap=True)
        self.assertTrue(out.startswith('bm:'))

    def test_sparse_missing_prefers_csv(self):
        # Two indices in a 200-bit space — CSV is shorter than bitmap.
        out = utils.pack_missing([3, 47], total=200, prefer_bitmap=True)
        self.assertTrue(out.startswith('csv:'))

    def test_total_zero_still_packs_when_bitmap_shorter(self):
        # When total is unknown (0) the implementation derives bit-count from
        # max(missing)+1, so a small dense list can still produce a bitmap that
        # beats the CSV form on the wire. Decoder side accepts either.
        out = utils.pack_missing([1, 2, 3], total=0, prefer_bitmap=True)
        self.assertTrue(out.startswith('csv:') or out.startswith('bm:'))
        self.assertEqual(sorted(utils.unpack_missing(out, total=0)), [1, 2, 3])


class UnpackMissingTests(unittest.TestCase):
    def test_round_trip_dense(self):
        missing = list(range(0, 100, 2))  # 50 indices in 100-bit space
        wire = utils.pack_missing(missing, total=100, prefer_bitmap=True)
        # Should be bitmap form
        self.assertTrue(wire.startswith('bm:'))
        self.assertEqual(utils.unpack_missing(wire, total=100), missing)

    def test_round_trip_sparse(self):
        missing = [7, 42]
        wire = utils.pack_missing(missing, total=200, prefer_bitmap=True)
        self.assertTrue(wire.startswith('csv:'))
        self.assertEqual(utils.unpack_missing(wire, total=200), missing)

    def test_legacy_bare_csv(self):
        self.assertEqual(utils.unpack_missing("1,3,5", total=10), [1, 3, 5])

    def test_empty_string(self):
        self.assertEqual(utils.unpack_missing("", total=10), [])

    def test_none(self):
        self.assertEqual(utils.unpack_missing(None, total=10), [])

    def test_csv_with_prefix(self):
        self.assertEqual(utils.unpack_missing("csv:1,3,5", total=10), [1, 3, 5])

    def test_bitmap_clamped_by_total(self):
        # Build a 16-bit bitmap with bits 0..15 set; ask for total=8 and verify only 0..7 returned.
        full = utils.pack_missing(list(range(16)), total=16, prefer_bitmap=True)
        self.assertTrue(full.startswith('bm:'))
        self.assertEqual(utils.unpack_missing(full, total=8), list(range(8)))

    def test_bitmap_invalid_base85_returns_empty(self):
        # ',' is not in the base85 alphabet used by base64.b85encode/decode
        # so this raises ValueError and our decoder returns an empty list.
        self.assertEqual(utils.unpack_missing("bm:,,,,", total=100), [])

    def test_bare_csv_garbage_returns_empty(self):
        self.assertEqual(utils.unpack_missing("not,a,number", total=10), [])


class WireForwardCompatTests(unittest.TestCase):
    def test_new_peer_can_decode_legacy_csv(self):
        # A legacy sender ships raw CSV; the new decoder accepts it.
        self.assertEqual(utils.unpack_missing("2,4,6,8", total=100), [2, 4, 6, 8])

    def test_old_decoder_can_still_decode_capable_csv_form(self):
        # If an old peer reads "csv:1,3,5" through the legacy
        # `int(x) for x in csv.split(',')` parser it would crash on the "csv:1"
        # token. So the sender MUST gate the prefix on peer capability — which
        # is what pack_missing(prefer_bitmap=False) ensures.
        legacy = utils.pack_missing([1, 3, 5], total=10, prefer_bitmap=False)
        # Verify a legacy parser still succeeds on this output:
        parsed = sorted({int(x) for x in legacy.split(',') if x.strip() != ''})
        self.assertEqual(parsed, [1, 3, 5])


class PerPeerGating(_DBFixtureMixin, unittest.TestCase):
    def test_peers_all_support_bmgap_gates_prefix(self):
        self._set_peer_caps('!aa', 'bmgap')
        self._set_peer_caps('!bb', 'scc')  # no bmgap
        # Capable peer: prefix expected
        self.assertTrue(utils.peers_all_support(['!aa'], 'bmgap'))
        self.assertFalse(utils.peers_all_support(['!bb'], 'bmgap'))
        self.assertFalse(utils.peers_all_support(['!aa', '!bb'], 'bmgap'))

        capable = utils.pack_missing([1, 2, 3], total=20,
                                     prefer_bitmap=utils.peers_all_support(['!aa'], 'bmgap'))
        legacy = utils.pack_missing([1, 2, 3], total=20,
                                    prefer_bitmap=utils.peers_all_support(['!bb'], 'bmgap'))
        self.assertTrue(capable.startswith('csv:') or capable.startswith('bm:'))
        self.assertEqual(legacy, "1,2,3")


if __name__ == '__main__':
    unittest.main()
