"""The web admin and SSH front end restart themselves onto new code.

A fleet update switches the working tree and then asks systemd to restart
the two services that do not exit by themselves. That request needs a sudo
rule, and where it is missing -- a node installed before the rule existed,
or one where it never landed -- both services keep running the OLD code.
The SSH front end serves a BBS from before the fix (seven hours, eight
deploys, once), and the web admin reports the version it started with for
ever, because DISPLAY_VERSION is read once at startup. That is the version
mismatch on the Fleet page.

So neither waits to be restarted now: each watches the checked-out commit
and exits when it moves. No privilege, nothing left to be missing.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import restart_on_update


class ReadingTheCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / ".git").mkdir()
        patcher = mock.patch.object(restart_on_update, "repo_root",
                                    return_value=self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_detached_head_is_the_commit(self):
        """What a fleet update leaves behind."""
        (self.repo / ".git" / "HEAD").write_text("a" * 40, encoding="utf-8")
        self.assertEqual("a" * 40, restart_on_update.current_code_id())

    def test_a_branch_is_followed_to_its_commit(self):
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n",
                                                 encoding="utf-8")
        ref = self.repo / ".git" / "refs" / "heads"
        ref.mkdir(parents=True)
        (ref / "main").write_text("b" * 40 + "\n", encoding="utf-8")
        self.assertEqual("b" * 40, restart_on_update.current_code_id())

    def test_a_packed_ref_still_yields_something_that_changes(self):
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n",
                                                 encoding="utf-8")
        (self.repo / ".git" / "packed-refs").write_text("x", encoding="utf-8")
        first = restart_on_update.current_code_id()
        self.assertTrue(first)
        self.assertIn("refs/heads/main", first)

    def test_no_git_checkout_reads_as_nothing(self):
        """A container image or a tarball install has no .git."""
        (self.repo / ".git").rmdir()
        self.assertEqual("", restart_on_update.current_code_id())


class WatchingTests(unittest.TestCase):
    def _watch(self, ids, interval=0.001):
        """Run the watcher against a scripted sequence of commit ids."""
        exits = []
        answers = list(ids)

        def _code_id():
            return answers.pop(0) if len(answers) > 1 else answers[0]

        thread = restart_on_update.watch(
            "test-service", interval=interval,
            exit_hook=lambda: exits.append(True), code_id=_code_id)
        if thread is not None:
            thread.join(timeout=2)
        return exits, thread

    def test_it_exits_when_the_commit_changes(self):
        exits, _thread = self._watch(["a" * 40, "b" * 40])
        self.assertEqual([True], exits)

    def test_it_stays_quiet_while_the_commit_holds(self):
        exits, thread = self._watch(["a" * 40])
        self.assertEqual([], exits)
        self.assertTrue(thread.is_alive())

    def test_an_unreadable_commit_is_not_treated_as_a_change(self):
        """A torn read during a checkout must not restart the service on the
        half-written tree it happened to catch."""
        exits, thread = self._watch(["a" * 40, "", "a" * 40])
        self.assertEqual([], exits)
        self.assertTrue(thread.is_alive())

    def test_nothing_is_watched_without_a_checkout(self):
        exits, thread = self._watch([""])
        self.assertIsNone(thread)
        self.assertEqual([], exits)

    def test_the_exit_code_restarts_under_either_policy(self):
        """bacon-web-admin is Restart=always, but bacon-ssh shipped as
        Restart=on-failure for a long time, where a clean exit would simply
        stop the service."""
        self.assertNotEqual(0, restart_on_update.EXIT_CODE)


class TheServicesUseItTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def test_the_web_admin_starts_the_watcher(self):
        source = (self.ROOT / "web_admin.py").read_text(encoding="utf-8")
        self.assertIn("restart_on_update.watch(\"bacon-web-admin\")", source)

    def test_the_ssh_service_starts_the_watcher(self):
        source = (self.ROOT / "ssh_server.py").read_text(encoding="utf-8")
        self.assertIn("restart_on_update.watch(\"bacon-ssh\")", source)

    def test_the_ssh_unit_restarts_on_a_clean_exit_too(self):
        unit = (self.ROOT / "bacon-ssh.service").read_text(encoding="utf-8")
        self.assertIn("Restart=always", unit)
        self.assertNotIn("Restart=on-failure", unit)


if __name__ == "__main__":
    unittest.main()
