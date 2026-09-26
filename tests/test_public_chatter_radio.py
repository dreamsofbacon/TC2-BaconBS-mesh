"""Public chatter travels over MQTT, and over a radio only when asked.

Chatter is what a node overhears on the air. Sharing it over MQTT costs
nothing that matters; over LoRa every observation is re-sent on air and a
repair sends the largest manifest there is, mostly to tell a peer what it
could hear for itself. `[public_chatter] sync_over_radio` turns it on for a
node whose radio peers are far enough apart to hear different things.

Also pinned: a peer that advertises the "I do not take part" sentinel was
still compared against, so every cycle found a chatter "gap" with a node
that refuses chatter, and tried to repair it.
"""
import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import message_processing as mp
import utils


RADIO = types.SimpleNamespace(protocol_name="Meshtastic", bbs_nodes=[], nodes={})
MQTT = types.SimpleNamespace(protocol_name="MQTT:mqtt2", is_low_latency=True,
                             bbs_nodes=[], nodes={})


def _config(sync=True, over_radio=None):
    values = {("public_chatter", "sync"): sync}
    if over_radio is not None:
        values[("public_chatter", "sync_over_radio")] = over_radio
    return mock.patch.object(
        utils, "_config_bool",
        side_effect=lambda section, key, default=False: values.get((section, key), default))


class WhichLinksCarryChatterTests(unittest.TestCase):
    def test_mqtt_carries_it_by_default(self):
        with _config():
            self.assertTrue(db_operations.public_chatter_syncs_on(MQTT))

    def test_a_radio_does_not_by_default(self):
        with _config():
            self.assertFalse(db_operations.public_chatter_syncs_on(RADIO))

    def test_a_radio_does_when_asked(self):
        with _config(over_radio=True):
            self.assertTrue(db_operations.public_chatter_syncs_on(RADIO))

    def test_sync_off_turns_off_every_link(self):
        with _config(sync=False, over_radio=True):
            self.assertFalse(db_operations.public_chatter_syncs_on(MQTT))
            self.assertFalse(db_operations.public_chatter_syncs_on(RADIO))


class RadioLinkSendsNoChatterTests(unittest.TestCase):
    ROW = ("uid-1", "!abcd", "Node", "LongName", "short", "0", "hello",
           "2026-09-26T10:00:00Z", "!self", "primary", "2026-10-03T10:00:00Z")

    def pushes(self, interface, **config):
        with _config(**config), \
             mock.patch.object(db_operations, "peer_supports", return_value=True), \
             mock.patch.object(utils, "_send_sync_with_cont") as send:
            utils.send_public_chatter_to_bbs_nodes(self.ROW, ["!peer"], interface)
        return send.call_count

    def test_an_overheard_message_is_not_re_sent_on_air(self):
        self.assertEqual(self.pushes(RADIO), 0)

    def test_it_is_when_radio_sync_is_on(self):
        self.assertEqual(self.pushes(RADIO, over_radio=True), 1)

    def test_it_still_goes_over_mqtt(self):
        self.assertEqual(self.pushes(MQTT), 1)

    def advertised(self, interface, **config):
        counts = {"public_chatter": 7, "public_chatter_hash": "real-hash"}
        with _config(**config), \
             mock.patch.object(db_operations, "get_all_board_audiences", return_value={}), \
             mock.patch.object(utils, "_send_one_sync_to_all") as send:
            utils.send_sync_state_to_bbs_nodes(counts, ["!peer"], interface)
        return send.call_args[0][0].split("|")

    def test_a_radio_syncstate_says_it_does_not_take_part(self):
        """Otherwise a radio peer sees our count, finds a gap, and asks for
        the chatter manifest over the air."""
        frame = self.advertised(RADIO)
        self.assertEqual(frame[15], "0")
        self.assertEqual(frame[16], db_operations.public_chatter_disabled_hash())

    def test_an_mqtt_syncstate_carries_the_real_state(self):
        frame = self.advertised(MQTT)
        self.assertEqual((frame[15], frame[16]), ("7", "real-hash"))

    def test_a_radio_peer_on_older_code_is_not_sent_the_manifest(self):
        with _config(), \
             mock.patch.object(mp, "_send_hash_manifest_to_peer") as send:
            mp.process_message(1, "HASHREQ|public_chatter", RADIO,
                               is_sync_message=True, sender_node_id="!peer")
        self.assertEqual(send.call_count, 0)

    def test_the_same_request_over_mqtt_is_answered(self):
        with _config(), \
             mock.patch.object(mp, "_send_hash_manifest_to_peer") as send:
            mp.process_message(1, "HASHREQ|public_chatter", MQTT,
                               is_sync_message=True, sender_node_id="mqtt:x:peer")
        self.assertEqual([c.args[0] for c in send.call_args_list], ["public_chatter"])

    def test_a_radio_link_does_not_ask_for_chatter(self):
        requested = []
        with _config(), \
             mock.patch.object(mp, "get_sync_progress", return_value={"in_progress": False}), \
             mock.patch.object(mp, "get_mismatched_peer_scopes",
                               # Chatter alone: with content also behind it is
                               # deferred anyway, and this would prove nothing.
                               return_value={"!peer": ["public_chatter"]}), \
             mock.patch.object(mp, "get_scopes_to_request_repair",
                               side_effect=lambda _p, scopes: list(scopes)), \
             mock.patch.object(mp, "send_hash_request_to_bbs_nodes",
                               side_effect=lambda p, i, scope=None: requested.append(scope)):
            mp._request_targeted_repair_if_needed("!peer", RADIO)
        self.assertEqual(requested, [])


