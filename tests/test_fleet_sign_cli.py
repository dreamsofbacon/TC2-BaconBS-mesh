"""The signing CLI is the operator's whole interface to the fleet.

Two of its behaviours are safety features rather than conveniences, and both
are tested here: refusing to overwrite an existing key, and refusing to sign
a commit that has not been pushed. The first is unrecoverable if it goes
wrong -- overwriting orphans every node that trusts the old key. The second
fails silently much later, on the nodes, as a fetch error.
"""
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
import fleet_update  # noqa: E402


def _result(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _Key:
    """A scratch key location for the duration of a test."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="fleetkey-")
        self.path = os.path.join(self.dir, "fleet-key")
        self.patch = mock.patch.dict(os.environ, {"BBS_FLEET_KEY_PATH": self.path})
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()
        return False

    def create(self, group="g"):
        return fleet_sign.cmd_init(types.SimpleNamespace(force=False, group=group))


class KeyCreationTests(unittest.TestCase):
    def test_init_creates_a_key_and_prints_a_config_block(self):
        with _Key() as key:
            with mock.patch("builtins.print") as printed:
                self.assertEqual(key.create(group="baconbbsvt"), 0)
            self.assertTrue(os.path.isfile(key.path))
            output = "\n".join(str(c.args[0]) for c in printed.call_args_list if c.args)
            self.assertIn("[fleet]", output)
            self.assertIn("group = baconbbsvt", output)
            self.assertIn("trusted_keys = fk", output)

    def test_init_refuses_to_overwrite_an_existing_key(self):
        """Overwriting orphans every node that trusts the old key, and there
        is no way back without visiting each one physically."""
        with _Key() as key:
            with mock.patch("builtins.print"):
                key.create()
            before = open(key.path, "rb").read()
            with mock.patch("builtins.print"):
                code = fleet_sign.cmd_init(
                    types.SimpleNamespace(force=False, group="g"))
            self.assertEqual(code, 1)
            self.assertEqual(open(key.path, "rb").read(), before)

    def test_force_overwrites_deliberately(self):
        with _Key() as key:
            with mock.patch("builtins.print"):
                key.create()
                before = open(key.path, "rb").read()
                self.assertEqual(
                    fleet_sign.cmd_init(types.SimpleNamespace(force=True, group="g")), 0)
            self.assertNotEqual(open(key.path, "rb").read(), before)

    def test_the_private_key_is_not_world_readable(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions do not apply on Windows")
        with _Key() as key:
            with mock.patch("builtins.print"):
                key.create()
            self.assertEqual(os.stat(key.path).st_mode & 0o077, 0)


class UnpushedCommitTests(unittest.TestCase):
    COMMIT = "c" * 40

    def _sign(self, branch_stdout, allow_unpushed=False):
        args = types.SimpleNamespace(ref="HEAD", version="", group="g",
                                     allow_unpushed=allow_unpushed)

        def fake_git(*a):
            if a[0] == "rev-parse":
                return _result(stdout=self.COMMIT + "\n")
            if a[0] == "branch":
                return _result(stdout=branch_stdout)
            if a[0] == "rev-list":
                return _result(stdout="334\n")
            return _result(stdout="a subject\n")

        with mock.patch.object(fleet_sign, "_git", side_effect=fake_git), \
             mock.patch("builtins.print"):
            return fleet_sign.cmd_sign(args)

    def test_signing_an_unpushed_commit_is_refused(self):
        """Nodes fetch from the remote. A commit that exists only on the
        admin machine converges nowhere."""
        with _Key() as key:
            with mock.patch("builtins.print"):
                key.create()
            self.assertEqual(self._sign(branch_stdout=""), 1)

    def test_signing_a_pushed_commit_succeeds(self):
        with _Key() as key:
            with mock.patch("builtins.print"):
                key.create()
            self.assertEqual(self._sign(branch_stdout="  origin/main\n"), 0)

    def test_the_refusal_can_be_overridden_deliberately(self):
        with _Key() as key:
            with mock.patch("builtins.print"):
                key.create()
            self.assertEqual(
                self._sign(branch_stdout="", allow_unpushed=True), 0)


class SignedOutputTests(unittest.TestCase):
    def _entry(self, group):
        with mock.patch("builtins.print") as printed:
            fleet_sign.cmd_show_pubkey(
                types.SimpleNamespace(entry_only=True, group=group))
        return str(printed.call_args_list[0].args[0])

    def test_the_blob_it_prints_actually_verifies(self):
        """End to end: what the CLI emits is what a node will accept."""
        with _Key() as key:
            with mock.patch("builtins.print"):
                key.create(group="baconbbsvt")
            entry = self._entry("baconbbsvt")

            commit = "d" * 40
            captured = []

            def fake_git(*a):
                if a[0] == "rev-parse":
                    return _result(stdout=commit + "\n")
                if a[0] == "branch":
                    return _result(stdout="  origin/main\n")
                if a[0] == "rev-list":
                    return _result(stdout="334\n")
                return _result(stdout="subject\n")

            def capture(*a, **k):
                captured.append(str(a[0]) if a else "")

            with mock.patch.object(fleet_sign, "_git", side_effect=fake_git), \
                 mock.patch("builtins.print", side_effect=capture):
                fleet_sign.cmd_sign(types.SimpleNamespace(
                    ref="HEAD", version="", group="baconbbsvt",
                    allow_unpushed=False))

            blob = captured[-1].strip()
            trusted = fleet_update.parse_trusted_keys(entry)
            verified = fleet_update.verify_instruction(blob, trusted, "baconbbsvt")
            self.assertEqual(verified["c"], commit)

    def test_a_node_in_another_group_rejects_the_same_blob(self):
        """The property materva's node depends on: sharing a broker is not
        joining a fleet."""
        with _Key() as key:
            with mock.patch("builtins.print"):
                key.create(group="mine")
            trusted = fleet_update.parse_trusted_keys(self._entry("mine"))
            payload = fleet_update.build_payload(
                "mine", "e" * 40, "0.1.1", list(trusted)[0])
            private = fleet_sign.load_private_key()
            blob = fleet_update.encode_instruction(
                payload, fleet_update.sign_payload(payload, private))
            with self.assertRaises(fleet_update.FleetVerificationError):
                fleet_update.verify_instruction(blob, trusted, "theirs")


if __name__ == "__main__":
    unittest.main()
