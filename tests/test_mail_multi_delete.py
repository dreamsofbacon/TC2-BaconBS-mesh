"""Several mail messages can be deleted at once from the radio/SSH mail lists.

Requested 2026-09-15. Both lists take D: Mail > Read (numbered by the
message ids it prints) and !CM (numbered by position). The user sends the
numbers -- "3,5", "3-7" or "ALL" -- sees what will go, and confirms with Y.
Each message is deleted through delete_mail, as the single Delete is.
"""
import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import bbs_emulator
import command_handlers
import db_operations
import ssh_auth
from command_handlers import parse_number_selection


class SelectionParsingTests(unittest.TestCase):
    def test_lists_and_ranges(self):
        self.assertEqual(parse_number_selection("3,5", {3, 4, 5}), ([3, 5], []))
        self.assertEqual(parse_number_selection("3 5", {3, 4, 5}), ([3, 5], []))
        self.assertEqual(parse_number_selection("7-3", {3, 4, 9}), ([3, 4], []))

    def test_ranges_pick_only_listed_numbers(self):
        """Mail ids have gaps, so 2-9 means the listed ones in between."""
        self.assertEqual(parse_number_selection("2-9", {2, 5, 12}), ([2, 5], []))

    def test_all(self):
        self.assertEqual(parse_number_selection("ALL", {4, 1}), ([1, 4], []))

    def test_what_is_not_listed_is_rejected(self):
        self.assertEqual(parse_number_selection("3,9,abc", {3}), ([3], ["9", "abc"]))
        self.assertEqual(parse_number_selection("20-30", {3}), ([], ["20-30"]))


class _Session(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        password_hash, salt = ssh_auth.hash_password("password-12345")
        self.account_id = db_operations.create_ssh_account("Tester", password_hash, salt)
        self.mailbox = f"ssh:{self.account_id}"
        self.ids = {}
        for subject in ("One", "Two", "Three"):
            db_operations.add_mail("!11112222", "Pers", self.mailbox, subject, "Body", [], None)
            self.ids[subject] = db_operations.get_db_connection().execute(
                "SELECT id, unique_id FROM mail WHERE subject = ?", (subject,)).fetchone()
        self.session = bbs_emulator.start_ssh_session(self.account_id, "Tester")
        self.addCleanup(self._close)

    def _close(self):
        bbs_emulator.end_session(self.session.token)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def say(self, *lines):
        text = ""
        for line in lines:
            chunks, error = self.session.send(line)
            self.assertIsNone(error)
            text = "".join(chunk["text"] for chunk in chunks)
        return text

    def subjects(self):
        return sorted(row[0] for row in db_operations.get_db_connection().execute(
            "SELECT subject FROM mail").fetchall())

    def open_mail_list(self):
        return self.say("hi", "2", "1", "1")


class MailReadListTests(_Session):
    def test_the_list_offers_d(self):
        self.assertIn("D to delete several", self.open_mail_list())

    def test_delete_two_by_their_ids(self):
        self.open_mail_list()
        self.assertIn("Send the numbers", self.say("D"))
        one, three = self.ids["One"][0], self.ids["Three"][0]
        confirm = self.say(f"{one},{three}")
        self.assertIn("Delete 2 messages?", confirm)
        self.assertIn("- One (Pers)", confirm)
        self.assertIn("- Three (Pers)", confirm)
        self.assertNotIn("Two", confirm)
        self.assertEqual(self.subjects(), ["One", "Three", "Two"], "deleted before confirming")
        done = self.say("Y")
        self.assertIn("Deleted 2 messages", done)
        self.assertIn("Mail Menu", done)
        self.assertEqual(self.subjects(), ["Two"])
        self.assertTrue(db_operations.has_sync_tombstone("mail", self.ids["One"][1]))
        self.assertTrue(db_operations.has_sync_tombstone("mail", self.ids["Three"][1]))

    def test_all(self):
        self.open_mail_list()
        self.assertIn("Delete 3 messages?", self.say("D", "all"))
        self.say("y")
        self.assertEqual(self.subjects(), [])

    def test_no_keeps_everything(self):
        self.open_mail_list()
        self.assertIn("Nothing deleted.", self.say("D", "all", "0"))
        self.assertEqual(self.subjects(), ["One", "Three", "Two"])

    def test_a_number_not_listed_asks_again(self):
        self.open_mail_list()
        reply = self.say("D", "99999")
        self.assertIn("Not in your list: 99999", reply)
        self.assertEqual(self.session.menu_state()["step"], command_handlers.MAIL_BULK_SELECT_STEP)

    def test_bang_cancel_backs_out(self):
        self.open_mail_list()
        self.assertIn("Mail Menu", self.say("D", "!cancel"))
        self.assertEqual(self.subjects(), ["One", "Three", "Two"])

    def test_anything_but_yes_at_the_confirm_asks_again(self):
        self.open_mail_list()
        self.assertIn("Reply Y to delete them", self.say("D", "all", "maybe"))
        self.assertEqual(self.subjects(), ["One", "Three", "Two"])

    def test_a_message_already_gone_is_not_counted(self):
        self.open_mail_list()
        self.say("D", "all")
        db_operations.delete_mail(self.ids["Two"][1], None, [], None)
        self.assertIn("Deleted 2 messages", self.say("Y"))
        self.assertEqual(self.subjects(), [])

    def test_the_preview_is_capped(self):
        for n in range(4, 9):
            db_operations.add_mail("!11112222", "Pers", self.mailbox, f"Extra {n}", "Body", [], None)
        self.open_mail_list()
        confirm = self.say("D", "all")
        self.assertIn("Delete 8 messages?", confirm)
        self.assertIn("...and 3 more", confirm)


class CheckMailListTests(_Session):
    def test_the_list_offers_d(self):
        self.assertIn("D to delete several", self.say("!CM"))

    def test_delete_by_position(self):
        self.say("!CM")
        confirm = self.say("D", "1-2")
        self.assertIn("Delete 2 messages?", confirm)
        self.say("Y")
        self.assertEqual(self.subjects(), ["Three"])

    def test_positions_are_not_ids(self):
        self.say("!CM")
        self.assertIn("Not in your list: 4", self.say("D", "4"))


if __name__ == "__main__":
    unittest.main()
