"""A version report lost in a simultaneous restart is re-sent.

A fleet deploy restarts every node within seconds, and MQTT sessions are
clean, so the VPS's first report on a new version twice went out while bbs
was reconnecting. It was gone, and the Fleet page read `pending` for up to
half an hour. A peer's first SYNCSTATE in this process is the moment it is
certainly listening, so that is when this node tells it its fleet state.
"""
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import message_processing as mp

SYNCSTATE = "SYNCSTATE|1|0|0|0|0|0|h|||||"


class FirstHeardTests(unittest.TestCase):
    def setUp(self):
        self.heard = []
        mp._peers_heard_this_run.clear()
        mp.set_peer_first_heard_hook(lambda peer, iface: self.heard.append(peer))
        self.addCleanup(mp.set_peer_first_heard_hook, None)
        self.addCleanup(mp._peers_heard_this_run.clear)
        self.iface = types.SimpleNamespace(bbs_nodes=[], nodes={})
        for patch in (mock.patch.object(mp, "upsert_peer_sync_state"),
                      mock.patch.object(mp, "_retry_stale_hash_manifest_buffers"),
                      mock.patch.object(mp, "_retry_stale_zork_save_buffers"),
                      mock.patch.object(mp, "_request_targeted_repair_if_needed")):
            patch.start()
            self.addCleanup(patch.stop)

    def syncstate_from(self, peer):
        mp.process_message(1, SYNCSTATE, self.iface, is_sync_message=True,
                           sender_node_id=peer)

    def test_a_peers_first_syncstate_gets_our_fleet_state(self):
        self.syncstate_from("mqtt:baconbbsvt:bbs")
        self.assertEqual(self.heard, ["mqtt:baconbbsvt:bbs"])

    def test_only_the_first(self):
        """Every later SYNCSTATE would otherwise cost a full advert."""
        for _ in range(3):
            self.syncstate_from("mqtt:baconbbsvt:bbs")
        self.assertEqual(self.heard, ["mqtt:baconbbsvt:bbs"])

    def test_each_peer_once(self):
        self.syncstate_from("mqtt:baconbbsvt:bbs")
        self.syncstate_from("mqtt:baconbbsvt:Chattanooga")
        self.syncstate_from("mqtt:baconbbsvt:bbs")
        self.assertEqual(self.heard, ["mqtt:baconbbsvt:bbs", "mqtt:baconbbsvt:Chattanooga"])

    def test_a_failing_hook_does_not_break_the_receive_path(self):
        mp.set_peer_first_heard_hook(lambda peer, iface: 1 / 0)
        self.syncstate_from("mqtt:baconbbsvt:bbs")
        mp._request_targeted_repair_if_needed.assert_called_once()


class ServerHookTests(unittest.TestCase):
    def test_it_sends_a_forced_advert_to_that_peer_alone(self):
        from test_fleet_wire import _install_fake_meshtastic_package
        _install_fake_meshtastic_package()
        import server
        config, iface = {"fleet": {}}, object()
        with mock.patch.object(server, "_advertise_fleet_state") as advertise:
            server.fleet_state_for_new_peer(config)("mqtt:baconbbsvt:bbs", iface)
        advertise.assert_called_once_with(config, ["mqtt:baconbbsvt:bbs"], iface, force=True)


if __name__ == "__main__":
    unittest.main()
