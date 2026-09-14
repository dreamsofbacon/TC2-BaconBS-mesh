"""Each attached radio's clock is set from this node's.

bbs.local's Meshtastic radio came back on 2026-09-12 about 48 days slow and
nothing corrected it: Public Chatter from that radio stopped, and every "last
heard" time it reported was seven weeks stale.
"""
import asyncio
import io
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import radio_clock
import utils

NOW = radio_clock.MIN_PLAUSIBLE_EPOCH + 86400 * 3


class _MeshtasticRadio:
    protocol_name = "Meshtastic"

    def __init__(self, fail=False):
        self.set_to = []
        self.fail = fail
        self.localNode = SimpleNamespace(setTime=self._set)

    def _set(self, epoch):
        if self.fail:
            raise OSError("serial write failed")
        self.set_to.append(epoch)


def _config(**values):
    return mock.patch.object(
        utils, "_config_raw",
        lambda section, option: values.get(option) if section == "radio_clock" else None)


class MaintainTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(radio_clock.time, "time", return_value=float(NOW)),
            mock.patch.object(radio_clock, "_ntp_synchronized", return_value=True),
            _config(),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_new_connection_is_set_straight_away(self):
        radio = _MeshtasticRadio()
        radio_clock.maintain(radio, "primary", now_monotonic=100.0)
        self.assertEqual(radio.set_to, [NOW])

    def test_then_not_again_until_the_interval(self):
        radio = _MeshtasticRadio()
        radio_clock.maintain(radio, "primary", now_monotonic=100.0)
        radio_clock.maintain(radio, "primary", now_monotonic=100.0 + 6 * 3600 - 1)
        self.assertEqual(len(radio.set_to), 1)
        radio_clock.maintain(radio, "primary", now_monotonic=100.0 + 6 * 3600)
        self.assertEqual(len(radio.set_to), 2)

    def test_the_interval_is_configurable(self):
        radio = _MeshtasticRadio()
        with _config(interval_hours="1"):
            radio_clock.maintain(radio, "primary", now_monotonic=0.0)
            radio_clock.maintain(radio, "primary", now_monotonic=3600.0)
        self.assertEqual(len(radio.set_to), 2)

    def test_a_reconnect_is_a_new_interface_and_is_set_at_once(self):
        """A reconnect is usually a radio that rebooted -- the moment its
        clock is most likely wrong."""
        first, second = _MeshtasticRadio(), _MeshtasticRadio()
        radio_clock.maintain(first, "primary", now_monotonic=100.0)
        radio_clock.maintain(second, "primary", now_monotonic=101.0)
        self.assertEqual(second.set_to, [NOW])

    def test_mqtt_links_are_skipped(self):
        link = SimpleNamespace(protocol_name="MQTT", localNode=SimpleNamespace(
            setTime=mock.Mock()))
        radio_clock.maintain(link, "mqtt1", now_monotonic=0.0)
        link.localNode.setTime.assert_not_called()

    def test_it_can_be_turned_off(self):
        radio = _MeshtasticRadio()
        with _config(enabled="false"):
            radio_clock.maintain(radio, "primary", now_monotonic=0.0)
        self.assertEqual(radio.set_to, [])

    def test_an_unsynced_node_does_not_push_its_wrong_clock(self):
        """A Pi has no real-time clock. Before NTP it runs on the time saved
        at the last shutdown, and copying that in helps nothing."""
        radio = _MeshtasticRadio()
        with mock.patch.object(radio_clock, "_ntp_synchronized", return_value=False), \
                self.assertLogs(level="WARNING") as logs:
            radio_clock.maintain(radio, "primary", now_monotonic=0.0)
            radio_clock.maintain(radio, "primary", now_monotonic=60.0)
        self.assertEqual(radio.set_to, [])
        self.assertEqual(len(logs.output), 1, "told once, not every minute")
        self.assertIn("NTP", logs.output[0])

    def test_it_retries_a_minute_later_and_sets_once_synced(self):
        radio = _MeshtasticRadio()
        with mock.patch.object(radio_clock, "_ntp_synchronized", return_value=False), \
                self.assertLogs(level="WARNING"):
            radio_clock.maintain(radio, "primary", now_monotonic=0.0)
        radio_clock.maintain(radio, "primary", now_monotonic=59.0)
        self.assertEqual(radio.set_to, [])
        radio_clock.maintain(radio, "primary", now_monotonic=60.0)
        self.assertEqual(radio.set_to, [NOW])

    def test_a_clock_earlier_than_any_real_date_is_never_trusted(self):
        radio = _MeshtasticRadio()
        with mock.patch.object(radio_clock.time, "time", return_value=1_000_000.0), \
                mock.patch.object(radio_clock, "_ntp_synchronized", return_value=None), \
                self.assertLogs(level="WARNING"):
            radio_clock.maintain(radio, "primary", now_monotonic=0.0)
        self.assertEqual(radio.set_to, [])

    def test_without_systemd_a_plausible_clock_is_used(self):
        radio = _MeshtasticRadio()
        with mock.patch.object(radio_clock, "_ntp_synchronized", return_value=None):
            radio_clock.maintain(radio, "primary", now_monotonic=0.0)
        self.assertEqual(radio.set_to, [NOW])

    def test_a_failure_is_logged_backs_off_and_never_raises(self):
        radio = _MeshtasticRadio(fail=True)
        with self.assertLogs(level="WARNING") as logs:
            radio_clock.maintain(radio, "primary", now_monotonic=0.0)
        self.assertIn("serial write failed", logs.output[0])
        radio.fail = False
        radio_clock.maintain(radio, "primary", now_monotonic=599.0)
        self.assertEqual(radio.set_to, [])
        radio_clock.maintain(radio, "primary", now_monotonic=600.0)
        self.assertEqual(radio.set_to, [NOW])

    def test_a_meshtastic_set_holds_the_send_lock(self):
        radio = _MeshtasticRadio()
        held = []
        lock = utils.interface_send_lock(radio)

        def check(epoch):
            held.append(lock.locked())
        radio.localNode = SimpleNamespace(setTime=check)
        radio_clock.maintain(radio, "primary", now_monotonic=0.0)
        self.assertEqual(held, [True])

    def test_an_interface_that_cannot_set_time_does_not_raise(self):
        with self.assertLogs(level="WARNING"):
            radio_clock.maintain(SimpleNamespace(protocol_name="Meshtastic"), "primary",
                                 now_monotonic=0.0)

    def test_nothing_attached_is_fine(self):
        radio_clock.maintain(None, "primary", now_monotonic=0.0)


