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
        db_operations.set_account_mail_relay(account_id, True)
        return account_id

    def test_mail_relay_defaults_off(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!recipient", account_id, "meshtastic")

        unique_id = db_operations.add_mail(
            "!sender", "Sender", "!recipient", "Stored", "Body", [], None
        )

        self.assertTrue(db_operations.get_mail("!recipient"))
        self.assertFalse(db_operations.get_mail_relay_preference("!recipient"))
        queued = db_operations.get_db_connection().execute(
            "SELECT COUNT(*) FROM mail_dm_deliveries WHERE mail_unique_id = ?", (unique_id,)
        ).fetchone()[0]
        self.assertEqual(queued, 0)

    def test_synced_preference_uses_newest_timestamp_for_linked_account(self):
        account_id = self._linked_account()

        changed = db_operations.apply_synced_mail_relay_preference(
            "!aaa11111", False, "2099-08-30T13:00:00+00:00"
        )
        stale = db_operations.apply_synced_mail_relay_preference(
            "bbbb2222", True, "2099-08-30T12:00:00+00:00"
        )

        self.assertTrue(changed)
        self.assertFalse(stale)
        self.assertFalse(db_operations.get_mail_relay_preference("!aaa11111"))
        self.assertFalse(db_operations.get_mail_relay_preference("bbbb2222"))

    def test_offline_opted_in_node_resolves_by_exact_id(self):
        db_operations.apply_synced_mail_relay_preference(
            "!offline", True, "2099-08-30T12:00:00+00:00"
        )

        recipient = command_handlers._resolve_mail_relay_recipient("!offline")

        self.assertIsNotNone(recipient)
        self.assertEqual(recipient["recipient_node_id"], "!offline")

    def test_relay_preference_frame_updates_local_consent(self):
        import message_processing

        message_processing.process_message(
            999,
            "RELAYPREF|!remote|1|2099-08-30T12:00:00+00:00",
            self.interface,
            is_sync_message=True,
            sender_node_id="!peer",
        )

        self.assertTrue(db_operations.get_mail_relay_preference("!remote"))

    def test_synced_preference_compares_timezone_offsets(self):
        self.assertTrue(db_operations.apply_synced_mail_relay_preference(
            "!remote", True, "2099-08-30T12:30:00+01:00"
        ))
        self.assertTrue(db_operations.apply_synced_mail_relay_preference(
            "!remote", False, "2099-08-30T12:00:00Z"
        ))
        self.assertFalse(db_operations.get_mail_relay_preference("!remote"))

    def test_malformed_synced_preference_timestamp_is_rejected(self):
        changed = db_operations.apply_synced_mail_relay_preference(
            "!remote", True, "not-a-timestamp"
        )

        self.assertFalse(changed)
        self.assertFalse(db_operations.get_mail_relay_preference("!remote"))

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
        db_operations.apply_synced_mail_relay_preference(
            "mqtt:home:guest", True, "2099-08-30T12:00:00+00:00"
        )

        directory = db_operations.get_active_mail_directory(900)

        self.assertEqual([entry["display_name"] for entry in directory], ["GST User", "Relay User"])
        account_entry = directory[1]
        self.assertEqual(account_entry["protocols"], ["MeshCore", "Meshtastic"])
        self.assertIn(account_entry["recipient_node_id"], {"!aaa11111", "bbbb2222"})

    def test_relay_directory_includes_enabled_account_without_preference_rows(self):
        account_id = self._linked_account()
        conn = db_operations.get_db_connection()
        conn.execute("DELETE FROM mail_relay_preferences")
        conn.commit()

        directory = db_operations.get_mail_relay_directory()

        self.assertEqual(len(directory), 1)
        self.assertEqual(directory[0]["account_id"], account_id)
        self.assertEqual(directory[0]["display_name"], "Relay User")
        self.assertEqual(directory[0]["node_ids"], ["!aaa11111", "bbbb2222"])

    def test_active_users_directory_includes_requesting_account(self):
        account_id = db_operations.create_account()
        db_operations.set_account_alias(account_id, "Sender Relay")
        db_operations.link_node_to_account("!sender", account_id, "meshtastic")
        db_operations.set_account_mail_relay(account_id, True)

        with mock.patch.object(command_handlers, "send_message") as send:
            command_handlers.handle_active_users_command(111, self.interface)

        message = send.call_args.args[0]
        self.assertIn("Sender Relay", message)
        self.assertNotIn("No users have opted", message)

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

    def test_latest_delivered_mail_uses_linked_scope_and_ignores_pending(self):
        self._linked_account()
        older = db_operations.add_mail(
            "!first", "First", "!aaa11111", "Older", "Body", [], None
        )
        newer = db_operations.add_mail(
            "!second", "Second", "!aaa11111", "Newer", "Body", [], None
        )
        conn = db_operations.get_db_connection()
        conn.execute(
            "UPDATE mail_dm_deliveries SET state = 'delivered', delivered_at = ? "
            "WHERE mail_unique_id = ? AND target_node_id = ?",
            ("2026-09-07T12:00:00+00:00", older, "bbbb2222"),
        )
        conn.execute(
            "UPDATE mail_dm_deliveries SET delivered_at = ? "
            "WHERE mail_unique_id = ? AND target_node_id = ?",
            ("2026-09-07T13:00:00+00:00", newer, "!aaa11111"),
        )
        conn.commit()

        latest = db_operations.get_latest_delivered_mail("!aaa11111")

        self.assertEqual(latest["unique_id"], older)
        self.assertEqual(latest["sender_id"], "!first")
        self.assertEqual(latest["subject"], "Older")

    def test_latest_delivered_mail_uses_delivery_id_to_break_timestamp_ties(self):
        self._linked_account()
        first = db_operations.add_mail(
            "!first", "First", "!aaa11111", "First", "Body", [], None
        )
        second = db_operations.add_mail(
            "!second", "Second", "!aaa11111", "Second", "Body", [], None
        )
        conn = db_operations.get_db_connection()
        for unique_id in (first, second):
            conn.execute(
                "UPDATE mail_dm_deliveries SET state = 'delivered', delivered_at = ? "
                "WHERE mail_unique_id = ? AND target_node_id = ?",
                ("2026-09-07T13:00:00+00:00", unique_id, "!aaa11111"),
            )
        conn.commit()

        latest = db_operations.get_latest_delivered_mail("!aaa11111")

        self.assertEqual(latest["unique_id"], second)

    def test_latest_delivered_mail_ignores_incomplete_mail(self):
        self._linked_account()
        complete = db_operations.add_mail(
            "!first", "First", "!aaa11111", "Complete", "Body", [], None
        )
        incomplete = db_operations.add_mail(
            "!second", "Second", "!aaa11111", "Incomplete", "Part", [], None
        )
        db_operations.apply_mail_expected_content_length(incomplete, 20)
        conn = db_operations.get_db_connection()
        conn.execute(
            "UPDATE mail_dm_deliveries SET state = 'delivered', delivered_at = ? "
            "WHERE mail_unique_id = ? AND target_node_id = ?",
            ("2026-09-07T12:00:00+00:00", complete, "!aaa11111"),
        )
        conn.execute(
            "UPDATE mail_dm_deliveries SET state = 'delivered', delivered_at = ? "
            "WHERE mail_unique_id = ? AND target_node_id = ?",
            ("2026-09-07T13:00:00+00:00", incomplete, "!aaa11111"),
        )
        conn.commit()

        latest = db_operations.get_latest_delivered_mail("!aaa11111")

        self.assertEqual(latest["unique_id"], complete)

    def test_quick_reply_opens_existing_composer_and_normalizes_subject(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!sender", account_id, "meshtastic")
        db_operations.set_account_mail_relay(account_id, True)
        unique_id = db_operations.add_mail(
            "!origin", "Origin", "!sender", "re: Hello", "Body", [], None
        )
        conn = db_operations.get_db_connection()
        conn.execute(
            "UPDATE mail_dm_deliveries SET state = 'delivered', delivered_at = ? "
            "WHERE mail_unique_id = ? AND target_node_id = ?",
            ("2026-09-07T13:00:00+00:00", unique_id, "!sender"),
        )
        conn.commit()

        with mock.patch.object(command_handlers, "send_message") as send:
            command_handlers.handle_quick_reply_command(111, self.interface)

        state = command_handlers.get_user_state(111)
        self.assertEqual(state["command"], "MAIL")
        self.assertEqual(state["step"], 7)
        self.assertEqual(state["subject"], "Re: Hello")
        self.assertEqual(state["reply_to_mail_id"], db_operations.get_mail("!sender")[0][0])
        self.assertIn("Origin", send.call_args.args[0])

    def test_quick_reply_without_delivery_directs_user_to_mailbox(self):
        with mock.patch.object(command_handlers, "send_message") as send:
            command_handlers.handle_quick_reply_command(111, self.interface)

        self.assertIn("!CM", send.call_args.args[0])
        self.assertIsNone(command_handlers.get_user_state(111))

    def test_quick_reply_stays_pinned_when_newer_mail_arrives(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!sender", account_id, "meshtastic")
        db_operations.set_account_mail_relay(account_id, True)
        db_operations.apply_synced_mail_relay_preference(
            "!first", True, "2026-09-07T10:00:00+00:00")
        first = db_operations.add_mail(
            "!first", "First", "!sender", "First subject", "Body", [], None
        )
        conn = db_operations.get_db_connection()
        conn.execute(
            "UPDATE mail_dm_deliveries SET state = 'delivered', delivered_at = ? "
            "WHERE mail_unique_id = ? AND target_node_id = ?",
            ("2026-09-07T12:00:00+00:00", first, "!sender"),
        )
        conn.commit()

        with mock.patch.object(command_handlers, "send_message"):
            command_handlers.handle_quick_reply_command(111, self.interface)
            pinned = command_handlers.get_user_state(111)

            second = db_operations.add_mail(
                "!second", "Second", "!sender", "Second subject", "Body", [], None
            )
            conn.execute(
                "UPDATE mail_dm_deliveries SET state = 'delivered', delivered_at = ? "
                "WHERE mail_unique_id = ? AND target_node_id = ?",
                ("2026-09-07T13:00:00+00:00", second, "!sender"),
            )
            conn.commit()

            command_handlers.handle_mail_steps(
                111, "Reply body", 7, pinned, self.interface, [])
            composing = command_handlers.get_user_state(111)
            command_handlers.handle_mail_steps(
                111, "END", 7, composing, self.interface, [])

        replies = db_operations.get_mail("!first")
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0][2], "Re: First subject")
        self.assertEqual(db_operations.get_mail("!second"), [])

    def test_mailbox_reply_also_collapses_repeated_subject_prefixes(self):
        state = {
            "command": "MAIL", "step": 4, "mail_id": 12,
            "sender": "Origin", "subject": "Re: RE: Hello",
        }

        with mock.patch.object(command_handlers, "send_message"):
            command_handlers.handle_mail_steps(
                111, "3", 4, state, self.interface, [])

        reply = command_handlers.get_user_state(111)
        self.assertEqual(reply["reply_to_mail_id"], 12)
        self.assertEqual(reply["subject"], "Re: Hello")

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
        db_operations.apply_synced_mail_relay_preference(
            "!recipient", True, "2099-08-30T12:00:00+00:00"
        )

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
        db_operations.set_account_mail_relay(account_id, True)
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


class RelayRetryBackoffTests(unittest.TestCase):
    """How long a failed relay delivery waits before trying again.

    Measured on the live fleet: MeshCore deliveries took 308 and 431
    minutes, Meshtastic under five. The difference was not the radio -- it
    was the backoff doubling all the way to an hour and staying there. A
    "send returned false" on MeshCore means the radio could not transmit
    just then; a peer that is genuinely absent takes the defer path and
    never increments attempts at all. Backing off an hour for a transient
    condition leaves mail sitting long after the radio recovered.
    """

    def setUp(self):
        import radio_stubs
        radio_stubs.install()
        import server
        self.server = server

    def test_the_wait_is_capped_in_minutes_not_hours(self):
        worst = max(self.server._mail_dm_retry_delay(a, 30) for a in range(20))
        self.assertLessEqual(worst, self.server.MAIL_DM_RETRY_MAX_SECONDS)
        self.assertLessEqual(worst, 600)

    def test_it_still_backs_off_at_first(self):
        """Retrying every few seconds forever would be its own problem."""
        first = self.server._mail_dm_retry_delay(0, 30)
        later = self.server._mail_dm_retry_delay(4, 30)
        self.assertGreater(later, first)

    def test_a_long_outage_no_longer_costs_hours(self):
        """The live case: thirteen consecutive failures."""
        total = sum(self.server._mail_dm_retry_delay(a, 30) for a in range(13))
        self.assertLess(total, 60 * 60)

    def test_recovery_is_noticed_within_the_cap(self):
        """The number that actually matters is how long a message waits
        after the radio comes back, which is one cap interval."""
        self.assertLessEqual(self.server._mail_dm_retry_delay(99, 30), 300)


class GameOutputLimitTests(unittest.TestCase):
    """A door's reply is capped by the transport it is going to.

    Flat 900 characters is about six chunks on a radio -- a fair ceiling,
    since one room description should not monopolise the channel. Over SSH,
    where a single message carries 8192 bytes, that same cap threw away
    most of a response and the player simply lost game text.
    """

    def _limit(self, max_text_bytes):
        import zork_port
        return zork_port.response_limit_for(
            types.SimpleNamespace(max_text_bytes=max_text_bytes))

    def test_radios_keep_exactly_the_old_ceiling(self):
        """No change on the transport the cap was chosen for."""
        import zork_port
        for mtb in (160, 220):
            with self.subTest(max_text_bytes=mtb):
                self.assertEqual(self._limit(mtb), zork_port.MAX_RESPONSE_CHARS)

    def test_ssh_is_no_longer_cut_to_a_radio_ceiling(self):
        import zork_port
        self.assertGreater(self._limit(8192), zork_port.MAX_RESPONSE_CHARS * 10)

    def test_an_unknown_transport_falls_back_to_the_floor(self):
        import zork_port
        self.assertEqual(zork_port.response_limit_for(None),
                         zork_port.MAX_RESPONSE_CHARS)

    def test_the_limit_is_actually_applied_to_output(self):
        """The cap has to reach read_output, not just be computed."""
        import queue as _queue
        import zork_port
        session = zork_port.ZorkSession.__new__(zork_port.ZorkSession)
        session.output_queue = _queue.Queue()
        session.last_output = ""
        session.output_queue.put("x" * 5000)
        short = session.read_output(settle_seconds=0.01, max_chars=100)
        self.assertIn("[Output truncated]", short)
        self.assertLess(len(short), 200)

        session.output_queue.put("y" * 5000)
        long = session.read_output(settle_seconds=0.01, max_chars=32768)
        self.assertNotIn("[Output truncated]", long)
