"""Two ways a deploy used to report success and change nothing.

**The group.** An instruction carries the fleet it is for, and a node
ignores one for another group -- correctly, and silently, because on a
shared broker other fleets' instructions are ordinary traffic. So a signing
default that does not match the fleet you are standing in produces a
perfectly valid instruction that no node will ever act on. That happened:
signed for 'baconbbs' while every node was in 'baconbbsvt'. The tool now
refuses to guess, and remembers the group once it is told.

**The waiting.** "The seed accepted it" says the signature was good. It
says nothing about the other nodes, and one that never moves is exactly the
failure that is easiest to miss. `deploy --wait` watches until they arrive
and exits non-zero if they do not.
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import fleet_sign

TARGET = "a" * 40


class GroupIsNeverGuessedTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp()
        self.group_file = os.path.join(folder, "fleet-group")
        patcher = mock.patch.object(
            fleet_sign, "group_path", return_value=fleet_sign.Path(self.group_file))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"BBS_FLEET_GROUP": "",
                                           "BBS_CONFIG_PATH": os.path.join(folder, "none.ini")})
        env.start()
        self.addCleanup(env.stop)

    def test_no_group_anywhere_is_a_refusal_not_a_default(self):
        with mock.patch.object(fleet_sign, "default_group", return_value=""):
            with self.assertRaises(ValueError) as caught:
                fleet_sign.require_group(types.SimpleNamespace(group=""))
        self.assertIn("--group", str(caught.exception))

    def test_a_given_group_is_remembered_for_next_time(self):
        fleet_sign.require_group(types.SimpleNamespace(group="baconbbsvt"))
        with open(self.group_file, encoding="utf-8") as handle:
            self.assertEqual("baconbbsvt", handle.read().strip())

    def test_the_remembered_group_becomes_the_default(self):
        fleet_sign.require_group(types.SimpleNamespace(group="baconbbsvt"))
        self.assertEqual("baconbbsvt", fleet_sign.default_group())

    def test_the_environment_wins_over_what_was_remembered(self):
        fleet_sign.require_group(types.SimpleNamespace(group="baconbbsvt"))
        with mock.patch.dict(os.environ, {"BBS_FLEET_GROUP": "other-fleet"}):
            self.assertEqual("other-fleet", fleet_sign.default_group())

    def test_a_config_group_wins_over_what_was_remembered(self):
        fleet_sign.require_group(types.SimpleNamespace(group="baconbbsvt"))
        folder = tempfile.mkdtemp()
        config_path = os.path.join(folder, "config.ini")
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write("[fleet]\ngroup = from-config\n")
        with mock.patch.dict(os.environ, {"BBS_CONFIG_PATH": config_path}):
            self.assertEqual("from-config", fleet_sign.default_group())


class WaitingForTheFleetTests(unittest.TestCase):
    def _args(self, wait=0):
        return types.SimpleNamespace(seed="http://seed:8081", token="secret",
                                     timeout=5, wait=wait, ref="HEAD",
                                     group="baconbbsvt", version="",
                                     allow_unpushed=True, allow_red_ci=True)

    def _deploy(self, statuses, wait=60):
        """Drive the wait loop on a clock that always advances, so the test
        can neither hang nor spin: each poll costs 30 simulated seconds, and
        the last status given is repeated for as long as it keeps polling."""
        payload = {"c": TARGET, "v": "0.1.700", "g": "baconbbsvt"}
        clock = [0]
        answers = list(statuses)

        def _tick():
            clock[0] += 30
            return clock[0]

        def _next_status(*_args, **_kwargs):
            return answers.pop(0) if len(answers) > 1 else answers[0]

        with mock.patch.object(fleet_sign, "_build_signed_instruction",
                               return_value=(payload, "HEAD", "blob")), \
                mock.patch.object(fleet_sign, "_submit_instruction",
                                  return_value={"code": "accepted"}), \
                mock.patch.object(fleet_sign, "_fetch_status",
                                  side_effect=_next_status), \
                mock.patch.object(fleet_sign.time, "sleep"), \
                mock.patch.object(fleet_sign.time, "time", side_effect=_tick), \
                mock.patch("builtins.print"):
            return fleet_sign.cmd_deploy(self._args(wait))

    def test_without_wait_it_returns_as_soon_as_the_seed_accepts(self):
        with mock.patch.object(fleet_sign, "_build_signed_instruction",
                               return_value=({"c": TARGET, "v": "1", "g": "g"},
                                             "HEAD", "blob")), \
                mock.patch.object(fleet_sign, "_submit_instruction",
                                  return_value={"code": "accepted"}), \
                mock.patch.object(fleet_sign, "_fetch_status") as status, \
                mock.patch("builtins.print"):
            self.assertEqual(0, fleet_sign.cmd_deploy(self._args(wait=0)))
        status.assert_not_called()

    def test_it_succeeds_once_every_node_is_on_the_target(self):
        converged = {"local": {"commit": TARGET, "on_target": True},
                     "nodes": [{"node_id": "!peer1", "commit_hash": TARGET}]}
        self.assertEqual(0, self._deploy([converged]))

    def test_it_fails_when_a_node_never_arrives(self):
        behind = {"local": {"commit": TARGET, "on_target": True},
                  "nodes": [{"node_id": "!vps", "commit_hash": "b" * 40,
                             "fleet_state": "held"}]}
        self.assertEqual(1, self._deploy([behind, behind, behind]))

    def test_a_node_that_arrives_late_still_counts(self):
        behind = {"local": {"commit": TARGET, "on_target": True},
                  "nodes": [{"node_id": "!peer1", "commit_hash": "b" * 40}]}
        converged = {"local": {"commit": TARGET, "on_target": True},
                     "nodes": [{"node_id": "!peer1", "commit_hash": TARGET}]}
        self.assertEqual(0, self._deploy([behind, converged], wait=60))


class NodesBehindTests(unittest.TestCase):
    def test_the_local_node_counts_too(self):
        behind = fleet_sign._nodes_behind(
            {"local": {"on_target": False, "update_state": {"state": "probation"}},
             "nodes": []}, TARGET)
        self.assertEqual(["local (probation)"], behind)

    def test_a_peer_with_no_state_reads_as_pending(self):
        behind = fleet_sign._nodes_behind(
            {"local": {"on_target": True},
             "nodes": [{"node_id": "!p", "commit_hash": "b" * 40}]}, TARGET)
        self.assertEqual(["!p (pending)"], behind)

    def test_a_short_commit_still_matches(self):
        """Peers report abbreviated hashes."""
        behind = fleet_sign._nodes_behind(
            {"local": {"on_target": True},
             "nodes": [{"node_id": "!p", "commit_hash": TARGET[:7]}]}, TARGET)
        self.assertEqual([], behind)

    def test_no_target_means_nothing_to_report(self):
        self.assertEqual([], fleet_sign._nodes_behind({"nodes": []}, ""))


if __name__ == "__main__":
    unittest.main()
