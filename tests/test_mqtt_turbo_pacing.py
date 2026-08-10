"""Tests for utils.py's per-interface turbo-pacing override (_effective_turbo
and the five pacing getters it feeds). The property under test: a node
running a normal-pacing LoRa radio AND a low-latency MQTT link at the same
time must be able to pace each independently -- the global [sync] sync_turbo
flag alone cannot express "radio at normal pace, MQTT at turbo pace
simultaneously".
"""
import types
import unittest
from unittest.mock import patch

import utils


class _FakeLowLatencyInterface:
    is_low_latency = True


class _FakeNormalInterface:
    pass  # no is_low_latency attribute at all -- must default to False


class EffectiveTurboTests(unittest.TestCase):
    def test_no_interface_falls_through_to_global_flag(self):
        with patch.object(utils, "_is_sync_turbo_enabled", return_value=False):
            self.assertFalse(utils._effective_turbo(None))
        with patch.object(utils, "_is_sync_turbo_enabled", return_value=True):
            self.assertTrue(utils._effective_turbo(None))

    def test_low_latency_interface_forces_turbo_regardless_of_global_flag(self):
        with patch.object(utils, "_is_sync_turbo_enabled", return_value=False):
            self.assertTrue(utils._effective_turbo(_FakeLowLatencyInterface()))

    def test_normal_interface_without_is_low_latency_uses_global_flag(self):
        with patch.object(utils, "_is_sync_turbo_enabled", return_value=False):
            self.assertFalse(utils._effective_turbo(_FakeNormalInterface()))


class PacingGetterOverrideTests(unittest.TestCase):
    """Each getter must independently honor per-call turbo, with no bleed
    between two simultaneous calls for different interfaces -- exactly the
    mixed LoRa+MQTT bridge scenario."""

    def setUp(self):
        # Isolate from any real config.ini / env vars on the test machine.
        self.config_patch = patch.object(utils, "_load_runtime_config", return_value=_empty_config())
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.env_patch = patch.dict("os.environ", {}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for var in (
            "BBS_SYNC_TURBO", "BBS_SYNC_PAUSE_SECONDS", "BBS_HASH_REPAIR_PAUSE_SECONDS",
            "BBS_FULL_SYNC_DELAY_MS", "BBS_REPAIR_CYCLE_SECONDS", "BBS_RECONCILE_MAX_PER_PASS",
        ):
            import os
            os.environ.pop(var, None)

    def test_sync_pause_seconds_mixed_radio_and_mqtt_simultaneously(self):
        radio = _FakeNormalInterface()
        mqtt = _FakeLowLatencyInterface()
        self.assertEqual(utils.get_sync_pause_seconds(radio), 0.75)   # normal LoRa pacing
        self.assertEqual(utils.get_sync_pause_seconds(mqtt), 0.02)    # turbo, same process, same moment
        self.assertEqual(utils.get_sync_pause_seconds(radio), 0.75)   # radio pacing unaffected by the MQTT call

    def test_hash_repair_pause_seconds_respects_low_latency(self):
        self.assertEqual(utils.get_hash_repair_pause_seconds(_FakeNormalInterface()), 0.1)
        self.assertEqual(utils.get_hash_repair_pause_seconds(_FakeLowLatencyInterface()), 0.0)

    def test_full_sync_delay_ms_respects_low_latency(self):
        self.assertEqual(utils.get_full_sync_delay_ms(_FakeNormalInterface()), 500)
        self.assertEqual(utils.get_full_sync_delay_ms(_FakeLowLatencyInterface()), 0)

    def test_repair_cycle_seconds_respects_low_latency(self):
        self.assertEqual(utils.get_repair_cycle_seconds(_FakeNormalInterface()), 90)
        self.assertEqual(utils.get_repair_cycle_seconds(_FakeLowLatencyInterface()), 15)

    def test_reconcile_max_per_pass_respects_low_latency(self):
        self.assertEqual(utils.get_reconcile_max_per_pass(_FakeNormalInterface()), 20)
        self.assertEqual(utils.get_reconcile_max_per_pass(_FakeLowLatencyInterface()), 100)

    def test_global_sync_turbo_true_does_not_downgrade_a_normal_interface(self):
        """A global sync_turbo=true (e.g. left on from a prior small-mesh
        tuning pass) still applies to a plain interface with no
        is_low_latency attribute -- this override only ever ADDS turbo
        pacing for low-latency links, never removes it elsewhere."""
        with patch.object(utils, "_is_sync_turbo_enabled", return_value=True):
            self.assertEqual(utils.get_sync_pause_seconds(_FakeNormalInterface()), 0.02)

    def test_global_diagnostics_snapshot_has_no_interface_context(self):
        """get_sync_runtime_settings() has no interface parameter -- it must
        keep reporting the un-overridden global settings without raising."""
        settings = utils.get_sync_runtime_settings()
        self.assertIn("sync_pause_seconds", settings)
        self.assertEqual(settings["sync_pause_seconds"], 0.75)


def _empty_config():
    import configparser
    cfg = configparser.ConfigParser()
    return cfg


if __name__ == "__main__":
    unittest.main()
