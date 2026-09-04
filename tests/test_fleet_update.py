"""A fleet update instruction is remote code execution, so verification is
the whole security boundary.

The BBS trusts inbound frames on a single string membership test, and on MQTT
the sender identity is read out of the message body with no binding to the
publishing client -- anyone with publish rights to the topic can claim to be
any peer. So these tests are written as attacks rather than as a happy path:
if any of them passes when it should not, an attacker on the broker owns
every node in the fleet.
"""
import os
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import fleet_update
from fleet_update import FleetVerificationError

GROUP = "baconbbsvt"
COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40


def _issue(group=GROUP, commit=COMMIT, version="0.1.507", issued_at=None,
           private_key=None, signer_id=None, keys=None):
    """Produce a signed blob and the trusted-key map that accepts it."""
    if private_key is None:
        private_key, public_raw, kid = fleet_update.generate_keypair()
        keys = {kid: public_raw}
        signer_id = kid
    payload = fleet_update.build_payload(
        group, commit, version, signer_id, issued_at=issued_at)
    signature = fleet_update.sign_payload(payload, private_key)
    return fleet_update.encode_instruction(payload, signature), keys, payload


class AcceptsAGenuineInstructionTests(unittest.TestCase):
    def test_a_correctly_signed_instruction_verifies(self):
        blob, keys, payload = _issue()
        verified = fleet_update.verify_instruction(blob, keys, GROUP)
        self.assertEqual(verified["c"], COMMIT)
        self.assertEqual(verified["g"], GROUP)

    def test_the_payload_survives_the_round_trip_intact(self):
        blob, keys, payload = _issue()
        self.assertEqual(fleet_update.verify_instruction(blob, keys, GROUP), payload)

    def test_a_newer_instruction_supersedes_an_older_one(self):
        blob, keys, _ = _issue(issued_at="2026-09-01T12:00:00Z")
        verified = fleet_update.verify_instruction(
            blob, keys, GROUP, last_issued_at="2026-08-01T00:00:00Z")
        self.assertEqual(verified["t"], "2026-09-01T12:00:00Z")


