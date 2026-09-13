"""The test suite never touches this checkout's real files.

conftest.py sandboxes every runtime path before any test module imports, so a
developer's machine and CI run the same tests against the same empty world.
These fail if that stops being true -- if a path escapes the sandbox, or a
config read goes back to being relative to the working directory, which is
how the suite used to read one config.ini locally and none at all in CI.
"""
import os
import re
import unittest
from pathlib import Path

import conftest

ROOT = Path(__file__).resolve().parent.parent


def _in_sandbox(path) -> bool:
    return os.path.realpath(str(path)).startswith(os.path.realpath(conftest.TEST_SANDBOX))


class SandboxTests(unittest.TestCase):
    def test_config_resolves_into_the_sandbox_not_the_checkout(self):
        import utils
        path = utils._get_config_path()
        self.assertTrue(_in_sandbox(path), path)
        self.assertNotEqual(os.path.realpath(path), os.path.realpath(ROOT / "config.ini"))

    def test_the_database_resolves_into_the_sandbox(self):
        """Tests used to write to the developer's real bulletins.db."""
        import db_operations
        path = db_operations.get_database_path()
        self.assertTrue(_in_sandbox(path), path)

    def test_every_sandboxed_variable_is_set(self):
        for var in conftest._SANDBOXED_PATHS:
            with self.subTest(var=var):
                self.assertTrue(_in_sandbox(os.environ[var]), var)


class NoWorkingDirectoryConfigReadsTests(unittest.TestCase):
    """A bare "config.ini" is relative to wherever the process started.

    That is what let local and CI runs disagree, and it is a production bug in
    its own right: a service with a different WorkingDirectory reads a
    different file than the one the operator edited, or none.
    """

    BARE = re.compile(r"""\.read\(\s*['"]config\.ini['"]\s*\)""")

    def test_no_production_module_reads_config_ini_by_bare_name(self):
        offenders = []
        for path in ROOT.glob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self.BARE.search(line):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [], "read config through _get_config_path()")


if __name__ == "__main__":
    unittest.main()
