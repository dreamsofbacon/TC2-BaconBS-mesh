"""Confirms server.py's already-generic diagnostics path (_describe_radio /
write_runtime_diagnostics_snapshot) renders a real MqttInterface sensibly,
with ZERO new code required -- per the MQTT sync plan's Phase 6, this is
confirmation work, not new functionality: web_admin.py's radios-array
handling was already fully N-radio-agnostic before MQTT support existed.
"""
import json
import sys
import types
import unittest
from unittest.mock import patch

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import mqtt_interface
from mqtt_interface import MqttInterface


class _FakeBroker:
    def __init__(self):
        self.subscribers = {}

    def subscribe(self, topic, client):
        self.subscribers.setdefault(topic, []).append(client)

    def publish(self, topic, payload):
        for client in self.subscribers.get(topic, []):
            client._deliver(topic, payload)


class _FakeMqttClient:
    def __init__(self, callback_api_version, client_id=None):
        del callback_api_version
        self.client_id = client_id
        self.on_connect = None
        self.on_message = None
        self._connected = False
        self._mid = 0
        self.broker = None

    def username_pw_set(self, *a, **kw):
        pass

    def tls_set(self, *a, **kw):
        pass

    def reconnect_delay_set(self, **kw):
        pass

    def connect(self, host, port, keepalive=60):
        pass

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
        self._connected = True
        self.broker.subscribe(topic, self)

    def publish(self, topic, payload, qos=0, retain=False):
        self._mid += 1
        self.broker.publish(topic, payload)
        return types.SimpleNamespace(
            rc=mqtt_interface.mqtt.MQTT_ERR_SUCCESS, mid=self._mid,
            wait_for_publish=lambda timeout=None: None,
        )

    def _deliver(self, topic, payload):
        if self.on_message is None:
            return
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.on_message(self, None, types.SimpleNamespace(topic=topic, payload=data))


class _MeshInterfaceError(Exception):
    pass


def _install_fake_meshtastic_package():
    def _stub(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    mesh = _stub("meshtastic")
    mesh.BROADCAST_NUM = 0
    mesh.mesh_interface = _stub("meshtastic.mesh_interface")
    mesh.stream_interface = _stub("meshtastic.stream_interface")
    mesh.serial_interface = _stub("meshtastic.serial_interface")
    mesh.tcp_interface = _stub("meshtastic.tcp_interface")
    mesh.stream_interface.StreamInterface = object
    mesh.mesh_interface.MeshInterface = types.SimpleNamespace(MeshInterfaceError=_MeshInterfaceError)
    return mesh


_CACHE_KEYS = ("config_init", "server", "radio_link")


class _FreshServerCase(unittest.TestCase):
    def setUp(self):
        self._saved = {key: sys.modules.pop(key, None) for key in _CACHE_KEYS}
        self._saved_meshtastic = {
            name: mod for name, mod in sys.modules.items()
            if name == "meshtastic" or name.startswith("meshtastic.")
        }
        for name in list(self._saved_meshtastic):
            del sys.modules[name]
        _install_fake_meshtastic_package()
        import server as _server
        from radio_link import RadioLink as _RadioLink
        self.server = _server
        self.RadioLink = _RadioLink

    def tearDown(self):
        for name in list(sys.modules):
            if name == "meshtastic" or name.startswith("meshtastic.") or name in _CACHE_KEYS:
                del sys.modules[name]
        sys.modules.update(self._saved_meshtastic)
        for key, mod in self._saved.items():
            if mod is not None:
                sys.modules[key] = mod


class MqttDiagnosticsRenderingTests(_FreshServerCase):
    def _make_mqtt_interface(self, link_name="mqtt1"):
        broker = _FakeBroker()

        def factory(callback_api_version, client_id=None):
            client = _FakeMqttClient(callback_api_version, client_id=client_id)
            client.broker = broker
            return client

        with patch.object(mqtt_interface.mqtt, "Client", factory):
            iface = MqttInterface(
                host="broker.example.com", topic_prefix="baconbs/city-a-b",
                local_id="node-a", link_name=link_name,
            )
        self.addCleanup(iface.close)
        return iface

    def test_describe_radio_renders_mqtt_interface_without_error(self):
        iface = self._make_mqtt_interface()
        entry = self.server._describe_radio(iface, {})
        self.assertTrue(entry["connected"])
        self.assertEqual(entry["radio_protocol"], "MQTT:mqtt1")
        self.assertIn("interface_attached", entry)
        self.assertNotIn("error", entry)
        # getMyNodeInfo()'s user.id is what local_node_id resolves to.
        self.assertTrue(entry["local_node_id"].startswith("mqtt:baconbs/city-a-b:"))

    def test_write_runtime_diagnostics_snapshot_includes_mqtt_link(self):
        import tempfile, os
        primary = self.RadioLink("primary", self._make_mqtt_interface(link_name="primary-stand-in"))
        mqtt_link = self.RadioLink(
            "mqtt1", self._make_mqtt_interface(link_name="mqtt1"),
            sync_section="sync_mqtt1", allow_section="allow_list_mqtt1",
        )
        links = [primary, mqtt_link]

        with tempfile.TemporaryDirectory() as tmp_dir:
            snapshot_path = os.path.join(tmp_dir, "runtime_diagnostics.json")
            with patch.object(self.server, "get_runtime_diagnostics_path", return_value=snapshot_path):
                self.server.write_runtime_diagnostics_snapshot(links, {})
            with open(snapshot_path, "r", encoding="utf-8") as f:
                snapshot = json.load(f)

        names = [r["name"] for r in snapshot["radios"]]
        self.assertEqual(names, ["primary", "mqtt1"])
        protocols = [r["radio_protocol"] for r in snapshot["radios"]]
        self.assertIn("MQTT:mqtt1", protocols)


if __name__ == "__main__":
    unittest.main()