PEER = "mqtt:baconbbsvt:Chattanooga"


class OptedOutPeerTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        for patch in (mock.patch.object(db_operations, "peer_supports", return_value=True),
                      _config()):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def advertise(self, chatter_count, chatter_hash):
        """A peer that matches us on everything but chatter."""
        local = db_operations.get_local_record_counts()
        db_operations.upsert_peer_sync_state(
            PEER,
            int(local["bulletins"]), int(local["mail"]), int(local["channels"]),
            int(local["zork_saves"]), int(local["profiles"]), int(local["game_scores"]),
            bulletins_hash=local["bulletins_hash"], mail_hash=local["mail_hash"],
            channels_hash=local["channels_hash"], zork_saves_hash=local["zork_saves_hash"],
            profiles_hash=local["profiles_hash"], game_scores_hash=local["game_scores_hash"],
            tombstones=int(local.get("tombstones", 0)),
            proto_v=2, caps="pchat,pch2",
            public_chatter=chatter_count, public_chatter_hash=chatter_hash,
        )

    def test_a_peer_that_has_chatter_we_lack_is_reported(self):
        """Guards the fixture: a real difference must still show."""
        self.advertise(5, "some-real-hash")
        self.assertIn("public_chatter", db_operations.get_mismatched_peer_scopes().get(PEER, []))
        self.assertIn(PEER, db_operations.get_mismatched_peer_nodes())

    def test_an_opted_out_peer_is_not(self):
        self.advertise(0, db_operations.public_chatter_disabled_hash())
        self.assertNotIn("public_chatter",
                         db_operations.get_mismatched_peer_scopes().get(PEER, []))
        self.assertNotIn(PEER, db_operations.get_mismatched_peer_nodes())

    def test_nor_is_any_peer_when_we_have_opted_out(self):
        self.advertise(5, "some-real-hash")
        with _config(sync=False):
            self.assertNotIn("public_chatter",
                             db_operations.get_mismatched_peer_scopes().get(PEER, []))
            self.assertNotIn(PEER, db_operations.get_mismatched_peer_nodes())


if __name__ == "__main__":
    unittest.main()
