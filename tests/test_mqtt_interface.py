import json
import os
import tempfile
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
        self.tls_kwargs = {}
        self.tls_insecure = None
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
        del args
        self.tls = True
        self.tls_kwargs = kwargs

    def tls_insecure_set(self, value):
        self.tls_insecure = value

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
        del qos
        self._mid += 1
        self.published.append((topic, payload, retain))
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

    def test_publish_status_writes_master_and_per_link_subtopics(self):
        iface = self._make_interface("node-a", link_name="mqtt1", topic_prefix="baconbs/city-a-b")
        client = iface._client
        client.published.clear()  # drop the initial subscribe-time noise, if any

        status = {
            "updated_at": "2026-08-12T00:00:00Z",
            "links": {
                "primary": {"protocol": "Meshtastic", "connected": True, "reconnecting": False},
                "mqtt1": {"protocol": "MQTT:mqtt1", "connected": True, "reconnecting": False},
            },
        }
        iface.publish_status(status)

        by_topic = {topic: (payload, retain) for topic, payload, retain in client.published}

        # Master topic: single-line JSON, retained, everything in one message.
        master_payload, master_retain = by_topic["baconbs/city-a-b/node-a/status"]
        self.assertTrue(master_retain)
        self.assertNotIn("\n", master_payload)
        self.assertEqual(json.loads(master_payload), status)

        # One retained sub-topic per link.
        primary_payload, primary_retain = by_topic["baconbs/city-a-b/node-a/status/links/primary"]
        self.assertTrue(primary_retain)
        self.assertEqual(json.loads(primary_payload), status["links"]["primary"])

        mqtt1_payload, mqtt1_retain = by_topic["baconbs/city-a-b/node-a/status/links/mqtt1"]
        self.assertTrue(mqtt1_retain)
        self.assertEqual(json.loads(mqtt1_payload), status["links"]["mqtt1"])

    def test_publish_status_after_close_is_a_silent_noop(self):
        iface = self._make_interface("node-a")
        iface.close()
        iface.publish_status({"updated_at": "now", "links": {}})  # must not raise

    def test_publish_status_malformed_links_field_still_publishes_master(self):
        """A non-dict 'links' value must not crash the sub-topic loop --
        the master topic (built by the caller, server.py's
        publish_mqtt_status, and always well-formed in practice) should
        still go out."""
        iface = self._make_interface("node-a", topic_prefix="baconbs/city-a-b")
        client = iface._client
        client.published.clear()

        iface.publish_status({"updated_at": "now", "links": "not-a-dict"})

        topics = [topic for topic, _payload, _retain in client.published]
        self.assertIn("baconbs/city-a-b/node-a/status", topics)
        self.assertEqual(len(topics), 1)  # no sub-topics attempted


