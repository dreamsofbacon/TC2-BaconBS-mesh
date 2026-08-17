"""Tests for the optional, open-ended [mqttN]/[sync_mqttN]/[allow_list_mqttN]
MQTT internet-bridge config schema (config_init.py) and get_mqtt_interfaces()/
get_mqtt_interface_by_name().

The core back-compat guarantee under test: a config file written before MQTT
support existed (no [mqttN] sections) must parse and behave byte-identically
to before -- mqtt_links is an empty list and get_mqtt_interfaces() returns []
without touching the network at all.
"""
import sys
import types
import unittest


class _MeshInterfaceError(Exception):
    pass


def _install_fake_meshtastic_package():
    """See tests/test_dual_interface_config.py's identical helper for the
    full rationale -- config_init.py does `import meshtastic.mesh_interface`
    etc. at module load time, and config_init/server get cached in
    sys.modules bound to whichever meshtastic module was active at that
    moment, so every test here goes through _fresh_config_init()."""
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


_CACHE_KEYS = ("config_init", "server")


class _FreshConfigInitCase(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in _CACHE_KEYS:
            self._saved[key] = sys.modules.pop(key, None)
        self._saved_meshtastic = {
            name: mod for name, mod in sys.modules.items()
            if name == "meshtastic" or name.startswith("meshtastic.")
        }
        for name in list(self._saved_meshtastic):
            del sys.modules[name]

        _install_fake_meshtastic_package()
        import config_init as _config_init
        self.config_init = _config_init

    def tearDown(self):
        for name in list(sys.modules):
            if name == "meshtastic" or name.startswith("meshtastic.") or name in _CACHE_KEYS:
                del sys.modules[name]
        sys.modules.update(self._saved_meshtastic)
        for key, mod in self._saved.items():
            if mod is not None:
                sys.modules[key] = mod


def _write_config(tmp_path, body: str) -> str:
    path = tmp_path / "config.ini"
    path.write_text(body, encoding="utf-8")
    return str(path)


_BASE = """
[interface]
type = serial
port = /dev/ttyACM0

[sync]
bbs_nodes = !aaa1,!bbb2

[allow_list]
allowed_nodes = !aaa1
"""


class _TempDirCase(_FreshConfigInitCase):
    def setUp(self):
        super().setUp()
        import tempfile
        import pathlib
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        super().tearDown()


class NoMqttSectionsBackCompatTests(_TempDirCase):
    def test_mqtt_links_empty_when_absent(self):
        cfg_path = _write_config(self.tmp_path, _BASE)
        system_config = self.config_init.initialize_config(cfg_path)
        self.assertEqual(system_config["mqtt_links"], [])
        self.assertEqual(self.config_init.get_mqtt_interfaces(system_config), [])
        self.assertIsNone(self.config_init.get_mqtt_interface_by_name(system_config, "mqtt1"))


class MqttDiscoveryTests(_TempDirCase):
    def test_discovers_single_link(self):
        body = _BASE + """
[mqtt1]
host = broker.example.com
port = 8883
topic_prefix = baconbs/city-a-b
local_id = node-a

[sync_mqtt1]
bbs_nodes = mqtt:baconbs/city-a-b:node-b
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)
        self.assertEqual(len(system_config["mqtt_links"]), 1)
        link = system_config["mqtt_links"][0]
        self.assertEqual(link["name"], "mqtt1")
        self.assertEqual(link["host"], "broker.example.com")
        self.assertEqual(link["port"], 8883)
        self.assertEqual(link["topic_prefix"], "baconbs/city-a-b")
        self.assertEqual(link["local_id"], "node-a")
        self.assertEqual(link["sync_section"], "sync_mqtt1")
        self.assertEqual(link["allow_section"], "allow_list_mqtt1")
        self.assertEqual(link["bbs_nodes_key"], "bbs_nodes_mqtt1")

    def test_reads_advanced_tls_certificate_settings(self):
        body = _BASE + """
[mqtt1]
host = broker.example.com
port = 8883
tls = true
tls_ca_certs = /etc/ssl/certs/my-ca.crt
tls_certfile = /etc/baconbs/client.crt
tls_keyfile = /etc/baconbs/client.key
tls_keyfile_password = s3cret
tls_insecure = true
topic_prefix = baconbs/city-a-b
local_id = node-a
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)
        link = system_config["mqtt_links"][0]
        self.assertTrue(link["tls"])
        self.assertEqual(link["tls_ca_certs"], "/etc/ssl/certs/my-ca.crt")
        self.assertEqual(link["tls_certfile"], "/etc/baconbs/client.crt")
        self.assertEqual(link["tls_keyfile"], "/etc/baconbs/client.key")
        self.assertEqual(link["tls_keyfile_password"], "s3cret")
        self.assertTrue(link["tls_insecure"])

    def test_tls_settings_default_to_absent_when_unset(self):
        """Back-compat: a config written before these options existed must
        produce the same behavior as before (system CA store, no client
        cert, verification on)."""
        body = _BASE + """
[mqtt1]
host = broker.example.com
topic_prefix = baconbs/city-a-b
local_id = node-a
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)
        link = system_config["mqtt_links"][0]
        self.assertFalse(link["tls"])
        self.assertIsNone(link["tls_ca_certs"])
        self.assertIsNone(link["tls_certfile"])
        self.assertIsNone(link["tls_keyfile"])
        self.assertIsNone(link["tls_keyfile_password"])
        self.assertFalse(link["tls_insecure"])

    def test_discovers_three_simultaneous_links_sorted_by_number(self):
        body = _BASE + """