class NtpCheckTests(unittest.TestCase):
    def setUp(self):
        radio_clock._ntp_cache.update(checked_at=None, value=None)
        self.addCleanup(radio_clock._ntp_cache.update, checked_at=None, value=None)

    def _run(self, stdout, returncode=0):
        return mock.patch.object(
            radio_clock.subprocess, "run",
            return_value=SimpleNamespace(stdout=stdout, returncode=returncode))

    def test_yes_and_no(self):
        with mock.patch.object(radio_clock.shutil, "which", return_value="/usr/bin/timedatectl"):
            with self._run("yes\n"):
                self.assertIs(radio_clock._ntp_synchronized(), True)
            radio_clock._ntp_cache.update(checked_at=None)
            with self._run("no\n"):
                self.assertIs(radio_clock._ntp_synchronized(), False)

    def test_no_timedatectl_means_unknown(self):
        with mock.patch.object(radio_clock.shutil, "which", return_value=None):
            self.assertIsNone(radio_clock._ntp_synchronized())

    def test_it_is_not_asked_on_every_tick(self):
        with mock.patch.object(radio_clock.shutil, "which", return_value="/usr/bin/timedatectl"), \
                self._run("no\n") as run:
            for _ in range(5):
                radio_clock._ntp_synchronized()
        self.assertEqual(run.call_count, 1)


