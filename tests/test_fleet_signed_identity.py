"""The BBS name and greeting, signed once with the fleet key.

Unsigned BBSID frames are adopted only from nodes named in each receiver's
[bbs] accept_identity_from, so a fleet had to set that list on every node,
including nodes someone else runs. A signed identity needs no list: whoever
holds the fleet key sets it, every node trusting that key adopts it, and
nobody else can -- the same authority, and the same checks, as an update.
"""
import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations
import fleet_update
import message_processing
import utils

from test_fleet_wire import GROUP, _FleetNode, _Iface
from test_fleet_web import API_TOKEN, _Node

GREETING = "Welcome to the Bacon BBS!\r\nAll of it is public.|Pipes survive."


def _identity(node, private=None, group=GROUP, issued_at=None, **values):
    values = values or {"name": "Bacon BBS", "welcome": GREETING}
    payload = fleet_update.build_identity_payload(
        group, node.key_id, issued_at=issued_at, **values)
    return fleet_update.encode_instruction(
        payload, fleet_update.sign_payload(payload, private or node.private))


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.private, public, self.key_id = fleet_update.generate_keypair()
        self.trusted = {self.key_id: public}

    def blob(self, **kw):
        return _identity(self, **kw)

    def test_a_signed_identity_verifies(self):
        payload = fleet_update.verify_identity_instruction(self.blob(), self.trusted, GROUP)
        self.assertEqual(payload["i"]["name"], "Bacon BBS")
        self.assertEqual(payload["i"]["welcome"], GREETING.replace("\r\n", "\n"))

    def test_it_is_recognised_as_an_identity_and_an_update_is_not(self):
        self.assertTrue(fleet_update.is_identity_instruction(self.blob()))
        update = fleet_update.build_payload(GROUP, "ab" * 20, "1", self.key_id)
        blob = fleet_update.encode_instruction(
            update, fleet_update.sign_payload(update, self.private))
        self.assertFalse(fleet_update.is_identity_instruction(blob))

    def test_anything_naming_a_commit_is_routed_as_an_update(self):
        """So the identity path can never swallow an update target."""
        payload = fleet_update.build_payload(GROUP, "ab" * 20, "1", self.key_id)
        payload["i"] = {"name": "X"}
        blob = fleet_update.encode_instruction(
            payload, fleet_update.sign_payload(payload, self.private))
        self.assertFalse(fleet_update.is_identity_instruction(blob))

    def test_an_identity_can_never_pass_as_an_update(self):
        """It names no commit, so it must not reach the updater."""
        with self.assertRaises(fleet_update.FleetVerificationError):
            fleet_update.verify_instruction(self.blob(), self.trusted, GROUP)

    def test_another_key_is_refused(self):
        other, _, _ = fleet_update.generate_keypair()
        with self.assertRaisesRegex(fleet_update.FleetVerificationError, "signature"):
            fleet_update.verify_identity_instruction(
                self.blob(private=other), self.trusted, GROUP)

    def test_an_edited_greeting_is_refused(self):
        payload, signature = fleet_update.decode_instruction(self.blob(), ())
        payload["i"]["welcome"] = "Hacked"
        forged = fleet_update.encode_instruction(payload, signature)
        with self.assertRaisesRegex(fleet_update.FleetVerificationError, "signature"):
            fleet_update.verify_identity_instruction(forged, self.trusted, GROUP)

    def test_another_group_is_refused(self):
        with self.assertRaisesRegex(fleet_update.FleetVerificationError, "group"):
            fleet_update.verify_identity_instruction(
                self.blob(group="elsewhere"), self.trusted, GROUP)

    def test_a_replay_is_refused(self):
        blob = self.blob(issued_at="2026-09-26T10:00:00Z")
        with self.assertRaisesRegex(fleet_update.FleetVerificationError, "replay"):
            fleet_update.verify_identity_instruction(
                blob, self.trusted, GROUP, last_issued_at="2026-09-26T10:00:00Z")

    def test_unknown_fields_are_refused(self):
        payload = fleet_update.build_identity_payload(GROUP, self.key_id, name="X")
        payload["i"]["trusted_keys"] = "fk000000:evil"
        blob = fleet_update.encode_instruction(
            payload, fleet_update.sign_payload(payload, self.private))
        with self.assertRaisesRegex(fleet_update.FleetVerificationError, "name and/or"):
            fleet_update.verify_identity_instruction(blob, self.trusted, GROUP)

    def test_no_trusted_key_means_nothing_is_adopted(self):
        with self.assertRaisesRegex(fleet_update.FleetVerificationError, "no trusted"):
            fleet_update.verify_identity_instruction(self.blob(), {}, GROUP)

    def test_it_must_say_something(self):
        with self.assertRaises(ValueError):
            fleet_update.build_identity_payload(GROUP, self.key_id)

    def test_a_long_greeting_still_fits_what_a_node_will_receive(self):
        """The live greeting is ~900 characters; a node reassembles 4096."""
        blob = self.blob(welcome="x" * 2500)
        self.assertLessEqual(len(blob), message_processing._FLEET_INSTRUCTION_MAX_CHARS)


