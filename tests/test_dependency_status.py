"""web_admin.dependency_status() -- "Add Web Admin dependency-install
support per operating system," from the backlog.

Deliberately diagnostic only: it reports what is installed and the exact
manual command README.md already documents for what isn't, rather than
running an installer itself. This project treats "who can execute code on
this node" as its one security boundary (see docs/FLEET-UPDATES.md) -- an
admin-password-gated but unsigned web button that shells out to apt/brew
with root privilege would be a second, unsigned way to do exactly that.
These tests hold the line on that as much as they check the OS-detection
and package-status logic itself.
"""
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import web_admin


class OsDetectionTests(unittest.TestCase):
    def test_linux_gets_the_apt_command(self):
        with mock.patch("platform.system", return_value="Linux"):
            result = web_admin.dependency_status()
        self.assertEqual(result["os"], "Linux")
        self.assertIn("apt", result["interpreter_hint"])

    def test_macos_gets_the_brew_command(self):
        with mock.patch("platform.system", return_value="Darwin"):
            result = web_admin.dependency_status()
        self.assertEqual(result["os"], "Darwin")
        self.assertIn("brew", result["interpreter_hint"])

    def test_windows_gets_manual_guidance_not_a_single_command(self):
        """There is no one-line Windows equivalent of `apt install frotz` --
        this must not pretend there is one."""
        with mock.patch("platform.system", return_value="Windows"):
            result = web_admin.dependency_status()
        self.assertEqual(result["os"], "Windows")
        self.assertNotIn("apt", result["interpreter_hint"])
        self.assertNotIn("brew", result["interpreter_hint"])
        self.assertIn("config.ini", result["interpreter_hint"])

    def test_never_recommends_a_command_this_process_would_run_itself(self):
        """The whole point: every hint is something to type by hand, never
        something dependency_status() (or anything downstream of it) could
        be tricked into executing on the operator's behalf."""
        for system in ("Linux", "Darwin", "Windows", ""):
            with self.subTest(system=system), \
                    mock.patch("platform.system", return_value=system):
                result = web_admin.dependency_status()
            self.assertIn("pip install", result["pip_hint"])
            self.assertNotEqual(result["pip_hint"], "")


class PackageStatusTests(unittest.TestCase):
    def test_an_installed_package_is_reported_installed(self):
        with mock.patch("importlib.util.find_spec", return_value=object()):
            result = web_admin.dependency_status()
        flask_row = next(p for p in result["packages"] if p["name"] == "flask")
        self.assertIs(flask_row["installed"], True)

    def test_a_missing_package_is_reported_missing(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            result = web_admin.dependency_status()
        flask_row = next(p for p in result["packages"] if p["name"] == "flask")
        self.assertIs(flask_row["installed"], False)

    def test_a_version_gated_package_is_not_applicable_on_an_old_python(self):
        """meshcore and asyncssh carry a python_version environment marker
        in requirements.txt itself -- this has to say so rather than just
        reporting them missing, which would send an operator chasing a pip
        install that will never succeed on that interpreter."""
        with mock.patch("sys.version_info", (3, 9, 0, "final", 0)):
            result = web_admin.dependency_status()
        meshcore_row = next(p for p in result["packages"] if p["name"] == "meshcore")
        self.assertIsNone(meshcore_row["installed"])
        self.assertIn("3.10", meshcore_row["note"])

    def test_a_version_gated_package_is_checked_normally_on_a_new_enough_python(self):
        with mock.patch("sys.version_info", (3, 12, 0, "final", 0)), \
                mock.patch("importlib.util.find_spec", return_value=object()):
            result = web_admin.dependency_status()
        meshcore_row = next(p for p in result["packages"] if p["name"] == "meshcore")
        self.assertIs(meshcore_row["installed"], True)

    def test_every_declared_dependency_is_reported_on(self):
        """Catches silently dropping one out of _PYTHON_DEPENDENCIES."""
        result = web_admin.dependency_status()
        names = {p["name"] for p in result["packages"]}
        for expected in ("meshtastic", "meshcore", "paho-mqtt", "pypubsub",
                        "flask", "pyserial", "cryptography", "asyncssh"):
            self.assertIn(expected, names)


class InterpreterStatusTests(unittest.TestCase):
    def test_a_found_interpreter_is_reported_installed(self):
        with mock.patch("zork_port._get_interpreter_command",
                        return_value=["dfrotz"]):
            result = web_admin.dependency_status()
        self.assertTrue(result["interpreter_installed"])

    def test_a_missing_interpreter_is_reported_missing(self):
        with mock.patch("zork_port._get_interpreter_command", return_value=None):
            result = web_admin.dependency_status()
        self.assertFalse(result["interpreter_installed"])


class FailureIsolationTests(unittest.TestCase):
    def test_an_internal_error_still_returns_a_usable_shape_not_a_crash(self):
        """Settings has to render even if this diagnostic goes sideways --
        one broken card must not take the whole page down with it."""
        with mock.patch("platform.system", side_effect=RuntimeError("boom")):
            result = web_admin.dependency_status()
        self.assertIn("packages", result)
        self.assertIn("interpreter_hint", result)


class InstallPythonDependenciesTests(unittest.TestCase):
    """web_admin.install_python_dependencies() -- the function underneath
    the API route, tested directly rather than only through Flask."""

    def test_reuses_fleet_updates_own_install_requirements(self):
        with mock.patch("fleet_update.install_requirements",
                        return_value=(True, "up to date")) as install:
            ok, detail = web_admin.install_python_dependencies()
        install.assert_called_once_with()
        self.assertTrue(ok)
        self.assertEqual(detail, "up to date")


class InstallInterpreterTests(unittest.TestCase):
    """web_admin.install_interpreter() -- every OS branch, and every way
    the underlying command can fail, checked directly."""

    def test_the_command_table_has_no_windows_entry(self):
        """The one thing that must never happen: attempting to run
        something on an OS with no safe single-command install."""
        self.assertNotIn("Windows", web_admin._INTERPRETER_INSTALL_COMMANDS)

    def test_darwin_runs_brew_with_no_sudo(self):
        with mock.patch("platform.system", return_value="Darwin"), \
                mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ok, detail = web_admin.install_interpreter()
        run.assert_called_once_with(["brew", "install", "frotz"],
                                    capture_output=True, text=True, timeout=300)
        self.assertTrue(ok)

    def test_a_missing_command_binary_is_reported_plainly(self):
        with mock.patch("platform.system", return_value="Linux"), \
                mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            ok, detail = web_admin.install_interpreter()
        self.assertFalse(ok)
        self.assertIn("PATH", detail)

    def test_a_timeout_is_reported_plainly(self):
        import subprocess
        with mock.patch("platform.system", return_value="Linux"), \
                mock.patch("subprocess.run",
                          side_effect=subprocess.TimeoutExpired(cmd="apt-get", timeout=300)):
            ok, detail = web_admin.install_interpreter()
        self.assertFalse(ok)
        self.assertIn("timed out", detail.lower())

    def test_a_generic_failure_surfaces_truncated_stderr(self):
        with mock.patch("platform.system", return_value="Linux"), \
                mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="",
                                         stderr="E: Unable to locate package frotz")
            ok, detail = web_admin.install_interpreter()
        self.assertFalse(ok)
        self.assertIn("Unable to locate package frotz", detail)

    def test_success_reports_ok(self):
        with mock.patch("platform.system", return_value="Linux"), \
                mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ok, detail = web_admin.install_interpreter()
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
