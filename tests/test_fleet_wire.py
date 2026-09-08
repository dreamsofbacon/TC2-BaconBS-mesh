"""A signed instruction arriving over the wire, end to end.

The unit tests prove verification is correct in isolation. These prove it is
actually WIRED IN: that an instruction reaching process_message is verified
before it reaches storage, that a forged one is not stored, and that both
frame prefixes are registered in the two lists which fail silently when
missed -- one treats the frame as a user command, the other files it as
public chatter for everyone to read.
"""
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations
import fleet_update
import message_processing
import public_chatter

GROUP = "baconbbsvt"
COMMIT = "a1b2c3d4" * 5


def _install_fake_meshtastic_package():
    def stub(name):
        module = types.ModuleType(name)
        sys.modules[name] = module
        return module

    mesh = stub("meshtastic")
    mesh.BROADCAST_NUM = 0
    mesh.mesh_interface = stub("meshtastic.mesh_interface")
    mesh.stream_interface = stub("meshtastic.stream_interface")
    mesh.serial_interface = stub("meshtastic.serial_interface")
    mesh.tcp_interface = stub("meshtastic.tcp_interface")
    mesh.stream_interface.StreamInterface = object
    mesh.mesh_interface.MeshInterface = types.SimpleNamespace(
        MeshInterfaceError=Exception)


class _Iface:
    def __init__(self):
        self.sent = []
        self.bbs_nodes = []
        self.allowed_nodes = []
        self.nodes = {}

    def sendText(self, text=None, destinationId=None, **kwargs):
        self.sent.append((destinationId, text))
        return types.SimpleNamespace(id=len(self.sent))