class _Commands:
    def __init__(self, radio_time, set_ok=True):
        from meshcore_interface import EventType
        self.EventType = EventType
        self.radio_time = radio_time
        self.set_ok = set_ok
        self.set_calls = []

    async def get_time(self):
        return SimpleNamespace(type=self.EventType.CURRENT_TIME, payload={"time": self.radio_time})

    async def set_time(self, value):
        self.set_calls.append(value)
        if self.set_ok:
            self.radio_time = value
            return SimpleNamespace(type=self.EventType.OK, payload={})
        return SimpleNamespace(type=self.EventType.ERROR, payload={})


class MeshCoreTimeTests(unittest.TestCase):
    """Driven through the real MeshCoreInterface method on its own loop."""

    def setUp(self):
        import meshcore_interface
        self.module = meshcore_interface
        self.iface = meshcore_interface.MeshCoreInterface.__new__(meshcore_interface.MeshCoreInterface)
        self.iface._loop = asyncio.new_event_loop()
        import threading
        self.thread = threading.Thread(target=self.iface._loop.run_forever, daemon=True)
        self.thread.start()
        self.iface._send_lock = None

        async def make_lock():
            return asyncio.Lock()
        self.iface._send_lock = asyncio.run_coroutine_threadsafe(
            make_lock(), self.iface._loop).result(2)
        self.addCleanup(self._stop)

    def _stop(self):
        self.iface._loop.call_soon_threadsafe(self.iface._loop.stop)
        self.thread.join(2)
        self.iface._loop.close()

    def _core(self, commands):
        self.iface._meshcore = SimpleNamespace(commands=commands)

    def test_a_slow_radio_is_set(self):
        commands = _Commands(radio_time=NOW - 48 * 86400)
        self._core(commands)
        outcome = self.iface.sync_device_time(NOW)
        self.assertEqual(commands.set_calls, [NOW])
        self.assertIn("set", outcome)
        self.assertIn(str(-48 * 86400), outcome)

    def test_a_correct_radio_is_left_alone(self):
        commands = _Commands(radio_time=NOW - 12)
        self._core(commands)
        self.assertEqual(self.iface.sync_device_time(NOW), "already correct")
        self.assertEqual(commands.set_calls, [])

    def test_a_radio_ahead_that_refuses_says_why(self):
        commands = _Commands(radio_time=NOW + 3600, set_ok=False)
        self._core(commands)
        with self.assertRaisesRegex(IOError, "backwards"):
            self.iface.sync_device_time(NOW)

    def test_an_unconfirmed_set_raises(self):
        commands = _Commands(radio_time=NOW - 3600, set_ok=False)
        self._core(commands)
        with self.assertRaisesRegex(IOError, "did not confirm"):
            self.iface.sync_device_time(NOW)

    def test_a_disconnected_radio_raises(self):
        self.iface._meshcore = None
        with self.assertRaises(ConnectionError):
            self.iface.sync_device_time(NOW)

    def test_maintain_uses_it(self):
        commands = _Commands(radio_time=NOW - 48 * 86400)
        self._core(commands)
        self.iface.protocol_name = "MeshCore"
        with mock.patch.object(radio_clock.time, "time", return_value=float(NOW)), \
                mock.patch.object(radio_clock, "_ntp_synchronized", return_value=True), \
                _config():
            radio_clock.maintain(self.iface, "secondary", now_monotonic=0.0)
        self.assertEqual(commands.set_calls, [NOW])


class WiringTests(unittest.TestCase):
    def test_every_link_tick_maintains_its_radio_clock(self):
        # Read, not imported: server pulls in the real meshtastic package,
        # which this file stubs out for the modules it does import.
        import ast
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "server.py")
        text = io.open(path, encoding="utf-8").read()
        node = next(n for n in ast.parse(text).body
                    if isinstance(n, ast.FunctionDef) and n.name == "_run_link_tick")
        source = ast.get_source_segment(text, node)
        self.assertIn("radio_clock.maintain(interface, link.name", source)
        # After the reconnect hand-off, so a link mid-reconnect is not poked,
        # and before any sync work.
        self.assertLess(source.index("link.reconnect_needed.is_set()"),
                        source.index("radio_clock.maintain("))
        self.assertLess(source.index("radio_clock.maintain("),
                        source.index("process_pending_candidate_resolutions(interface)"))


if __name__ == "__main__":
    unittest.main()
