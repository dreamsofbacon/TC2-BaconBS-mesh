"""Tests for the 'pgos' peer-gossip protocol (node-to-node sync-state relay).

A node relays what it last heard about OTHER peers so neighbours become aware
of (and can detect drift against) peers they can't hear directly — including
zork_saves. Relayed knowledge must only be adopted when strictly fresher than
what the receiver already has, and must never clobber first-hand SYNCSTATE.
"""

import sqlite3
import sys
import types
import time
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import message_processing
import utils


class _Iface:
    def __init__(self):
        self.sent_texts = []
        self.bbs_nodes = []

    def sendText(self, text, destinationId, wantAck, wantResponse):
        self.sent_texts.append(text)


class PeerGossipMergeTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _counts(self, **kw):
        base = {'bulletins': 0, 'mail': 0, 'channels': 0, 'zork_saves': 0,
                'profiles': 0, 'game_scores': 0, 'tombstones': -1}
        base.update(kw)
        return base

    def test_merge_adopts_state_for_unknown_peer(self):
        ok = db_operations.merge_relayed_peer_state("!peerB", self._counts(zork_saves=5, bulletins=9), age_seconds=10)
        self.assertTrue(ok)
        states = {r[0]: r for r in db_operations.get_peer_sync_states()}
        self.assertIn("!peerB", states)
        self.assertEqual(int(states["!peerB"][4]), 5)   # zork_saves column
        self.assertEqual(int(states["!peerB"][1]), 9)   # bulletins column

    def test_merge_rejects_staler_relay(self):
        # First-hand: heard !peerB 5s ago with zork=5.
        db_operations.merge_relayed_peer_state("!peerB", self._counts(zork_saves=5), age_seconds=5)
        # A staler relay (heard 600s ago) with different data must NOT overwrite.
        changed = db_operations.merge_relayed_peer_state("!peerB", self._counts(zork_saves=0), age_seconds=600)
        self.assertFalse(changed)
        states = {r[0]: r for r in db_operations.get_peer_sync_states()}
        self.assertEqual(int(states["!peerB"][4]), 5)

    def test_merge_adopts_fresher_relay(self):
        db_operations.merge_relayed_peer_state("!peerB", self._counts(zork_saves=1), age_seconds=600)
        changed = db_operations.merge_relayed_peer_state("!peerB", self._counts(zork_saves=7), age_seconds=2)
        self.assertTrue(changed)
        states = {r[0]: r for r in db_operations.get_peer_sync_states()}
        self.assertEqual(int(states["!peerB"][4]), 7)

    def test_relay_does_not_clobber_caps(self):
        # Direct SYNCSTATE established caps; a later relay (proto_v=0) must keep them.
        db_operations.upsert_peer_sync_state("!peerB", 0, 0, 0, 0, proto_v=2, caps="cck,cuid,pgos")
        # Make the relay strictly fresher so it WILL update counts.
        db_operations.merge_relayed_peer_state("!peerB", self._counts(zork_saves=3), age_seconds=0)
        self.assertTrue(db_operations.peer_supports("!peerB", "pgos"))
        self.assertTrue(db_operations.peer_supports("!peerB", "cuid"))


class PeerGossipFrameTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_build_frames_excludes_self_and_recipient(self):
        db_operations.upsert_peer_sync_state("!peerB", 9, 4, 16, 5, proto_v=2, caps="pgos")
        db_operations.upsert_peer_sync_state("!recip", 1, 1, 1, 1, proto_v=2, caps="pgos")
        frames = utils.build_peer_gossip_frames(local_node_id="!self", recipient_id="!recip")
        joined = "\n".join(frames)
        self.assertIn("PEERGOSSIP|!peerB|", joined)
        self.assertNotIn("!recip", joined)     # don't relay recipient's state back
        self.assertNotIn("!self", joined)      # our own state goes via SYNCSTATE

    def test_frame_roundtrip_through_receive_handler(self):
        # Receiver learns about !peerC purely from a relayed frame.
        frame = "PEERGOSSIP|!peerC|2|3|4|6|1|0|-1|12"
        message_processing.process_message(
            sender_id=1, message=frame, interface=_Iface(),
            is_sync_message=True, sender_node_id="!relayer",
        )
        states = {r[0]: r for r in db_operations.get_peer_sync_states()}
        self.assertIn("!peerC", states)
        self.assertEqual(int(states["!peerC"][4]), 6)  # zork_saves came through

    def test_malformed_frame_ignored(self):
        before = len(db_operations.get_peer_sync_states())
        message_processing.process_message(
            sender_id=1, message="PEERGOSSIP|!peerC|notanint", interface=_Iface(),
            is_sync_message=True, sender_node_id="!relayer",
        )
        self.assertEqual(len(db_operations.get_peer_sync_states()), before)


