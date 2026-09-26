"""MQTT sync should not cost data it does not need to.

Measured on the live bbs node before this work, one hour on the baconbbsvt
broker: 14.1 MB of sync frames, 95% of them hash manifests. forgecam asked
for the public_chatter manifest (~165 KB) 27 times, and two nodes it had not
asked -- Chattanooga and another fleet's bbs -- answered it too, because a
frame for one node on the shared topic reached every node and nothing
dropped it. Four causes, each pinned here:

- a frame for another node was dispatched as if it were ours
- every unicast went to the one shared topic, so every node downloaded it
- a scope that could not converge re-sent its whole manifest every cycle
- retained status was republished every 30 seconds whether or not it changed
"""

import sys
import threading
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

from pubsub import pub

import message_processing as mp
import mqtt_interface
from mqtt_interface import MqttInterface, PUBLISH_HEARTBEAT_SECONDS

from test_mqtt_interface import _FakeBroker, _make_client_factory


class _MqttCase(unittest.TestCase):
    def setUp(self):
        self.broker = _FakeBroker()
        patch = mock.patch.object(
            mqtt_interface.mqtt, "Client", _make_client_factory(self.broker))
        patch.start()
        self.addCleanup(patch.stop)
        # Which peers have said they read their direct topic.
        self.direct_peers = set()
        caps = mock.patch(
            "utils.peers_all_support",
            side_effect=lambda peers, cap: cap == "mqdm" and all(
                p in self.direct_peers for p in peers))
        caps.start()
        self.addCleanup(caps.stop)

    def node(self, label):
        iface = MqttInterface(host="broker.example.com",
                              topic_prefix="baconbbsvt", local_id=label)
        self.addCleanup(iface.close)
        iface.receive_topic = f"test.efficiency.{label}.{id(self)}"
        return iface

    def listen(self, iface):
        received = []

        def listener(packet, interface):
            received.append(packet)

        pub.subscribe(listener, iface.receive_topic)
        self.addCleanup(pub.unsubscribe, listener, iface.receive_topic)
        # Keep the listener alive: pubsub holds only a weak reference.
        self._listeners = getattr(self, "_listeners", []) + [listener]
        return received

    def settle(self, *ifaces):
        for iface in ifaces:
            iface._incoming.join()


class OverheardFramesTests(_MqttCase):
    def test_a_frame_for_another_node_is_not_dispatched(self):
        """The live failure: forgecam asked bbs for a manifest, and the two
        other nodes on the topic answered as well."""
        asker, asked, bystander = self.node("VT2"), self.node("NNE"), self.node("Chatt")
        asker.bbs_nodes = [asked.self_node_id]
        heard = self.listen(bystander)
        answered = self.listen(asked)

        asker.sendText("HASHREQ|h", asked.myInfo.my_node_num)
        self.settle(asked, bystander)

        self.assertEqual(len(answered), 1)
        self.assertEqual(heard, [], "a bystander must not act on another node's request")

    def test_a_broadcast_still_reaches_everyone(self):
        sender, one, two = self.node("A"), self.node("B"), self.node("C")
        got_one, got_two = self.listen(one), self.listen(two)
        sender.sendText("PCHAT|x", 0)
        self.settle(one, two)
        self.assertEqual((len(got_one), len(got_two)), (1, 1))


class DirectTopicTests(_MqttCase):
    def test_a_capable_peer_is_sent_its_own_topic(self):
        sender, peer = self.node("A"), self.node("B")
        sender.bbs_nodes = [peer.self_node_id]
        self.direct_peers.add(peer.self_node_id)
        got = self.listen(peer)

        sender.sendText("HASHREQ|h", peer.myInfo.my_node_num)
        self.settle(peer)

        topic = sender._client.published[-1][0]
        self.assertEqual(topic, "baconbbsvt/bbs/to/B")
        self.assertEqual(len(got), 1)

    def test_nobody_else_downloads_it(self):
        """What the capability is for: the frame never reaches a bystander's
        connection at all, not merely its dispatcher."""
        sender, peer, bystander = self.node("A"), self.node("B"), self.node("C")
        sender.bbs_nodes = [peer.self_node_id]
        self.direct_peers.add(peer.self_node_id)
        delivered = []
        original = bystander._client._deliver
        bystander._client._deliver = lambda topic, payload: (
            delivered.append(topic), original(topic, payload))

        sender.sendText("HASHREQ|h", peer.myInfo.my_node_num)

        self.assertEqual(delivered, [])

    def test_a_peer_on_older_code_keeps_the_shared_topic(self):
        sender, peer = self.node("A"), self.node("B")
        sender.bbs_nodes = [peer.self_node_id]
        got = self.listen(peer)

        sender.sendText("HASHREQ|h", peer.myInfo.my_node_num)
        self.settle(peer)

        self.assertEqual(sender._client.published[-1][0], "baconbbsvt/bbs")
        self.assertEqual(len(got), 1)

    def test_broadcasts_stay_on_the_shared_topic(self):
        sender = self.node("A")
        self.direct_peers.add("anything")
        sender.sendText("PCHAT|x", 0)
        self.assertEqual(sender._client.published[-1][0], "baconbbsvt/bbs")

    def test_the_capability_is_advertised(self):
        import utils
        self.assertIn("mqdm", utils.WIRE_CAPABILITIES)


