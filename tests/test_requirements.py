"""requirements.txt must actually cover what the code imports.

Two real failures motivate this. pyserial is imported directly by
config_init.py but was never declared -- it only installed because
meshtastic happened to depend on it. And meshcore requires Python 3.10+
with no marker, so on a 3.9 node pip refused the ENTIRE file: that node got
no paho-mqtt either, and MQTT failed with nothing pointing at the Python
version as the cause.
"""
import ast
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent

# CircuitPython built-ins used by pico_node/code.py, which runs ON the Pico
# and not on the BBS host. They are not installable for CPython and must
# never be added to requirements.
CIRCUITPYTHON_BUILTINS = {"alarm", "board", "busio", "digitalio"}

# Import name -> distribution name, where they differ.
IMPORT_TO_DIST = {
    "paho": "paho-mqtt",
    "pubsub": "pypubsub",
    "serial": "pyserial",
}


def _declared(path):
    names = set()
    for line in (REPO / path).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-r"):
            continue
        name = line.split(";")[0]
        for sep in (">=", "<=", "==", "!=", "~=", ">", "<"):
            name = name.split(sep)[0]
        names.add(name.strip().lower())
    return names


def _imported():
    stdlib = set(sys.stdlib_module_names)
    files = [p for p in REPO.rglob("*.py")
             if ".venv" not in p.parts and "node_modules" not in p.parts]
    local = {p.stem for p in files}
    found = {}
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                if m in stdlib or m in local or m.startswith("_"):
                    continue
                found.setdefault(m, set()).add(f.relative_to(REPO).as_posix())
    return found


class RequirementsCoverImportsTests(unittest.TestCase):
    def test_every_third_party_import_is_declared(self):
        declared = _declared("requirements.txt") | _declared("requirements-dev.txt")
        missing = {}
        for mod, where in _imported().items():
            if mod in CIRCUITPYTHON_BUILTINS:
                continue
            dist = IMPORT_TO_DIST.get(mod, mod).lower()
            if dist not in declared:
                missing[dist] = sorted(where)
        self.assertEqual(missing, {},
                         "imported but not in any requirements file: %r" % (missing,))

    def test_pyserial_is_declared_rather_than_inherited(self):
        """config_init.py imports it directly; relying on meshtastic to pull
        it in breaks the day meshtastic stops depending on it."""
        self.assertIn("pyserial", _declared("requirements.txt"))

    def test_pytest_is_available_to_a_fresh_checkout(self):
        self.assertIn("pytest", _declared("requirements-dev.txt"))

    def test_circuitpython_modules_are_not_declared(self):
        """They run on the Pico, not the host, and are not installable."""
        declared = _declared("requirements.txt") | _declared("requirements-dev.txt")
        self.assertEqual(declared & CIRCUITPYTHON_BUILTINS, set())


class PythonVersionMarkerTests(unittest.TestCase):
    """meshcore needs 3.10+. Without a marker pip rejects the whole file on
    an older host, so nothing installs -- not even paho-mqtt."""

    def _resolve(self, python_version):
        from packaging.requirements import Requirement
        env = {"python_version": python_version}
        kept = []
        for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if not line:
                continue
            req = Requirement(line)
            if req.marker is None or req.marker.evaluate(env):
                kept.append(req.name.lower())
        return kept

    def test_an_older_host_still_gets_the_rest_of_the_stack(self):
        on_39 = self._resolve("3.9")
        self.assertNotIn("meshcore", on_39)
        for essential in ("meshtastic", "paho-mqtt", "flask", "pyserial"):
            self.assertIn(essential, on_39,
                          f"{essential} would not install on Python 3.9")

    def test_a_modern_host_gets_meshcore(self):
        self.assertIn("meshcore", self._resolve("3.12"))


if __name__ == "__main__":
    unittest.main()
