"""Tests that one RadioLink's reconnect-with-backoff does not stall another
active link's tick -- the real behavior change dual-radio bridge mode makes
versus single-radio's original inline-blocking reconnect loop (see the
project plan). server._run_link_tick / server._reconnect_link / RadioLink.
"""
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# db_operations.py does `from meshtastic import BROADCAST_NUM` at module
# load time. If some other test file already cached a meshtastic stub that
# doesn't define it (e.g. test_radio_recovery.py's, built for a narrower
# purpose), patch it in rather than replacing the whole module -- unlike
# config_init.py/server.py below, db_operations doesn't care which fake
# backs it, only that this one attribute exists, so once cached it can stay
# cached for the rest of the session.
if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations


class _MeshInterfaceError(Exception):
    pass


def _install_fake_meshtastic_package():
    """Build a complete fake meshtastic.* package tree -- server.py ->
    config_init.py does `import meshtastic.mesh_interface` etc. at module
    load time.

    IMPORTANT: server.py/config_init.py get cached in sys.modules on first
    import, bound to whatever meshtastic module was active at that moment.
    tests/test_radio_recovery.py depends on being the FIRST thing in the
    whole suite to import config_init, so its own fake (with a distinct
    MeshInterfaceError it patches against) sticks. This file must not win
    that race by doing a module-level `import server` -- see
    _FreshServerCase below, which pops any cached server/config_init/
    meshtastic-family modules, imports its own fresh copies, and restores
    the prior sys.modules state after each test.
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


_CACHE_KEYS = ("config_init", "server", "radio_link")


class _FreshServerCase(unittest.TestCase):
    """Gives each test its own freshly-imported server/radio_link modules,
    isolated from whatever any other test file cached in sys.modules, and
    restores sys.modules exactly afterward (see _install_fake_meshtastic_package)."""

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
    def __init__(self, is_connected=True, max_text_bytes=220):
        self.is_connected = is_connected
        self.max_text_bytes = max_text_bytes
        self.bbs_nodes = []
        self.allowed_nodes = []
        self.subscriber_nodes = []
        self.sent = []

    def close(self):
        pass

    def sendText(self, text, destinationId, wantAck, wantResponse):
        self.sent.append((text, destinationId))


class RadioLinkTickTests(_FreshServerCase):
    def setUp(self):
        super().setUp()
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".ini", delete=False, encoding="utf-8"
        )
        self._tmp.write(
            "[interface]\ntype = serial\nport = COM1\n\n"
            "[sync]\nbbs_nodes =\n\n[allow_list]\nallowed_nodes =\n"
        )
        self._tmp.close()
        self.config_path = self._tmp.name

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        import os
        os.unlink(self.config_path)
        super().tearDown()

    def _base_triggers(self):
        return {
            'manual': False, 'force_check': False, 'peer_resync_node': None,
            'resolve_zork_save': None, 'resolve_record': None,
        }

    def test_reconnecting_link_skips_work_without_blocking(self):
        """A link with reconnecting=True must return from _run_link_tick
        immediately, doing no sync/send work, and must not touch the (dead)
        interface at all beyond what already set reconnecting=True."""
        link = self.RadioLink('primary', _FakeInterface(is_connected=True))
        link.reconnecting = True
        before_tick = link.last_tick

        self.server._run_link_tick(
            link, system_config={}, config_path=self.config_path,
            triggers=self._base_triggers(), now=before_tick + 100,
        )

        # bump_tick() still runs (diagnostic only), but nothing else did --
        # next_node_sync_check must be untouched (still its initial 0.0).
        self.assertGreaterEqual(link.last_tick, before_tick)
        self.assertEqual(link.next_node_sync_check, 0.0)
        self.assertEqual(link.interface.sent, [])

    def test_dead_interface_triggers_background_reconnect_not_inline_block(self):
        """A dead interface must flip reconnecting=True and hand off to a
        background thread -- _run_link_tick itself must return promptly
        (this test would hang if it were still the old inline blocking
        retry loop)."""
        dead_iface = _FakeInterface(is_connected=False)
        link = self.RadioLink('primary', dead_iface)
        system_config = {'interface_type': 'serial'}

        import time
        start = time.time()
        self.server._run_link_tick(
            link, system_config=system_config, config_path=self.config_path,
            triggers=self._base_triggers(), now=time.time(),
        )
        elapsed = time.time() - start

        self.assertLess(elapsed, 2.0, "reconnect handoff must not block the calling tick")
        self.assertTrue(link.reconnecting)

    def test_none_interface_triggers_background_reconnect_not_inline_block(self):
        """A link that never connected at startup (interface=None -- see
        config_init._open_interface's None-on-exhaustion return and main()'s
        RadioLink construction) must be treated exactly like a dead
        interface: reconnecting flips to True via the background thread
        handoff, not an inline block or a crash trying to use None as an
        interface."""
        link = self.RadioLink('primary', None, reconnect_fn=lambda cfg: None)
        system_config = {'interface_type': 'serial'}

        import time
        start = time.time()
        self.server._run_link_tick(
            link, system_config=system_config, config_path=self.config_path,
            triggers=self._base_triggers(), now=time.time(),
        )
        elapsed = time.time() - start

        self.assertLess(elapsed, 2.0, "reconnect handoff must not block the calling tick")
        self.assertTrue(link.reconnecting)

    def test_is_interface_alive_treats_none_as_not_alive(self):
        self.assertFalse(self.server._is_interface_alive(None))

    def test_healthy_link_keeps_ticking_while_sibling_link_is_reconnecting(self):
        """The scenario dual-radio bridge mode exists to support: one link
        stuck reconnecting must not prevent the OTHER active link's tick
        from completing and advancing its own schedule."""
        stuck_link = self.RadioLink('primary', _FakeInterface(is_connected=False))
        stuck_link.reconnecting = True  # already handed off to a bg thread

        healthy_link = self.RadioLink(
            'secondary', _FakeInterface(is_connected=True),
            sync_section='sync2', allow_section='allow_list2',
            bbs_nodes_key='bbs_nodes2', allowed_nodes_key='allowed_nodes2',
            subscriber_nodes_key='subscriber_nodes2',
        )
        self.server._active_links = [stuck_link, healthy_link]

        system_config = {'interface_type': 'serial'}
        now = 1_000_000.0

        self.server._run_link_tick(
            stuck_link, system_config=system_config, config_path=self.config_path,
            triggers=self._base_triggers(), now=now,
        )
        self.server._run_link_tick(
            healthy_link, system_config=system_config, config_path=self.config_path,
            triggers=self._base_triggers(), now=now,
        )

        # The healthy link ran its full node-sync-check block (it was due:
        # next_node_sync_check starts at 0.0) and rescheduled itself --
        # proof its tick was not starved by the sibling link's outage.
        self.assertEqual(healthy_link.next_node_sync_check, now + 5)
        # The stuck link, by contrast, never got past the early return.
        self.assertEqual(stuck_link.next_node_sync_check, 0.0)


class LinkReconnectRequestTests(_FreshServerCase):
    """Operator-requested per-link reconnect (web admin's Links & Services
    card -> trigger file -> server.apply_link_reconnect_request). The point
    is that it reconnects ONE link in place without restarting anything --
    so the key assertions are that siblings are left alone."""

    def test_named_link_is_flagged_and_siblings_are_untouched(self):
        primary = self.RadioLink('primary', _FakeInterface())
        mqtt1 = self.RadioLink('mqtt1', _FakeInterface())
        links = [primary, mqtt1]

        matched = self.server.apply_link_reconnect_request(links, 'mqtt1')

        self.assertEqual(matched, ['mqtt1'])
        self.assertTrue(mqtt1.reconnect_needed.is_set())
        self.assertFalse(primary.reconnect_needed.is_set())

    def test_all_flags_every_link(self):
        links = [
            self.RadioLink('primary', _FakeInterface()),
            self.RadioLink('secondary', _FakeInterface()),
            self.RadioLink('mqtt1', _FakeInterface()),
        ]

        matched = self.server.apply_link_reconnect_request(links, 'all')

        self.assertEqual(matched, ['primary', 'secondary', 'mqtt1'])
        self.assertTrue(all(link.reconnect_needed.is_set() for link in links))

    def test_unknown_link_flags_nothing(self):
        primary = self.RadioLink('primary', _FakeInterface())
        matched = self.server.apply_link_reconnect_request([primary], 'nope')
        self.assertEqual(matched, [])
        self.assertFalse(primary.reconnect_needed.is_set())

    def test_blank_target_flags_nothing(self):
        primary = self.RadioLink('primary', _FakeInterface())
        self.assertEqual(self.server.apply_link_reconnect_request([primary], '   '), [])
        self.assertFalse(primary.reconnect_needed.is_set())

    def test_link_that_never_connected_can_still_be_reconnected(self):
        """interface=None (gave up at startup) must still accept a manual
        reconnect request -- that's exactly when an operator would use it."""
        dead = self.RadioLink('secondary', None)
        matched = self.server.apply_link_reconnect_request([dead], 'secondary')
        self.assertEqual(matched, ['secondary'])
        self.assertTrue(dead.reconnect_needed.is_set())


