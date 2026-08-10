"""Proves the RadioLink/main-loop generalization genuinely isn't capped at
two links -- the gap the MQTT sync plan flagged in server.py/radio_link.py:
_reconnect_link's old link.name=='primary' special-case, and network_key /
_link_for_node's single meshcore-or-meshtastic bucket, both silently broke
the moment a THIRD link (the first MQTT link) existed. This file exercises
three simultaneous links: primary (Meshtastic-shaped) + mqtt1 + mqtt2.
"""
import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations
from db_operations import add_bulletin, sync_bulletins_to_nodes


class _MeshInterfaceError(Exception):
    pass


def _install_fake_meshtastic_package():
    """See tests/test_radio_link_watchdog.py's identical helper for the
    full rationale (config_init.py/server.py import meshtastic.* at module
    load time and get cached in sys.modules bound to whichever fake was
    active first)."""
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


class _FakeInterface:
    def __init__(self, protocol_name="Meshtastic", max_text_bytes=220, is_connected=True):
        self.protocol_name = protocol_name
        self.max_text_bytes = max_text_bytes
        self.is_connected = is_connected
        self.bbs_nodes = []
        self.allowed_nodes = []
        self.subscriber_nodes = []
        self.sent = []

    def close(self):
        pass

    def sendText(self, text, destinationId, wantAck, wantResponse):
        del wantAck, wantResponse
        self.sent.append((text, destinationId))


class ThreeLinkRecordPropagationTests(unittest.TestCase):
    """Extends test_dual_interface_bridge.py's core "no relay layer" property
    from 2 links to 3: a record synced in via one link is picked up by BOTH
    other links' own independent sync push, purely because it's now in the
    shared DB."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_record_propagates_to_both_other_links(self):
        primary = _FakeInterface(protocol_name="Meshtastic", max_text_bytes=220)
        mqtt1 = _FakeInterface(protocol_name="MQTT:mqtt1", max_text_bytes=32768)
        mqtt2 = _FakeInterface(protocol_name="MQTT:mqtt2", max_text_bytes=32768)

        # Arrived via the primary radio's sync (stored with bbs_nodes=[]).
        add_bulletin(
            "General", "PEERA", "Hello", "posted on the primary radio",
            [], primary, unique_id="uid-n-links-1", date="2026-01-01 00:00",
        )
        self.assertEqual(primary.sent, [])

        result_1 = sync_bulletins_to_nodes(["peer-on-mqtt1"], mqtt1)
        result_2 = sync_bulletins_to_nodes(["peer-on-mqtt2"], mqtt2)

        self.assertEqual(result_1["bulletins_synced"], 1)
        self.assertEqual(result_2["bulletins_synced"], 1)
        self.assertTrue(mqtt1.sent)
        self.assertTrue(mqtt2.sent)
        self.assertIn("uid-n-links-1", mqtt1.sent[0][0])
        self.assertIn("uid-n-links-1", mqtt2.sent[0][0])
        # Never echoed back out on the link it arrived via.
        self.assertEqual(primary.sent, [])


class ReconnectFnDisambiguationTests(_FreshServerCase):
    """_reconnect_link must call each link's OWN reconnect_fn -- the old
    `if link.name == 'primary': ... else: get_secondary_interface()` special
    case would have wrongly rebuilt a third link (mqtt2) using the secondary
    radio's config."""

    def test_reconnect_link_uses_the_links_own_reconnect_fn(self):
        old_iface = _FakeInterface(protocol_name="MQTT:mqtt2")
        new_iface = _FakeInterface(protocol_name="MQTT:mqtt2")
        calls = []

        def fake_reconnect_fn(system_config):
            calls.append(system_config)
            return new_iface

        link = self.RadioLink(
            "mqtt2", old_iface,
            sync_section="sync_mqtt2", allow_section="allow_list_mqtt2",
            bbs_nodes_key="bbs_nodes_mqtt2", allowed_nodes_key="allowed_nodes_mqtt2",
            subscriber_nodes_key="subscriber_nodes_mqtt2",
            reconnect_fn=fake_reconnect_fn,
        )
        system_config = {"mqtt_links": []}

        import tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False, encoding="utf-8")
        tmp.write("[sync_mqtt2]\nbbs_nodes =\n\n[allow_list_mqtt2]\nallowed_nodes =\n")
        tmp.close()
        try:
            from unittest.mock import patch
            with patch("time.sleep", return_value=None):
                self.server._reconnect_link(link, system_config, tmp.name)
        finally:
            import os
            os.unlink(tmp.name)

        self.assertEqual(len(calls), 1)
        self.assertIs(link.interface, new_iface)
        self.assertFalse(link.reconnecting)

    def test_reconnecting_third_link_does_not_stall_the_other_two(self):
        """The dual-radio isolation property (test_radio_link_watchdog.py),
        extended to 3 links: mqtt2 stuck reconnecting must not prevent
        primary's or mqtt1's tick from completing."""
        import tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False, encoding="utf-8")
        tmp.write(
            "[interface]\ntype = serial\nport = COM1\n\n"
            "[sync]\nbbs_nodes =\n\n[allow_list]\nallowed_nodes =\n"
        )
        tmp.close()
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        try:
            primary = self.RadioLink("primary", _FakeInterface(is_connected=True))
            mqtt1 = self.RadioLink(
                "mqtt1", _FakeInterface(protocol_name="MQTT:mqtt1", is_connected=True),
                sync_section="sync_mqtt1", allow_section="allow_list_mqtt1",
                bbs_nodes_key="bbs_nodes_mqtt1", allowed_nodes_key="allowed_nodes_mqtt1",
                subscriber_nodes_key="subscriber_nodes_mqtt1",
            )
            mqtt2 = self.RadioLink(
                "mqtt2", _FakeInterface(protocol_name="MQTT:mqtt2", is_connected=False),
                sync_section="sync_mqtt2", allow_section="allow_list_mqtt2",
                bbs_nodes_key="bbs_nodes_mqtt2", allowed_nodes_key="allowed_nodes_mqtt2",
                subscriber_nodes_key="subscriber_nodes_mqtt2",
            )
            mqtt2.reconnecting = True
            self.server._active_links = [primary, mqtt1, mqtt2]

            system_config = {"interface_type": "serial"}
            now = 1_000_000.0
            triggers = {
                "manual": False, "force_check": False, "peer_resync_node": None,
                "resolve_zork_save": None, "resolve_record": None,
            }

            for link in (primary, mqtt1, mqtt2):
                self.server._run_link_tick(
                    link, system_config=system_config, config_path=tmp.name,
                    triggers=triggers, now=now,
                )

            self.assertEqual(primary.next_node_sync_check, now + 5)
            self.assertEqual(mqtt1.next_node_sync_check, now + 5)
            # mqtt2 never got past the early return for a reconnecting link.
            self.assertEqual(mqtt2.next_node_sync_check, 0.0)
        finally:
            conn = getattr(db_operations.thread_local, "connection", None)
            if conn is not None:
                conn.close()
                del db_operations.thread_local.connection
            import os
            os.unlink(tmp.name)


