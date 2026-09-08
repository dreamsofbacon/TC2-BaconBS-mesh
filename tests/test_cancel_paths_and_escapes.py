"""Cancel paths that leaked to the main menu, and menus with no way out.

Every `!cancel`-style word is bang-prefixed (CANCEL_WORDS is all `!exit`,
`!cancel`, `!x`, `!0`), and every bang-prefixed message goes through
process_message's global-command router FIRST. It only stays local to the
flow the user is actually in when message_processing._TEXT_PROMPTS lists
the current (command, step) pair -- otherwise it is treated as a global
command, matches nothing, and falls to the catch-all: the main menu, with
no word said about what happened to the thing in progress.

A live beta-test pass found this for the device-unlink confirmation and
channel-comment composing specifically. Auditing the same table turned up
the identical gap for mail subject and body entry, both of which already
advertised "!cancel" in their own prompt text without it ever having
worked. This file drives every one of those through process_message
itself -- not the handler function in isolation -- because a handler that
checks is_cancel() correctly proves nothing if the router never lets the
message reach it.
"""

import os
import sqlite3
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import command_handlers as ch
import db_operations
import message_processing as mp


class _Case(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.sent = []
        self._real = ch.send_message
        ch.send_message = lambda text, sid, iface: self.sent.append(text)
        self.iface = types.SimpleNamespace(bbs_nodes=[], nodes={"!abc": {
            "num": 1234, "user": {"id": "!abc", "shortName": "bac"}}})
        self.addCleanup(self._restore)

    def _restore(self):
        ch.send_message = self._real
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    @property
    def last(self):
        return self.sent[-1]

    def _drive(self, state, message, sender_node_id="!abc"):
        ch.update_user_state(1234, state)
        mp.process_message(1234, message, self.iface,
                           sender_node_id=sender_node_id)


class UnlinkCancelTests(_Case):
    """ACCOUNT steps 5 and 6 -- the unlink flow's own picker and Y/N."""

    def test_cancel_while_picking_a_device_returns_to_linked_devices(self):
        self._drive(
            {"command": "ACCOUNT", "step": 5,
             "devices": [("!abc", "meshtastic", "2026-01-01"),
                         ("!def", "meshtastic", "2026-01-02")]},
            "!cancel")
        self.assertIn("Linked Devices", self.last)
        self.assertNotIn("Bacon BBS", self.last)
        self.assertEqual(ch.get_user_state(1234)["command"], "ACCOUNT")

    def test_cancel_at_the_confirm_prompt_returns_to_linked_devices(self):
        """This is the one the report actually hit: 'Unlink !def? [Y]es
        [N]o' followed by !cancel used to land on the main menu."""
        self._drive(
            {"command": "ACCOUNT", "step": 6, "unlink_node_id": "!def"},
            "!cancel")
        self.assertIn("Linked Devices", self.last)
        self.assertNotIn("Bacon BBS", self.last)

    def test_the_device_is_not_unlinked_by_cancelling(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!abc", account_id, "meshtastic")
        db_operations.link_node_to_account("!def", account_id, "meshtastic")
        self._drive(
            {"command": "ACCOUNT", "step": 6, "unlink_node_id": "!def"},
            "!cancel")
        remaining = {row[0] for row in db_operations.get_linked_nodes_detail(account_id)}
        self.assertIn("!def", remaining)


class ChannelCommentCancelTests(_Case):
    """CHANNEL_DIRECTORY step 7 -- comment composing."""

    def setUp(self):
        super().setUp()
        db_operations.get_db_connection().execute(
            "INSERT INTO channels (name, url, local_only) VALUES ('General', 'x', 0)")
        db_operations.get_db_connection().commit()
        self.channel_id = db_operations.get_db_connection().execute(
            "SELECT id FROM channels").fetchone()[0]

    def test_cancel_while_composing_returns_to_the_channel_not_main(self):
        self._drive(
            {"command": "CHANNEL_DIRECTORY", "step": 7,
             "channel_id": self.channel_id, "channel_name": "General",
             "comment_content": "half-written "},
            "!cancel")
        self.assertIn("View comments", self.last)
        self.assertNotIn("Bacon BBS", self.last)
        self.assertEqual(ch.get_user_state(1234),
                         {"command": "CHANNEL_DIRECTORY", "step": 6,
                          "channel_id": self.channel_id, "channel_name": "General"})

    def test_the_half_written_comment_is_not_posted(self):
        self._drive(
            {"command": "CHANNEL_DIRECTORY", "step": 7,
             "channel_id": self.channel_id, "channel_name": "General",
             "comment_content": "half-written "},
            "!cancel")
        self.assertEqual(db_operations.get_channel_comments(self.channel_id), [])

    def test_the_prompt_now_advertises_the_escape(self):
        """The report's other half of this finding: the prompt did not
        even say !cancel was an option."""
        self._drive(
            {"command": "CHANNEL_DIRECTORY", "step": 6,
             "channel_id": self.channel_id, "channel_name": "General"},
            "2")
        self.assertIn("!cancel", self.last.lower())


class MailCancelTests(_Case):
    """MAIL steps 5 (subject) and 7 (body) were suspected of having the
    same gap as the unlink/comment flows below, since neither's own elif
    branch checked is_cancel(). They didn't: handle_mail_steps has its own
    top-level guard ("if step in (3, 5, 7) and is_cancel(message)") that
    already covers exactly these steps, predating tonight entirely -- my
    first pass at this added a second, dead is_cancel check inside each
    branch, which a mutation test correctly flagged as having no effect
    once removed. Kept here as regression coverage for real, working
    behavior, not as evidence of a bug that turned out not to exist."""

    def test_cancel_at_the_subject_prompt_returns_to_the_mail_menu(self):
        self._drive(
            {"command": "MAIL", "step": 5, "recipient_id": "!def",
             "recipient_name": "Def"},
            "!cancel")
        self.assertIn("Mail Menu", self.last)
        self.assertNotIn("Bacon BBS", self.last)

    def test_cancel_while_writing_the_body_returns_to_the_mail_menu(self):
        self._drive(
            {"command": "MAIL", "step": 7, "recipient_id": "!def",
             "recipient_name": "Def", "subject": "Hi", "content": "partial\n"},
            "!cancel")
        self.assertIn("Mail Menu", self.last)
        self.assertNotIn("Bacon BBS", self.last)

    def test_a_cancelled_message_is_not_sent(self):
        db_operations.apply_synced_mail_relay_preference(
            "!def", True, "2026-01-01T00:00:00+00:00")
        self._drive(
            {"command": "MAIL", "step": 7, "recipient_id": "!def",
             "recipient_name": "Def", "subject": "Hi", "content": "partial\n"},
            "!cancel")
        self.assertEqual(db_operations.get_mail("!def"), [])

    def test_reply_composing_can_still_be_cancelled_too(self):
        """The reply flow reuses MAIL step 7; the fix must not be
        conditioned on is_reply."""
        db_operations.apply_synced_mail_relay_preference(
            "!def", True, "2026-01-01T00:00:00+00:00")
        unique_id = db_operations.add_mail(
            "!def", "Def", "!abc", "Subject", "Body", [], None)
        self._drive(
            {"command": "MAIL", "step": 7,
             "reply_to_mail_id": db_operations.get_mail("!abc")[0][0],
             "subject": "Re: Subject", "content": "partial reply\n"},
            "!cancel")
        self.assertIn("Mail Menu", self.last)
        self.assertEqual(db_operations.get_mail("!def"), [])


class BareQuickCommandTests(_Case):
    """!SM, !PB, !CB and !CHP all silently redrew the main menu when sent
    bare, because the dispatch only matched the ',,'-args form -- even
    though every one of these already has its own format-help text for
    exactly this input, sitting unreachable behind that comma."""

    def test_bare_cb_shows_its_own_format_instead_of_the_main_menu(self):
        self._drive({"command": "MAIN_MENU", "step": 1}, "!CB")
        self.assertIn("!CB,,board_name", self.last)
        self.assertNotIn("Bacon BBS", self.last)

    def test_bare_sm_shows_its_own_format(self):
        self._drive({"command": "MAIN_MENU", "step": 1}, "!SM")
        self.assertIn("!SM,,", self.last)

    def test_bare_pb_shows_its_own_format(self):
        self._drive({"command": "MAIN_MENU", "step": 1}, "!PB")
        self.assertIn("!PB,,", self.last)

    def test_bare_chp_shows_its_own_format(self):
        self._drive({"command": "MAIN_MENU", "step": 1}, "!CHP")
        self.assertIn("!CHP,,", self.last)

    def test_the_full_form_is_unaffected(self):
        board = ch.get_bulletin_boards()[0]
        db_operations.add_bulletin(board, "Someone", "Subj", "Body", [], self.iface)
        self._drive({"command": "MAIN_MENU", "step": 1}, f"!CB,,{board}")
        self.assertIn("Subj", "\n".join(self.sent))


class MailReadEscapeTests(_Case):
    """MAIL step 2 (Mail -> Read, numbered by raw id) and CHECK_MAIL step 1
    (!CM, numbered by list position) both offered no way back at their
    number prompt -- "0" is the back command everywhere else in this BBS
    and was not accepted as one at either."""

    def test_mail_step_2_treats_0_as_back_not_an_id(self):
        self._drive({"command": "MAIL", "step": 2}, "0")
        self.assertIn("Mail Menu", self.last)
        self.assertNotIn("Mail not found", self.last)
        self.assertNotIn("Bacon BBS", self.last)

    def test_mail_step_2_still_rejects_genuine_garbage(self):
        self._drive({"command": "MAIL", "step": 2}, "not-a-number")
        self.assertIn("Invalid message number", self.last)

    def test_mail_step_2_accepts_the_bang_form_of_cancel_too(self):
        """"0" bare bypasses the global router entirely (it is not
        "!"-prefixed), which is not evidence that the actual advertised
        cancel word works. Step 2 is not in _MAIL_TEXT_STEPS the way 3/5/7
        are, so this is the one MAIL step that genuinely depends on its own
        _TEXT_PROMPTS entry."""
        self._drive({"command": "MAIL", "step": 2}, "!cancel")
        self.assertIn("Mail Menu", self.last)
        self.assertNotIn("Bacon BBS", self.last)

    def test_check_mail_treats_0_as_back_not_a_position(self):
        self._drive(
            {"command": "CHECK_MAIL", "step": 1,
             "mail": [(1, "Sender", "Subj", "2026-01-01")]},
            "0")
        self.assertIn("Mail Menu", self.last)
        self.assertNotIn("Invalid message number", self.last)
        self.assertNotIn("Bacon BBS", self.last)

    def test_check_mail_still_rejects_an_out_of_range_position(self):
        self._drive(
            {"command": "CHECK_MAIL", "step": 1,
             "mail": [(1, "Sender", "Subj", "2026-01-01")]},
            "9")
        self.assertIn("Invalid message number", self.last)

    def test_check_mail_accepts_cancel_too(self):
        self._drive(
            {"command": "CHECK_MAIL", "step": 1,
             "mail": [(1, "Sender", "Subj", "2026-01-01")]},
            "!cancel")
        self.assertIn("Mail Menu", self.last)


if __name__ == "__main__":
    unittest.main()