class _FleetNode:
    """A node configured to trust one key, with a scratch database."""

    def __enter__(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.private, self.public, self.key_id = fleet_update.generate_keypair()
        self.entry = fleet_update.public_key_entry(self.public)
        self.dir = tempfile.mkdtemp(prefix="fleetnode-")
        self.trigger = os.path.join(self.dir, "apply_update.trigger")
        self.env = mock.patch.dict(
            os.environ, {"BBS_FLEET_APPLY_TRIGGER_PATH": self.trigger})
        self.env.start()
        self.settings = mock.patch.object(
            message_processing, "_fleet_settings",
            return_value={"group": GROUP, "trusted_keys": self.entry,
                          "updates": "auto", "pin_commit": ""})
        self.settings.start()
        return self

    def __exit__(self, *exc):
        self.settings.stop()
        self.env.stop()
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        return False

    def instruction(self, commit=COMMIT, group=GROUP, private=None, issued_at=None):
        payload = fleet_update.build_payload(
            group, commit, "0.1.999", self.key_id, issued_at=issued_at)
        return fleet_update.encode_instruction(
            payload, fleet_update.sign_payload(payload, private or self.private))

    def deliver(self, blob, sender="mqtt:baconbbsvt:someone", interface=None):
        """Hand a whole instruction to the receive path as one frame."""
        message_processing._add_fleet_instruction_chunk(
            f"FLEETVER|abc123|{len(blob)}|{blob}", sender,
            interface or _Iface())

    def target(self):
        return db_operations.get_fleet_target(GROUP)


class GenuineInstructionTests(unittest.TestCase):
    def test_a_signed_instruction_is_stored(self):
        with _FleetNode() as node:
            node.deliver(node.instruction())
            stored = node.target()
            self.assertIsNotNone(stored, "a valid instruction was not stored")
            self.assertEqual(stored["commit"], COMMIT)

    def test_accepting_one_asks_the_server_to_apply_it(self):
        """The receive thread must not update in place -- an update ends in
        the process exiting, so it goes through the trigger channel."""
        with _FleetNode() as node:
            node.deliver(node.instruction())
            self.assertTrue(os.path.exists(node.trigger))

    def test_accepting_one_relays_it_to_other_peers(self):
        with _FleetNode() as node:
            interface = _Iface()
            interface.bbs_nodes = ["peer", "other"]
            blob = node.instruction()
            with mock.patch.object(
                    message_processing, "send_fleet_target_to_bbs_nodes",
                    return_value=1) as relay:
                node.deliver(blob, sender="peer", interface=interface)

            relay.assert_called_once_with(blob, ["other"], interface)

    def test_a_replay_is_not_relayed(self):
        with _FleetNode() as node:
            interface = _Iface()
            interface.bbs_nodes = ["other"]
            blob = node.instruction()
            node.deliver(blob, interface=interface)
            with mock.patch.object(
                    message_processing, "send_fleet_target_to_bbs_nodes") as relay:
                node.deliver(blob, interface=interface)

            relay.assert_not_called()

    def test_a_chunked_instruction_reassembles(self):
        """An ed25519 signature does not fit a LoRa packet, so the real path
        is always chunked."""
        with _FleetNode() as node:
            blob = node.instruction()
            head, tail = blob[:40], blob[40:]
            iface = _Iface()
            message_processing._add_fleet_instruction_chunk(
                f"FLEETVER|xy|{len(blob)}|{head}", "peer", iface)
            self.assertIsNone(node.target(), "acted before the whole blob arrived")
            message_processing._add_fleet_instruction_chunk(
                f"FLEETVERCONT|xy|{len(head)}|{tail}", "peer", iface)
            self.assertEqual(node.target()["commit"], COMMIT)

    def test_an_oversized_declared_instruction_is_not_buffered(self):
        with _FleetNode() as node:
            message_processing._pending_fleet_instructions.clear()
            message_processing._add_fleet_instruction_chunk(
                "FLEETVER|huge|999999|abc", "peer", _Iface())
            self.assertNotIn(
                "huge", message_processing._pending_fleet_instructions)
            self.assertIsNone(node.target())

    def test_non_contiguous_chunks_do_not_verify(self):
        with _FleetNode() as node:
            blob = node.instruction()
            head, tail = blob[:40], blob[40:]
            message_processing._add_fleet_instruction_chunk(
                f"FLEETVER|gap|{len(blob)}|{head}", "peer", _Iface())
            message_processing._add_fleet_instruction_chunk(
                f"FLEETVERCONT|gap|41|{tail}", "peer", _Iface())
            self.assertIsNone(node.target())

    def test_stale_assemblies_are_expired(self):
        with _FleetNode():
            message_processing._pending_fleet_instructions.clear()
            with mock.patch.object(message_processing.time, "time", return_value=10):
                message_processing._add_fleet_instruction_chunk(
                    "FLEETVER|old|100|abc", "peer", _Iface())
            with mock.patch.object(message_processing.time, "time", return_value=400):
                message_processing._add_fleet_instruction_chunk(
                    "FLEETVER|new|100|abc", "peer", _Iface())
            self.assertNotIn("old", message_processing._pending_fleet_instructions)
            self.assertIn("new", message_processing._pending_fleet_instructions)

    def test_accepted_targets_are_kept_in_newest_first_history(self):
        with _FleetNode() as node:
            old_commit = "b" * 40
            node.deliver(node.instruction(
                commit=old_commit, issued_at="2026-01-01T00:00:00Z"))
            node.deliver(node.instruction(
                commit=COMMIT, issued_at="2026-02-01T00:00:00Z"))

            history = db_operations.get_fleet_target_history(GROUP)
            self.assertEqual(
                [entry["commit"] for entry in history], [COMMIT, old_commit])


class ForgedInstructionTests(unittest.TestCase):
    """Each of these arrives from a peer the node would otherwise 'trust'."""

    def test_an_instruction_signed_by_another_key_is_not_stored(self):
        with _FleetNode() as node:
            attacker, _, _ = fleet_update.generate_keypair()
            node.deliver(node.instruction(private=attacker))
            self.assertIsNone(node.target())

    def test_a_tampered_commit_is_not_stored(self):
        with _FleetNode() as node:
            blob = node.instruction()
            payload, signature = fleet_update.decode_instruction(blob)
            payload["c"] = "f" * 40
            node.deliver(fleet_update.encode_instruction(payload, signature))
            self.assertIsNone(node.target())

    def test_an_instruction_for_another_group_is_not_stored(self):
        with _FleetNode() as node:
            node.deliver(node.instruction(group="someone-elses-fleet"))
            self.assertIsNone(node.target())

    def test_a_replay_cannot_move_the_node_backwards(self):
        """The attack this defends against: capture a real instruction, then
        rebroadcast it later to pin the node to old, vulnerable code."""
        with _FleetNode() as node:
            node.deliver(node.instruction(issued_at="2026-06-01T00:00:00Z"))
            self.assertEqual(node.target()["commit"], COMMIT)
            old_commit = "b" * 40
            node.deliver(node.instruction(
                commit=old_commit, issued_at="2026-01-01T00:00:00Z"))
            self.assertEqual(node.target()["commit"], COMMIT)

    def test_no_trigger_is_written_for_a_forgery(self):
        with _FleetNode() as node:
            attacker, _, _ = fleet_update.generate_keypair()
            node.deliver(node.instruction(private=attacker))
            self.assertFalse(os.path.exists(node.trigger))

    def test_garbage_does_not_raise_into_the_receive_loop(self):
        """A crash here would take down message handling for everything."""
        with _FleetNode() as node:
            for junk in ("FLEETVER|", "FLEETVER|a|b|c", "FLEETVER|x|9|!!!",
                         "FLEETVERCONT|unknown|0|zz"):
                with self.subTest(junk=junk):
                    message_processing._add_fleet_instruction_chunk(
                        junk, "peer", _Iface())
            self.assertIsNone(node.target())


class OptOutTests(unittest.TestCase):
    def test_a_node_with_no_trusted_key_ignores_a_valid_instruction(self):
        """What lets someone share a broker without joining the fleet."""
        with _FleetNode() as node:
            blob = node.instruction()
            with mock.patch.object(
                    message_processing, "_fleet_settings",
                    return_value={"group": GROUP, "trusted_keys": "",
                                  "updates": "auto", "pin_commit": ""}):
                node.deliver(blob)
            self.assertIsNone(node.target())

    def test_updates_off_ignores_a_valid_instruction(self):
        """The local override, for a node being debugged."""
        with _FleetNode() as node:
            blob = node.instruction()
            with mock.patch.object(
                    message_processing, "_fleet_settings",
                    return_value={"group": GROUP, "trusted_keys": node.entry,
                                  "updates": "off", "pin_commit": ""}):
                node.deliver(blob)
            self.assertIsNone(node.target())


class RegistrationTests(unittest.TestCase):
    """Two lists that fail silently when a new frame is missed."""

    def test_the_frames_are_treated_as_sync_not_user_commands(self):
        source = (Path(__file__).resolve().parent.parent
                  / "message_processing.py").read_text(encoding="utf-8")
        marker = source.index("is_sync_message = any(")
        block = source[marker:marker + 1600]
        for prefix in ('"FLEETVER|"', '"FLEETVERCONT|"', '"NODEVER|"',
                       '"FLEETSTATUS|"'):
            self.assertIn(prefix, block)

    def test_the_frames_are_not_captured_as_public_chatter(self):
        """Otherwise a fleet instruction is stored and displayed to everyone
        as though someone had said it on the public channel."""
        for prefix in ("FLEETVER|", "FLEETVERCONT|", "NODEVER|",
                       "FLEETSTATUS|"):
            self.assertIn(prefix, public_chatter.CONTROL_PREFIXES)

    def test_the_capability_is_advertised(self):
        import utils
        self.assertIn("fver", utils.WIRE_CAPABILITIES)
        self.assertIn("fstat", utils.WIRE_CAPABILITIES)
        self.assertIn("fver", utils.local_capabilities_token())
        self.assertIn("fstat", utils.local_capabilities_token())


class NodeVersionTests(unittest.TestCase):
    def test_a_reported_version_is_recorded(self):
        with _FleetNode():
            db_operations.record_node_version("!peer1", "0.1.500", "abc1234")
            versions = {v["node_id"]: v for v in db_operations.get_node_versions()}
            self.assertEqual(versions["!peer1"]["app_version"], "0.1.500")

    def test_a_later_report_replaces_an_earlier_one(self):
        with _FleetNode():
            db_operations.record_node_version("!peer1", "0.1.500", "aaa")
            db_operations.record_node_version("!peer1", "0.1.507", "bbb")
            versions = db_operations.get_node_versions()
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0]["app_version"], "0.1.507")

    def test_fleet_status_is_recorded_through_the_receive_path(self):
        with _FleetNode():
            message_processing.process_message(
                None,
                f"FLEETSTATUS|!peer1|0.1.999|{COMMIT[:7]}|{COMMIT}|failed|1",
                _Iface(), is_sync_message=True, sender_node_id="!peer1")
            status = db_operations.get_node_versions()[0]
            self.assertEqual(status["target_commit"], COMMIT)
            self.assertEqual(status["rollout_state"], "failed")

    def test_legacy_nodever_does_not_erase_richer_status(self):
        with _FleetNode():
            db_operations.record_node_version(
                "!peer1", "0.1.999", COMMIT[:7], COMMIT, "probation")
            db_operations.record_node_version(
                "!peer1", "0.1.999", COMMIT[:7])
            status = db_operations.get_node_versions()[0]
            self.assertEqual(status["target_commit"], COMMIT)
            self.assertEqual(status["rollout_state"], "probation")

    def test_a_reason_is_recorded_alongside_the_state(self):
        with _FleetNode():
            db_operations.record_node_version(
                "!peer1", "0.1.999", COMMIT[:7], COMMIT, "failed",
                "refused: target does not compile")
            status = db_operations.get_node_versions()[0]
            self.assertEqual(status["rollout_detail"],
                             "refused: target does not compile")

    def test_a_later_report_with_no_reason_clears_the_old_one(self):
        """Unlike rollout_state, which a legacy peer's blank field must not
        stamp out, a stale reason from a past failure has to actually clear
        once that peer reports something else -- otherwise a node that
        recovered would still show last week's refusal forever."""
        with _FleetNode():
            db_operations.record_node_version(
                "!peer1", "0.1.999", COMMIT[:7], COMMIT, "failed",
                "fetch failed: connection refused")
            db_operations.record_node_version(
                "!peer1", "0.1.999", COMMIT[:7], COMMIT, "healthy", "")
            status = db_operations.get_node_versions()[0]
            self.assertEqual(status["rollout_detail"], "")