class MqttTlsOptionTests(unittest.TestCase):
    """Advanced TLS / certificate options -- see MqttInterface's docstring.
    Covers the security-relevant behaviors: certs are never silently
    ignored on a plaintext connection, and a bad path fails fast with a
    clear message instead of an opaque SSL error at connect time."""

    def setUp(self):
        self.broker = _FakeBroker()
        self.client_patch = patch.object(
            mqtt_interface.mqtt, "Client", _make_client_factory(self.broker)
        )
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _cert_file(self, name):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("-----BEGIN CERTIFICATE-----\nnot-a-real-cert\n-----END CERTIFICATE-----\n")
        return path

    def _make(self, **kwargs):
        iface = MqttInterface(
            host="broker.example.com",
            topic_prefix="baconbs/city-a-b",
            local_id="node-a",
            link_name="mqtt1",
            **kwargs,
        )
        self.addCleanup(iface.close)
        return iface

    def test_plain_tls_uses_system_ca_store(self):
        """`tls = true` with no cert options must pass ca_certs=None, i.e.
        paho's default system-CA behavior -- unchanged from before these
        options existed."""
        iface = self._make(tls=True)
        self.assertTrue(iface._client.tls)
        self.assertIsNone(iface._client.tls_kwargs.get("ca_certs"))
        self.assertIsNone(iface._client.tls_kwargs.get("certfile"))
        self.assertIsNone(iface._client.tls_insecure)

    def test_no_tls_at_all_does_not_call_tls_set(self):
        iface = self._make(tls=False)
        self.assertFalse(iface._client.tls)

    def test_ca_certs_passed_through(self):
        ca = self._cert_file("ca.crt")
        iface = self._make(tls=True, tls_ca_certs=ca)
        self.assertEqual(iface._client.tls_kwargs.get("ca_certs"), ca)

    def test_client_cert_and_key_passed_through(self):
        cert = self._cert_file("client.crt")
        key = self._cert_file("client.key")
        iface = self._make(tls=True, tls_certfile=cert, tls_keyfile=key,
                           tls_keyfile_password="s3cret")
        kwargs = iface._client.tls_kwargs
        self.assertEqual(kwargs.get("certfile"), cert)
        self.assertEqual(kwargs.get("keyfile"), key)
        self.assertEqual(kwargs.get("keyfile_password"), "s3cret")

    def test_cert_options_imply_tls_even_when_tls_flag_is_false(self):
        """Security-relevant: configuring certs but forgetting `tls = true`
        must NOT silently connect in plaintext."""
        ca = self._cert_file("ca.crt")
        iface = self._make(tls=False, tls_ca_certs=ca)
        self.assertTrue(iface._client.tls)
        self.assertEqual(iface._client.tls_kwargs.get("ca_certs"), ca)

    def test_tls_insecure_calls_insecure_set(self):
        iface = self._make(tls=True, tls_insecure=True)
        self.assertTrue(iface._client.tls_insecure)

    def test_missing_cert_file_raises_naming_the_setting_and_path(self):
        missing = os.path.join(self.tmp.name, "nope.crt")
        with self.assertRaises(ValueError) as ctx:
            self._make(tls=True, tls_ca_certs=missing)
        message = str(ctx.exception)
        self.assertIn("tls_ca_certs", message)
        self.assertIn(missing, message)

    def test_keyfile_without_certfile_is_rejected(self):
        key = self._cert_file("client.key")
        with self.assertRaises(ValueError) as ctx:
            self._make(tls=True, tls_keyfile=key)
        self.assertIn("tls_keyfile", str(ctx.exception))

    def test_blank_cert_settings_are_ignored_not_treated_as_paths(self):
        """Empty strings come from the web form's untouched fields -- they
        must behave exactly like 'not configured', not like a path of ''."""
        iface = self._make(tls=True, tls_ca_certs="", tls_certfile="   ",
                           tls_keyfile="", tls_keyfile_password="")
        self.assertTrue(iface._client.tls)
        self.assertIsNone(iface._client.tls_kwargs.get("ca_certs"))
        self.assertIsNone(iface._client.tls_kwargs.get("keyfile_password"))


class TopicSanitizationTests(unittest.TestCase):
    """Spaces are legal in MQTT topics but break CLI tooling and broker ACL
    patterns; '+'/'#'/'/' are wildcards and separators, so leaving them in
    a segment silently changes the topic's SHAPE instead of naming it."""

    def test_spaces_become_hyphens(self):
        self.assertEqual(
            mqtt_interface.sanitize_topic_segment("Burlington NNE"), "Burlington-NNE")

    def test_whitespace_runs_collapse_to_one_hyphen(self):
        self.assertEqual(
            mqtt_interface.sanitize_topic_segment("  a   b  "), "a-b")

    def test_wildcards_are_neutralized(self):
        self.assertEqual(mqtt_interface.sanitize_topic_segment("a+b#c"), "a-b-c")

    def test_slash_becomes_hyphen_in_a_segment(self):
        self.assertEqual(mqtt_interface.sanitize_topic_segment("a/b"), "a-b")

    def test_slash_is_preserved_in_a_prefix(self):
        self.assertEqual(
            mqtt_interface.sanitize_topic_segment("baconbs/cityA cityB", allow_slash=True),
            "baconbs/cityA-cityB")

    def test_prefix_strips_redundant_slashes(self):
        self.assertEqual(
            mqtt_interface.sanitize_topic_segment("/a//b/", allow_slash=True), "a/b")