[mqtt3]
host = broker-c.example.com
topic_prefix = baconbs/c

[mqtt1]
host = broker-a.example.com
topic_prefix = baconbs/a

[mqtt2]
host = broker-b.example.com
topic_prefix = baconbs/b
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)
        names = [link["name"] for link in system_config["mqtt_links"]]
        self.assertEqual(names, ["mqtt1", "mqtt2", "mqtt3"])

    def test_enabled_false_skips_link(self):
        body = _BASE + """
[mqtt1]
enabled = false
host = broker.example.com
topic_prefix = baconbs/city-a-b
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)
        self.assertEqual(system_config["mqtt_links"], [])

    def test_missing_required_field_skips_link(self):
        body = _BASE + """
[mqtt1]
host = broker.example.com
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)
        self.assertEqual(system_config["mqtt_links"], [])

    def test_local_id_defaults_when_unset(self):
        body = _BASE + """
[mqtt1]
host = broker.example.com
topic_prefix = baconbs/city-a-b
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)
        self.assertEqual(system_config["mqtt_links"][0]["local_id"], "mqtt1-node")


class GetMqttInterfacesTests(_TempDirCase):
    def _fake_mqtt_module(self, captured_calls):
        class _FakeMqttInterface:
            def __init__(self, **kwargs):
                captured_calls.append(kwargs)
                self.link_name = kwargs["link_name"]

        return types.SimpleNamespace(MqttInterface=_FakeMqttInterface)

    def test_opens_one_interface_per_configured_link(self):
        body = _BASE + """
[mqtt1]
host = broker-a.example.com
topic_prefix = baconbs/a
local_id = node-a

[mqtt2]
host = broker-b.example.com
topic_prefix = baconbs/b
local_id = node-a
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)

        captured = []
        sys.modules["mqtt_interface"] = self._fake_mqtt_module(captured)
        try:
            results = self.config_init.get_mqtt_interfaces(system_config)
        finally:
            sys.modules.pop("mqtt_interface", None)

        self.assertEqual(len(results), 2)
        self.assertEqual({r["name"] for r in results}, {"mqtt1", "mqtt2"})
        self.assertEqual({c["host"] for c in captured}, {"broker-a.example.com", "broker-b.example.com"})
        for entry in results:
            self.assertIn("interface", entry)
            self.assertIn("sync_section", entry)
            self.assertIn("allow_section", entry)

    def test_get_mqtt_interface_by_name_rebuilds_one_link(self):
        body = _BASE + """
[mqtt1]
host = broker-a.example.com
topic_prefix = baconbs/a
local_id = node-a
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)

        captured = []
        sys.modules["mqtt_interface"] = self._fake_mqtt_module(captured)
        try:
            iface = self.config_init.get_mqtt_interface_by_name(system_config, "mqtt1")
            missing = self.config_init.get_mqtt_interface_by_name(system_config, "mqtt99")
        finally:
            sys.modules.pop("mqtt_interface", None)

        self.assertIsNotNone(iface)
        self.assertEqual(iface.link_name, "mqtt1")
        self.assertIsNone(missing)

    def test_connect_failure_gives_up_without_exiting(self):
        body = _BASE + """
[mqtt1]
host = unreachable.example.com
topic_prefix = baconbs/a
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)

        class _AlwaysFailsMqttInterface:
            def __init__(self, **kwargs):
                raise ConnectionError("no route to broker")

        sys.modules["mqtt_interface"] = types.SimpleNamespace(MqttInterface=_AlwaysFailsMqttInterface)
        try:
            from unittest.mock import patch
            with patch("time.sleep", return_value=None), \
                 patch.object(self.config_init, "_MQTT_MAX_CONNECT_FAILURES", 2):
                results = self.config_init.get_mqtt_interfaces(system_config)
        finally:
            sys.modules.pop("mqtt_interface", None)

        # Gives up and continues (empty list), never raises/exits the process.
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