class RetainedPublishTests(_MqttCase):
    STATUS = {"updated_at": "t1", "links": {"mqtt2": {"connected": True}}}

    def published(self, iface, topic):
        return [p for p in iface._client.published if p[0] == topic]

    def test_an_unchanged_status_is_not_sent_again(self):
        iface = self.node("A")
        iface.publish_status(dict(self.STATUS))
        iface.publish_status(dict(self.STATUS, updated_at="t2"))
        self.assertEqual(len(self.published(iface, "baconbbsvt/A/status")), 1)
        self.assertEqual(len(self.published(iface, "baconbbsvt/A/status/links/mqtt2")), 1)

    def test_a_changed_status_is_sent(self):
        iface = self.node("A")
        iface.publish_status(dict(self.STATUS))
        iface.publish_status({"updated_at": "t2", "links": {"mqtt2": {"connected": False}}})
        self.assertEqual(len(self.published(iface, "baconbbsvt/A/status")), 2)

    def test_it_is_repeated_once_the_heartbeat_is_due(self):
        iface = self.node("A")
        clock = [1000.0]
        with mock.patch.object(mqtt_interface.time, "monotonic", side_effect=lambda: clock[0]):
            iface.publish_status(dict(self.STATUS))
            clock[0] += PUBLISH_HEARTBEAT_SECONDS - 1
            iface.publish_status(dict(self.STATUS))
            clock[0] += 2
            iface.publish_status(dict(self.STATUS))
        self.assertEqual(len(self.published(iface, "baconbbsvt/A/status")), 2)

    def test_a_reconnect_republishes_everything(self):
        """The broker we reconnect to may have lost its retained copies."""
        iface = self.node("A")
        iface.publish_status(dict(self.STATUS))
        iface._on_connect(iface._client, None, {}, 0)
        iface.publish_status(dict(self.STATUS))
        self.assertEqual(len(self.published(iface, "baconbbsvt/A/status")), 2)

    def test_a_client_seen_again_is_not_republished(self):
        iface = self.node("A")
        iface.apply_publish_settings({"clients": True}, max_age_hours=0)
        client = {"node_id": "!abcd", "link_name": "primary", "last_seen": "2026-09-26 10:00:00"}
        iface.publish_clients([client])
        iface.publish_clients([dict(client, last_seen="2026-09-26 10:00:30")])
        self.assertEqual(len(self.published(iface, "baconbbsvt/A/clients/primary/!abcd")), 1)

    def test_events_are_never_held_back(self):
        """Not retained, so a repeat is a new event, not a copy."""
        iface = self.node("A")
        iface.apply_publish_settings({"activity": True})
        iface.publish_activity({"kind": "mail"})
        iface.publish_activity({"kind": "mail"})
        self.assertEqual(len(self.published(iface, "baconbbsvt/A/activity/mail")), 2)


PEER = "mqtt:baconbbsvt:BaconBBS-VT2"