class OverTheWireTests(unittest.TestCase):
    def test_it_is_adopted_with_no_accept_list(self):
        with _FleetNode() as node, \
             mock.patch.object(utils, "identity_sync_sources", return_value=set()):
            node.deliver(_identity(node))
            self.assertEqual(db_operations.get_fleet_identity("name")[0], "Bacon BBS")
            self.assertEqual(utils.get_fleet_welcome(), GREETING.replace("\r\n", "\n"))

    def test_it_overrides_a_newer_unsigned_value(self):
        with _FleetNode() as node:
            db_operations.set_fleet_identity("welcome", "old local text",
                                             "2099-01-01T00:00:00+00:00", "local")
            node.deliver(_identity(node, welcome="signed text"))
            self.assertEqual(db_operations.get_fleet_identity("welcome")[0], "signed text")

    def test_a_value_it_leaves_out_is_left_alone(self):
        with _FleetNode() as node:
            db_operations.set_fleet_identity("name", "Kept Name")
            node.deliver(_identity(node, welcome="only the greeting"))
            self.assertEqual(db_operations.get_fleet_identity("name")[0], "Kept Name")

    def test_it_is_passed_on_to_other_peers(self):
        with _FleetNode() as node:
            interface = _Iface()
            interface.bbs_nodes = ["peer", "other"]
            blob = _identity(node)
            with mock.patch.object(message_processing, "send_fleet_target_to_bbs_nodes",
                                   return_value=1) as relay:
                node.deliver(blob, sender="peer", interface=interface)
            relay.assert_called_once_with(blob, ["other"], interface)

    def test_a_replay_is_not_passed_on(self):
        with _FleetNode() as node:
            interface = _Iface()
            interface.bbs_nodes = ["other"]
            blob = _identity(node)
            node.deliver(blob, interface=interface)
            with mock.patch.object(message_processing,
                                   "send_fleet_target_to_bbs_nodes") as relay:
                node.deliver(blob, interface=interface)
            relay.assert_not_called()

    def test_an_older_identity_does_not_undo_a_newer_one(self):
        with _FleetNode() as node:
            node.deliver(_identity(node, welcome="new", issued_at="2026-09-26T12:00:00Z"))
            node.deliver(_identity(node, welcome="old", issued_at="2026-09-26T11:00:00Z"))
            self.assertEqual(db_operations.get_fleet_identity("welcome")[0], "new")

    def test_a_forgery_is_not_adopted(self):
        with _FleetNode() as node:
            attacker, _, _ = fleet_update.generate_keypair()
            node.deliver(_identity(node, private=attacker, welcome="pwned"))
            self.assertIsNone(db_operations.get_fleet_identity("welcome")[0])

    def test_it_does_not_touch_the_update_target_or_trigger(self):
        with _FleetNode() as node:
            node.deliver(_identity(node))
            self.assertIsNone(node.target())
            self.assertFalse(__import__("os").path.exists(node.trigger))


class ReadvertiseTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        db_operations._identity_last_full_sweep.clear()
        db_operations._advertised_identity.clear()
        self.addCleanup(db_operations._identity_last_full_sweep.clear)
        self.addCleanup(db_operations._advertised_identity.clear)
        self.private, _, self.key_id = fleet_update.generate_keypair()

    def tearDown(self):
        db_operations.thread_local.connection.close()
        del db_operations.thread_local.connection

    def test_storage_refuses_one_no_newer_than_it_holds(self):
        """Behind the verifier's own replay check, not instead of it."""
        newer = fleet_update.build_identity_payload(
            GROUP, self.key_id, welcome="new", issued_at="2026-09-26T12:00:00Z")
        older = fleet_update.build_identity_payload(
            GROUP, self.key_id, welcome="old", issued_at="2026-09-26T11:00:00Z")
        self.assertTrue(db_operations.adopt_signed_fleet_identity(newer, "blob-new"))
        self.assertFalse(db_operations.adopt_signed_fleet_identity(older, "blob-old"))
        self.assertEqual(db_operations.get_fleet_identity("welcome")[0], "new")

    def test_the_sweep_sends_it_to_every_peer(self):
        """A node that was offline when it was pasted still gets it."""
        blob = _identity(self)
        payload, _ = fleet_update.decode_instruction(blob, ())
        db_operations.adopt_signed_fleet_identity(payload, blob)
        with mock.patch.object(utils, "send_fleet_identity_to_bbs_nodes", return_value=1), \
             mock.patch.object(utils, "send_fleet_target_to_bbs_nodes",
                               return_value=1) as send:
            db_operations.sync_fleet_identity_to_nodes(["a", "b"], object())
        self.assertEqual([c.args[:2] for c in send.call_args_list],
                         [(blob, ["a"]), (blob, ["b"])])


class PasteBoxTests(unittest.TestCase):
    def test_pasting_it_adopts_it(self):
        with _Node() as node:
            page = node.paste(_identity(node)).get_data(as_text=True)
            self.assertIn("Name and greeting adopted", page)
            self.assertEqual(db_operations.get_fleet_identity("name")[0], "Bacon BBS")
            self.assertIsNone(db_operations.get_fleet_target(GROUP))

    def test_the_api_takes_it_too(self):
        with _Node() as node:
            response = node.client.post(
                "/api/fleet/apply", json={"instruction": _identity(node)},
                headers={"Authorization": f"Bearer {API_TOKEN}"})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.get_json()["identity"]["keys"], ["name", "welcome"])

    def test_a_forged_paste_is_rejected(self):
        with _Node() as node:
            attacker, _, _ = fleet_update.generate_keypair()
            page = node.paste(_identity(node, private=attacker)).get_data(as_text=True)
            self.assertIn("rejected", page)
            self.assertIsNone(db_operations.get_fleet_identity("welcome")[0])


if __name__ == "__main__":
    unittest.main()