class ReloadLinksFromConfigTests(_FreshServerCase):
    """Applying MQTT config changes to a RUNNING service. `links` is built
    once in main(), so without this a newly-added [mqttN] simply wouldn't
    exist and an edited one would be rebuilt from stale startup settings."""

    def _link_cfg(self, name, **overrides):
        cfg = {
            'name': name,
            'host': 'broker.example.com',
            'port': 1883,
            'username': None,
            'password': None,
            'tls': False,
            'tls_ca_certs': None,
            'tls_certfile': None,
            'tls_keyfile': None,
            'tls_keyfile_password': None,
            'tls_insecure': False,
            'topic_prefix': f'baconbs/{name}',
            'local_id': f'{name}-node',
            'client_id': None,
            'keepalive': 60,
            'sync_section': f'sync_{name}',
            'allow_section': f'allow_list_{name}',
            'bbs_nodes_key': f'bbs_nodes_{name}',
            'allowed_nodes_key': f'allowed_nodes_{name}',
            'subscriber_nodes_key': f'subscriber_nodes_{name}',
        }
        cfg.update(overrides)
        return cfg

    def _patch_config(self, configured):
        """Stand in for re-reading config.ini."""
        import config_init
        return patch.object(config_init, 'reload_mqtt_links', return_value=configured)

    def test_newly_configured_broker_is_opened_and_added(self):
        """The case that made the restart message necessary: mqtt2 added
        after startup must become a live link without a restart."""
        links = [self.RadioLink('primary', _FakeInterface())]
        new_cfg = self._link_cfg('mqtt2')

        with self._patch_config([new_cfg]), \
             patch.object(self.server, 'get_mqtt_interface_by_name',
                          return_value=_FakeInterface()), \
             patch.object(self.server, 'refresh_peer_lists_from_config'):
            result = self.server.reload_links_from_config(links, {}, 'config.ini')

        self.assertEqual(result['added'], ['mqtt2'])
        self.assertIn('mqtt2', [link.name for link in links])
        self.assertEqual(len(links), 2)  # primary untouched

    def test_removed_broker_is_closed_and_dropped(self):
        closed = {'called': False}

        class _ClosableInterface(_FakeInterface):
            def close(self):
                closed['called'] = True

        mqtt1 = self.RadioLink('mqtt1', _ClosableInterface())
        links = [self.RadioLink('primary', _FakeInterface()), mqtt1]

        with self._patch_config([]):  # no longer in config.ini
            result = self.server.reload_links_from_config(links, {}, 'config.ini')

        self.assertEqual(result['removed'], ['mqtt1'])
        self.assertTrue(closed['called'], "the dropped link's interface must be closed")
        self.assertEqual([link.name for link in links], ['primary'])

    def test_edited_broker_is_flagged_for_reconnect(self):
        mqtt1 = self.RadioLink('mqtt1', _FakeInterface())
        mqtt1.connection_settings = {
            key: self._link_cfg('mqtt1')[key] for key in self.server._MQTT_CONNECTION_KEYS
        }
        links = [mqtt1]
        edited = self._link_cfg('mqtt1', host='new-broker.example.com')

        with self._patch_config([edited]):
            result = self.server.reload_links_from_config(links, {}, 'config.ini')

        self.assertEqual(result['changed'], ['mqtt1'])
        self.assertTrue(mqtt1.reconnect_needed.is_set())

    def test_unchanged_broker_is_not_reconnected(self):
        """A no-op save must not needlessly drop a healthy connection."""
        mqtt1 = self.RadioLink('mqtt1', _FakeInterface())
        cfg = self._link_cfg('mqtt1')
        mqtt1.connection_settings = {
            key: cfg[key] for key in self.server._MQTT_CONNECTION_KEYS
        }
        links = [mqtt1]

        with self._patch_config([cfg]):
            result = self.server.reload_links_from_config(links, {}, 'config.ini')

        self.assertEqual(result['changed'], [])
        self.assertFalse(mqtt1.reconnect_needed.is_set())

    def test_radios_are_never_touched_by_a_reload(self):
        primary = self.RadioLink('primary', _FakeInterface())
        secondary = self.RadioLink('secondary', _FakeInterface())
        links = [primary, secondary]

        with self._patch_config([]):
            result = self.server.reload_links_from_config(links, {}, 'config.ini')

        self.assertEqual(result, {'added': [], 'removed': [], 'changed': []})
        self.assertEqual([link.name for link in links], ['primary', 'secondary'])
        self.assertFalse(primary.reconnect_needed.is_set())

    def test_unreachable_new_broker_is_still_registered_for_retry(self):
        """Broker down at the moment of reload -- the link should still be
        registered so the normal reconnect loop keeps retrying, rather than
        silently vanishing until the next restart."""
        links = []
        with self._patch_config([self._link_cfg('mqtt2')]), \
             patch.object(self.server, 'get_mqtt_interface_by_name', return_value=None), \
             patch.object(self.server, 'refresh_peer_lists_from_config'):
            result = self.server.reload_links_from_config(links, {}, 'config.ini')

        self.assertEqual(result['added'], ['mqtt2'])
        self.assertEqual(len(links), 1)
        self.assertIsNone(links[0].interface)

    def test_links_list_is_mutated_in_place(self):
        """main() and the _active_links global alias the same list object --
        rebinding instead of mutating would leave one of them stale."""
        links = []
        original_id = id(links)
        with self._patch_config([self._link_cfg('mqtt2')]), \
             patch.object(self.server, 'get_mqtt_interface_by_name',
                          return_value=_FakeInterface()), \
             patch.object(self.server, 'refresh_peer_lists_from_config'):
            self.server.reload_links_from_config(links, {}, 'config.ini')
        self.assertEqual(id(links), original_id)
        self.assertEqual(len(links), 1)