class FleetStatusReasonWireTests(unittest.TestCase):
    """The reason text, specifically, crossing the actual FLEETSTATUS frame
    -- not just the DB layer underneath it."""

    def test_the_new_eight_field_frame_carries_a_reason(self):
        with _FleetNode():
            message_processing.process_message(
                None,
                f"FLEETSTATUS|!peer1|0.1.999|{COMMIT[:7]}|{COMMIT}|failed|"
                "refused: bad import|1700000000",
                _Iface(), is_sync_message=True, sender_node_id="!peer1")
            status = db_operations.get_node_versions()[0]
            self.assertEqual(status["rollout_state"], "failed")
            self.assertEqual(status["rollout_detail"], "refused: bad import")

    def test_the_old_seven_field_frame_still_parses_with_no_reason(self):
        """A peer that has not picked up this change yet sends the
        original shape -- it must still be accepted, just with an empty
        reason, not dropped as malformed."""
        with _FleetNode():
            message_processing.process_message(
                None,
                f"FLEETSTATUS|!peer1|0.1.999|{COMMIT[:7]}|{COMMIT}|failed|1700000000",
                _Iface(), is_sync_message=True, sender_node_id="!peer1")
            status = db_operations.get_node_versions()[0]
            self.assertEqual(status["rollout_state"], "failed")
            self.assertEqual(status["rollout_detail"], "")

    def test_a_pipe_or_newline_in_the_reason_cannot_corrupt_the_frame(self):
        """send_fleet_status_to_bbs_nodes is what has to guarantee this --
        a literal '|' reaching the frame builder would shift every field
        after it, and a newline would let a second, forged-looking frame
        ride along inside what should be one message. Sanitized before it
        is ever interpolated in, not left to whatever wrote the reason."""
        import utils
        with mock.patch.object(db_operations, "peer_supports", return_value=True), \
                mock.patch.object(utils, "_send_one_sync") as send:
            utils.send_fleet_status_to_bbs_nodes(
                "!local", "0.1.999", COMMIT[:7], COMMIT, "failed",
                ["!peer1"], _Iface(),
                rollout_detail="refused: bad|import\nsecond line")
        message = send.call_args.args[0]
        self.assertEqual(len(message.split("|")), 8)
        self.assertNotIn("\n", message)

    def test_an_overlong_reason_cannot_blow_the_frame_over_budget(self):
        """_send_one_sync drops the whole frame outright if it exceeds the
        transport's byte limit -- an unbounded error message could take
        the entire advisory status report down with it on a radio link,
        not just get truncated on its own."""
        import utils
        with mock.patch.object(db_operations, "peer_supports", return_value=True), \
                mock.patch.object(utils, "_send_one_sync") as send:
            utils.send_fleet_status_to_bbs_nodes(
                "!local", "0.1.999", COMMIT[:7], COMMIT, "failed",
                ["!peer1"], _Iface(), rollout_detail="x" * 500)
        message = send.call_args.args[0]
        self.assertLess(len(message.encode("utf-8")), 220)


