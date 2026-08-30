import sqlite3
import sys
import time
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import command_handlers


class _Interface:
    def __init__(self):
        self.nodes = {
            "!sender": {
                "num": 111,
                "user": {"shortName": "SEND", "longName": "Sender User"},
            }
        }
        self.bbs_nodes = []
        self.allowed_nodes = []


class MailRelayDatabaseTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        command_handlers.update_user_state(111, None)
        self.interface = _Interface()

    def tearDown(self):
        command_handlers.update_user_state(111, None)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _client(self, node_id, protocol, short_name, link_name="primary"):
        return {
            "link_name": link_name,
            "node_id": node_id,
            "node_num": node_id,
            "protocol": protocol,
            "short_name": short_name,
            "long_name": f"{short_name} User",
            "hw_model": "",
            "role": "CLIENT",
            "battery_level": None,
            "last_heard_epoch": None,
        }

    def _linked_account(self):
        account_id = db_operations.create_account()
        db_operations.set_account_alias(account_id, "Relay User")
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.link_node_to_account("bbbb2222", account_id, "meshcore")
        return account_id

    def test_linked_nodes_share_mailbox_without_exposing_it_to_other_nodes(self):
        self._linked_account()
        unique_id = db_operations.add_mail(
            "!sender", "Sender", "!aaa11111", "Hello", "Shared body", [], None
        )

        sibling_mail = db_operations.get_mail("bbbb2222")
        self.assertEqual([row[4] for row in sibling_mail], [unique_id])
        mail_id = sibling_mail[0][0]
        self.assertEqual(db_operations.get_mail_content(mail_id, "bbbb2222")[3], "Shared body")
        self.assertIsNone(db_operations.get_mail_content(mail_id, "!outsider"))

    def test_active_directory_groups_linked_protocols_and_keeps_unlinked_client(self):
        self._linked_account()
        db_operations.upsert_mesh_clients([
            self._client("!aaa11111", "Meshtastic", "MESH"),
            self._client("bbbb2222", "MeshCore", "CORE", "secondary"),
            self._client("mqtt:home:guest", "MQTT", "GST", "mqtt1"),
        ])

        directory = db_operations.get_active_mail_directory(900)

        self.assertEqual([entry["display_name"] for entry in directory], ["GST User", "Relay User"])
        account_entry = directory[1]
        self.assertEqual(account_entry["protocols"], ["MeshCore", "Meshtastic"])
        self.assertIn(account_entry["recipient_node_id"], {"!aaa11111", "bbbb2222"})

    def test_mail_snapshots_linked_targets_and_replay_is_idempotent(self):
        account_id = self._linked_account()
        unique_id = db_operations.add_mail(
            "!sender", "Sender", "!aaa11111", "Hello", "Body", [], None
        )
        inserted_again = db_operations.enqueue_mail_dm_deliveries(unique_id, settle_seconds=0)
        rows = db_operations.get_db_connection().execute(
            "SELECT recipient_account_id, target_node_id FROM mail_dm_deliveries ORDER BY target_node_id"
        ).fetchall()

        self.assertEqual(inserted_again, 0)
        self.assertEqual(rows, [(account_id, "!aaa11111"), (account_id, "bbbb2222")])

    def test_unknown_transit_recipient_is_not_queued(self):
        db_operations.add_mail(
            "!sender", "Sender", "!unknown", "Transit", "Body", [], None
        )
        count = db_operations.get_db_connection().execute(
            "SELECT COUNT(*) FROM mail_dm_deliveries"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_incomplete_mail_is_not_due_until_content_is_complete(self):
        self._linked_account()
        unique_id = db_operations.add_mail(
            "!sender", "Sender", "!aaa11111", "Chunked", "First", [], None
        )
        db_operations.apply_mail_expected_content_length(unique_id, 11)
        self.assertEqual(db_operations.get_due_mail_dm_deliveries(time.time() + 60), [])

        db_operations.append_mail_content(unique_id, 5, "Second")
        due = db_operations.get_due_mail_dm_deliveries(time.time() + 60)
        self.assertEqual(len(due), 2)
        self.assertEqual({row["content"] for row in due}, {"FirstSecond"})

    def test_delete_from_sibling_cleans_mail_and_delivery_rows(self):
        self._linked_account()
        unique_id = db_operations.add_mail(
            "!sender", "Sender", "!aaa11111", "Delete", "Body", [], None
        )

        db_operations.delete_mail(unique_id, "bbbb2222", [], None)

        self.assertEqual(db_operations.get_mail("!aaa11111"), [])
        count = db_operations.get_db_connection().execute(
            "SELECT COUNT(*) FROM mail_dm_deliveries WHERE mail_unique_id = ?", (unique_id,)
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_interactive_send_selects_from_active_directory_snapshot(self):
        db_operations.upsert_mesh_clients([
            self._client("!recipient", "Meshtastic", "RCPT")
        ])

        with mock.patch.object(command_handlers, "send_message"):
            command_handlers.handle_mail_command(111, self.interface)
            state = command_handlers.get_user_state(111)
            command_handlers.handle_mail_steps(111, "2", state["step"], state, self.interface, [])
            state = command_handlers.get_user_state(111)
            self.assertEqual(state["step"], 9)
            command_handlers.handle_mail_steps(111, "1", 9, state, self.interface, [])

        selected = command_handlers.get_user_state(111)
        self.assertEqual(selected["step"], 5)
        self.assertEqual(selected["recipient_id"], "!recipient")
        self.assertEqual(selected["recipient_name"], "RCPT User")

    def test_quick_send_resolves_account_alias_without_direct_notification(self):
        account_id = db_operations.create_account()
        db_operations.set_account_alias(account_id, "Cross Radio")
        db_operations.link_node_to_account("!recipient", account_id, "meshtastic")
        db_operations.link_node_to_account("core-recipient", account_id, "meshcore")
        db_operations.upsert_mesh_clients([
            self._client("core-recipient", "MeshCore", "CORE", "secondary")
        ])

        with mock.patch.object(command_handlers, "send_message") as send:
            command_handlers.handle_send_mail_command(
                111, "SM,,Cross Radio,,Subject,,Full body", self.interface, []
            )

        self.assertTrue(db_operations.get_mail("!recipient"))
        self.assertEqual([call.args[1] for call in send.call_args_list], [111])


if __name__ == "__main__":
    unittest.main()
