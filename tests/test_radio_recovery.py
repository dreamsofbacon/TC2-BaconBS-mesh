"""Tests for unattended radio-connection recovery in config_init.get_interface.

A wedged radio (USB-serial enumerates but the ESP32 stops answering the
Meshtastic handshake) must be recovered without physical access: retry,
DTR/RTS hardware-reset on repeated failure, and finally exit for a supervised
restart. These tests mock the serial layer so no hardware is required.
"""

import sys
import types
import unittest
from unittest.mock import patch, MagicMock

class _MeshInterfaceError(Exception):
    pass


def _stub(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


# Build the meshtastic package tree with submodules linked as attributes
# (config_init uses `import meshtastic.stream_interface` and references it).
_mesh = _stub("meshtastic")
_mesh.mesh_interface = _stub("meshtastic.mesh_interface")
_mesh.stream_interface = _stub("meshtastic.stream_interface")
_mesh.serial_interface = _stub("meshtastic.serial_interface")
_mesh.tcp_interface = _stub("meshtastic.tcp_interface")
_mesh.stream_interface.StreamInterface = object
_mesh.mesh_interface.MeshInterface = types.SimpleNamespace(MeshInterfaceError=_MeshInterfaceError)

# Stub serial + serial.tools.list_ports.
_ser = _stub("serial")
_ser.Serial = MagicMock(name="Serial")
_ser.tools = _stub("serial.tools")
_ser.tools.list_ports = _stub("serial.tools.list_ports")
_ser.tools.list_ports.comports = lambda: []

import config_init


class RadioRecoveryTests(unittest.TestCase):
    def _cfg(self):
        return {"interface_type": "serial", "port": "/dev/ttyUSB0", "hostname": None}

    def test_recovers_after_transient_failures_with_reset(self):
        calls = {"n": 0}
        good = MagicMock(name="iface")

        def flaky(_device):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("could not exclusively lock port")
            return good

        with patch.object(config_init.meshtastic.serial_interface,
                          "SerialInterface", side_effect=flaky, create=True), \
             patch.object(config_init, "_reset_serial_radio", return_value=True) as reset, \
             patch("time.sleep"):
            iface = config_init.get_interface(self._cfg())

        self.assertIs(iface, good)
        self.assertEqual(calls["n"], 3)            # 2 failures then success
        reset.assert_called()                       # reset fired on the 2nd failure

    def test_gives_up_and_returns_none_after_max_failures(self):
        """A radio that never responds must NOT take the whole process down
        (os._exit) -- it gives up on just this interface and returns None,
        so the caller (server.py's main()) can still run with whatever other
        radio/MQTT links DID connect, and retry this one in the background
        via _reconnect_link. See config_init._open_interface."""
        calls = {"n": 0}

        def always_fail(_device):
            calls["n"] += 1
            raise _MeshInterfaceError("radio not responding")

        with patch.object(config_init.meshtastic.serial_interface,
                          "SerialInterface", side_effect=always_fail, create=True), \
             patch.object(config_init, "_reset_serial_radio", return_value=True), \
             patch("time.sleep"), \
             patch("os._exit") as exit_mock:
            iface = config_init.get_interface(self._cfg())

        self.assertIsNone(iface)
        exit_mock.assert_not_called()
        self.assertEqual(calls["n"], config_init._MAX_CONNECT_FAILURES_BEFORE_EXIT)

    def test_invalid_interface_type_is_fatal(self):
        with self.assertRaises(ValueError):
            config_init.get_interface({"interface_type": "bogus", "port": None, "hostname": None})

    def test_tcp_requires_hostname(self):
        with self.assertRaises(ValueError):
            config_init.get_interface({"interface_type": "tcp", "port": None, "hostname": None})

    def test_tcp_never_calls_serial_reset(self):
        good = MagicMock(name="tcp_iface")
        calls = {"n": 0}

        def flaky(hostname):
            calls["n"] += 1
            if calls["n"] < 2:
                raise ConnectionRefusedError("refused")
            return good

        with patch.object(config_init.meshtastic.tcp_interface,
                          "TCPInterface", side_effect=flaky, create=True), \
             patch.object(config_init, "_reset_serial_radio") as reset, \
             patch("time.sleep"):
            iface = config_init.get_interface({"interface_type": "tcp", "port": None, "hostname": "1.2.3.4"})

        self.assertIs(iface, good)
        reset.assert_not_called()  # never DTR/RTS-reset a network device


if __name__ == "__main__":
    unittest.main()