class IsLinkStillConfiguredTests(_FreshServerCase):
    """_reconnect_link must distinguish "still configured, just failed to
    connect again" (keep retrying) from "removed/disabled in config.ini
    since startup" (abandon reconnect) -- see server._is_link_still_configured
    and its docstring for why conflating these two None-return reasons was
    a real bug (both for the primary/secondary radio fix and, pre-existing,
    for MQTT links whose broker was down long enough to exhaust
    _open_mqtt_interface's bounded retry)."""

    def test_primary_is_always_configured(self):
        link = self.RadioLink('primary', None)
        self.assertTrue(self.server._is_link_still_configured(link, {}))

    def test_secondary_configured_iff_interface2_enabled(self):
        link = self.RadioLink('secondary', None)
        self.assertTrue(
            self.server._is_link_still_configured(link, {'interface2_enabled': True})
        )
        self.assertFalse(
            self.server._is_link_still_configured(link, {'interface2_enabled': False})
        )
        self.assertFalse(self.server._is_link_still_configured(link, {}))

    def test_mqtt_link_configured_iff_still_in_mqtt_links(self):
        link = self.RadioLink('mqtt1', None)
        self.assertTrue(self.server._is_link_still_configured(
            link, {'mqtt_links': [{'name': 'mqtt1'}, {'name': 'mqtt2'}]}
        ))
        self.assertFalse(self.server._is_link_still_configured(
            link, {'mqtt_links': [{'name': 'mqtt2'}]}
        ))
        self.assertFalse(self.server._is_link_still_configured(link, {}))


if __name__ == "__main__":
    unittest.main()