class RejectsForgeryTests(unittest.TestCase):
    """Each of these is a way an attacker on the MQTT topic could try to move
    the fleet onto code of their choosing."""

    def test_an_unsigned_node_ignores_everything(self):
        """The default. It is what lets someone share a broker with this
        fleet without joining it."""
        blob, _keys, _ = _issue()
        with self.assertRaises(FleetVerificationError) as caught:
            fleet_update.verify_instruction(blob, {}, GROUP)
        self.assertIn("no trusted fleet keys", str(caught.exception))

    def test_a_signature_from_an_untrusted_key_is_rejected(self):
        """The attacker signs perfectly well -- with their own key."""
        blob, _their_keys, _ = _issue()
        _, our_public, our_id = fleet_update.generate_keypair()
        with self.assertRaises(FleetVerificationError) as caught:
            fleet_update.verify_instruction(blob, {our_id: our_public}, GROUP)
        self.assertIn("does not trust", str(caught.exception))

    def test_swapping_the_commit_invalidates_the_signature(self):
        """The single most valuable forgery: keep a real instruction, point
        it at attacker-controlled code."""
        blob, keys, payload = _issue()
        tampered = dict(payload)
        tampered["c"] = OTHER_COMMIT
        forged = fleet_update.encode_instruction(
            tampered, fleet_update._unb64(blob.split(".", 1)[1]))
        with self.assertRaises(FleetVerificationError) as caught:
            fleet_update.verify_instruction(forged, keys, GROUP)
        self.assertIn("tampered", str(caught.exception))

    def test_claiming_a_trusted_key_id_without_the_key_fails(self):
        """The key id is public. Naming it must not be enough."""
        _attacker_key, _pub, _ = fleet_update.generate_keypair()
        _, victim_public, victim_id = fleet_update.generate_keypair()
        attacker_private, _, _ = fleet_update.generate_keypair()
        payload = fleet_update.build_payload(GROUP, COMMIT, "0.1.1", victim_id)
        signature = fleet_update.sign_payload(payload, attacker_private)
        blob = fleet_update.encode_instruction(payload, signature)
        with self.assertRaises(FleetVerificationError):
            fleet_update.verify_instruction(blob, {victim_id: victim_public}, GROUP)

    def test_a_replayed_instruction_is_rejected(self):
        """Captured off the air and rebroadcast later, this would otherwise
        pin a node to an old, known-vulnerable version forever."""
        blob, keys, _ = _issue(issued_at="2026-01-01T00:00:00Z")
        with self.assertRaises(FleetVerificationError) as caught:
            fleet_update.verify_instruction(
                blob, keys, GROUP, last_issued_at="2026-06-01T00:00:00Z")
        self.assertIn("replay", str(caught.exception))

    def test_replaying_the_same_instruction_twice_is_rejected(self):
        """Equal timestamps must not pass: it has to be strictly newer."""
        blob, keys, payload = _issue()
        with self.assertRaises(FleetVerificationError):
            fleet_update.verify_instruction(
                blob, keys, GROUP, last_issued_at=payload["t"])

    def test_an_instruction_for_another_group_is_rejected(self):
        blob, keys, _ = _issue(group="someone-elses-fleet")
        with self.assertRaises(FleetVerificationError) as caught:
            fleet_update.verify_instruction(blob, keys, GROUP)
        self.assertIn("someone-elses-fleet", str(caught.exception))

    def test_garbage_is_rejected_without_raising_something_else(self):
        """Malformed input must fail as a verification error, not as an
        unhandled decode crash in whatever called us."""
        _, keys, _ = _issue()
        for junk in ("", "  ", "not-a-blob", "a.b.c", "!!!.???",
                     "eyJhIjoxfQ", "." , "x."):
            with self.subTest(junk=junk):
                with self.assertRaises(FleetVerificationError):
                    fleet_update.verify_instruction(junk, keys, GROUP)

    def test_a_payload_missing_fields_is_rejected(self):
        private_key, public_raw, kid = fleet_update.generate_keypair()
        payload = {"g": GROUP, "c": COMMIT}          # no t, n, v, k
        blob = fleet_update.encode_instruction(
            payload, fleet_update.sign_payload(payload, private_key))
        with self.assertRaises(FleetVerificationError) as caught:
            fleet_update.verify_instruction(blob, {kid: public_raw}, GROUP)
        self.assertIn("missing required field", str(caught.exception))

    def test_an_abbreviated_commit_is_refused_at_build_time(self):
        """Seven hex characters are ambiguous and cannot be checked against
        what a fetch actually produced."""
        with self.assertRaises(ValueError):
            fleet_update.build_payload(GROUP, "a8db4d1", "0.1.1", "fkabc123")

    def test_a_non_hex_commit_is_refused(self):
        with self.assertRaises(ValueError):
            fleet_update.build_payload(GROUP, "z" * 40, "0.1.1", "fkabc123")


class TrustedKeyParsingTests(unittest.TestCase):
    def test_a_valid_entry_round_trips(self):
        _, public_raw, kid = fleet_update.generate_keypair()
        entry = fleet_update.public_key_entry(public_raw)
        self.assertEqual(fleet_update.parse_trusted_keys(entry), {kid: public_raw})

    def test_several_keys_are_accepted(self):
        """Key rotation needs both keys trusted at once."""
        entries = []
        expected = {}
        for _ in range(2):
            _, public_raw, kid = fleet_update.generate_keypair()
            entries.append(fleet_update.public_key_entry(public_raw))
            expected[kid] = public_raw
        self.assertEqual(
            fleet_update.parse_trusted_keys(",".join(entries)), expected)

    def test_a_key_whose_declared_id_does_not_match_is_dropped(self):
        """The id is derived from the key. A mismatch means hand-editing, and
        trusting it would show an operator an id that is not the key in use."""
        _, public_raw, _ = fleet_update.generate_keypair()
        entry = fleet_update.public_key_entry(public_raw)
        _, _, wrong = entry.partition(":")
        self.assertEqual(fleet_update.parse_trusted_keys(f"fkdead99:{wrong}"), {})

    def test_a_wrong_length_key_is_dropped(self):
        self.assertEqual(fleet_update.parse_trusted_keys("fkabc123:c2hvcnQ"), {})

    def test_malformed_entries_do_not_take_out_good_ones(self):
        """One bad paste must not silently disarm the keys either side."""
        _, public_raw, kid = fleet_update.generate_keypair()
        good = fleet_update.public_key_entry(public_raw)
        parsed = fleet_update.parse_trusted_keys(f"nonsense,{good},also:::bad")
        self.assertEqual(parsed, {kid: public_raw})

    def test_an_empty_configuration_yields_no_keys(self):
        for empty in ("", "   ", ",,,", None, []):
            with self.subTest(empty=empty):
                self.assertEqual(fleet_update.parse_trusted_keys(empty), {})


