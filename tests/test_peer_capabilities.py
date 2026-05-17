"""Tests for the SYNCSTATE wire-protocol capability handshake (PR 0).

Exercises:
- ``utils.local_capabilities_token`` produces a well-formed ``vN:`` token.
- ``utils.parse_capabilities_token`` round-trips and tolerates malformed input.
- SYNCSTATE frames built by ``send_sync_state_to_bbs_nodes`` carry the token.
- ``upsert_peer_sync_state`` persists ``proto_v`` + ``caps``; ``peer_supports``
  + ``get_peer_caps`` read them back through the in-process cache.
- Legacy 14-field SYNCSTATE persistence does not clobber previously-observed
  caps (the upsert preserves them when ``proto_v`` is 0).
"""

import sqlite3
import unittest
from unittest.mock import patch

import db_operations
import utils


class CapabilitiesTokenTests(unittest.TestCase):
    def test_local_token_uses_module_constants(self):
        tok = utils.local_capabilities_token()
        self.assertEqual(tok, f"v{utils.WIRE_PROTOCOL_VERSION}:{','.join(utils.WIRE_CAPABILITIES)}")
        self.assertTrue(tok.startswith("v"))
        self.assertIn(":", tok)

    def test_parse_empty_token(self):
        self.assertEqual(utils.parse_capabilities_token(""), (0, ""))
        self.assertEqual(utils.parse_capabilities_token(None), (0, ""))

    def test_parse_well_formed_token(self):
        self.assertEqual(utils.parse_capabilities_token("v2:"), (2, ""))
        self.assertEqual(utils.parse_capabilities_token("v2:cck"), (2, "cck"))
        self.assertEqual(
            utils.parse_capabilities_token("v3:cck,epoch,scc"),
            (3, "cck,epoch,scc"),
        )

    def test_parse_strips_whitespace_in_cap_list(self):
        self.assertEqual(
            utils.parse_capabilities_token("v2: cck , epoch "),
            (2, "cck,epoch"),
        )

    def test_parse_malformed_returns_zero(self):
        self.assertEqual(utils.parse_capabilities_token("garbage"), (0, ""))
        self.assertEqual(utils.parse_capabilities_token("v:cck"), (0, ""))
        self.assertEqual(utils.parse_capabilities_token("vX:cck"), (0, ""))
        self.assertEqual(utils.parse_capabilities_token("v-1:cck"), (0, ""))


class SyncStateFramingTests(unittest.TestCase):
    def test_frame_appends_caps_token(self):
        captured = []

        def fake_send(message, node_id, interface, **_kwargs):
            captured.append((message, node_id))

        counts = {
            'bulletins': 1, 'mail': 2, 'channels': 3, 'zork_saves': 4,
            'profiles': 5, 'game_scores': 6, 'tombstones': 7,
            'bulletins_hash': 'b', 'mail_hash': 'm', 'channels_hash': 'c',
            'zork_saves_hash': 'z', 'profiles_hash': 'p', 'game_scores_hash': 'g',
        }
        with patch.object(utils, "_send_one_sync", side_effect=fake_send):
            utils.send_sync_state_to_bbs_nodes(counts, ["!04059140"], interface=None)

        self.assertEqual(len(captured), 1)
        msg = captured[0][0]
        parts = msg.split("|")
        self.assertEqual(parts[0], "SYNCSTATE")
        self.assertEqual(len(parts), 15)
        self.assertEqual(parts[14], utils.local_capabilities_token())


class PeerCapsPersistenceTests(unittest.TestCase):
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

    def test_upsert_stores_caps_and_lookup_works(self):
        db_operations.upsert_peer_sync_state(
            "!04059140", 0, 0, 0, 0, proto_v=2, caps="cck,epoch",
        )
        proto_v, caps = db_operations.get_peer_caps("!04059140")
        self.assertEqual(proto_v, 2)
        self.assertEqual(caps, frozenset({"cck", "epoch"}))
        self.assertTrue(db_operations.peer_supports("!04059140", "cck"))
        self.assertTrue(db_operations.peer_supports("!04059140", "epoch"))
        self.assertFalse(db_operations.peer_supports("!04059140", "nob64"))

    def test_unknown_peer_supports_nothing(self):
        proto_v, caps = db_operations.get_peer_caps("!deadbeef")
        self.assertEqual(proto_v, 0)
        self.assertEqual(caps, frozenset())
        self.assertFalse(db_operations.peer_supports("!deadbeef", "cck"))

    def test_legacy_upsert_preserves_existing_caps(self):
        # First a v2 peer announces caps.
        db_operations.upsert_peer_sync_state(
            "!04059140", 0, 0, 0, 0, proto_v=2, caps="cck",
        )
        # Then a legacy SYNCSTATE (no proto_v / caps) arrives.
        db_operations.upsert_peer_sync_state(
            "!04059140", 1, 2, 3, 4, proto_v=0, caps="",
        )
        proto_v, caps = db_operations.get_peer_caps("!04059140")
        self.assertEqual(proto_v, 2)
        self.assertEqual(caps, frozenset({"cck"}))

    def test_upsert_invalidates_cache(self):
        db_operations.upsert_peer_sync_state(
            "!04059140", 0, 0, 0, 0, proto_v=2, caps="cck",
        )
        self.assertTrue(db_operations.peer_supports("!04059140", "cck"))
        # New advertisement drops cck and adds epoch.
        db_operations.upsert_peer_sync_state(
            "!04059140", 0, 0, 0, 0, proto_v=2, caps="epoch",
        )
        self.assertFalse(db_operations.peer_supports("!04059140", "cck"))
        self.assertTrue(db_operations.peer_supports("!04059140", "epoch"))


if __name__ == "__main__":
    unittest.main()
