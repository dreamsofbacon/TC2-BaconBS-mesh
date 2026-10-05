"""Fewer steps to do the same thing, measured as a radio user sees it.

Each test drives the real command path through the emulator at the 220-byte
Meshtastic limit and counts what comes back. Reading the newest bulletin was
five round trips and ten packets; posting one ended on the BBS menu, three
screens from the board it went to; the quick-command list's [0] hung up.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import radio_stubs
radio_stubs.install()

import bbs_emulator
import db_operations
import utils


class _Session(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="steps-")
        config = os.path.join(self.dir, "config.ini")
        with open(config, "w", encoding="utf-8") as handle:
            handle.write("[interface]\ntype = none\n")
        env = mock.patch.dict(os.environ, {"BBS_CONFIG_PATH": config})
        env.start()
        self.addCleanup(env.stop)
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)
        self.me = bbs_emulator.start_session(max_text_bytes=220)
        self.addCleanup(bbs_emulator.end_session, self.me.token)
        self.say(self.me, "?")  # met the BBS: no longer first contact

    def _close(self):
        utils.user_states.clear()
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    @staticmethod
    def say(session, text):
        chunks, error = session.send(text)
        assert error is None, error
        return [c["text"] for c in chunks]

    def post(self, session, board, subject, body):
        self.say(session, f"!PB {board} {subject} | {body}")


class TypeAheadTests(_Session):
    def test_a_path_reaches_the_bulletin_in_one_message_and_one_packet(self):
        self.post(self.me, "general", "Hello world", "First post.")
        self.say(self.me, "0")
        self.say(self.me, "?")
        packets = self.say(self.me, "2 2 1 1 1")
        self.assertEqual(len(packets), 1)
        self.assertIn("Subject: Hello world", packets[0])

    def test_it_stops_at_a_text_prompt_and_drops_the_rest(self):
        """The keys after Post must not become the subject."""
        packets = self.say(self.me, "2 2 1 2 9 9")
        self.assertIn("subject", packets[-1].lower())
        state = utils.get_user_state(self.me.sender_id)
        self.assertEqual(state["command"], "BULLETIN_POST")

    def test_it_stops_at_an_invalid_key_and_says_so(self):
        packets = self.say(self.me, "2 9 1 1")
        self.assertIn("Invalid choice", packets[-1])
        self.assertIn("BBS Menu", packets[-1])

    def test_text_that_only_looks_like_keys_is_left_alone_at_a_prompt(self):
        self.say(self.me, "2 2 1 2")  # at the subject prompt
        self.say(self.me, "1 2")      # a subject, not a path
        state = utils.get_user_state(self.me.sender_id)
        self.assertEqual(state.get("subject"), "1 2")

    def test_a_strangers_first_message_is_not_walked(self):
        stranger = bbs_emulator.start_session(max_text_bytes=220)
        self.addCleanup(bbs_emulator.end_session, stranger.token)
        with mock.patch("message_processing.get_user_profile", return_value=None), \
                mock.patch("message_processing._walk") as walk:
            self.say(stranger, "2 2 1")
        walk.assert_not_called()


class ShortcutTests(_Session):
    def test_post_in_one_line_without_double_commas(self):
        packets = self.say(self.me, "!PB news Storm | High winds tonight.")
        self.assertIn("posted to News", " ".join(packets))
        rows = db_operations.get_bulletins("News")
        self.assertEqual([r[1] for r in rows], ["Storm"])

    def test_the_double_comma_form_still_works(self):
        self.say(self.me, "!PB,,News,,Old style,,Still fine.")
        self.assertEqual([r[1] for r in db_operations.get_bulletins("News")], ["Old style"])

    def test_a_board_by_first_letter(self):
        self.say(self.me, "!PB g Swap meet | Saturday.")
        self.assertEqual([r[1] for r in db_operations.get_bulletins("General")], ["Swap meet"])

    def test_an_unknown_board_is_refused_not_created(self):
        packets = self.say(self.me, "!PB nowhere Hi | there")
        self.assertIn("No board 'nowhere'", " ".join(packets))

    def test_b_with_a_board_opens_it(self):
        packets = self.say(self.me, "!B news")
        self.assertIn("News has 0 messages", packets[-1])
        self.assertEqual(utils.get_user_state(self.me.sender_id)["command"], "BULLETIN_ACTION")

    def test_bare_b_is_still_the_bbs_menu(self):
        self.assertIn("BBS Menu", self.say(self.me, "!B")[-1])


class FixTests(_Session):
    def test_back_from_the_quick_command_list_does_not_hang_up(self):
        self.say(self.me, "1")
        packets = self.say(self.me, "0")
        self.assertIn("Bacon BBS", packets[-1])
        self.assertNotIn("73", packets[-1])

    def test_a_menu_number_after_the_quick_list_opens_that_item(self):
        self.say(self.me, "1")
        self.assertIn("BBS Menu", self.say(self.me, "2")[-1])

    def test_after_posting_you_are_on_that_board_in_one_packet(self):
        self.say(self.me, "2 2 1 2")
        self.say(self.me, "Subject")
        self.say(self.me, "Body")
        packets = self.say(self.me, "END")
        self.assertEqual(len(packets), 1)
        self.assertIn("posted to General", packets[0])
        self.assertIn("[1]Read [2]Post", packets[0])

    def test_reading_from_a_list_stays_on_it(self):
        self.post(self.me, "general", "One", "a")
        self.post(self.me, "general", "Two", "b")
        self.say(self.me, "!CB general")
        self.say(self.me, "1")
        self.assertIn("Subject: Two", self.say(self.me, "2")[-1])


class WhatsNewTests(_Session):
    def setUp(self):
        super().setUp()
        self.other = bbs_emulator.start_session(max_text_bytes=220)
        self.addCleanup(bbs_emulator.end_session, self.other.token)
        self.say(self.other, "?")

    def come_back_later(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn = db_operations.get_db_connection()
        conn.execute("UPDATE user_profiles SET last_menu_at = ?", (past,))
        conn.execute("UPDATE bulletins SET received_at = ?",
                     (datetime.now(timezone.utc).isoformat(),))
        conn.commit()

    def test_new_posts_are_announced_on_return_in_the_menu_packet(self):
        self.post(self.other, "news", "Storm", "Winds.")
        self.come_back_later()
        packets = self.say(self.me, "?")
        self.assertEqual(len(packets), 1)
        self.assertIn("1 new post: News 1 [W]", packets[0])

    def test_w_lists_them_and_a_number_reads_one(self):
        self.post(self.other, "news", "Storm", "Winds.")
        self.post(self.other, "general", "Meet", "Saturday.")
        self.come_back_later()
        self.say(self.me, "?")
        listing = self.say(self.me, "w")[-1]
        self.assertIn("[1] News: Storm", listing)
        self.assertIn("[2] General: Meet", listing)
        self.assertIn("Subject: Meet", self.say(self.me, "2")[-1])

    def test_it_is_said_once(self):
        self.post(self.other, "news", "Storm", "Winds.")
        self.come_back_later()
        self.say(self.me, "?")
        self.assertNotIn("new post", self.say(self.me, "?")[-1])

    def test_your_own_posts_are_not_new_to_you(self):
        self.post(self.me, "news", "Mine", "Hi.")
        self.come_back_later()
        self.assertNotIn("new post", self.say(self.me, "?")[-1])

    def test_a_quick_hop_between_screens_is_not_an_arrival(self):
        self.post(self.other, "news", "Storm", "Winds.")
        self.assertNotIn("new post", self.say(self.me, "?")[-1])


if __name__ == "__main__":
    unittest.main()
