import asyncio
import threading
import time
import types
import unittest
from unittest.mock import patch

from pubsub import pub

import meshcore_interface
from meshcore_interface import MESHCORE_MAX_TEXT_BYTES, MeshCoreInterface


LOCAL_KEY = "11" * 32
PEER_KEY = "22" * 32


class _FakeCommands:
    def __init__(self):
        self.sent = []
        self.channel_sent = []

    async def send_msg(self, destination, text):
        self.sent.append((destination, text))
        return types.SimpleNamespace(
            type=meshcore_interface.EventType.MSG_SENT,
            payload={"expected_ack": b"\x01\x02\x03\x04"},
        )

    async def send_msg_with_retry(self, destination, text, **kwargs):
        self.sent.append((destination, text))
        self.retry_options = kwargs
        return types.SimpleNamespace(
            type=meshcore_interface.EventType.MSG_SENT,
            payload={"expected_ack": b"\x01\x02\x03\x04"},
        )

    async def send_chan_msg(self, channel_index, text):
        self.channel_sent.append((channel_index, text))
        return types.SimpleNamespace(
            type=meshcore_interface.EventType.OK,
            payload={},
        )


class _FakeCore:
    def __init__(self):
        self.self_info = {
            "public_key": LOCAL_KEY,
            "name": "Bacon BBS",
            "adv_type": 1,
        }
        self.contacts = {
            PEER_KEY: {
                "public_key": PEER_KEY,
                "adv_name": "Peer Node",
                "type": 1,
                "adv_lat": 1.25,
                "adv_lon": -2.5,
            }
        }
        self.commands = _FakeCommands()
        self.subscriptions = {}
        self.is_connected = True
        self.receive_started = False
        self.ack = True
        self.ack_waits = []

    async def wait_for_event(self, event_type, attribute_filters=None, timeout=None):
        self.ack_waits.append((event_type, attribute_filters, timeout))
        if not self.ack:
            return None
        return types.SimpleNamespace(type=event_type, payload=attribute_filters)

    def subscribe(self, event_type, callback):
        self.subscriptions[event_type] = callback
        return types.SimpleNamespace()

    async def ensure_contacts(self, follow=False):
        return True

    def get_contact_by_key_prefix(self, prefix):
        for key, contact in self.contacts.items():
            if key.startswith(prefix.lower()):
                return contact
        return None

    async def start_auto_message_fetching(self):
        self.receive_started = True
        return types.SimpleNamespace()

    async def stop_auto_message_fetching(self):
        self.receive_started = False

    async def disconnect(self):
        self.is_connected = False


class _FakeMeshCoreFactory:
    last_core = None

    @classmethod
    async def create_serial(cls, *args, **kwargs):
        del args, kwargs
        cls.last_core = _FakeCore()
        return cls.last_core

    create_tcp = create_serial
    create_ble = create_serial


