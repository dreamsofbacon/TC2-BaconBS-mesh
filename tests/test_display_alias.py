"""Tests for Phase 3: account-wide display alias resolution.

resolve_display_name() falls through to exactly get_node_short_name's
existing behavior for any node with no account link -- these tests assert
that explicitly, plus that a linked account's alias is picked up correctly,
including the case where the alias-only node isn't present in the live
interface.nodes table at all (proves alias resolution doesn't depend on
live radio state, unlike get_node_short_name).
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
import utils


class _Iface:
    def __init__(self, nodes=None):
        self.sent_texts = []
        self.bbs_nodes = []
        self.allowed_nodes = []
        self.nodes = nodes or {}

    def sendText(self, text, destinationId, wantAck, wantResponse):
        self.sent_texts.append((destinationId, text))


class ResolveDisplayNameTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_unlinked_node_falls_through_to_short_name(self):
        interface = _Iface(nodes={"!aaa11111": {"user": {"shortName": "CALL"}}})
        self.assertEqual(utils.resolve_display_name("!aaa11111", interface), "CALL")

    def test_unlinked_unknown_node_returns_none(self):
        interface = _Iface(nodes={})
        self.assertIsNone(utils.resolve_display_name("!zzz99999", interface))

    def test_linked_but_no_alias_set_falls_through(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        interface = _Iface(nodes={"!aaa11111": {"user": {"shortName": "CALL"}}})
        self.assertEqual(utils.resolve_display_name("!aaa11111", interface), "CALL")

    def test_linked_with_alias_returns_alias(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.set_account_alias(account_id, "BaconFan")
        interface = _Iface(nodes={"!aaa11111": {"user": {"shortName": "CALL"}}})
        self.assertEqual(utils.resolve_display_name("!aaa11111", interface), "BaconFan")

    def test_alias_works_even_when_node_absent_from_live_interface_table(self):
        """Alias resolution doesn't depend on live interface.nodes presence,
        unlike get_node_short_name -- a real advantage when the sending
        node has aged out of the local node cache."""
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.set_account_alias(account_id, "BaconFan")
        interface = _Iface(nodes={})  # node not present at all
        self.assertEqual(utils.resolve_display_name("!aaa11111", interface), "BaconFan")
        # But get_node_short_name itself still returns None, confirming the
        # fallback path genuinely is the plain existing behavior.
        self.assertIsNone(utils.get_node_short_name("!aaa11111", interface))

    def test_sibling_node_shares_the_same_alias(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.link_node_to_account("7e18ca9d30a1", account_id, "meshcore")
        db_operations.set_account_alias(account_id, "BaconFan")
        interface = _Iface(nodes={})
        self.assertEqual(utils.resolve_display_name("!aaa11111", interface), "BaconFan")
        self.assertEqual(utils.resolve_display_name("7e18ca9d30a1", interface), "BaconFan")


class BulletinAndMailAliasIntegrationTests(unittest.TestCase):
    """Drives the actual quick-command handlers end-to-end, asserting the
    STORED sender_short_name reflects the account alias for a linked node
    and the raw short_name for an unlinked one -- i.e. the six call-site
    swaps in command_handlers.py genuinely took effect."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.interface = _Iface(nodes={
            "!aaa11111": {"num": 111, "user": {"shortName": "CALL", "longName": "Caller"}},
        })

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_quick_post_bulletin_uses_short_name_when_unlinked(self):
        with mock.patch.object(command_handlers, "send_message"):
            command_handlers.handle_post_bulletin_command(
                111, "PB,,General,,Subject,,Hello world", self.interface, bbs_nodes=[]
            )
        rows = db_operations.get_bulletins("General")
        self.assertTrue(rows)
        self.assertEqual(rows[-1][2], "CALL")

    def test_quick_post_bulletin_uses_alias_when_linked(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.set_account_alias(account_id, "BaconFan")
        with mock.patch.object(command_handlers, "send_message"):
            command_handlers.handle_post_bulletin_command(
                111, "PB,,General,,Subject,,Hello world", self.interface, bbs_nodes=[]
            )
        rows = db_operations.get_bulletins("General")
        self.assertTrue(rows)
        self.assertEqual(rows[-1][2], "BaconFan")

    def test_quick_send_mail_uses_alias_when_linked(self):
        self.interface.nodes["!bbb22222"] = {"num": 222, "user": {"shortName": "DEST", "longName": "Destination"}}
        db_operations.upsert_mesh_clients([{
            "link_name": "primary", "node_id": "!bbb22222", "node_num": 222,
            "protocol": "Meshtastic", "short_name": "DEST", "long_name": "Destination",
            "hw_model": "", "role": "CLIENT", "battery_level": None,
            "last_heard_epoch": None,
        }])
        db_operations.apply_synced_mail_relay_preference(
            "!bbb22222", True, "2099-08-30T12:00:00+00:00"
        )
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.set_account_alias(account_id, "BaconFan")
        with mock.patch.object(command_handlers, "send_message"):
            command_handlers.handle_send_mail_command(
                111, "SM,,DEST,,Subject,,Hello there", self.interface, bbs_nodes=[]
            )
        mail = db_operations.get_mail("!bbb22222")
        self.assertTrue(mail)
        self.assertEqual(mail[-1][1], "BaconFan")


if __name__ == "__main__":
    unittest.main()