class RepairBackoffTests(unittest.TestCase):
    BASE = 15

    def setUp(self):
        self.iface = types.SimpleNamespace(bbs_nodes=[], nodes={}, is_low_latency=True)
        self.requested = []
        self.clock = [10_000.0]
        mp._reset_repair_backoff()
        self.addCleanup(mp._reset_repair_backoff)
        for state in (mp._recent_syncstate_repairs, mp._pending_hashreq, mp._zork_deferrals):
            state.clear()
            self.addCleanup(state.clear)
        patches = [
            mock.patch.object(mp, "get_sync_progress", return_value={"in_progress": False}),
            mock.patch.object(mp, "get_repair_cycle_seconds", return_value=self.BASE),
            mock.patch.object(mp, "get_scopes_to_request_repair",
                              side_effect=lambda _peer, scopes: list(scopes)),
            mock.patch.object(mp, "send_hash_request_to_bbs_nodes",
                              side_effect=lambda peers, interface, scope=None:
                              self.requested.append(scope)),
            mock.patch.object(mp.time, "time", side_effect=lambda: self.clock[0]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def syncstate(self, scopes, after=0.0):
        """A SYNCSTATE arriving `after` seconds later, reporting `scopes` out of step."""
        self.clock[0] += after
        self.requested = []
        # The previous exchange has finished by the time the next SYNCSTATE lands.
        mp._pending_hashreq.clear()
        with mock.patch.object(mp, "get_mismatched_peer_scopes",
                               return_value={PEER: list(scopes)}):
            mp._request_targeted_repair_if_needed(PEER, self.iface)
        return self.requested

    def test_the_first_mismatch_is_repaired_at_once(self):
        self.assertEqual(self.syncstate(["mail"]), ["mail"])

    def test_a_scope_that_stays_out_of_step_waits_longer_each_time(self):
        self.syncstate(["mail"])
        self.assertEqual(self.syncstate(["mail"], after=self.BASE), ["mail"])
        # Now waiting 2 x base: one base later is too soon.
        self.assertEqual(self.syncstate(["mail"], after=self.BASE), [])
        self.assertEqual(self.syncstate(["mail"], after=self.BASE), ["mail"])

    def test_the_wait_is_capped(self):
        for _ in range(30):
            self.syncstate(["mail"], after=mp.REPAIR_BACKOFF_CAP_SECONDS)
        self.assertEqual(
            mp._repair_backoff[(PEER, "mail")]["delay"], mp.REPAIR_BACKOFF_CAP_SECONDS)
        self.assertEqual(self.syncstate(["mail"], after=mp.REPAIR_BACKOFF_CAP_SECONDS), ["mail"])

    def test_convergence_forgets_the_wait(self):
        for _ in range(5):
            self.syncstate(["mail"], after=self.BASE * 16)
        self.syncstate([], after=self.BASE)
        self.assertEqual(self.syncstate(["mail"], after=self.BASE), ["mail"])

    def test_a_backed_off_scope_does_not_hold_back_the_others(self):
        self.syncstate(["mail"])
        self.syncstate(["mail"], after=self.BASE)
        self.assertEqual(self.syncstate(["mail", "bulletins"], after=self.BASE), ["bulletins"])

    def test_a_reconcile_that_finds_new_keys_resets_the_wait(self):
        """A backlog worked through a pass at a time is progress, not a loop."""
        self.syncstate(["mail"])
        mp._note_reconcile_keys(PEER, "mail", {"a", "b"})
        self.syncstate(["mail"], after=self.BASE)
        mp._note_reconcile_keys(PEER, "mail", {"c"})
        self.assertEqual(self.syncstate(["mail"], after=self.BASE), ["mail"])

    def test_the_same_keys_again_do_not(self):
        self.syncstate(["mail"])
        mp._note_reconcile_keys(PEER, "mail", {"a"})
        self.syncstate(["mail"], after=self.BASE)
        mp._note_reconcile_keys(PEER, "mail", {"a"})
        self.assertEqual(self.syncstate(["mail"], after=self.BASE), [])

    def test_channel_comments_do_not_count_as_progress_for_channels(self):
        """One channels request reconciles both halves separately; if they
        shared a record each would look new to the other every time."""
        self.syncstate(["channels"])
        mp._note_reconcile_keys(PEER, "channels", {"x"})
        mp._note_reconcile_keys(PEER, "channel_comments", {"y"})
        self.syncstate(["channels"], after=self.BASE)
        mp._note_reconcile_keys(PEER, "channels", {"x"})
        mp._note_reconcile_keys(PEER, "channel_comments", {"y"})
        self.assertEqual(self.syncstate(["channels"], after=self.BASE), [])

    def test_public_chatter_is_repaired_at_most_every_ten_minutes(self):
        self.assertIn("public_chatter", self.syncstate(["public_chatter"]))
        self.assertEqual(
            self.syncstate(["public_chatter"],
                           after=mp.PUBLIC_CHATTER_REPAIR_FLOOR_SECONDS - 1), [])
        self.assertIn("public_chatter", self.syncstate(["public_chatter"], after=1))

    def test_backoff_is_per_peer(self):
        self.syncstate(["mail"])
        self.syncstate(["mail"], after=self.BASE)
        self.clock[0] += self.BASE
        self.requested = []
        other = "mqtt:baconbbsvt:Chattanooga"
        with mock.patch.object(mp, "get_mismatched_peer_scopes",
                               return_value={other: ["mail"]}):
            mp._request_targeted_repair_if_needed(other, self.iface)
        self.assertEqual(self.requested, ["mail"])


class ReconcileFeedsBackoffTests(unittest.TestCase):
    """Both reconcile paths must report what they found, or the backoff never
    learns that a backlog is moving and slows it down like a stuck scope."""

    def setUp(self):
        mp._reset_repair_backoff()
        self.addCleanup(mp._reset_repair_backoff)
        self.iface = types.SimpleNamespace(bbs_nodes=[], nodes={})
        patches = [
            mock.patch.object(mp, "get_record_hash_manifest", return_value={"mine": "h1"}),
            mock.patch.object(mp, "get_reconcile_max_per_pass", return_value=0),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_striped_reconcile_reports_its_keys(self):
        mp._do_striped_reconcile("mail", {PEER: {"theirs": "h2"}}, self.iface)
        self.assertEqual(mp._repair_progress.get((PEER, "mail")), {"theirs", "mine"})

    def test_the_single_peer_reconcile_reports_its_keys(self):
        with mp._hash_buffer_lock:
            mp._peer_hash_manifest_buffers[(PEER, "mail")] = {"theirs": "h2"}
        mp._reconcile_remote_manifest("mail", PEER, self.iface)
        self.assertEqual(mp._repair_progress.get((PEER, "mail")), {"theirs", "mine"})


if __name__ == "__main__":
    unittest.main()
