"""Tests for Phase 4: account-aware ACL resolution at the two real
end-user-facing check sites (urgent board post permission, API gateway
requester authorization). db_operations.account_authorized() itself is
covered in tests/test_user_accounts.py; these tests confirm the two call
sites actually use it correctly, including the dual-radio union case.
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
import gateway


class _Iface:
    def __init__(self, allowed_nodes=None, nodes=None):
        self.sent_texts = []
        self.bbs_nodes = []
        self.allowed_nodes = allowed_nodes or []
        self.nodes = nodes or {}

    def sendText(self, text, destinationId, wantAck, wantResponse):
        self.sent_texts.append((destinationId, text))


def _sent(mock_send_message):
    return [call.args[0] for call in mock_send_message.call_args_list]


class UrgentBoardAclTests(unittest.TestCase):
    """_urgent_board_allow_lists() (config.ini reading) is patched directly
    in each test rather than exercised through real files, both to avoid
    any chance of picking up a real local config.ini and because the
    config-reading mechanics aren't what Phase 4 is about -- the union
    logic at the handle_bb_steps call site is."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _post_to_urgent(self, sender_id, interface, allow_lists):
        state = {"board": "Urgent"}
        with mock.patch.object(command_handlers, "send_message") as sm, \
             mock.patch.object(command_handlers, "_urgent_board_allow_lists", return_value=allow_lists):
            command_handlers.handle_bb_steps(sender_id, "p", 2, state, interface, bbs_nodes=[])
        return _sent(sm)

    def test_no_allow_list_configured_permits_everyone(self):
        interface = _Iface(allowed_nodes=[], nodes={"!aaa11111": {"num": 111}})
        messages = self._post_to_urgent(111, interface, allow_lists=[[], []])
        self.assertTrue(any("What is the subject" in m for m in messages))

    def test_node_directly_on_list_permitted(self):
        interface = _Iface(allowed_nodes=["!aaa11111"], nodes={"!aaa11111": {"num": 111}})
        messages = self._post_to_urgent(111, interface, allow_lists=[["!aaa11111"], []])
        self.assertTrue(any("What is the subject" in m for m in messages))

    def test_node_not_on_list_denied(self):
        interface = _Iface(allowed_nodes=["!aaa11111"], nodes={"!zzz99999": {"num": 999}})
        messages = self._post_to_urgent(999, interface, allow_lists=[["!aaa11111"], []])
        self.assertTrue(any("don't have permission" in m for m in messages))

    def test_sibling_node_on_other_radios_allow_list_is_authorized(self):
        """The dual-radio nuance: the Meshtastic node is on [allow_list],
        the MeshCore sibling is on NEITHER interface.allowed_nodes (since
        it's a different radio) NOR [allow_list] -- only [allow_list2].
        Must still be authorized via the account link + the union of both
        configured lists, not just interface.allowed_nodes."""
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.link_node_to_account("7e18ca9d30a1", account_id, "meshcore")

        # The MeshCore node posts through the SECONDARY interface, whose own
        # .allowed_nodes (from [allow_list2]) does NOT include it either --
        # authorization must come purely from the account link, using the
        # union [allow_list=[!aaa11111], allow_list2=[]].
        interface = _Iface(allowed_nodes=[], nodes={"7e18ca9d30a1": {"num": 222}})
        messages = self._post_to_urgent(222, interface, allow_lists=[["!aaa11111"], []])
        self.assertTrue(any("What is the subject" in m for m in messages), messages)

    def test_unrelated_node_still_denied_even_with_accounts_in_play(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        interface = _Iface(allowed_nodes=[], nodes={"!zzz99999": {"num": 999}})
        messages = self._post_to_urgent(999, interface, allow_lists=[["!aaa11111"], []])
        self.assertTrue(any("don't have permission" in m for m in messages))


class GatewayAclTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_unlinked_node_behavior_unchanged(self):
        with mock.patch.object(gateway, "gateway_allowed_nodes", lambda: ["!hand"]):
            self.assertTrue(gateway.is_requester_authorized("!hand", []))
            self.assertFalse(gateway.is_requester_authorized("!other", ["!other"]))

    def test_open_gateway_permits_everyone_regardless_of_accounts(self):
        with mock.patch.object(gateway, "gateway_allowed_nodes", lambda: []):
            self.assertTrue(gateway.is_requester_authorized("!anyone", []))

    def test_sibling_node_inherits_gateway_authorization(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.link_node_to_account("7e18ca9d30a1", account_id, "meshcore")
        with mock.patch.object(gateway, "gateway_allowed_nodes", lambda: ["!aaa11111"]):
            self.assertTrue(gateway.is_requester_authorized("!aaa11111"))
            self.assertTrue(gateway.is_requester_authorized("7e18ca9d30a1"))

    def test_unrelated_account_not_authorized_via_gateway(self):
        account_a = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_a, "meshtastic")
        account_b = db_operations.create_account()
        db_operations.link_node_to_account("!bbb22222", account_b, "meshtastic")
        with mock.patch.object(gateway, "gateway_allowed_nodes", lambda: ["!aaa11111"]):
            self.assertFalse(gateway.is_requester_authorized("!bbb22222"))


if __name__ == "__main__":
    unittest.main()