class LinkForNodeMembershipRoutingTests(_FreshServerCase):
    """The test that would have caught the network_key collision if it had
    shipped unfixed: two simultaneous MQTT links (same protocol family)
    must route a peer to the specific link it's actually configured on,
    not just "the first link whose coarse network bucket matches" (which,
    for two same-protocol links, either link would satisfy -- or with
    unrecognized id shapes, NEITHER would, since a per-link network_key
    like 'mqtt:mqtt2' never equals the generic bucket home_network()
    returns for an id it's never seen before)."""

    def test_peer_configured_only_on_mqtt2_routes_to_mqtt2_not_mqtt1(self):
        primary = self.RadioLink("primary", _FakeInterface(protocol_name="Meshtastic"))
        mqtt1 = self.RadioLink(
            "mqtt1", _FakeInterface(protocol_name="MQTT:mqtt1"),
            sync_section="sync_mqtt1", allow_section="allow_list_mqtt1",
        )
        mqtt2 = self.RadioLink(
            "mqtt2", _FakeInterface(protocol_name="MQTT:mqtt2"),
            sync_section="sync_mqtt2", allow_section="allow_list_mqtt2",
        )
        peer_id = "mqtt:baconbs/city-a-c:peer-label"
        mqtt2.interface.allowed_nodes = [peer_id]

        links = [primary, mqtt1, mqtt2]
        resolved = self.server._link_for_node(links, peer_id)

        self.assertIs(resolved, mqtt2)

    def test_unconfigured_peer_falls_back_to_first_link(self):
        primary = self.RadioLink("primary", _FakeInterface(protocol_name="Meshtastic"))
        mqtt1 = self.RadioLink(
            "mqtt1", _FakeInterface(protocol_name="MQTT:mqtt1"),
            sync_section="sync_mqtt1", allow_section="allow_list_mqtt1",
        )
        links = [primary, mqtt1]
        resolved = self.server._link_for_node(links, "!totally-unconfigured")
        self.assertIs(resolved, primary)


if __name__ == "__main__":
    unittest.main()
