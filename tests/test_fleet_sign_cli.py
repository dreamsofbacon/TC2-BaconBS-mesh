"""The signing CLI is the operator's whole interface to the fleet.

Two of its behaviours are safety features rather than conveniences, and both
are tested here: refusing to overwrite an existing key, and refusing to sign
a commit that has not been pushed. The first is unrecoverable if it goes
wrong -- overwriting orphans every node that trusts the old key. The second
fails silently much later, on the nodes, as a fetch error.
"""
import configparser
import hashlib
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
            self.assertIn(
                "python scripts/fleet_sign.py --group baconbbsvt enroll \"fk",
                output)
            self.assertNotIn("download", output.lower())
            self.assertNotIn("move", output.lower())

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


class DeployTests(unittest.TestCase):
    def test_deploy_submits_the_signed_instruction_to_one_seed(self):
        payload = {
            "g": "fleet", "c": "a" * 40, "v": "0.1.700",
            "t": "2026-09-03T00:00:00Z", "k": "fkabc123",
        }
        args = types.SimpleNamespace(
            seed="http://seed:8081", token="secret", timeout=30,
            ref="HEAD", version="", group="fleet", allow_unpushed=False)
        with mock.patch.object(
                fleet_sign, "_build_signed_instruction",
                return_value=(payload, "release", "signed-blob")), \
                mock.patch.object(
                    fleet_sign, "_submit_instruction",
                    return_value={"ok": True, "code": "accepted"}) as submit, \
                mock.patch("builtins.print"):
            code = fleet_sign.cmd_deploy(args)

        self.assertEqual(code, 0)
        submit.assert_called_once_with(
            "http://seed:8081", "secret", "signed-blob", timeout=30)

    def test_deploy_requires_a_seed_and_token(self):
        base = dict(timeout=30, ref="HEAD", version="", group="fleet",
                    allow_unpushed=False)
        with mock.patch("builtins.print"):
            self.assertEqual(fleet_sign.cmd_deploy(types.SimpleNamespace(
                seed="", token="secret", **base)), 1)
            self.assertEqual(fleet_sign.cmd_deploy(types.SimpleNamespace(
                seed="http://seed", token="", **base)), 1)

    def test_apply_url_normalizes_a_trailing_slash(self):
        self.assertEqual(
            fleet_sign._fleet_apply_url("http://seed:8081/"),
            "http://seed:8081/api/fleet/apply")


class StatusTests(unittest.TestCase):
    def test_status_shows_local_and_peer_convergence(self):
        args = types.SimpleNamespace(
            seed="http://seed:8081", token="secret", timeout=30)
        response = {
            "ok": True, "group": "fleet",
            "target": {"commit": "a" * 40},
            "local": {"commit": "aaaaaaa", "on_target": True,
                      "update_state": {"state": "confirmed"}},
            "nodes": [
                {"node_id": "!peer1", "commit_hash": "aaaaaaa",
                 "reported_at": "2026-09-03T00:00:00Z"},
                {"node_id": "!peer2", "commit_hash": "bbbbbbb",
                 "reported_at": "2026-09-03T00:00:00Z"},
            ],
        }
        output = []
        with mock.patch.object(
                fleet_sign, "_fetch_status", return_value=response), \
                mock.patch("builtins.print", side_effect=lambda text="": output.append(str(text))):
            code = fleet_sign.cmd_status(args)

        self.assertEqual(code, 0)
        rendered = "\n".join(output)
        self.assertIn("!peer1", rendered)
        self.assertIn("!peer2", rendered)
        self.assertIn("healthy", rendered)
        self.assertIn("pending", rendered)

    def test_status_requires_seed_credentials(self):
        with mock.patch("builtins.print"):
            code = fleet_sign.cmd_status(types.SimpleNamespace(
                seed="", token="", timeout=30))
        self.assertEqual(code, 1)


class RollbackTests(unittest.TestCase):
    def test_confirmed_rollback_uses_the_normal_signed_deploy_path(self):
        args = types.SimpleNamespace(ref="v1", yes=False)
        with mock.patch("builtins.input", return_value="rollback"), \
                mock.patch.object(fleet_sign, "cmd_deploy", return_value=0) as deploy:
            self.assertEqual(fleet_sign.cmd_rollback(args), 0)
        deploy.assert_called_once_with(args)

    def test_rollback_can_be_cancelled(self):
        args = types.SimpleNamespace(ref="v1", yes=False)
        with mock.patch("builtins.input", return_value="no"), \
                mock.patch("builtins.print"):
            self.assertEqual(fleet_sign.cmd_rollback(args), 1)

    def test_rollback_without_a_ref_selects_the_previous_target(self):
        current = "a" * 40
        previous = "b" * 40
        args = types.SimpleNamespace(
            ref="", yes=True, seed="http://seed", token="secret", timeout=30)
        status = {
            "target": {"commit": current},
            "history": [{"commit": current}, {"commit": previous}],
        }
        with mock.patch.object(
                fleet_sign, "_fetch_status", return_value=status), \
                mock.patch.object(fleet_sign, "cmd_deploy", return_value=0) as deploy:
            self.assertEqual(fleet_sign.cmd_rollback(args), 0)

        self.assertEqual(args.ref, previous)
        deploy.assert_called_once_with(args)

    def test_rollback_without_history_fails_safely(self):
        args = types.SimpleNamespace(
            ref="", yes=True, seed="http://seed", token="secret", timeout=30)
        with mock.patch.object(fleet_sign, "_fetch_status", return_value={
                "target": {"commit": "a" * 40}, "history": []}), \
                mock.patch("builtins.print"):
            self.assertEqual(fleet_sign.cmd_rollback(args), 1)


