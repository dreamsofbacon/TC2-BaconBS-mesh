"""A node with no radio of its own.

An MQTT-only node -- one that mirrors another BBS's content over a broker --
had no way to say so. [interface] type was mandatory and every valid value
named hardware, so the only way to run radio-less was to point at a serial
port that would never appear. That node then spent the rest of its life in
the reconnect loop: eight connect attempts, give up, back off, repeat,
forever, with a permanently broken "primary" link in the GUI. It also meant
a container started with no device passed through could not be configured
into a valid state at all.

`type = none` says it outright: no radio, don't look for one.
"""
import configparser
import io
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


import radio_stubs

radio_stubs.install()

import config_init
from radio_link import RadioLink


class InterfaceTypeNoneTests(unittest.TestCase):
    def test_none_is_a_valid_interface_type(self):
        with patch("time.sleep"):
            self.assertIsNone(config_init.get_interface(
                {"interface_type": "none", "port": None, "hostname": None}))

    def test_it_does_not_try_to_open_anything(self):
        """The whole point: no attempts, no backoff, no waiting."""
        with patch.object(config_init.meshtastic.serial_interface, "SerialInterface",
                          side_effect=AssertionError("tried to open a radio"),
                          create=True), \
             patch("time.sleep", side_effect=AssertionError("slept on a retry")):
            self.assertIsNone(config_init.get_interface(
                {"interface_type": "none", "port": None, "hostname": None}))

    def test_a_real_type_still_opens_a_radio(self):
        """Guards against 'none' short-circuiting everything."""
        good = MagicMock(name="iface")
        with patch.object(config_init.meshtastic.serial_interface,
                          "SerialInterface", return_value=good, create=True), \
             patch("time.sleep"):
            self.assertIs(config_init.get_interface(
                {"interface_type": "serial", "port": "/dev/ttyUSB0",
                 "hostname": None}), good)

    def test_an_unknown_type_is_still_rejected(self):
        with self.assertRaises(ValueError):
            config_init.get_interface(
                {"interface_type": "bogus", "port": None, "hostname": None})


class ConfigParsingTests(unittest.TestCase):
    def _read(self, text):
        config = configparser.ConfigParser()
        config.read_file(io.StringIO(text))
        return (config.get("interface", "type", fallback="none").strip().lower()
                or "none")

    def test_a_missing_interface_section_means_no_radio(self):
        """Rather than the KeyError it used to raise, which read like a
        corrupt config rather than an unconfigured one."""
        self.assertEqual(self._read("[sync]\nbbs_nodes =\n"), "none")

    def test_an_empty_type_means_no_radio(self):
        self.assertEqual(self._read("[interface]\ntype =\n"), "none")

    def test_the_example_config_documents_it(self):
        import pathlib
        example = (pathlib.Path(__file__).resolve().parent.parent
                   / "example_config.ini").read_text(encoding="utf-8")
        self.assertIn("type = none", example)


class DormantPrimaryLinkTests(unittest.TestCase):
    """The primary link still exists -- links[0] is assumed all over the main
    loop -- but it must never be ticked or reconnected."""

    def setUp(self):
        import server
        self.server = server

    def test_a_link_is_enabled_by_default(self):
        """Every existing link, and every test that builds a bare one."""
        self.assertTrue(RadioLink("primary", None).enabled)

    def test_a_no_radio_primary_is_not_considered_configured(self):
        """_reconnect_link abandons a link that is no longer configured, which
        is what stops the endless retry."""
        link = RadioLink("primary", None, enabled=False)
        self.assertFalse(self.server._is_link_still_configured(
            link, {"interface_type": "none"}))

    def test_a_real_primary_is_still_always_configured(self):
        """A radio that is merely unplugged must keep being retried."""
        link = RadioLink("primary", None)
        self.assertTrue(self.server._is_link_still_configured(
            link, {"interface_type": "serial"}))

    def test_a_dormant_link_is_skipped_before_the_liveness_check(self):
        """Otherwise every tick spawns another reconnect thread for a radio
        that does not exist."""
        link = RadioLink("primary", None, enabled=False)
        with patch.object(self.server, "_is_interface_alive",
                          side_effect=AssertionError("checked a dormant link")):
            self.server._run_link_tick(
                link, system_config={"interface_type": "none"},
                config_path="config.ini", triggers={}, now=0.0)
        self.assertFalse(link.reconnect_needed.is_set())
        self.assertFalse(link.reconnecting)


class RoutingTests(unittest.TestCase):
    """links[0] is the primary, and on an MQTT-only node that is the dormant
    one -- so the unmatched-peer fallback has to skip it or messages go
    nowhere."""

    def setUp(self):
        import server
        self.server = server

    @staticmethod
    def _iface(protocol):
        # bbs_nodes/allowed_nodes/network_key are read-only properties derived
        # from the interface, so an empty-listed stand-in is how a link with
        # no matching peers is expressed.
        return types.SimpleNamespace(
            bbs_nodes=[], allowed_nodes=[], subscriber_nodes=[],
            protocol_name=protocol)

    def _links(self):
        dormant = RadioLink("primary", self._iface("Meshtastic"), enabled=False)
        live = RadioLink("mqtt1", self._iface("MQTT"))
        return [dormant, live]

    def test_an_unknown_peer_falls_back_to_a_live_link(self):
        links = self._links()
        with patch.object(self.server, "home_network", lambda _n: "nowhere"):
            chosen = self.server._link_for_node(links, "!deadbeef")
        self.assertEqual(chosen.name, "mqtt1")

    def test_with_a_normal_primary_the_fallback_is_unchanged(self):
        links = self._links()
        links[0].enabled = True
        with patch.object(self.server, "home_network", lambda _n: "nowhere"):
            chosen = self.server._link_for_node(links, "!deadbeef")
        self.assertEqual(chosen.name, "primary")


class ReportingTests(unittest.TestCase):
    def test_a_dormant_link_is_not_counted_as_active(self):
        """The startup line said "3 active links" on a node that had two,
        because the dormant primary was still in the list."""
        import pathlib
        source = (pathlib.Path(__file__).resolve().parent.parent
                  / "server.py").read_text(encoding="utf-8")
        self.assertIn("active_links = [l for l in links if getattr(l, 'enabled', True)]",
                      source)
        self.assertNotIn("running on {len(links)} active links", source)


if __name__ == "__main__":
    unittest.main()