class AdvertisementTests(unittest.TestCase):
    def test_server_advertises_stored_target_and_running_version(self):
        _install_fake_meshtastic_package()
        import server

        with _FleetNode() as node:
            blob = node.instruction()
            payload, _ = fleet_update.decode_instruction(blob)
            db_operations.store_fleet_target(payload, blob)
            with mock.patch.object(
                    server, "send_fleet_target_to_bbs_nodes",
                    return_value=2) as send_target, mock.patch.object(
                    server, "send_node_version_to_bbs_nodes",
                    return_value=2) as send_version, mock.patch.object(
                    server, "send_fleet_status_to_bbs_nodes",
                    return_value=2) as send_status, mock.patch.object(
                    server, "get_local_node_id", return_value="!local"):
                sent = server._advertise_fleet_state(
                    {"fleet": {"group": GROUP, "updates": "auto"}},
                    ["!peer1", "!peer2"], _Iface())

            self.assertEqual(sent, 6)
            send_target.assert_called_once_with(
                blob, ["!peer1", "!peer2"], mock.ANY)
            send_version.assert_called_once_with(
                "!local", mock.ANY, mock.ANY,
                ["!peer1", "!peer2"], mock.ANY)
            send_status.assert_called_once_with(
                "!local", mock.ANY, mock.ANY, COMMIT, mock.ANY,
                ["!peer1", "!peer2"], mock.ANY, rollout_detail=mock.ANY)

    def test_a_refused_update_advertises_its_own_reason_verbatim(self):
        """apply_target() writes 'detail' straight into update_state.json
        on a refusal -- that is already the exact reason, and it should
        cross the wire untouched, not get re-derived from nothing."""
        _install_fake_meshtastic_package()
        import server

        with _FleetNode() as node:
            blob = node.instruction()
            payload, _ = fleet_update.decode_instruction(blob)
            db_operations.store_fleet_target(payload, blob)
            with mock.patch.object(
                    fleet_update, "read_update_state",
                    return_value={"state": "failed", "detail":
                                 "refused: target does not compile"}), \
                    mock.patch.object(server, "send_fleet_status_to_bbs_nodes",
                                      return_value=1) as send_status, \
                    mock.patch.object(server, "get_local_node_id",
                                      return_value="!local"):
                server._advertise_fleet_state(
                    {"fleet": {"group": GROUP, "updates": "auto"}},
                    ["!peer1"], _Iface())
            self.assertEqual(send_status.call_args.kwargs["rollout_detail"],
                             "refused: target does not compile")

    def test_a_crash_loop_revert_synthesizes_a_reason(self):
        """update_guard.py's own state file has no 'detail' key at all --
        it writes failed_commit/restored_commit/attempts instead. Without
        this, a crash-looped peer would advertise "rolled_back" and
        nothing else, the exact gap this feature exists to close."""
        _install_fake_meshtastic_package()
        import server

        with _FleetNode() as node:
            blob = node.instruction()
            payload, _ = fleet_update.decode_instruction(blob)
            db_operations.store_fleet_target(payload, blob)
            with mock.patch.object(
                    fleet_update, "read_update_state",
                    return_value={"state": "rolled_back",
                                 "restored_commit": "deadbeef1234",
                                 "attempts": 3}), \
                    mock.patch.object(server, "send_fleet_status_to_bbs_nodes",
                                      return_value=1) as send_status, \
                    mock.patch.object(server, "get_local_node_id",
                                      return_value="!local"):
                server._advertise_fleet_state(
                    {"fleet": {"group": GROUP, "updates": "auto"}},
                    ["!peer1"], _Iface())
            reason = send_status.call_args.kwargs["rollout_detail"]
            self.assertIn("3", reason)
            self.assertIn("deadbeef1234", reason)

    def test_a_healthy_state_advertises_no_spurious_reason(self):
        _install_fake_meshtastic_package()
        import server

        with _FleetNode() as node:
            blob = node.instruction()
            payload, _ = fleet_update.decode_instruction(blob)
            db_operations.store_fleet_target(payload, blob)
            with mock.patch.object(
                    fleet_update, "read_update_state",
                    return_value={"state": "healthy"}), \
                    mock.patch.object(server, "send_fleet_status_to_bbs_nodes",
                                      return_value=1) as send_status, \
                    mock.patch.object(server, "get_local_node_id",
                                      return_value="!local"):
                server._advertise_fleet_state(
                    {"fleet": {"group": GROUP, "updates": "auto"}},
                    ["!peer1"], _Iface())
            self.assertEqual(send_status.call_args.kwargs["rollout_detail"], "")

    def test_server_does_not_advertise_when_fleet_is_off(self):
        _install_fake_meshtastic_package()
        import server

        with mock.patch.object(
                server, "send_fleet_target_to_bbs_nodes") as send_target, \
                mock.patch.object(
                    server, "send_node_version_to_bbs_nodes") as send_version:
            sent = server._advertise_fleet_state(
                {"fleet": {"group": GROUP, "updates": "off"}},
                ["!peer1"], _Iface())

        self.assertEqual(sent, 0)
        send_target.assert_not_called()
        send_version.assert_not_called()

    def test_server_advertises_before_applying_a_target(self):
        _install_fake_meshtastic_package()
        import server

        order = []
        with mock.patch.object(
                server, "_advertise_fleet_state_to_links",
                side_effect=lambda *_: order.append("advertise")), \
                mock.patch.object(
                    server, "_apply_fleet_target_if_due",
                    side_effect=lambda *_: order.append("apply") or True):
            applied = server._process_fleet_target(
                {"fleet": {"group": GROUP, "updates": "auto"}}, [])

        self.assertTrue(applied)
        self.assertEqual(order, ["advertise", "apply"])

    def test_server_records_a_failed_apply_for_status(self):
        _install_fake_meshtastic_package()
        import server

        target = {"commit": COMMIT, "version": "0.1.999"}
        writes = []
        with mock.patch.object(
                db_operations, "get_fleet_target", return_value=target), \
                mock.patch.object(
                    fleet_update, "apply_target",
                    return_value=(False, "fetch failed: offline")), \
                mock.patch.object(
                    fleet_update, "write_update_state",
                    side_effect=lambda state: writes.append(state)):
            applied = server._apply_fleet_target_if_due({
                "fleet": {"group": GROUP, "updates": "auto",
                          "pin_commit": ""}})

        self.assertFalse(applied)
        self.assertEqual(writes[0]["state"], "applying")
        self.assertEqual(writes[-1]["state"], "failed")
        self.assertIn("offline", writes[-1]["detail"])


if __name__ == "__main__":
    unittest.main()
