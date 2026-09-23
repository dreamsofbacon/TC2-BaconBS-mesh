"""A node that gains [fleet] after startup still applies its target.

Found on the new VPS node. Its config was written hours after the service
started, and MQTT links pick config up on the fly, so it connected to the
fleet, verified a signed target and stored it -- and then never moved. The
receive path reads [fleet] fresh; the applier read the startup snapshot,
where updates were off, and returned before writing anything at all. There
was nothing in the log to explain a node that was simply one version behind
for a day.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radio_stubs

# The shared stubs, not a private copy: installing is additive, so every
# importer keeps the same module object whatever the test order.
radio_stubs.install()
import server  # noqa: E402  -- needs the stub package above

FLEET_INI = """[fleet]
group = baconbbsvt
updates = auto
trusted_keys = fkec622a:0hGvExa6i9yRn-kdbW4Kn6FHMfurPdmYeTCoud4vbuc
"""


class _Fleet(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp()
        self.config_path = os.path.join(folder, "config.ini")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(FLEET_INI)
        patcher = mock.patch.dict(os.environ, {"BBS_CONFIG_PATH": self.config_path})
        patcher.start()
        self.addCleanup(patcher.stop)


class ReadingTheLiveConfigTests(_Fleet):
    def test_a_fleet_section_added_after_startup_is_seen(self):
        settings = server._current_fleet_settings({})
        self.assertEqual("baconbbsvt", settings.get("group"))
        self.assertEqual("auto", settings.get("updates"))

    def test_the_file_wins_over_the_startup_snapshot(self):
        stale = {"fleet": {"group": "baconbbsvt", "updates": "off"}}
        self.assertEqual("auto",
                         server._current_fleet_settings(stale).get("updates"))

    def test_an_unreadable_config_falls_back_to_the_snapshot(self):
        """A half-written file during an edit must not switch updates off."""
        snapshot = {"fleet": {"group": "baconbbsvt", "updates": "auto"}}
        with mock.patch("configparser.ConfigParser.read",
                        side_effect=OSError("torn read")):
            self.assertEqual("auto",
                             server._current_fleet_settings(snapshot).get("updates"))


class ApplyingWithAStaleSnapshotTests(_Fleet):
    def test_the_stored_target_is_applied(self):
        """The whole point: startup config had no [fleet] at all."""
        target = {"commit": "680b5bed3015e6bc75e4ef8ef19292c7a3d630c9",
                  "version": "0.1.700"}
        with mock.patch("db_operations.get_fleet_target", return_value=target), \
                mock.patch("db_operations.mark_fleet_target_applied"), \
                mock.patch("fleet_update.write_update_state"), \
                mock.patch("fleet_update.apply_target",
                           return_value=(True, "switched")) as apply_target:
            self.assertTrue(server._apply_fleet_target_if_due({}))
        apply_target.assert_called_once()
        self.assertEqual(target["commit"], apply_target.call_args[0][0])

    def test_updates_off_in_the_live_config_still_stops_it(self):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(FLEET_INI.replace("updates = auto", "updates = off"))
        with mock.patch("db_operations.get_fleet_target", return_value=None),                 mock.patch("fleet_update.apply_target") as apply_target:
            self.assertFalse(server._apply_fleet_target_if_due(
                {"fleet": {"group": "baconbbsvt", "updates": "auto"}}))
        apply_target.assert_not_called()

    def test_a_pin_in_the_live_config_still_stops_it(self):
        with open(self.config_path, "a", encoding="utf-8") as handle:
            handle.write("pin_commit = abc123\n")
        target = {"commit": "680b5bed3015e6bc75e4ef8ef19292c7a3d630c9",
                  "version": "0.1.700"}
        with mock.patch("db_operations.get_fleet_target", return_value=target), \
                mock.patch("fleet_update.write_update_state") as state, \
                mock.patch("fleet_update.apply_target") as apply_target:
            self.assertFalse(server._apply_fleet_target_if_due({}))
        apply_target.assert_not_called()
        self.assertEqual("pinned", state.call_args[0][0]["state"])


class AHeldTargetIsNeverSilentTests(_Fleet):
    """The deploy-visible half of the same failure.

    A node that has accepted a target and is not acting on it used to return
    without a word: no log line, and an update_state.json that still
    described the previous deploy. From the outside that is indistinguishable
    from a node that never heard the instruction, which is how a deploy
    reported success while one node stood still.
    """

    TARGET = {"commit": "680b5bed3015e6bc75e4ef8ef19292c7a3d630c9",
              "version": "0.1.700"}

    def setUp(self):
        super().setUp()
        server._reset_fleet_hold_state()
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(FLEET_INI.replace("updates = auto", "updates = off"))

    def _run(self):
        writes = []
        with mock.patch("db_operations.get_fleet_target", return_value=self.TARGET),                 mock.patch("fleet_update.current_commit", return_value="eb93ff4a6657"),                 mock.patch("fleet_update.write_update_state",
                           side_effect=writes.append),                 mock.patch("fleet_update.apply_target") as apply_target:
            applied = server._apply_fleet_target_if_due({})
        return applied, writes, apply_target

    def test_the_held_target_is_written_to_the_status_file(self):
        applied, writes, apply_target = self._run()
        self.assertFalse(applied)
        apply_target.assert_not_called()
        self.assertEqual(1, len(writes))
        self.assertEqual("held", writes[0]["state"])
        self.assertEqual(self.TARGET["commit"], writes[0]["target_commit"])
        self.assertEqual("0.1.700", writes[0]["target_version"])
        self.assertIn("updates", writes[0]["detail"])

    def test_it_is_logged_once_not_on_every_poll(self):
        with self.assertLogs("root", level="WARNING") as logged:
            self._run()
        self.assertTrue(any("NOT applied" in line for line in logged.output))
        with mock.patch("db_operations.get_fleet_target", return_value=self.TARGET),                 mock.patch("fleet_update.current_commit", return_value="eb93ff4a6657"),                 mock.patch("fleet_update.write_update_state"),                 mock.patch.object(server.logging, "warning") as warn:
            server._apply_fleet_target_if_due({})
        warn.assert_not_called()

    def test_a_node_already_on_the_target_is_not_reported_as_held(self):
        """Updates switched off after the node arrived is not a problem."""
        writes = []
        with mock.patch("db_operations.get_fleet_target", return_value=self.TARGET),                 mock.patch("fleet_update.current_commit",
                           return_value=self.TARGET["commit"]),                 mock.patch("fleet_update.write_update_state",
                           side_effect=writes.append):
            server._apply_fleet_target_if_due({})
        self.assertEqual([], writes)

    def test_reporting_failures_do_not_stop_the_check(self):
        with mock.patch("db_operations.get_fleet_target",
                        side_effect=RuntimeError("database is locked")):
            self.assertFalse(server._apply_fleet_target_if_due({}))


if __name__ == "__main__":
    unittest.main()
