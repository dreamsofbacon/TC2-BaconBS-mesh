"""Tests for the optional [interface2]/[sync2]/[allow_list2] dual-radio
bridge-mode config schema (config_init.py) and get_secondary_interface().

The core back-compat guarantee under test: a config file written before
dual-radio support existed (no [interface2] section) must parse and behave
byte-identically to before -- interface2_enabled is False and
get_secondary_interface() returns None without touching the network at all.
"""
import sys
import types
import unittest


class _MeshInterfaceError(Exception):
    pass


def _install_fake_meshtastic_package():
    """Build a complete fake meshtastic.* package tree -- config_init.py
    does `import meshtastic.mesh_interface` etc. at module load time, so a
    bare SimpleNamespace(BROADCAST_NUM=...) (what several OTHER test files
    in this suite use, since they only need utils.py's/db_operations.py's
    shallow BROADCAST_NUM access) isn't enough.

    IMPORTANT: config_init.py (and server.py, which imports it) get cached
    in sys.modules on first import, bound to WHATEVER meshtastic module was
    active at that moment. tests/test_radio_recovery.py already depends on
    being the first thing in the whole suite to import config_init, so its
    own fake meshtastic (with a distinct MeshInterfaceError class it patches
    against) sticks for its patch.object()/assertRaises() calls. Since
    pytest imports every test file during collection, this file cannot
    safely do a module-level `import config_init` -- doing so would win the
    sys.modules race in either direction depending on file collection
    order. Instead, every test below goes through _fresh_config_init(),
    which pops any cached config_init/server/meshtastic-family modules,
    imports its OWN fresh copies against ITS OWN fake package, and restores
    the prior sys.modules state afterward -- leaving zero footprint for
    whichever file legitimately owns the first-import race.
    """
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
    """Base class: gives each test its own freshly-imported config_init,
    isolated from whatever any other test file cached in sys.modules, and
    restores sys.modules exactly afterward (see _install_fake_meshtastic_package)."""

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


class SingleRadioBackCompatTests(_FreshConfigInitCase):
    """No [interface2] at all -- must behave exactly like pre-dual-radio config_init.py."""

    def setUp(self):
        super().setUp()
        import tempfile
        import pathlib
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        super().tearDown()

    def test_interface2_disabled_when_absent(self):
        cfg_path = _write_config(self.tmp_path, _BASE)
        system_config = self.config_init.initialize_config(cfg_path)
        self.assertFalse(system_config["interface2_enabled"])
        self.assertIsNone(system_config["interface2_type"])
        self.assertEqual(system_config["bbs_nodes2"], [])
        self.assertIsNone(self.config_init.get_secondary_interface(system_config))

    def test_primary_fields_unchanged(self):
        cfg_path = _write_config(self.tmp_path, _BASE)
        system_config = self.config_init.initialize_config(cfg_path)
        self.assertEqual(system_config["interface_type"], "serial")
        self.assertEqual(system_config["bbs_nodes"], ["!aaa1", "!bbb2"])
        self.assertEqual(system_config["allowed_nodes"], ["!aaa1"])


class DualRadioConfigTests(_FreshConfigInitCase):
    def setUp(self):
        super().setUp()
        import tempfile
        import pathlib
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        super().tearDown()

    def test_interface2_enabled_by_default_when_type_present(self):
        body = _BASE + """
[interface2]
type = meshcore_tcp
hostname = 192.168.1.50
tcp_port = 5000

[sync2]
bbs_nodes = 7e18ca9d30a1

[allow_list2]
allowed_nodes = 7e18ca9d30a1
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)
        self.assertTrue(system_config["interface2_enabled"])
        self.assertEqual(system_config["interface2_type"], "meshcore_tcp")
        self.assertEqual(system_config["interface2_hostname"], "192.168.1.50")
        self.assertEqual(system_config["interface2_tcp_port"], 5000)
        self.assertEqual(system_config["bbs_nodes2"], ["7e18ca9d30a1"])
        self.assertEqual(system_config["allowed_nodes2"], ["7e18ca9d30a1"])

    def test_interface2_explicitly_disabled(self):
        body = _BASE + """
[interface2]
enabled = false
type = meshcore_tcp
hostname = 192.168.1.50

[sync2]
bbs_nodes = 7e18ca9d30a1
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)
        self.assertFalse(system_config["interface2_enabled"])
        self.assertIsNone(self.config_init.get_secondary_interface(system_config))

    def test_interface2_section_present_but_no_type_is_disabled(self):
        body = _BASE + """
[interface2]
enabled = true
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)
        self.assertFalse(system_config["interface2_enabled"])

    def test_get_secondary_interface_opens_meshcore_tcp(self):
        """get_secondary_interface() must route to _open_interface() with the
        secondary's own settings, and share the SAME mqtt_topic as the
        primary (required for server.py's single pub.subscribe() to see
        both radios' traffic -- see project plan)."""
        body = _BASE + """
[interface2]
type = meshcore_tcp
hostname = 192.168.1.50
tcp_port = 5000

[sync2]
bbs_nodes = 7e18ca9d30a1
"""
        cfg_path = _write_config(self.tmp_path, body)
        system_config = self.config_init.initialize_config(cfg_path)

        captured = {}

        class _FakeMeshCoreInterface:
            protocol_name = "MeshCore"
            max_text_bytes = 160

            def __init__(self, transport, **kwargs):
                captured["transport"] = transport
                captured.update(kwargs)

        fake_module = types.SimpleNamespace(MeshCoreInterface=_FakeMeshCoreInterface)
        sys.modules["meshcore_interface"] = fake_module
        try:
            iface = self.config_init.get_secondary_interface(system_config)
        finally:
            sys.modules.pop("meshcore_interface", None)

        self.assertIsInstance(iface, _FakeMeshCoreInterface)
        self.assertEqual(captured["transport"], "tcp")
        self.assertEqual(captured["hostname"], "192.168.1.50")
        self.assertEqual(captured["tcp_port"], 5000)
        self.assertEqual(captured["receive_topic"], system_config["mqtt_topic"])


if __name__ == "__main__":
    unittest.main()
