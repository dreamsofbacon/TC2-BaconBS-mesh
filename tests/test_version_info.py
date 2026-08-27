"""The running build's version.

Two separate faults made every release look identical. APP_VERSION was a
hardcoded literal that nothing ever bumped -- it sat at 0.1.6 across 171
commits -- so the commit hash was the only thing telling builds apart. And
the hash came from a single `git rev-parse` whose every failure fell
through silently, so an install that could not run git showed a bare
version forever with nothing saying why. One operator saw exactly that.

The patch number is now the commit count, and the hash resolves without
needing git to run.
"""
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import version_info

REPO = Path(__file__).resolve().parent.parent


class CommitFromGitFilesTests(unittest.TestCase):
    """Reading .git directly avoids the two things that actually break this
    on a deployment: git not being installed, and git refusing the repo with
    'dubious ownership' when the service user is not the one that cloned it.
    """

    def _repo(self, tmp):
        root = Path(tmp)
        (root / ".git").mkdir()
        return root

    def test_a_normal_checkout_resolves_without_running_git(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            (root / ".git" / "refs" / "heads").mkdir(parents=True)
            (root / ".git" / "refs" / "heads" / "main").write_text("a8db4d15f8f5abc\n")
            with mock.patch.object(version_info, "_repo_root", lambda: root), \
                 mock.patch.object(subprocess, "run", side_effect=AssertionError("git was run")):
                self.assertEqual(version_info.get_git_commit_short(), "a8db4d1")

    def test_a_detached_head_resolves(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            (root / ".git" / "HEAD").write_text("a8db4d15f8f5abcdef\n")
            with mock.patch.object(version_info, "_repo_root", lambda: root):
                self.assertEqual(version_info.get_git_commit_short(), "a8db4d1")

    def test_a_packed_ref_resolves(self):
        """`git gc` moves refs into packed-refs, leaving no loose file."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            (root / ".git" / "packed-refs").write_text(
                "# pack-refs with: peeled fully-peeled sorted\n"
                "a8db4d15f8f5abc refs/heads/main\n")
            with mock.patch.object(version_info, "_repo_root", lambda: root):
                self.assertEqual(version_info.get_git_commit_short(), "a8db4d1")

    def test_the_short_form_matches_what_git_itself_prints(self):
        """Otherwise an install appears to change version merely because of
        HOW its commit was resolved."""
        self.assertEqual(version_info._SHORT_LEN, 7)


class MissingCommitIsExplainedTests(unittest.TestCase):
    def setUp(self):
        version_info._resolution_note = ""

    def test_no_git_and_no_repo_reports_why(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(version_info, "_repo_root", lambda: Path(tmp)), \
                 mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git")):
                self.assertEqual(version_info.get_git_commit_short(), "")
        self.assertIn("git is not installed", version_info.get_version_resolution_note())

    def test_dubious_ownership_is_reported_rather_than_swallowed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            failed = types.SimpleNamespace(
                returncode=128, stdout="",
                stderr="fatal: detected dubious ownership in repository at '/srv/bbs'\n")
            with mock.patch.object(version_info, "_repo_root", lambda: Path(tmp)), \
                 mock.patch.object(subprocess, "run", return_value=failed):
                self.assertEqual(version_info.get_git_commit_short(), "")
        self.assertIn("dubious ownership", version_info.get_version_resolution_note())

    def test_the_git_call_disarms_dubious_ownership(self):
        """A daemon running as a different user than the one that cloned the
        repo is normal, and git rejects that by default."""
        import tempfile
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(version_info, "_repo_root", lambda: Path(tmp)), \
                 mock.patch.object(subprocess, "run", fake_run):
                version_info.get_git_commit_short()
        self.assertIn("-c", captured["cmd"])
        self.assertTrue(any(str(a).startswith("safe.directory=") for a in captured["cmd"]))

    def test_a_resolved_commit_leaves_no_note(self):
        version_info._resolution_note = "stale"
        with mock.patch.dict("os.environ", {"BBS_GIT_COMMIT": "deadbee"}, clear=False):
            self.assertEqual(version_info.get_git_commit_short(), "deadbee")
        self.assertEqual(version_info.get_version_resolution_note(), "")


class BuildNumberTests(unittest.TestCase):
    """The patch number is the commit count.

    APP_VERSION sat at 0.1.6 for 171 commits because it was a literal
    nobody edited and no automation touched, which left the commit hash
    doing all the work of telling builds apart -- and that hash is exactly
    what goes missing on the installs that cannot run git.
    """

    @staticmethod
    def _git_returns(count):
        def run(cmd, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout=f"{count}\n", stderr="")
        return run

    def test_the_patch_number_is_the_commit_count(self):
        with mock.patch.object(subprocess, "run", self._git_returns(177)), \
             mock.patch.object(version_info, "_write_build_cache", lambda b: None):
            self.assertEqual(version_info.get_build_number(), 177)
            self.assertEqual(version_info.get_app_version(),
                             f"{version_info.APP_VERSION_BASE}.177")

    def test_a_cached_count_is_used_when_git_is_unavailable(self):
        """A node that was packaged or moved keeps a truthful number."""
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git")), \
             mock.patch.object(version_info, "_cached_build_number", lambda: 177):
            self.assertEqual(version_info.get_build_number(), 177)

    def test_with_neither_git_nor_cache_it_falls_back(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git")), \
             mock.patch.object(version_info, "_cached_build_number", lambda: 0):
            self.assertEqual(version_info.get_build_number(),
                             version_info._FALLBACK_BUILD)

    def test_an_explicit_build_number_wins(self):
        """The escape hatch for a packaged install with no git at all."""
        with mock.patch.dict("os.environ", {"BBS_BUILD_NUMBER": "999"}, clear=False):
            self.assertEqual(version_info.get_build_number(), 999)

    def test_a_resolved_count_is_cached_for_later(self):
        written = {}
        with mock.patch.object(subprocess, "run", self._git_returns(177)), \
             mock.patch.object(version_info, "_cached_build_number", lambda: 0), \
             mock.patch.object(version_info, "_write_build_cache",
                               lambda b: written.setdefault("build", b)):
            version_info.get_build_number()
        self.assertEqual(written.get("build"), 177)

    def test_the_count_is_not_committed_to_the_repo(self):
        """A committed count would change on every commit, which changes
        the count."""
        ignored = (REPO / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("_build_version.py", ignored)

    def test_the_version_moves_when_the_commit_count_does(self):
        """The whole point: two different builds must not share a version."""
        with mock.patch.object(version_info, "_write_build_cache", lambda b: None):
            with mock.patch.object(subprocess, "run", self._git_returns(400)):
                first = version_info.get_app_version()
            with mock.patch.object(subprocess, "run", self._git_returns(401)):
                second = version_info.get_app_version()
        self.assertNotEqual(first, second)


class DisplayVersionTests(unittest.TestCase):
    def test_the_commit_is_shown_alongside_the_version(self):
        with mock.patch.object(version_info, "get_git_commit_short", lambda: "abc1234"):
            self.assertEqual(version_info.get_display_version(),
                             f"v{version_info.APP_VERSION} (abc1234)")

    def test_without_a_commit_it_falls_back_to_the_bare_version(self):
        with mock.patch.object(version_info, "get_git_commit_short", lambda: ""):
            self.assertEqual(version_info.get_display_version(),
                             f"v{version_info.APP_VERSION}")

    def test_an_explicit_override_wins(self):
        """The escape hatch for an install with no .git at all."""
        with mock.patch.dict("os.environ", {"BBS_VERSION_DISPLAY": "v9.9 custom"}, clear=False):
            self.assertEqual(version_info.get_display_version(), "v9.9 custom")


if __name__ == "__main__":
    unittest.main()
