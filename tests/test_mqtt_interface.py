import threading
import types
import unittest
from unittest.mock import patch

from pubsub import pub

import mqtt_interface
import utils
from mqtt_interface import MQTT_MAX_TEXT_BYTES, MqttInterface, _node_num


class _FakeBroker:
    """Shared in-memory broker: delivers every publish to every subscriber
    of that topic, INCLUDING the publisher itself -- real MQTT brokers do
    not suppress self-echo, which is why MqttInterface._on_message filters
    it explicitly. Letting the fake echo too keeps that behavior honest."""

    def __init__(self):
        self.subscribers = {}

    def subscribe(self, topic, client):
        self.subscribers.setdefault(topic, []).append(client)

    def publish(self, topic, payload):
        for client in self.subscribers.get(topic, []):
            client._deliver(topic, payload)


class _FakeMqttClient:
    """Stand-in for paho.mqtt.client.Client, synchronous for test determinism
    (real paho fires on_connect/on_message from a background network thread;
    here loop_start()/publish() invoke the callbacks inline)."""

    def __init__(self, callback_api_version, client_id=None):
        del callback_api_version
        self.client_id = client_id
        self.username = None
        self.password = None
        self.tls = False
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self._connected = False
        self._mid = 0
        self.published = []
        self.broker = None  # assigned by the test factory

    def username_pw_set(self, username, password=None):
        self.username = username
        self.password = password

    def tls_set(self, *args, **kwargs):
        del args, kwargs
        self.tls = True

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        del min_delay, max_delay

    def connect(self, host, port, keepalive=60):
        self.host = host
        self.port = port
        del keepalive

    def loop_start(self):
        if self.on_connect:
            self.on_connect(self, None, {}, 0)

    def loop_stop(self):
        pass

    def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    def subscribe(self, topic, qos=0):
        del qos
        self._connected = True
        self.broker.subscribe(topic, self)

    def publish(self, topic, payload, qos=0, retain=False):
        del qos, retain
        self._mid += 1
        self.published.append((topic, payload))
        self.broker.publish(topic, payload)
        return types.SimpleNamespace(
            rc=mqtt_interface.mqtt.MQTT_ERR_SUCCESS,
            mid=self._mid,
            wait_for_publish=lambda timeout=None: None,
        )

    def _deliver(self, topic, payload):
        if self.on_message is None:
            return
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        msg = types.SimpleNamespace(topic=topic, payload=data)
        self.on_message(self, None, msg)


class _RejectingMqttClient(_FakeMqttClient):
    """Simulates a broker rejecting the connection (e.g. bad credentials)."""

    def loop_start(self):
        if self.on_connect:
            self.on_connect(self, None, {}, 5)  # 5 = not authorized


def _make_client_factory(broker):
    def factory(callback_api_version, client_id=None):
        client = _FakeMqttClient(callback_api_version, client_id=client_id)
        client.broker = broker
        return client

    return factory


class MqttInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.broker = _FakeBroker()
        self.client_patch = patch.object(
            mqtt_interface.mqtt, "Client", _make_client_factory(self.broker)
        )
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def _make_interface(self, local_id, link_name="mqtt1", topic_prefix="baconbs/city-a-b"):
        iface = MqttInterface(
            host="broker.example.com",
            topic_prefix=topic_prefix,
            local_id=local_id,
            link_name=link_name,
        )
        self.addCleanup(iface.close)
        return iface

    def test_connects_and_reports_protocol_name_per_link(self):
        iface = self._make_interface("node-a", link_name="mqtt1")
        self.assertTrue(iface.is_connected)
        self.assertEqual(iface.protocol_name, "MQTT:mqtt1")
        self.assertEqual(iface.max_text_bytes, MQTT_MAX_TEXT_BYTES)
        self.assertTrue(iface.is_low_latency)

    def test_connect_rejection_raises(self):
        def rejecting_factory(callback_api_version, client_id=None):
            client = _RejectingMqttClient(callback_api_version, client_id=client_id)
            client.broker = self.broker
            return client

        with patch.object(mqtt_interface.mqtt, "Client", rejecting_factory):
            with self.assertRaises(ConnectionError):
                MqttInterface(
                    host="broker.example.com",
                    topic_prefix="baconbs/reject",
                    local_id="node-a",
                )

    def test_send_text_direct_round_trips_between_two_links(self):
        node_a = self._make_interface("node-a")
        node_b = self._make_interface("node-b")
        # Mirrors real production config: a configured peer's full string
        # id lives in bbs_nodes even before any message from it is seen --
        # this is what lets sendText resolve a numeric destination for a
        # peer node_a hasn't heard from yet (see _resolve_configured_label).
        node_a.bbs_nodes = [node_b._self_node_id]

        received = []
        ready = threading.Event()
        topic = "test.mqtt.receive"
        node_b.receive_topic = topic

        def listener(packet, interface):
            received.append((packet, interface))
            ready.set()

        pub.subscribe(listener, topic)
        try:
            node_a.sendText("hello b", node_b.myInfo.my_node_num)
            self.assertTrue(ready.wait(2))
        finally:
            pub.unsubscribe(listener, topic)

        packet, iface = received[0]
        self.assertIs(iface, node_b)
        self.assertEqual(packet["from"], node_a.myInfo.my_node_num)
        self.assertEqual(packet["to"], node_b.myInfo.my_node_num)
        self.assertEqual(packet["decoded"]["payload"], b"hello b")
        self.assertIn(node_a._self_node_id, node_b.nodes)

    def test_broadcast_send_reaches_all_subscribers_as_to_zero(self):
        node_a = self._make_interface("node-a")
        node_b = self._make_interface("node-b")

        received = []
        topic = "test.mqtt.broadcast.receive"
        node_b.receive_topic = topic
        ready = threading.Event()

        def listener(packet, interface):
            received.append(packet)
            ready.set()

        pub.subscribe(listener, topic)
        try:
            node_a.sendText("urgent notice", 0)
            self.assertTrue(ready.wait(2))
        finally:
            pub.unsubscribe(listener, topic)

        self.assertEqual(received[0]["to"], 0)

    def test_sender_never_receives_its_own_publish(self):
        node_a = self._make_interface("node-a")
        topic = "test.mqtt.self_echo"
        node_a.receive_topic = topic
        received = []
        pub.subscribe(lambda packet, interface: received.append(packet), topic)
        try:
            node_a.sendText("echo test", 0)
        finally:
            pub.unsubscribe(lambda packet, interface: None, topic)
        self.assertEqual(received, [])

    def test_rejects_frames_over_mqtt_text_limit(self):
        iface = self._make_interface("node-a")
        with self.assertRaisesRegex(ValueError, f"exceeds {MQTT_MAX_TEXT_BYTES} bytes"):
            iface.sendText("x" * (MQTT_MAX_TEXT_BYTES + 1), 0)

    def test_max_text_bytes_avoids_chunking_for_large_sync_frames(self):
        iface = self._make_interface("node-a")
        # A frame far larger than LoRa's ~220-byte cap still fits in one
        # chunk under the MQTT interface's much larger max_text_bytes --
        # this is what satisfies "turbo mode" chunking with zero new code.
        big_text = "SYNCFRAME|" + ("x" * 4000)
        chunks = utils._split_into_chunks(big_text, max_len=utils.get_max_text_bytes(iface))
        self.assertEqual(len(chunks), 1)

    def test_node_id_from_num_resolves_synthesized_id(self):
        node_a = self._make_interface("node-a")
        node_b = self._make_interface("node-b")
        topic = "test.mqtt.resolve.receive"
        node_b.receive_topic = topic
        ready = threading.Event()
        listener = lambda packet, interface: ready.set()
        pub.subscribe(listener, topic)
        try:
            # Addressed by node_b's own string id -- node_b learns node_a's
            # num<->id mapping purely from receiving this message, with no
            # prior configured peer list needed.
            node_a.sendText("hi", node_b._self_node_id)
            self.assertTrue(ready.wait(2))
        finally:
            pub.unsubscribe(listener, topic)
        resolved = node_b.node_id_from_num(node_a.myInfo.my_node_num)
        self.assertEqual(resolved, node_a._self_node_id)

    def test_sendtext_resolves_unseen_peer_via_configured_bbs_nodes(self):
        node_a = self._make_interface("node-a")
        node_b = self._make_interface("node-b")
        # node_a has never received anything from node_b -- only a config
        # peer-list entry (mirroring `[sync_mqtt1] bbs_nodes = ...`).
        node_a.bbs_nodes = [node_b._self_node_id]
        self.assertNotIn(node_b.myInfo.my_node_num, node_a._num_to_label)

        result = node_a.sendText("hello, unseen peer", node_b.myInfo.my_node_num)

        self.assertIsNotNone(result.id)
        self.assertEqual(node_a._num_to_label[node_b.myInfo.my_node_num], "node-b")


class NodeIdCollisionAvoidanceTests(unittest.TestCase):
    """Regression test: meshcore_interface._node_num truncates to the first
    8 hex chars of a public key, a real collision surface. This proves the
    MQTT scheme -- a full-width hash over the COMPLETE id string -- does not
    repeat that specific mistake for labels that would collide under an
    8-char truncation."""

    def test_labels_colliding_under_8_char_truncation_get_distinct_ids(self):
        # Two distinct labels sharing the same first 8 hex characters --
        # exactly the shape that would collide under meshcore's scheme.
        shared_prefix = "deadbeef"
        label_1 = shared_prefix + "1111111111111111"
        label_2 = shared_prefix + "2222222222222222"

        # A naive truncate-to-8-hex-chars scheme WOULD collide here:
        truncated_1 = int(label_1[:8], 16) if all(c in "0123456789abcdef" for c in label_1[:8]) else None
        truncated_2 = int(label_2[:8], 16) if all(c in "0123456789abcdef" for c in label_2[:8]) else None
        self.assertEqual(truncated_1, truncated_2)  # sanity: the trap this avoids

        id_1 = mqtt_interface._mqtt_node_id("baconbs/prefix", label_1)
        id_2 = mqtt_interface._mqtt_node_id("baconbs/prefix", label_2)
        self.assertNotEqual(_node_num(id_1), _node_num(id_2))

    def test_node_id_shape_is_unambiguous_against_meshtastic(self):
        # Full three-way home_network() classification (including the new
        # 'mqtt' bucket) is covered in tests/test_home_network_mqtt.py --
        # this only checks the shape itself never collides with a
        # Meshtastic-style '!'-prefixed id.
        node_id = mqtt_interface._mqtt_node_id("baconbs/prefix", "some-label")
        self.assertFalse(node_id.startswith("!"))
        self.assertTrue(node_id.startswith("mqtt"))


if __name__ == "__main__":
    unittest.main()
