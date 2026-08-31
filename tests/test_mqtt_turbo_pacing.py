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

    def test_hash_chunk_pause_seconds_respects_low_latency(self):
        # The LoRa collision-safety floor (1.5s) must be unchanged for a
        # normal/radio interface; a low-latency transport like MQTT has none
        # of the half-duplex constraints that floor exists for.
        self.assertEqual(utils.get_hash_chunk_pause_seconds(_FakeNormalInterface()), 1.5)
        self.assertEqual(utils.get_hash_chunk_pause_seconds(_FakeLowLatencyInterface()), 0.0)

    def test_hash_chunk_pause_seconds_env_override_applies_regardless_of_interface(self):
        with patch.dict("os.environ", {"BBS_HASH_CHUNK_PAUSE_SECONDS": "0.3"}):
            self.assertEqual(utils.get_hash_chunk_pause_seconds(_FakeNormalInterface()), 0.3)
            self.assertEqual(utils.get_hash_chunk_pause_seconds(_FakeLowLatencyInterface()), 0.3)

    def test_radio_config_values_do_not_throttle_low_latency_interface(self):
        cfg = _radio_paced_config()
        with patch.object(utils, "_load_runtime_config", return_value=cfg):
            radio = _FakeNormalInterface()
            mqtt = _FakeLowLatencyInterface()
            self.assertEqual(utils.get_sync_pause_seconds(radio), 0.75)
            self.assertEqual(utils.get_sync_pause_seconds(mqtt), 0.02)
            self.assertEqual(utils.get_hash_repair_pause_seconds(radio), 1.0)
            self.assertEqual(utils.get_hash_repair_pause_seconds(mqtt), 0.0)
            self.assertEqual(utils.get_hash_chunk_pause_seconds(radio), 3.0)
            self.assertEqual(utils.get_hash_chunk_pause_seconds(mqtt), 0.0)
            self.assertEqual(utils.get_full_sync_delay_ms(radio), 100)
            self.assertEqual(utils.get_full_sync_delay_ms(mqtt), 0)
            self.assertEqual(utils.get_repair_cycle_seconds(radio), 120)
            self.assertEqual(utils.get_repair_cycle_seconds(mqtt), 15)
            self.assertEqual(utils.get_reconcile_max_per_pass(radio), 20)
            self.assertEqual(utils.get_reconcile_max_per_pass(mqtt), 100)

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


def _radio_paced_config():
    cfg = _empty_config()
    cfg["sync"] = {
        "sync_pause_seconds": "0.75",
        "hash_repair_pause_seconds": "1.0",
        "hash_chunk_pause_seconds": "3.0",
        "full_sync_delay_ms": "100",
        "repair_cycle_seconds": "120",
        "reconcile_max_per_pass": "20",
    }
    return cfg


if __name__ == "__main__":
    unittest.main()


class SyncIntervalIsPerTransportTests(unittest.TestCase):
    """Everything INSIDE a sync was already transport-aware, but the schedule
    was not: one global [sync] sync_interval_minutes applied to every link,
    so an MQTT bridge sat idle for a radio's five or ten minutes before it
    even looked. That wait was almost all of its sync latency.
    """

    def setUp(self):
        import os
        self._saved = os.environ.pop("BBS_LOW_LATENCY_SYNC_INTERVAL_SECONDS", None)

    def tearDown(self):
        import os
        os.environ.pop("BBS_LOW_LATENCY_SYNC_INTERVAL_SECONDS", None)
        if self._saved is not None:
            os.environ["BBS_LOW_LATENCY_SYNC_INTERVAL_SECONDS"] = self._saved

    def test_a_radio_keeps_the_configured_interval_exactly(self):
        with patch.object(utils, "_is_sync_turbo_enabled", return_value=False):
            self.assertEqual(
                utils.get_sync_interval_seconds(_FakeNormalInterface(), 5), 300.0)
            self.assertEqual(
                utils.get_sync_interval_seconds(_FakeNormalInterface(), 10), 600.0)

    def test_a_low_latency_link_checks_far_more_often(self):
        with patch.object(utils, "_is_sync_turbo_enabled", return_value=False):
            interval = utils.get_sync_interval_seconds(_FakeLowLatencyInterface(), 10)
        self.assertEqual(interval, 30.0)

    def test_the_fast_interval_is_tunable(self):
        import os
        os.environ["BBS_LOW_LATENCY_SYNC_INTERVAL_SECONDS"] = "5"
        with patch.object(utils, "_is_sync_turbo_enabled", return_value=False):
            self.assertEqual(
                utils.get_sync_interval_seconds(_FakeLowLatencyInterface(), 10), 5.0)

    def test_it_never_ends_up_slower_than_the_configured_interval(self):
        """Raising sync_interval_minutes should slow every link down, not
        leave the fast ones running at a hidden faster default."""
        with patch.object(utils, "_is_sync_turbo_enabled", return_value=False):
            # Configured interval of 10 seconds is shorter than the 30s default.
            interval = utils.get_sync_interval_seconds(_FakeLowLatencyInterface(), 10 / 60.0)
        self.assertEqual(interval, 10.0)

    def test_a_turbo_radio_keeps_the_slow_interval(self):
        """sync_turbo speeds LoRa frame pacing; it does not remove airtime
        limits. A radio checking every 30s would flood a busy mesh -- and
        turbo nodes are the busy ones, so this is where it would hurt most."""
        with patch.object(utils, "_is_sync_turbo_enabled", return_value=True):
            interval = utils.get_sync_interval_seconds(_FakeNormalInterface(), 5)
        self.assertEqual(interval, 300.0)