class MqttPublishSelectionTests(unittest.TestCase):
    """Per-broker control over what gets published."""

    def setUp(self):
        self.broker = _FakeBroker()
        self.client_patch = patch.object(
            mqtt_interface.mqtt, "Client", _make_client_factory(self.broker)
        )
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def _make(self, **kwargs):
        kwargs.setdefault("local_id", "node-a")
        iface = MqttInterface(
            host="broker.example.com", topic_prefix="baconbs/city-a-b",
            link_name="mqtt1", **kwargs,
        )
        self.addCleanup(iface.close)
        return iface

    def _topics(self, iface):
        return [topic for topic, _payload, _retain in iface._client.published]

    def test_local_id_with_space_is_normalized_in_topics(self):
        iface = self._make(local_id="Burlington NNE",
                           publish_kinds={"status": True})
        iface._client.published.clear()
        iface.publish_status({"updated_at": "now", "links": {}})
        self.assertIn("baconbs/city-a-b/Burlington-NNE/status", self._topics(iface))

    def test_disabled_category_publishes_nothing(self):
        iface = self._make(publish_kinds={"status": True, "clients": False})
        iface._client.published.clear()
        iface.publish_clients([{"node_id": "!abc", "link_name": "primary"}])
        self.assertEqual(self._topics(iface), [])

    def test_enabled_clients_publishes_summary_and_per_node(self):
        iface = self._make(publish_kinds={"clients": True})
        iface._client.published.clear()
        iface.publish_clients([
            {"node_id": "!abc", "link_name": "primary", "short_name": "AAA"},
        ])
        topics = self._topics(iface)
        self.assertIn("baconbs/city-a-b/node-a/clients", topics)
        self.assertIn("baconbs/city-a-b/node-a/clients/primary/!abc", topics)

    def test_node_id_containing_a_slash_cannot_fan_out_topic_levels(self):
        iface = self._make(publish_kinds={"clients": True})
        iface._client.published.clear()
        iface.publish_clients([
            {"node_id": "mqtt:pre/fix:node", "link_name": "mqtt2"},
        ])
        per_node = [t for t in self._topics(iface) if t.endswith("node")]
        self.assertEqual(len(per_node), 1)
        self.assertNotIn("mqtt:pre/fix:node", per_node[0])
        self.assertTrue(per_node[0].endswith("mqtt:pre_fix:node"))

    def test_publish_prefix_overrides_only_data_not_the_sync_topic(self):
        """The sync topic identifies the bridge relationship -- both ends
        must agree on it, so an override must not move it."""
        iface = self._make(publish_kinds={"status": True},
                           publish_prefix="homeassistant/baconbbs")
        self.assertEqual(iface._topic, "baconbs/city-a-b/bbs")  # unchanged
        iface._client.published.clear()
        iface.publish_status({"updated_at": "now", "links": {}})
        self.assertIn("homeassistant/baconbbs/node-a/status", self._topics(iface))

    def test_activity_events_are_not_retained(self):
        iface = self._make(publish_kinds={"activity": True})
        iface._client.published.clear()
        iface.publish_activity({"kind": "bulletin", "id": 7})
        published = [p for p in iface._client.published if "/activity/" in p[0]]
        self.assertEqual(len(published), 1)
        self.assertFalse(published[0][2], "an event must not be retained")

    def test_status_defaults_on_when_no_kinds_given(self):
        """Back-compat: a link built without publish_kinds keeps the
        original status-publishing behavior."""
        iface = self._make()
        self.assertTrue(iface.publishes("status"))
        self.assertFalse(iface.publishes("clients"))


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


class ApplyPublishSettingsTests(unittest.TestCase):
    """Toggling what a broker receives must take effect on a RUNNING link.

    Regression: publish_kinds was only read in __init__, and the publish
    flags aren't connection settings, so a reload saw no change and never
    reconnected -- the saved toggles silently did nothing until restart.
    """

    def setUp(self):
        self.broker = _FakeBroker()
        self.client_patch = patch.object(
            mqtt_interface.mqtt, "Client", _make_client_factory(self.broker)
        )
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.iface = MqttInterface(
            host="broker.example.com", topic_prefix="baconbs/city-a-b",
            local_id="node-a", link_name="mqtt1",
            publish_kinds={"status": True},
        )
        self.addCleanup(self.iface.close)

    def _topics(self):
        return [t for t, _p, _r in self.iface._client.published]

    def test_enabling_a_category_takes_effect_without_reconnecting(self):
        self.assertFalse(self.iface.publishes("clients"))
        self.iface.apply_publish_settings({"status": True, "clients": True})
        self.assertTrue(self.iface.publishes("clients"))

        self.iface._client.published.clear()
        self.iface.publish_clients([{"node_id": "!abc", "link_name": "primary"}])
        self.assertIn("baconbs/city-a-b/node-a/clients", self._topics())

    def test_disabling_a_category_stops_publishing(self):
        self.iface.apply_publish_settings({"status": False})
        self.iface._client.published.clear()
        self.iface.publish_status({"updated_at": "now", "links": {}})
        self.assertEqual(self._topics(), [])

    def test_prefix_change_applies_and_is_sanitized(self):
        self.iface.apply_publish_settings({"status": True}, "home assistant/bacon")
        self.iface._client.published.clear()
        self.iface.publish_status({"updated_at": "now", "links": {}})
        self.assertIn("home-assistant/bacon/node-a/status", self._topics())

    def test_blank_prefix_falls_back_to_topic_prefix(self):
        self.iface.apply_publish_settings({"status": True}, "")
        self.iface._client.published.clear()
        self.iface.publish_status({"updated_at": "now", "links": {}})
        self.assertIn("baconbs/city-a-b/node-a/status", self._topics())

    def test_connection_is_not_disturbed(self):
        """Changing output routing must not drop a healthy session."""
        self.assertTrue(self.iface.is_connected)
        self.iface.apply_publish_settings({"status": True, "telemetry": True})
        self.assertTrue(self.iface.is_connected)