class MeshCoreInterfaceTests(unittest.TestCase):
    def setUp(self):
        _FakeMeshCoreFactory.last_core = None
        self.factory_patch = patch.object(
            meshcore_interface, "MeshCore", _FakeMeshCoreFactory
        )
        self.factory_patch.start()
        self.interface = MeshCoreInterface("serial", port="/dev/fake")

    def tearDown(self):
        self.interface.close()
        self.factory_patch.stop()

    def test_exposes_meshtastic_shaped_node_inventory(self):
        self.assertIn(PEER_KEY, self.interface.nodes)
        self.assertEqual(
            self.interface.nodes[PEER_KEY]["user"]["longName"], "Peer Node"
        )
        self.assertEqual(
            self.interface.getMyNodeInfo()["user"]["id"], LOCAL_KEY[:12]
        )
        self.assertEqual(
            self.interface.getMyNodeInfo()["user"]["publicKey"], LOCAL_KEY
        )
        self.assertEqual(self.interface.protocol_name, "MeshCore")

    def test_send_text_maps_to_meshcore_direct_message(self):
        result = self.interface.sendText("hello", PEER_KEY[:12])
        self.assertEqual(result.id, "01020304")
        core = _FakeMeshCoreFactory.last_core
        self.assertEqual(core.commands.sent, [(PEER_KEY, "hello")])
        # Waits for this message's own ACK, for as long as it always did.
        self.assertEqual(core.ack_waits, [
            (meshcore_interface.EventType.ACK, {"code": "01020304"}, 12)])

    def test_no_ack_is_its_own_error(self):
        """The relay retries a silent recipient over hours and a radio that
        could not transmit in minutes, so the two must not look alike."""
        import relay_ack
        _FakeMeshCoreFactory.last_core.ack = False
        with self.assertRaises(relay_ack.RecipientNoAck):
            self.interface.sendText("hello", PEER_KEY[:12])

    def test_a_refused_send_is_not_mistaken_for_no_ack(self):
        import relay_ack
        core = _FakeMeshCoreFactory.last_core

        async def refuse(destination, text):
            return types.SimpleNamespace(type=meshcore_interface.EventType.ERROR,
                                         payload={"reason": "table full"})
        core.commands.send_msg = refuse
        with self.assertRaises(IOError) as caught:
            self.interface.sendText("hello", PEER_KEY[:12])
        self.assertNotIsInstance(caught.exception, relay_ack.RecipientNoAck)
        self.assertEqual(core.ack_waits, [], "no ACK is waited for a send that never went")

    def test_has_contact(self):
        self.assertTrue(self.interface.has_contact(PEER_KEY))
        self.assertTrue(self.interface.has_contact(PEER_KEY[:12]))
        self.assertFalse(self.interface.has_contact("33" * 32))
        self.assertFalse(self.interface.has_contact(""))

    def _run(self, coroutine):
        asyncio.run_coroutine_threadsafe(coroutine, self.interface._loop).result(timeout=2)

    def test_signs_of_life_are_heard_and_woken(self):
        for handler in (self.interface._on_sign_of_life, self.interface._on_contact_update):
            with self.subTest(handler=handler.__name__):
                self.interface.take_wakes()
                self.interface._last_heard.clear()
                before = int(time.time())
                self._run(handler(types.SimpleNamespace(payload={"public_key": PEER_KEY})))
                self.assertEqual(self.interface.take_wakes(), {PEER_KEY})
                self.assertEqual(self.interface.take_wakes(), set(), "emptied by reading")
                self.assertGreaterEqual(self.interface.nodes[PEER_KEY]["lastHeard"], before)

    def test_a_direct_message_is_a_sign_of_life(self):
        self.interface.take_wakes()
        self._run(self.interface._on_contact_message(types.SimpleNamespace(payload={
            "pubkey_prefix": PEER_KEY[:12], "txt_type": 0, "text": "hi"})))
        self.assertEqual(self.interface.take_wakes(), {PEER_KEY})
        self.assertIn("lastHeard", self.interface.nodes[PEER_KEY])

    def test_last_heard_survives_a_contact_refresh(self):
        self._run(self.interface._on_sign_of_life(
            types.SimpleNamespace(payload={"public_key": PEER_KEY})))
        heard = self.interface.nodes[PEER_KEY]["lastHeard"]
        self.interface._refresh_nodes()
        self.assertEqual(self.interface.nodes[PEER_KEY]["lastHeard"], heard)

    def test_adverts_and_path_updates_are_subscribed(self):
        core = _FakeMeshCoreFactory.last_core
        self.assertIn(meshcore_interface.EventType.ADVERTISEMENT, core.subscriptions)
        self.assertIn(meshcore_interface.EventType.PATH_UPDATE, core.subscriptions)

    def test_rejects_frames_over_meshcore_text_limit(self):
        with self.assertRaisesRegex(ValueError, "exceeds 160 bytes"):
            self.interface.sendText("x" * (MESHCORE_MAX_TEXT_BYTES + 1), PEER_KEY)

    def test_meshtastic_broadcast_id_maps_to_meshcore_channel(self):
        self.interface.channel_index = 2
        self.interface.sendText("urgent notice", 0)
        self.assertEqual(
            _FakeMeshCoreFactory.last_core.commands.channel_sent,
            [(2, "urgent notice")],
        )

    def test_receive_event_is_published_in_bbs_packet_shape(self):
        received = []
        ready = threading.Event()
        topic = "test.meshcore.receive"
        self.interface.receive_topic = topic
        self.interface.bbs_nodes = [PEER_KEY[:12]]

        def listener(packet, interface):
            received.append((packet, interface))
            ready.set()

        pub.subscribe(listener, topic)
        try:
            event = types.SimpleNamespace(
                payload={
                    "pubkey_prefix": PEER_KEY[:12],
                    "txt_type": 0,
                    "text": "SYNCSTATE|1|2",
                }
            )
            future = asyncio.run_coroutine_threadsafe(
                self.interface._on_contact_message(event), self.interface._loop
            )
            future.result(timeout=2)
            self.assertTrue(ready.wait(2))
        finally:
            pub.unsubscribe(listener, topic)

        packet, iface = received[0]
        self.assertIs(iface, self.interface)
        self.assertEqual(packet["fromId"], PEER_KEY[:12])
        self.assertEqual(packet["to"], self.interface.myInfo.my_node_num)
        self.assertEqual(
            packet["decoded"]["payload"], b"SYNCSTATE|1|2"
        )

    def test_channel_receive_event_is_published_for_chatter_capture(self):
        received = []
        ready = threading.Event()
        topic = "test.meshcore.channel.receive"
        self.interface.receive_topic = topic

        def listener(packet, interface):
            received.append((packet, interface))
            ready.set()

        pub.subscribe(listener, topic)
        try:
            event = types.SimpleNamespace(payload={
                "channel_idx": 0,
                "txt_type": 0,
                "sender_timestamp": 1788091200,
                "text": "Hello Public",
                "txt_hash": 1234,
            })
            future = asyncio.run_coroutine_threadsafe(
                self.interface._on_channel_message(event), self.interface._loop
            )
            future.result(timeout=2)
            self.assertTrue(ready.wait(2))
        finally:
            pub.unsubscribe(listener, topic)

        packet, iface = received[0]
        self.assertIs(iface, self.interface)
        self.assertEqual(packet["to"], 0)
        self.assertEqual(packet["channel_index"], 0)
        self.assertEqual(packet["channel_name"], "Public")
        self.assertTrue(packet["public_chatter_only"])
        self.assertNotIn("fromId", packet)

    def test_subscribes_to_channel_messages(self):
        core = _FakeMeshCoreFactory.last_core
        self.assertIn(meshcore_interface.EventType.CHANNEL_MSG_RECV, core.subscriptions)

    def test_receive_fetching_starts_only_when_server_is_ready(self):
        core = _FakeMeshCoreFactory.last_core
        self.assertFalse(core.receive_started)
        self.interface.start_receive()
        self.assertTrue(core.receive_started)

    def test_numeric_sender_resolves_to_configured_key_alias(self):
        self.interface.allowed_nodes = [PEER_KEY[:12]]
        # The interface's own derivation, not a copy of it. This used to be
        # int(PEER_KEY[:8], 16), which is the 32-bit number that let two
        # MeshCore players share an identity; see player_identity.
        peer_num = meshcore_interface._node_num(PEER_KEY)
        self.assertEqual(
            self.interface.node_id_from_num(peer_num), PEER_KEY[:12]
        )


if __name__ == "__main__":
    unittest.main()
