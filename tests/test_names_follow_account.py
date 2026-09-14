"""Names follow the account, not just what a device called itself.

Reported 2026-09-14: mail and chatter kept whatever name a radio had when the
message was written -- often just the tail of a device id -- even after the
person linked that radio to an account with a proper alias.

- Mail "From" shows the sending device's account alias as it is now, falling
  back to the stored name. The stored column is untouched (sync hashes).
- Public Chatter from a linked radio shows the device first, then the account.
"""
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import command_handlers
import db_operations

SENDER = "!0a1b2c3d"
RECIPIENT = "!0e0e0e0e"


class _Case(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def link(self, node_id, alias):
        account_id = db_operations.create_account()
        conn = db_operations.get_db_connection()
        if alias is not None:
            conn.execute("UPDATE accounts SET alias = ?, alias_normalized = ? WHERE account_id = ?",
                         (alias, db_operations.normalize_alias(alias), account_id))
            conn.commit()
        db_operations.link_node_to_account(node_id, account_id, "meshtastic")
        return account_id


class MailFromTests(_Case):
    def send(self):
        db_operations.add_mail(SENDER, "2c3d", RECIPIENT, "Hi", "Body", [], None)

    def test_the_account_alias_replaces_the_stored_name(self):
        self.send()
        self.link(SENDER, "Materva")
        (row,) = db_operations.get_mail(RECIPIENT)
        self.assertEqual(row[1], "Materva")
        self.assertEqual(db_operations.get_mail_content(row[0], RECIPIENT)[0], "Materva")
        self.assertEqual(db_operations.get_latest_mailbox_message(RECIPIENT)["sender_short_name"],
                         "Materva")

    def test_the_stored_name_is_kept_without_an_alias(self):
        self.send()
        self.assertEqual(db_operations.get_mail(RECIPIENT)[0][1], "2c3d")
        self.link(SENDER, None)
        self.assertEqual(db_operations.get_mail(RECIPIENT)[0][1], "2c3d")

    def test_the_stored_column_is_not_rewritten(self):
        self.send()
        self.link(SENDER, "Materva")
        db_operations.get_mail(RECIPIENT)
        stored = db_operations.get_db_connection().execute(
            "SELECT sender_short_name FROM mail").fetchone()[0]
        self.assertEqual(stored, "2c3d")

    def test_another_senders_alias_is_not_borrowed(self):
        self.send()
        self.link("!ffffffff", "Someone")
        self.assertEqual(db_operations.get_mail(RECIPIENT)[0][1], "2c3d")


class ChatterTests(_Case):
    def chatter(self, sender_node_id="!04058ac8", name="Pers"):
        now = datetime.now(timezone.utc)
        iso = lambda t: t.isoformat().replace("+00:00", "Z")
        db_operations.add_public_chatter(
            unique_id="c1", network="meshtastic", channel_index=0, channel_name="baconnet",
            sender_node_id=sender_node_id, sender_name=name, content="Yo",
            message_timestamp=iso(now), captured_at=iso(now), capture_node_id="!00000001",
            expires_at=iso(now + timedelta(hours=1)))
        return db_operations.get_public_chatter_history(hours=1)["entries"][0]

    def test_history_carries_the_linked_account(self):
        self.link("!04058ac8", "Materva")
        self.assertEqual(self.chatter()["sender_account_alias"], "Materva")

    def test_unlinked_radio_has_no_account(self):
        self.assertIsNone(self.chatter()["sender_account_alias"])

    def test_radio_feed_shows_device_then_account(self):
        self.link("!04058ac8", "Materva")
        first_line = command_handlers._chatter_entry_lines(self.chatter()).splitlines()[0]
        self.assertIn("Pers (Materva)", first_line)

    def test_radio_feed_does_not_repeat_a_matching_name(self):
        self.link("!04058ac8", "pers")
        first_line = command_handlers._chatter_entry_lines(self.chatter()).splitlines()[0]
        self.assertIn(" Pers ", first_line)
        self.assertNotIn("(", first_line)


if __name__ == "__main__":
    unittest.main()
