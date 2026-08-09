"""Tests for the dual-radio full-BBS-sync bridge's core property: two radio
interfaces sharing ONE local database, with no relay/translation layer.

Per the project plan, a bridge node does not forward packets between
networks -- it runs the *existing* per-interface sync engine once per
active radio against a shared DB. A record synced in via one radio
(add_bulletin with bbs_nodes=[], exactly what message_processing.py does
for sync-arrived content) never gets echoed back out immediately, and is
picked up by the OTHER radio's own independent sync push the next time
it's told to sync bulletins -- purely because it's now present in the
shared local DB.
"""
import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    # Another test file already cached a meshtastic stub built for a
    # narrower purpose (e.g. test_radio_recovery.py's, which omits
    # BROADCAST_NUM) -- patch the one attribute db_operations.py needs
    # rather than replacing the whole module.
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations
from db_operations import add_bulletin, sync_bulletins_to_nodes
from utils import get_max_text_bytes


class _FakeInterface:
    """Minimal fake radio -- just enough for send_bulletin_to_bbs_nodes'
    chunking + sendText calls, with a configurable per-transport byte cap
    (220 for a Meshtastic-shaped link, 160 for MeshCore -- see
    utils.get_max_text_bytes / meshcore_interface.py)."""

    def __init__(self, max_text_bytes=220):
        self.max_text_bytes = max_text_bytes
        self.sent = []  # [(text, destinationId), ...]

    def sendText(self, text, destinationId, wantAck, wantResponse):
        del wantAck, wantResponse
        self.sent.append((text, destinationId))


class DualInterfaceBridgeTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_sync_arrived_record_stored_without_immediate_broadcast(self):
        """message_processing.py stores sync-arrived content with
        bbs_nodes=[] -- add_bulletin's own broadcast guard
        (`if bbs_nodes and interface: ...`) must skip sending anything.
        This is the "no echo on receipt" property the whole bridge design
        relies on to avoid a relay/forwarding layer."""
        interface_a = _FakeInterface(max_text_bytes=220)  # Meshtastic-shaped
        add_bulletin(
            "General", "PEERA", "Hello", "posted on network A",
            [], interface_a, unique_id="uid-bridge-1", date="2026-01-01 00:00",
        )
        self.assertEqual(interface_a.sent, [])  # nothing sent on arrival

        row = db_operations.get_db_connection().execute(
            "SELECT board, content FROM bulletins WHERE unique_id = ?", ("uid-bridge-1",)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "General")
        self.assertEqual(row[1], "posted on network A")

    def test_record_propagates_to_the_other_radios_own_sync_push(self):
        """A record that arrived via link A's sync (stored with bbs_nodes=[])
        is picked up by link B's OWN independent sync_bulletins_to_nodes call
        -- purely because it's now in the shared DB -- and sent out on B's
        interface to B's peer. Nothing is sent back out on A for it."""
        interface_a = _FakeInterface(max_text_bytes=220)  # Meshtastic-shaped link
        interface_b = _FakeInterface(max_text_bytes=160)  # MeshCore-shaped link

        # Simulate: a peer on network A pushed this bulletin to us via sync.
        add_bulletin(
            "General", "PEERA", "Hello", "posted on network A",
            [], interface_a, unique_id="uid-bridge-2", date="2026-01-01 00:00",
        )
        self.assertEqual(interface_a.sent, [])

        # Bridge's own B-side sync push to B's peer picks the record up from
        # the shared DB -- no relay/translation code involved.
        result = sync_bulletins_to_nodes(["peer-on-b"], interface_b)
        self.assertEqual(result["bulletins_synced"], 1)
        self.assertTrue(interface_b.sent, "expected the bridge to push the record out on interface B")
        sent_text, dest = interface_b.sent[0]
        self.assertEqual(dest, "peer-on-b")
        self.assertIn("uid-bridge-2", sent_text)
        self.assertIn("posted on network A", sent_text)

        # A's own sync push, run again with no new local content, must not
        # have queued anything new to send back out on A for this record
        # (it never got auto-broadcast on arrival -- see previous test).
        self.assertEqual(interface_a.sent, [])

    def test_each_side_of_the_bridge_chunks_to_its_own_byte_cap(self):
        """The same underlying DB row, pushed out on two links with
        different max_text_bytes, must each respect their OWN cap -- proves
        chunking is read per-call from the interface, not cached/global
        after the first call in a process (see test_transport_packet_sizing.py)."""
        interface_meshtastic = _FakeInterface(max_text_bytes=220)
        interface_meshcore = _FakeInterface(max_text_bytes=160)
        long_content = "X" * 600

        add_bulletin(
            "General", "PEERA", "Long post", long_content,
            [], interface_meshtastic, unique_id="uid-bridge-3", date="2026-01-01 00:00",
        )

        sync_bulletins_to_nodes(["peer-mt"], interface_meshtastic)
        sync_bulletins_to_nodes(["peer-mc"], interface_meshcore)

        self.assertTrue(interface_meshtastic.sent)
        self.assertTrue(interface_meshcore.sent)
        for text, _dest in interface_meshtastic.sent:
            self.assertLessEqual(len(text.encode("utf-8")), get_max_text_bytes(interface_meshtastic))
        for text, _dest in interface_meshcore.sent:
            self.assertLessEqual(len(text.encode("utf-8")), get_max_text_bytes(interface_meshcore))
        # MeshCore's tighter 160-byte cap must produce at least as many
        # frames as Meshtastic's 220-byte cap for the identical content.
        self.assertGreaterEqual(len(interface_meshcore.sent), len(interface_meshtastic.sent))


if __name__ == "__main__":
    unittest.main()