class CanonicalisationTests(unittest.TestCase):
    def test_key_order_does_not_change_the_signed_bytes(self):
        """Signing and verification must agree byte-for-byte; a dict's
        insertion order is not something to bet a boundary on."""
        a = {"g": "x", "c": COMMIT, "v": "1", "t": "T", "n": "N", "k": "K"}
        b = {"k": "K", "n": "N", "t": "T", "v": "1", "c": COMMIT, "g": "x"}
        self.assertEqual(fleet_update.canonical_payload(a),
                         fleet_update.canonical_payload(b))

    def test_a_reordered_payload_still_verifies(self):
        private_key, public_raw, kid = fleet_update.generate_keypair()
        payload = fleet_update.build_payload(GROUP, COMMIT, "0.1.1", kid)
        blob = fleet_update.encode_instruction(
            payload, fleet_update.sign_payload(payload, private_key))
        reordered = fleet_update.encode_instruction(
            dict(reversed(list(payload.items()))),
            fleet_update._unb64(blob.split(".", 1)[1]))
        self.assertEqual(
            fleet_update.verify_instruction(reordered, {kid: public_raw}, GROUP)["c"],
            COMMIT)


class KeyIdTests(unittest.TestCase):
    def test_the_id_is_derived_from_the_key(self):
        _, public_raw, kid = fleet_update.generate_keypair()
        self.assertEqual(fleet_update.key_id(public_raw), kid)

    def test_different_keys_get_different_ids(self):
        ids = {fleet_update.generate_keypair()[2] for _ in range(25)}
        self.assertEqual(len(ids), 25)


