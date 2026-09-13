"""Signing a fleet update requires CI to have passed for that exact commit.

A signed instruction is applied automatically by every node. Fifteen commits
in a row reached the whole fleet with failing regression tests, because
nothing between "the tests are red" and "every node is running it" ever
asked. The signing tool is the one step every deploy passes through, so it
asks now -- and refuses on anything short of a completed success, because
every other answer is a case where nobody knows the commit is good.

Nothing here reaches GitHub: gh, git and subprocess are all stubbed.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fleet_sign  # noqa: E402

COMMIT = "d" * 40
ORIGIN = "https://github.com/dreamsofbacon/TC2-BaconBS-mesh"


def _result(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _run(conclusion="success", status="completed", url="https://run/1"):
    return {"status": status, "conclusion": conclusion, "url": url}


class VerdictTests(unittest.TestCase):
    def verdict(self, runs=None, returncode=0, stderr="", gh="/usr/bin/gh", origin=ORIGIN):
        stdout = json.dumps(runs if runs is not None else [])
        with mock.patch.object(fleet_sign, "_git", return_value=_result(stdout=origin + "\n")), \
             mock.patch.object(fleet_sign.shutil, "which", return_value=gh), \
             mock.patch.object(fleet_sign.subprocess, "run",
                               return_value=_result(returncode, stdout, stderr)) as run:
            passed, reason = fleet_sign._ci_verdict(COMMIT)
        return passed, reason, run

    def test_a_green_run_passes(self):
        passed, _, _ = self.verdict([_run("success")])
        self.assertTrue(passed)

    def test_a_failed_run_is_refused_and_says_where_to_look(self):
        passed, reason, _ = self.verdict([_run("failure", url="https://run/42")])
        self.assertFalse(passed)
        self.assertIn("https://run/42", reason)

    def test_a_cancelled_run_is_refused(self):
        passed, _, _ = self.verdict([_run("cancelled")])
        self.assertFalse(passed)

    def test_a_run_still_in_progress_is_refused(self):
        """Not red yet is not green."""
        passed, reason, _ = self.verdict([_run("", status="in_progress")])
        self.assertFalse(passed)
        self.assertIn("in_progress", reason)

    def test_a_commit_with_no_run_at_all_is_refused(self):
        passed, _, _ = self.verdict([])
        self.assertFalse(passed)

    def test_being_unable_to_ask_github_is_refused(self):
        passed, _, _ = self.verdict(returncode=1, stderr="HTTP 401")
        self.assertFalse(passed)

    def test_without_the_github_cli_it_refuses_rather_than_assuming(self):
        passed, reason, _ = self.verdict(gh=None)
        self.assertFalse(passed)
        self.assertIn("gh", reason)

    def test_garbage_from_github_is_refused(self):
        with mock.patch.object(fleet_sign, "_git", return_value=_result(stdout=ORIGIN + "\n")), \
             mock.patch.object(fleet_sign.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(fleet_sign.subprocess, "run",
                               return_value=_result(0, "not json")):
            passed, _ = fleet_sign._ci_verdict(COMMIT)
        self.assertFalse(passed)

    def test_it_asks_about_this_repository_and_this_exact_commit(self):
        """This checkout has several remotes, and gh left to infer one has
        picked the wrong repository before."""
        _, _, run = self.verdict([_run("success")])
        argv = run.call_args[0][0]
        self.assertIn("dreamsofbacon/TC2-BaconBS-mesh", argv)
        self.assertIn(COMMIT, argv)
        self.assertIn(fleet_sign.CI_WORKFLOW, argv)


class OriginParsingTests(unittest.TestCase):
    def origin(self, url):
        with mock.patch.object(fleet_sign, "_git", return_value=_result(stdout=url + "\n")):
            return fleet_sign._origin_repo()

    def test_https(self):
        self.assertEqual(self.origin(ORIGIN), "dreamsofbacon/TC2-BaconBS-mesh")

    def test_https_with_git_suffix(self):
        self.assertEqual(self.origin(ORIGIN + ".git"), "dreamsofbacon/TC2-BaconBS-mesh")

    def test_ssh(self):
        self.assertEqual(self.origin("git@github.com:dreamsofbacon/TC2-BaconBS-mesh.git"),
                         "dreamsofbacon/TC2-BaconBS-mesh")

    def test_not_github(self):
        self.assertIsNone(self.origin("https://example.com/some/repo"))


class SigningTests(unittest.TestCase):
    """cmd_sign itself: the verdict actually stops a signature."""

    def sign(self, verdict, allow_red_ci=False):
        key_dir = tempfile.mkdtemp(prefix="fleetkey-")
        key_path = os.path.join(key_dir, "fleet-key")

        def fake_git(*a):
            if a[0] == "rev-parse":
                return _result(stdout=COMMIT + "\n")
            if a[0] == "branch":
                return _result(stdout="  origin/main\n")
            if a[0] == "rev-list":
                return _result(stdout="334\n")
            return _result(stdout="a subject\n")

        args = types.SimpleNamespace(ref="HEAD", version="", group="g",
                                     allow_unpushed=False, allow_red_ci=allow_red_ci)
        with mock.patch.dict(os.environ, {"BBS_FLEET_KEY_PATH": key_path}), \
             mock.patch("builtins.print"):
            fleet_sign.cmd_init(types.SimpleNamespace(force=False, group="g"))
            with mock.patch.object(fleet_sign, "_git", side_effect=fake_git), \
                 mock.patch.object(fleet_sign, "_ci_verdict", return_value=verdict) as asked:
                code = fleet_sign.cmd_sign(args)
        return code, asked

    def test_a_red_commit_is_not_signed(self):
        code, _ = self.sign((False, "regression tests failure"))
        self.assertEqual(code, 1)

    def test_a_green_commit_is_signed(self):
        code, _ = self.sign((True, "regression tests passed"))
        self.assertEqual(code, 0)

    def test_the_refusal_can_be_overridden_deliberately(self):
        code, asked = self.sign((False, "regression tests failure"), allow_red_ci=True)
        self.assertEqual(code, 0)
        asked.assert_not_called()

    def test_deploy_offers_the_same_override(self):
        """deploy signs through the same path, so it must be gated too."""
        parser_help = Path(fleet_sign.__file__).read_text(encoding="utf-8")
        self.assertEqual(parser_help.count('add_argument("--allow-red-ci"'), 2)


if __name__ == "__main__":
    unittest.main()
