"""Five reported rough edges from the open list, fixed together.

- Quick Commands print the fields each command needs, instead of a bare
  "!SM,," whose real shape only appeared after getting it wrong.
- Deleting a message you are reading asks first. It sat next to Keep and
  Reply, and took effect on the keypress.
- A linking code that is not six digits says so, rather than sharing the
  "Invalid or already-used code" reply of a wrong or spent one.
- A lone "0" at the comment composer means Back, as it does everywhere
  else, instead of being stored as the comment's last line.
- !AU returns to the screen it was typed on, not always the Mail menu.
"""
import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import bbs_emulator
import command_handlers as ch
import db_operations
import ssh_auth

RADIO = "!0a1b2c3d"
SENDER = 1234


class _Case(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.sent = []
        patcher = mock.patch.object(ch, "send_message",
                                    side_effect=lambda text, *a, **k: self.sent.append(text))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.iface = types.SimpleNamespace(
            nodes={RADIO: {"num": SENDER, "user": {"shortName": "2c3d"}}},
            bbs_nodes=[], allowed_nodes=[])
        self.addCleanup(ch.update_user_state, SENDER, None)

    @property
    def last(self):
        return self.sent[-1] if self.sent else ""

    @property
    def text(self):
        return "\n".join(self.sent)


class QuickCommandTests(_Case):
    def test_each_command_shows_its_fields(self):
        ch.handle_quick_help_command(SENDER, self.iface)
        for expected in ("!SM,,to,,subject,,message", "!PB,,board,,subject,,text",
                         "!CB,,board", "!CHP,,name,,link"):
            self.assertIn(expected, self.text)


class LinkCodeTests(_Case):
    def code(self, text):
        self.sent.clear()
        ch._handle_submit_link_code(SENDER, self.iface, RADIO, text)
        return self.last if len(self.sent) < 2 else self.sent[0]

    def test_malformed_codes_say_what_a_code_looks_like(self):
        for bad in ("12345", "1234567", "abcdef", "12 34 56", ""):
            with self.subTest(code=bad):
                self.assertIn("six digits", self.code(bad))

    def test_a_well_formed_but_unknown_code_still_gets_the_usual_reply(self):
        self.assertIn("Invalid or already-used", self.code("123456"))


class CommentComposerTests(_Case):
    def setUp(self):
        super().setUp()
        self.channel_id = db_operations.add_channel("General", "https://example.invalid/#x")

    def compose(self, *lines, started="half-written "):
        state = {"command": "CHANNEL_DIRECTORY", "step": 7, "channel_id": self.channel_id,
                 "channel_name": "General", "comment_content": started}
        ch.update_user_state(SENDER, state)
        for line in lines:
            state = ch.get_user_state(SENDER)
            ch.handle_channel_directory_steps(SENDER, line, state["step"], state, self.iface)

    def test_a_lone_zero_cancels_instead_of_being_stored(self):
        self.compose("0")
        self.assertIn("Comment cancelled.", self.text)
        self.assertEqual(db_operations.get_channel_comments(self.channel_id), [])
        self.assertEqual(ch.get_user_state(SENDER)["step"], 6)

    def test_a_zero_inside_a_line_is_still_content(self):
        self.compose("meet at 0700", "END", started="")
        (comment,) = db_operations.get_channel_comments(self.channel_id)
        self.assertEqual(comment[3], "meet at 0700")

    def test_the_prompt_names_both_escapes(self):
        state = {"command": "CHANNEL_DIRECTORY", "step": 6, "channel_id": self.channel_id,
                 "channel_name": "General"}
        ch.update_user_state(SENDER, state)
        ch.handle_channel_directory_steps(SENDER, "2", 6, state, self.iface)
        self.assertIn("0", self.last)
        self.assertIn("!cancel", self.last)


class RelayDirectoryReturnTests(_Case):
    def setUp(self):
        super().setUp()
        account_id = db_operations.create_account()
        db_operations.get_db_connection().execute(
            "UPDATE accounts SET alias = 'Materva', alias_normalized = 'materva',"
            " mail_relay_enabled = 1 WHERE account_id = ?", (account_id,))
        db_operations.get_db_connection().commit()
        db_operations.link_node_to_account(RADIO, account_id, "meshtastic")

    def open_and_leave(self, origin):
        ch.update_user_state(SENDER, origin)
        ch.handle_active_users_command(SENDER, self.iface)
        state = ch.get_user_state(SENDER)
        ch.handle_mail_steps(SENDER, "0", state["step"], state, self.iface, [])
        return self.text

    def test_from_the_main_menu_it_goes_back_to_the_main_menu(self):
        text = self.open_and_leave({"command": "MAIN_MENU", "step": 1})
        self.assertNotIn("Mail Menu", text)
        self.assertEqual(ch.get_user_state(SENDER)["command"], "MAIN_MENU")

    def test_from_mail_it_still_goes_back_to_mail(self):
        text = self.open_and_leave({"command": "MAIL", "step": 1})
        self.assertIn("Mail Menu", text)

    def test_an_empty_directory_returns_to_the_caller_too(self):
        db_operations.get_db_connection().execute("UPDATE accounts SET mail_relay_enabled = 0")
        db_operations.get_db_connection().commit()
        ch.update_user_state(SENDER, {"command": "MAIN_MENU", "step": 1})
        ch.handle_active_users_command(SENDER, self.iface)
        self.assertNotIn("Mail Menu", self.text)


class MailDeleteConfirmTests(unittest.TestCase):
    """Driven through the real command path, from an SSH session."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        password_hash, salt = ssh_auth.hash_password("password-12345")
        self.account_id = db_operations.create_ssh_account("Tester", password_hash, salt)
        db_operations.add_mail("!11112222", "Pers", f"ssh:{self.account_id}",
                               "Hello", "Body", [], None)
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

    def count(self):
        return db_operations.get_db_connection().execute(
            "SELECT COUNT(*) FROM mail").fetchone()[0]

    def read_it(self, quick=False):
        return self.say("!CM", "1") if quick else self.say("hi", "2", "1", "1", "1")

    def test_the_reader_asks_before_deleting(self):
        self.read_it()
        self.assertIn('Delete "Hello"?', self.say("2"))
        self.assertEqual(self.count(), 1, "deleted before confirming")
        self.assertIn("has been deleted", self.say("Y"))
        self.assertEqual(self.count(), 0)

    def test_no_keeps_it(self):
        self.read_it()
        self.say("2")
        self.assertIn("kept in your inbox", self.say("0"))
        self.assertEqual(self.count(), 1)

    def test_the_quick_reader_asks_too(self):
        self.read_it(quick=True)
        self.assertIn('Delete "Hello"?', self.say("2"))
        self.say("y")
        self.assertEqual(self.count(), 0)

    def test_an_unclear_answer_asks_again(self):
        self.read_it()
        self.say("2")
        self.assertIn("Reply Y to delete it", self.say("maybe"))
        self.assertEqual(self.count(), 1)


if __name__ == "__main__":
    unittest.main()
