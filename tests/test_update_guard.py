"""The rollback guard is what stops a bad version bricking a node.

It runs at import, before any project module, because when the new code
cannot load the thing that has to notice is the thing that already ran. These
tests exercise the states it can find itself in, including the ones where it
must do nothing at all -- a guard that fires when no update is in flight is
worse than no guard.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import update_guard

TARGET = "b" * 40
PREVIOUS = "a" * 40


class _StateFile:
    """Points the guard at a scratch state file for the duration of a test."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="guard-test-")
        self.path = os.path.join(self.dir, "update_state.json")
        self.patch = mock.patch.dict(
            os.environ, {"BBS_UPDATE_STATE_PATH": self.path})

    def __enter__(self):
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()
        return False

    def write(self, state):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except OSError:
            return None

    def probation(self, attempts=0):
        self.write({"state": "probation", "target_commit": TARGET,
                    "previous_commit": PREVIOUS, "attempts": attempts})


class QuietWhenNothingIsHappeningTests(unittest.TestCase):
    """Almost every start. The guard must be invisible."""

    def test_no_state_file_does_nothing(self):
        with _StateFile() as state:
            with mock.patch.object(update_guard, "_revert",
                                   side_effect=AssertionError("reverted!")):
                update_guard.check()
            self.assertIsNone(state.read())

    def test_a_confirmed_state_does_nothing(self):
        with _StateFile() as state:
            state.write({"state": "confirmed", "commit": TARGET})
            with mock.patch.object(update_guard, "_revert",
                                   side_effect=AssertionError("reverted!")):
                update_guard.check()
            self.assertEqual(state.read()["state"], "confirmed")

    def test_unreadable_state_does_not_stop_the_node_booting(self):
        """Refusing to start over a corrupt file would be a self-inflicted
        outage."""
        with _StateFile() as state:
            with open(state.path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            update_guard.check()  # must not raise


class ProbationCountingTests(unittest.TestCase):
    def test_the_first_start_is_counted_not_reverted(self):
        with _StateFile() as state:
            state.probation()
            with mock.patch.object(update_guard, "_revert",
                                   side_effect=AssertionError("reverted too soon")):
                update_guard.check()
            self.assertEqual(state.read()["attempts"], 1)
            self.assertEqual(state.read()["state"], "probation")

    def test_attempts_accumulate_across_restarts(self):
        with _StateFile() as state:
            state.probation()
            with mock.patch.object(update_guard, "_revert", return_value=True):
                for expected in (1, 2, 3):
                    update_guard.check()
                    self.assertEqual(state.read()["attempts"], expected)

    def test_a_transient_failure_within_budget_does_not_revert(self):
        """Two bad starts can just be a busy boot -- a port not yet free, a
        serial device still settling."""
        with _StateFile() as state:
            state.probation(attempts=1)
            with mock.patch.object(update_guard, "_revert",
                                   side_effect=AssertionError("reverted too soon")):
                update_guard.check()


class RollbackTests(unittest.TestCase):
    def test_it_reverts_after_the_attempt_budget_is_spent(self):
        with _StateFile() as state:
            state.probation(attempts=update_guard.MAX_ATTEMPTS)
            with mock.patch.object(update_guard, "_revert", return_value=True) as revert, \
                 mock.patch.object(os, "_exit", side_effect=SystemExit(1)):
                with self.assertRaises(SystemExit):
                    update_guard.check()
            revert.assert_called_once_with(PREVIOUS)
            self.assertEqual(state.read()["state"], "rolled_back")
            self.assertEqual(state.read()["failed_commit"], TARGET)

    def test_it_exits_so_systemd_restarts_on_the_old_code(self):
        """Reverting the working tree is only half the job: the running
        process is still the broken version until it exits."""
        with _StateFile() as state:
            state.probation(attempts=update_guard.MAX_ATTEMPTS)
            with mock.patch.object(update_guard, "_revert", return_value=True), \
                 mock.patch.object(os, "_exit", side_effect=SystemExit(1)) as exiter:
                with self.assertRaises(SystemExit):
                    update_guard.check()
            exiter.assert_called_once()
            self.assertNotEqual(exiter.call_args.args[0], 0)

    def test_a_failed_revert_keeps_running_rather_than_looping(self):
        """If git itself fails there is nothing better to do than carry on and
        say so; exiting would just spin the service."""
        with _StateFile() as state:
            state.probation(attempts=update_guard.MAX_ATTEMPTS)
            with mock.patch.object(update_guard, "_revert", return_value=False), \
                 mock.patch.object(os, "_exit", side_effect=AssertionError("exited")):
                update_guard.check()
            self.assertEqual(state.read()["state"], "rollback_failed")

    def test_probation_with_no_previous_commit_clears_itself(self):
        """Otherwise it would re-trigger on every single start forever."""
        with _StateFile() as state:
            state.write({"state": "probation", "target_commit": TARGET,
                         "previous_commit": "",
                         "attempts": update_guard.MAX_ATTEMPTS})
            with mock.patch.object(os, "_exit", side_effect=AssertionError("exited")):
                update_guard.check()
            self.assertIsNone(state.read())


class ConfirmationTests(unittest.TestCase):
    def test_confirming_ends_probation(self):
        with _StateFile() as state:
            state.probation(attempts=1)
            self.assertTrue(update_guard.confirm_healthy())
            self.assertEqual(state.read()["state"], "confirmed")

    def test_confirming_when_not_on_probation_is_a_no_op(self):
        with _StateFile() as state:
            state.write({"state": "confirmed", "commit": TARGET})
            self.assertFalse(update_guard.confirm_healthy())

    def test_after_confirming_restarts_are_no_longer_counted(self):
        """The budget must not carry over into normal operation."""
        with _StateFile() as state:
            state.probation(attempts=2)
            update_guard.confirm_healthy()
            with mock.patch.object(update_guard, "_revert",
                                   side_effect=AssertionError("reverted!")):
                update_guard.check()
            self.assertEqual(state.read()["state"], "confirmed")


class IsolationTests(unittest.TestCase):
    def test_the_guard_imports_nothing_from_the_project(self):
        """It has to run when the rest of the new code cannot import. A
        project import here would defeat the entire mechanism."""
        source = (Path(__file__).resolve().parent.parent
                  / "update_guard.py").read_text(encoding="utf-8")
        project_modules = ("db_operations", "utils", "config_init", "server",
                           "web_admin", "message_processing", "fleet_update")
        for module in project_modules:
            self.assertNotIn(f"import {module}", source)

    def test_it_only_uses_the_standard_library(self):
        stdlib_only = {"json", "os", "subprocess", "sys", "pathlib"}
        source = (Path(__file__).resolve().parent.parent
                  / "update_guard.py").read_text(encoding="utf-8")
        imported = set()
        for line in source.splitlines():
            line = line.strip()
            if line.startswith("import "):
                imported.add(line.split()[1].split(".")[0])
            elif line.startswith("from ") and " import " in line:
                imported.add(line.split()[1].split(".")[0])
        self.assertEqual(imported - stdlib_only, set())


if __name__ == "__main__":
    unittest.main()
