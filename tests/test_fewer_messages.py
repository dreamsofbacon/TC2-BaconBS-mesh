"""Fewer packets for the same answer.

Two things cost a radio user packets for nothing. A tip under a menu on
every visit pushed the main menu, the board list and Games past one packet
each time, to repeat what the reader already knew. And one message from a
user often got several sends back -- a heading, then a one-line list -- each
its own packet, its own 2-second pace and its own chance to collide on a
multi-hop mesh. Tips are now said once per screen, and a reply is packed.

The packing must never touch anything but the user being answered: sync
frames between nodes, a mail notice to someone else, an answer arriving
later on a worker thread.
"""
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import command_handlers as ch
import db_operations
import utils


class _Radio:
    max_text_bytes = 220
    is_low_latency = True
    protocol_name = "test"
    nodes = {}

    def __init__(self):
        self.packets = []

    def sendText(self, text=None, destinationId=None, **kwargs):
        self.packets.append((destinationId, text))
        return mock.Mock(id=len(self.packets))


class PackChunksTests(unittest.TestCase):
    def test_small_replies_share_a_packet(self):
        self.assertEqual(utils.pack_chunks(["Pick one:", "[1] Hello"], 220),
                         ["Pick one:\n[1] Hello"])

    def test_nothing_is_merged_past_the_limit(self):
        a, b = "a" * 150, "b" * 100
        self.assertEqual(utils.pack_chunks([a, b], 220), [a, b])

    def test_a_long_reply_is_split_exactly_as_before(self):
        long = ("This is a sentence. " * 30).strip()
        self.assertEqual(utils.pack_chunks([long], 220),
                         utils._split_into_chunks(long, max_len=220))

    def test_a_short_reply_joins_the_tail_of_a_long_one(self):
        long = ("This is a sentence. " * 15).strip()
        packed = utils.pack_chunks([long, "Done."], 220)
        self.assertEqual(len(packed), len(utils._split_into_chunks(long, max_len=220)))
        self.assertTrue(packed[-1].endswith("\nDone."))
        self.assertTrue(all(len(p.encode()) <= 220 for p in packed))


class ReplyBatchTests(unittest.TestCase):
    def setUp(self):
        self.radio = _Radio()

    def test_the_user_gets_one_packet_at_the_end(self):
        with utils.reply_batch(42, self.radio):
            utils.send_message("Select a bulletin:", 42, self.radio)
            utils.send_message("[1] Hello world", 42, self.radio)
            self.assertEqual(self.radio.packets, [], "nothing goes out mid-handler")
        self.assertEqual(self.radio.packets, [(42, "Select a bulletin:\n[1] Hello world")])

    def test_someone_else_is_sent_to_at_once(self):
        """A mail notice to the recipient is not the sender's reply."""
        with utils.reply_batch(42, self.radio):
            utils.send_message("You have mail", 7, self.radio)
            self.assertEqual(self.radio.packets, [(7, "You have mail")])
            utils.send_message("Sent.", 42, self.radio)
        self.assertEqual(self.radio.packets[-1], (42, "Sent."))

    def test_another_interface_is_not_held(self):
        other = _Radio()
        with utils.reply_batch(42, self.radio):
            utils.send_message("over MQTT", 42, other)
        self.assertEqual(other.packets, [(42, "over MQTT")])
        self.assertEqual(self.radio.packets, [])

    def test_a_worker_thread_is_not_held(self):
        """Ask Nomad's answer arrives on its own thread, after the handler."""
        with utils.reply_batch(42, self.radio):
            t = threading.Thread(target=utils.send_message,
                                 args=("answer", 42, self.radio))
            t.start()
            t.join()
            self.assertEqual(self.radio.packets, [(42, "answer")])

    def test_what_was_said_still_goes_when_the_handler_raises(self):
        with self.assertRaises(RuntimeError):
            with utils.reply_batch(42, self.radio):
                utils.send_message("Working on it", 42, self.radio)
                raise RuntimeError("boom")
        self.assertEqual(self.radio.packets, [(42, "Working on it")])

    def test_a_nested_batch_leaves_sending_to_the_outer_one(self):
        with utils.reply_batch(42, self.radio):
            with utils.reply_batch(42, self.radio):
                utils.send_message("inner", 42, self.radio)
            self.assertEqual(self.radio.packets, [])
            utils.send_message("outer", 42, self.radio)
        self.assertEqual(self.radio.packets, [(42, "inner\nouter")])


class ProcessMessageTests(unittest.TestCase):
    def test_sync_frames_are_never_batched(self):
        import message_processing as mp
        with mock.patch.object(mp, "_process_message") as inner, \
                mock.patch.object(utils, "reply_batch") as batch:
            mp.process_message(1, "BULLETIN|x", object(), is_sync_message=True)
        batch.assert_not_called()
        inner.assert_called_once()

    def test_a_user_message_is_answered_in_a_batch(self):
        import message_processing as mp
        seen = {}

        def inner(*args):
            seen["batch"] = getattr(utils._reply_batch, "active", None)
        with mock.patch.object(mp, "_process_message", side_effect=inner):
            mp.process_message(1, "?", object())
        self.assertIsNotNone(seen["batch"])
        self.assertIsNone(getattr(utils._reply_batch, "active", None))


class TipsOnceTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        db_operations.auto_upsert_user_profile(1234, "bac", "bacon")
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_a_tip_is_shown_on_the_first_visit_only(self):
        self.assertEqual(ch.help_tip(1234, "main"), ch.HELP_TIPS["main"])
        self.assertEqual(ch.help_tip(1234, "main"), "")

    def test_each_screen_has_its_own_first_visit(self):
        ch.help_tip(1234, "main")
        self.assertEqual(ch.help_tip(1234, "bbs"), ch.HELP_TIPS["bbs"])

    def test_each_person_has_their_own_first_visit(self):
        ch.help_tip(1234, "main")
        self.assertEqual(ch.help_tip(5678, "main"), ch.HELP_TIPS["main"])

    def test_switching_tips_back_on_shows_them_again(self):
        ch.help_tip(1234, "main")
        db_operations.set_help_tips_enabled(1234, False)
        db_operations.set_help_tips_enabled(1234, True)
        self.assertEqual(ch.help_tip(1234, "main"), ch.HELP_TIPS["main"])

    def test_tips_off_does_not_use_up_the_first_visit(self):
        db_operations.set_help_tips_enabled(1234, False)
        self.assertEqual(ch.help_tip(1234, "main"), "")
        db_operations.set_help_tips_enabled(1234, True)
        self.assertEqual(ch.help_tip(1234, "main"), ch.HELP_TIPS["main"])

    def test_the_games_save_warning_is_said_once(self):
        sent = []
        warning = "Warning: this node does not sync game saves."
        with mock.patch.object(ch, "send_message", side_effect=lambda t, s, i: sent.append(t)), \
                mock.patch.object(ch, "get_zork_save_sync_notice", return_value=warning):
            ch.handle_games_command(1234, mock.Mock(bbs_nodes=[], nodes={}))
            ch.handle_games_command(1234, mock.Mock(bbs_nodes=[], nodes={}))
        self.assertIn(warning, sent[0])
        self.assertNotIn(warning, sent[1])


if __name__ == "__main__":
    unittest.main()