class DoctorTests(unittest.TestCase):
    def test_doctor_passes_when_key_git_and_seed_agree(self):
        with _Key() as key:
            with mock.patch("builtins.print"):
                key.create(group="fleet")
            private_key = fleet_sign.load_private_key()
            from cryptography.hazmat.primitives import serialization
            public_raw = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw)
            kid = fleet_update.key_id(public_raw)
            args = types.SimpleNamespace(
                ref="HEAD", group="fleet", seed="http://seed",
                token="secret", timeout=30)

            def fake_git(*command):
                if command[0] == "rev-parse":
                    return _result(stdout="a" * 40 + "\n")
                return _result(stdout="  origin/main\n")

            status = {
                "group": "fleet", "mode": "auto",
                "trusted_key_ids": [kid], "config_error": "",
            }
            with mock.patch.object(fleet_sign, "_git", side_effect=fake_git), \
                    mock.patch.object(
                        fleet_sign, "_fetch_status", return_value=status), \
                    mock.patch("builtins.print"):
                code = fleet_sign.cmd_doctor(args)

            self.assertEqual(code, 0)

    def test_doctor_fails_for_a_mismatched_seed_group(self):
        with _Key() as key:
            with mock.patch("builtins.print"):
                key.create(group="fleet")
            args = types.SimpleNamespace(
                ref="HEAD", group="fleet", seed="http://seed",
                token="secret", timeout=30)
            with mock.patch.object(
                    fleet_sign, "_git", return_value=_result(
                        stdout="a" * 40 + "\n")), \
                    mock.patch.object(fleet_sign, "_fetch_status", return_value={
                        "group": "other", "mode": "auto",
                        "trusted_key_ids": [], "config_error": "",
                    }), mock.patch("builtins.print"):
                code = fleet_sign.cmd_doctor(args)

            self.assertEqual(code, 1)


class ApiTokenTests(unittest.TestCase):
    def test_token_prints_a_hash_for_the_node_not_the_raw_token(self):
        with mock.patch("builtins.print") as printed:
            self.assertEqual(fleet_sign.cmd_token(types.SimpleNamespace()), 0)
        output = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        lines = output.splitlines()
        token = lines[-1]
        expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.assertIn(f"api_token_hash = {expected}", output)


class EnrollmentTests(unittest.TestCase):
    def test_enroll_backs_up_and_updates_a_node_config(self):
        with tempfile.TemporaryDirectory(prefix="fleet-enroll-") as directory:
            config_path = Path(directory) / "config.ini"
            config_path.write_text("[interface]\ntype = none\n", encoding="utf-8")
            _, public_raw, kid = fleet_update.generate_keypair()
            entry = fleet_update.public_key_entry(public_raw)
            args = types.SimpleNamespace(
                public_key=entry, config=str(config_path), group="fleet",
                updates="notify", api_token_hash="a" * 64, force=False)
            with mock.patch("builtins.print"):
                code = fleet_sign.cmd_enroll(args)

            self.assertEqual(code, 0)
            self.assertTrue(Path(str(config_path) + ".fleet-backup").is_file())
            config = configparser.ConfigParser()
            config.read(config_path)
            self.assertEqual(config.get("interface", "type"), "none")
            self.assertEqual(config.get("fleet", "group"), "fleet")
            self.assertEqual(config.get("fleet", "updates"), "notify")
            self.assertIn(kid, config.get("fleet", "trusted_keys"))
            self.assertEqual(config.get("fleet", "api_token_hash"), "a" * 64)

    def test_enroll_rejects_an_invalid_public_key(self):
        with mock.patch("builtins.print"):
            code = fleet_sign.cmd_enroll(types.SimpleNamespace(
                public_key="not-a-key", config="config.ini", group="fleet",
                updates="auto", api_token_hash="", force=False))
        self.assertEqual(code, 1)

    def test_enroll_refuses_to_change_an_existing_group_without_force(self):
        with tempfile.TemporaryDirectory(prefix="fleet-enroll-") as directory:
            config_path = Path(directory) / "config.ini"
            config_path.write_text(
                "[fleet]\ngroup = existing\n", encoding="utf-8")
            _, public_raw, _ = fleet_update.generate_keypair()
            with mock.patch("builtins.print"):
                code = fleet_sign.cmd_enroll(types.SimpleNamespace(
                    public_key=fleet_update.public_key_entry(public_raw),
                    config=str(config_path), group="different", updates="auto",
                    api_token_hash="", force=False))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
