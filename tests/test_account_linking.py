"""Tests for the multi-device account DM flow (Phase 2): the nested
Profile > Linked Devices menu in command_handlers.py, and its wiring into
message_processing.py's state-machine routing.

send_message is mocked throughout (matching the established pattern in
tests/test_zork_save_sync.py) rather than driven through a real interface,
since utils.send_message has a real time.sleep(2) per chunk.
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
import command_handlers
import message_processing


class _Iface:
    def __init__(self):
        self.sent_texts = []
        self.bbs_nodes = []
        self.allowed_nodes = []
        self.subscriber_nodes = []
        self.nodes = {}

    def sendText(self, text, destinationId, wantAck, wantResponse):
        self.sent_texts.append((destinationId, text))


def _sent(mock_send_message):
    return [call.args[0] for call in mock_send_message.call_args_list]


class AccountMenuTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        command_handlers.update_user_state(1, None)
        command_handlers.update_user_state(2, None)
        self.interface = _Iface()

    def tearDown(self):
        command_handlers.update_user_state(1, None)
        command_handlers.update_user_state(2, None)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_menu_command_shows_options_and_sets_state(self):
        with mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_account_command(1, self.interface)
        self.assertIn("Linked Devices", _sent(sm)[0])
        self.assertEqual(command_handlers.get_user_state(1), {"command": "ACCOUNT", "step": 1})

    def test_missing_sender_node_id_is_a_safe_no_op(self):
        command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 1})
        with mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_account_steps(1, "1", self.interface, sender_node_id=None)
        self.assertIn("Couldn't verify", _sent(sm)[0])
        self.assertIsNone(command_handlers.get_user_state(1))

    def test_request_code_bootstraps_an_account(self):
        command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 1})
        with mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_account_steps(1, "1", self.interface, sender_node_id="!aaa11111")
        messages = _sent(sm)
        self.assertTrue(any("Your link code:" in m for m in messages))
        account_id = db_operations.get_account_id_for_node("!aaa11111")
        self.assertIsNotNone(account_id)

    def test_full_link_flow_between_two_devices(self):
        # Device A requests a code.
        command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 1})
        with mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_account_steps(1, "1", self.interface, sender_node_id="!aaa11111")
        code_msg = next(m for m in _sent(sm) if "Your link code:" in m)
        code = code_msg.split("Your link code:")[1].strip().split()[0]
        self.assertEqual(len(code), 6)

        # Device B enters the code.
        command_handlers.update_user_state(2, {"command": "ACCOUNT", "step": 2})
        with mock.patch.object(command_handlers, "send_message") as sm2:
            command_handlers.handle_account_steps(2, code, self.interface, sender_node_id="7e18ca9d30a1")
        self.assertTrue(any("linked successfully" in m for m in _sent(sm2)))

        account_a = db_operations.get_account_id_for_node("!aaa11111")
        account_b = db_operations.get_account_id_for_node("7e18ca9d30a1")
        self.assertEqual(account_a, account_b)

    def test_bogus_code_does_not_link(self):
        command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 2})
        with mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_account_steps(1, "999999", self.interface, sender_node_id="!aaa11111")
        self.assertTrue(any("Invalid" in m for m in _sent(sm)))
        self.assertIsNone(db_operations.get_account_id_for_node("!aaa11111"))

    def test_request_code_rate_limited(self):
        for _ in range(3):
            command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 1})
            with mock.patch.object(command_handlers, "send_message"):
                command_handlers.handle_account_steps(1, "1", self.interface, sender_node_id="!aaa11111")
        command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 1})
        with mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_account_steps(1, "1", self.interface, sender_node_id="!aaa11111")
        self.assertTrue(any("Too many link-code requests" in m for m in _sent(sm)))

    def test_submit_code_rate_limited(self):
        for _ in range(5):
            command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 2})
            with mock.patch.object(command_handlers, "send_message"):
                command_handlers.handle_account_steps(1, "000000", self.interface, sender_node_id="!aaa11111")
        command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 2})
        with mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_account_steps(1, "000000", self.interface, sender_node_id="!aaa11111")
        self.assertTrue(any("Too many attempts" in m for m in _sent(sm)))

    def test_list_devices_shows_alias_and_marks_this_device(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.link_node_to_account("7e18ca9d30a1", account_id, "meshcore")
        db_operations.set_account_alias(account_id, "BaconFan")
        command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 1})
        with mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_account_steps(1, "3", self.interface, sender_node_id="!aaa11111")
        listing = next(m for m in _sent(sm) if "Account alias" in m)
        self.assertIn("BaconFan", listing)
        self.assertIn("(this device)", listing)
        self.assertIn("7e18ca9d30a1", listing)

    def test_set_alias_updates_account(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 4})
        with mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_account_steps(1, "Skipper", self.interface, sender_node_id="!aaa11111")
        self.assertTrue(any('Alias set to "Skipper"' in m for m in _sent(sm)))
        self.assertEqual(db_operations.get_account_alias(account_id), "Skipper")

    def test_unlink_with_only_one_device_refuses(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 1})
        with mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_account_steps(1, "5", self.interface, sender_node_id="!aaa11111")
        self.assertTrue(any("nothing to unlink" in m for m in _sent(sm)))

    def test_unlink_full_confirm_flow(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.link_node_to_account("7e18ca9d30a1", account_id, "meshcore")

        command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 1})
        with mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_account_steps(1, "5", self.interface, sender_node_id="!aaa11111")
        self.assertTrue(any("Reply with the number" in m for m in _sent(sm)))
        state = command_handlers.get_user_state(1)
        self.assertEqual(state["step"], 5)

        # Pick the meshcore sibling (whichever index it landed at).
        idx = next(i for i, d in enumerate(state["devices"]) if d[0] == "7e18ca9d30a1")
        with mock.patch.object(command_handlers, "send_message") as sm2:
            command_handlers.handle_account_steps(1, str(idx + 1), self.interface, sender_node_id="!aaa11111")
        self.assertTrue(any("Unlink 7e18ca9d30a1?" in m for m in _sent(sm2)))

        with mock.patch.object(command_handlers, "send_message") as sm3:
            command_handlers.handle_account_steps(1, "Y", self.interface, sender_node_id="!aaa11111")
        self.assertTrue(any("Unlinked 7e18ca9d30a1" in m for m in _sent(sm3)))
        self.assertIsNone(db_operations.get_account_id_for_node("7e18ca9d30a1"))

    def test_unlink_confirm_no_cancels(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.link_node_to_account("7e18ca9d30a1", account_id, "meshcore")
        command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 6, "unlink_node_id": "7e18ca9d30a1"})
        with mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_account_steps(1, "N", self.interface, sender_node_id="!aaa11111")
        self.assertTrue(any("Cancelled" in m for m in _sent(sm)))
        self.assertEqual(db_operations.get_account_id_for_node("7e18ca9d30a1"), account_id)

    def test_profile_menu_offers_linked_devices_entry(self):
        with mock.patch.object(command_handlers, "get_user_profile", return_value=(
            "1", "CALL", "Caller", "2026-01-01", "2026-01-01", 3, "",
        )):
            with mock.patch.object(command_handlers, "send_message") as sm:
                command_handlers.handle_profile_command(1, self.interface)
        self.assertIn("Linked Devices", _sent(sm)[0])

    def test_profile_step_2_routes_into_account_menu(self):
        command_handlers.update_user_state(1, {"command": "PROFILE", "step": 1})
        with mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_profile_steps(1, "2", self.interface)
        self.assertIn("Linked Devices", _sent(sm)[0])
        self.assertEqual(command_handlers.get_user_state(1), {"command": "ACCOUNT", "step": 1})

    def test_profile_relay_opt_in_bootstraps_account(self):
        command_handlers.update_user_state(1, {"command": "PROFILE", "step": 1})
        with mock.patch.object(command_handlers, "send_message"):
            command_handlers.handle_profile_steps(
                1, "3", self.interface, sender_node_id="!aaa11111"
            )
            state = command_handlers.get_user_state(1)
            self.assertEqual(state["step"], 3)
            command_handlers.handle_profile_steps(
                1, "Y", self.interface, sender_node_id="!aaa11111"
            )

        self.assertIsNotNone(db_operations.get_account_id_for_node("!aaa11111"))
        self.assertTrue(db_operations.get_mail_relay_preference("!aaa11111"))


class AccountRoutingIntegrationTests(unittest.TestCase):
    """Confirms message_processing.py's routing genuinely threads
    sender_node_id (string) through to handle_account_steps, and that two
    node ids which would collide on their NUMERIC sender_id (mimicking
    meshcore_interface.py's _node_num truncation) are still resolved as
    fully distinct identities end-to-end through process_message()."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        command_handlers.update_user_state(1234, None)
        self.interface = _Iface()

    def tearDown(self):
        command_handlers.update_user_state(1234, None)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_process_message_routes_account_state_with_string_node_id(self):
        command_handlers.update_user_state(1234, {"command": "ACCOUNT", "step": 1})
        with mock.patch.object(command_handlers, "send_message") as sm:
            message_processing.process_message(
                1234, "1", self.interface,
                is_sync_message=False, sender_node_id="!04d2ff00",
            )
        self.assertTrue(any("Your link code:" in m for m in _sent(sm)))
        # Linked under the STRING id, not "1234" (the numeric sender_id).
        self.assertIsNotNone(db_operations.get_account_id_for_node("!04d2ff00"))
        self.assertIsNone(db_operations.get_account_id_for_node("1234"))

    def test_numerically_colliding_sender_ids_resolve_independently(self):
        """Same numeric sender_id (1234) used for both calls -- as would
        happen if a MeshCore node's synthesized _node_num happened to
        collide with a Meshtastic node's real number -- but different
        STRING sender_node_id each time. The two must never be conflated."""
        command_handlers.update_user_state(1234, {"command": "ACCOUNT", "step": 1})
        with mock.patch.object(command_handlers, "send_message"):
            message_processing.process_message(
                1234, "1", self.interface,
                is_sync_message=False, sender_node_id="!04d2ff00",
            )
        account_a = db_operations.get_account_id_for_node("!04d2ff00")
        self.assertIsNotNone(account_a)

        # Same numeric sender_id, but a DIFFERENT string node id (the
        # MeshCore side of a hypothetical collision) -- state is shared
        # (user_states is numeric-keyed, a pre-existing, out-of-scope
        # limitation), but the resulting account link must still be scoped
        # to the correct string id, not bleed into node "1234".
        command_handlers.update_user_state(1234, {"command": "ACCOUNT", "step": 1})
        with mock.patch.object(command_handlers, "send_message"):
            message_processing.process_message(
                1234, "1", self.interface,
                is_sync_message=False, sender_node_id="04d2ff00aabbccdd",
            )
        account_b = db_operations.get_account_id_for_node("04d2ff00aabbccdd")
        self.assertIsNotNone(account_b)
        self.assertNotEqual(account_a, account_b)


if __name__ == "__main__":
    unittest.main()