if __name__ == "__main__":
    unittest.main()


class GossipAboutOurselfIsIgnoredTests(unittest.TestCase):
    """A node has a DIFFERENT identity on every link: a radio id from
    get_local_node_id(), and mqtt:<topic>:<name> per MQTT bridge.

    The self-check only compared the radio id, so a peer gossiping about us
    over MQTT was not recognised as us. The node recorded peer sync state
    for ITSELF, and then listed itself in Settings > Sync as a third sync
    peer that nothing was configured to talk to.
    """

    SELF_MQTT_ID = "mqtt:baconbbs:bbs-main"

    class _MqttIface(_Iface):
        # What MqttInterface exposes for the link a message arrived on.
        self_node_id = "mqtt:baconbbs:bbs-main"

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        db_operations.set_local_node_id("!04058ac8")   # the RADIO identity

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _gossip(self, about, iface):
        message_processing.process_message(
            1, f"PEERGOSSIP|{about}|1|2|3|4|5|6|7|10", iface,
            is_sync_message=True, sender_node_id="mqtt:baconbbs:node2")

    def _tracked(self):
        return [row[0] for row in db_operations.get_peer_sync_states()]

    def test_a_peer_gossiping_about_us_does_not_make_us_our_own_peer(self):
        self._gossip(self.SELF_MQTT_ID, self._MqttIface())
        self.assertNotIn(self.SELF_MQTT_ID, self._tracked())

    def test_gossip_about_a_real_peer_is_still_recorded(self):
        """The guard must not swallow legitimate relayed state."""
        self._gossip("mqtt:baconbbs:someone-else", self._MqttIface())
        self.assertIn("mqtt:baconbbs:someone-else", self._tracked())

    def test_our_radio_identity_is_still_rejected(self):
        self._gossip("!04058ac8", self._MqttIface())
        self.assertNotIn("!04058ac8", self._tracked())

    def test_gossip_about_another_of_our_identities_arriving_on_a_different_link(self):
        """The case a per-link check misses. This node is bbs-main on the
        baconbbs bridge and Burlington-NNE on baconbbsvt. A peer relays what
        it knows about bbs-main OVER baconbbsvt -- so the arriving link's
        identity does not match, and we recorded ourselves as a peer."""
        class _OtherLink(_Iface):
            self_node_id = "mqtt:baconbbsvt:Burlington-NNE"   # NOT bbs-main

        db_operations.set_local_link_identities(
            [self.SELF_MQTT_ID, "mqtt:baconbbsvt:Burlington-NNE"])
        self._gossip(self.SELF_MQTT_ID, _OtherLink())
        self.assertNotIn(self.SELF_MQTT_ID, self._tracked())

    def test_the_identity_registry_includes_every_link_and_the_radio(self):
        db_operations.set_local_link_identities(["mqtt:a:one", "mqtt:b:two"])
        ids = db_operations.get_local_link_identities()
        self.assertIn("mqtt:a:one", ids)
        self.assertIn("mqtt:b:two", ids)
        self.assertIn("!04058ac8", ids)   # the radio identity

    def test_an_interface_without_a_link_identity_still_works(self):
        """Radios have no self_node_id attribute at all."""
        self._gossip("mqtt:baconbbs:someone-else", _Iface())
        self.assertIn("mqtt:baconbbs:someone-else", self._tracked())