class ApplyLifecycleTests(unittest.TestCase):
    def test_successful_apply_refreshes_companion_services(self):
        previous = "a" * 40
        target = "b" * 40
        git_result = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(fleet_update, "current_commit", return_value=previous), \
                mock.patch.object(
                    fleet_update, "fetch_commit", return_value=(True, "fetched")), \
                mock.patch.object(
                    fleet_update, "smoke_test_commit", return_value=(True, "passes")), \
                mock.patch.object(fleet_update, "write_update_state"), \
                mock.patch.object(
                    fleet_update, "install_requirements",
                    return_value=(True, "dependencies up to date")), \
                mock.patch.object(
                    fleet_update, "restart_companion_services",
                    return_value=(True, "refreshed")) as restart, \
                mock.patch.object(fleet_update, "_git", return_value=git_result):
            applied, detail = fleet_update.apply_target(target, "0.1.999")

        self.assertTrue(applied)
        self.assertIn(target[:12], detail)
        restart.assert_called_once_with()

    def test_dependency_failure_restores_previous_commit_without_restart(self):
        previous = "a" * 40
        target = "b" * 40
        git_result = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(fleet_update, "current_commit", return_value=previous), \
                mock.patch.object(
                    fleet_update, "fetch_commit", return_value=(True, "fetched")), \
                mock.patch.object(
                    fleet_update, "smoke_test_commit",
                    return_value=(True, "passes")), \
                mock.patch.object(fleet_update, "write_update_state"), \
                mock.patch.object(
                    fleet_update, "install_requirements",
                    return_value=(False, "pip failed")), \
                mock.patch.object(
                    fleet_update, "restart_companion_services") as restart, \
                mock.patch.object(fleet_update, "clear_update_state") as clear, \
                mock.patch.object(
                    fleet_update, "_git", return_value=git_result) as git:
            applied, detail = fleet_update.apply_target(target, "0.1.999")

        self.assertFalse(applied)
        self.assertIn("dependency install failed", detail)
        git.assert_any_call("checkout", "--quiet", "--detach", target)
        git.assert_any_call(
            "checkout", "--quiet", "--force", "--detach", previous)
        clear.assert_called_once_with()
        restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class CompanionRestartTests(unittest.TestCase):
    """Units that only pick up new code when something restarts them.

    mesh-bbs exits on update and systemd brings it back, so it is absent
    from the list on purpose. bacon-ssh was absent by accident, and nothing
    else restarts it: on the live node it ran the code it had started with
    for seven hours across eight deploys, while the file on disk had every
    fix. A hand-verified SSH change looked completely broken, and the
    service had simply never loaded it.
    """

    def _run(self, installed, results):
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            if "list-unit-files" in command:
                unit = command[-1]
                return types.SimpleNamespace(
                    returncode=0, stdout=(unit if unit in installed else ""), stderr="")
            unit = command[-1]
            code = results.get(unit, 0)
            return types.SimpleNamespace(
                returncode=code, stdout="", stderr="boom" if code else "")

        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(fleet_update.os, "name", "posix"), \
                mock.patch.object(fleet_update.shutil, "which", return_value="/bin/systemctl"), \
                mock.patch.object(fleet_update.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(fleet_update.subprocess, "run", side_effect=fake_run):
            os.environ.pop("BBS_FLEET_COMPANION_RESTART_COMMAND", None)
            ok, detail = fleet_update.restart_companion_services()
        restarted = [c[-1] for c in calls if "restart" in c]
        return ok, detail, restarted

    def test_the_ssh_front_end_is_restarted_too(self):
        """The bug: it was never in the list, so it never got the new code."""
        ok, _detail, restarted = self._run(
            {"bacon-web-admin.service", "bacon-ssh.service"}, {})
        self.assertTrue(ok)
        self.assertIn("bacon-ssh.service", restarted)
        self.assertIn("bacon-web-admin.service", restarted)

    def test_a_node_without_ssh_installed_is_not_a_failure(self):
        """forgecam is MQTT-only and deliberately has no bacon-ssh."""
        ok, _detail, restarted = self._run({"bacon-web-admin.service"}, {})
        self.assertTrue(ok)
        self.assertEqual(restarted, ["bacon-web-admin.service"])

    def test_a_real_restart_failure_is_reported(self):
        ok, detail, _restarted = self._run(
            {"bacon-web-admin.service", "bacon-ssh.service"},
            {"bacon-ssh.service": 1})
        self.assertFalse(ok)
        self.assertIn("bacon-ssh.service", detail)

    def test_one_units_failure_does_not_skip_the_other(self):
        _ok, _detail, restarted = self._run(
            {"bacon-web-admin.service", "bacon-ssh.service"},
            {"bacon-web-admin.service": 1})
        self.assertIn("bacon-ssh.service", restarted)

    def test_mesh_bbs_is_not_restarted_here(self):
        """It exits on update and systemd restarts it; doing it here as well
        would cut the update short."""
        self.assertNotIn("mesh-bbs.service", fleet_update.COMPANION_UNITS)

    def test_an_explicit_override_still_wins(self):
        with mock.patch.dict(
                os.environ,
                {"BBS_FLEET_COMPANION_RESTART_COMMAND": "/bin/true"}, clear=False), \
                mock.patch.object(fleet_update.subprocess, "run",
                                  return_value=types.SimpleNamespace(
                                      returncode=0, stdout="", stderr="")):
            ok, _detail = fleet_update.restart_companion_services()
        self.assertTrue(ok)
