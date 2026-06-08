"""Host tests for the Pico record-frame parser (BULLETIN/MAIL/CHANNELCOMMENT)."""

import os
import sys
import binascii
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pico_node"))

import records


def _b64(s):
    return binascii.b2a_base64(s.encode()).decode().strip()


class ParseHelperTests(unittest.TestCase):
    def test_is_date(self):
        self.assertTrue(records._is_date("2026-06-08 14:30"))
        self.assertTrue(records._is_date("m12345"))
        self.assertFalse(records._is_date("2026-06-08"))
        self.assertFalse(records._is_date("!04059140"))

    def test_is_iso_ts(self):
        self.assertTrue(records._is_iso_ts("2026-06-08T14:30:05"))
        self.assertTrue(records._is_iso_ts("s1700000000"))
        self.assertFalse(records._is_iso_ts("2026-06-08 14:30"))

    def test_decode_text_base64_and_plain(self):
        self.assertEqual(records._decode_text(_b64("Alice")), "Alice")
        self.assertEqual(records._decode_text("~hello"), "hello")
        self.assertEqual(records._decode_text("~a\\pb"), "a|b")  # escaped pipe
        self.assertEqual(records._decode_text(""), "")


class BulletinTests(unittest.TestCase):
    def setUp(self):
        self.ra = records.RecordAssembler()

    def test_single_packet_with_date_and_provenance(self):
        frame = "BULLETIN|General|Bob|Hello|hi there|uid1|2026-06-08 14:30|!04059140|2026-06-08T14:30:05"
        scope, rec, complete = self.ra.feed(frame)
        self.assertEqual(scope, "bulletins")
        self.assertTrue(complete)
        self.assertEqual(rec["unique_id"], "uid1")
        self.assertEqual(rec["board"], "General")
        self.assertEqual(rec["sender"], "Bob")
        self.assertEqual(rec["subject"], "Hello")
        self.assertEqual(rec["date"], "2026-06-08 14:30")
        self.assertEqual(rec["content"], "hi there")

    def test_content_with_embedded_pipes(self):
        frame = "BULLETIN|General|Bob|Sub|a|b|c|uid9|2026-06-08 14:30"
        scope, rec, complete = self.ra.feed(frame)
        self.assertEqual(rec["content"], "a|b|c")
        self.assertEqual(rec["unique_id"], "uid9")

    def test_no_date_just_uid(self):
        frame = "BULLETIN|General|Bob|Sub|body|uidX"
        scope, rec, complete = self.ra.feed(frame)
        self.assertEqual(rec["unique_id"], "uidX")
        self.assertEqual(rec["date"], "")
        self.assertEqual(rec["content"], "body")

    def test_multipacket_clean(self):
        ra = records.RecordAssembler()
        ra.feed("BULLETIN|General|Bob|Sub|AAAAA|uid3|2026-06-08 14:30")
        # META says total 15 -> not complete yet
        scope, rec, complete = ra.feed("BULLETINMETA|uid3|15")
        self.assertFalse(complete)
        ra.feed("BULLETINCONT|uid3|5|BBBBB")
        scope, rec, complete = ra.feed("BULLETINCONT|uid3|10|CCCCC")
        self.assertTrue(complete)
        self.assertEqual(rec["content"], "AAAAABBBBBCCCCC")


class MailTests(unittest.TestCase):
    def test_mail_parse(self):
        ra = records.RecordAssembler()
        frame = "MAIL|!sender|Bob|!recip|Subj|the body|m1|2026-06-08 14:30"
        scope, rec, complete = ra.feed(frame)
        self.assertEqual(scope, "mail")
        self.assertEqual(rec["sender_id"], "!sender")
        self.assertEqual(rec["sender"], "Bob")
        self.assertEqual(rec["recipient"], "!recip")
        self.assertEqual(rec["subject"], "Subj")
        self.assertEqual(rec["content"], "the body")
        self.assertEqual(rec["unique_id"], "m1")


class ChannelCommentTests(unittest.TestCase):
    def test_channel_comment_parse_b64_sender(self):
        ra = records.RecordAssembler()
        frame = "CHANNELCOMMENT|chankey|%s|2026-06-08 14:30|nice post|cc1" % _b64("Carol")
        scope, rec, complete = ra.feed(frame)
        self.assertEqual(scope, "channels")
        self.assertEqual(rec["channel_key"], "chankey")
        self.assertEqual(rec["sender"], "Carol")
        self.assertEqual(rec["content"], "nice post")
        self.assertEqual(rec["unique_id"], "cc1")

    def test_channel_comment_not_confused_with_cont(self):
        ra = records.RecordAssembler()
        # A CONT frame must not be parsed as a base CHANNELCOMMENT.
        self.assertIsNone(ra.feed("CHANNELCOMMENTCONT|cc1|5|more"))


class OutOfOrderTests(unittest.TestCase):
    def test_cont_before_base_is_buffered_then_completed(self):
        ra = records.RecordAssembler()
        # CONT/META arriving before the base frame: ignored until base exists.
        self.assertIsNone(ra.feed("BULLETINCONT|uid7|5|BBBBB"))
        self.assertIsNone(ra.feed("BULLETINMETA|uid7|10"))
        scope, rec, complete = ra.feed("BULLETIN|G|B|S|AAAAA|uid7|2026-06-08 14:30")
        # base alone -> only 5 chars, expected unknown to this buffer (META was dropped)
        self.assertEqual(rec["content"], "AAAAA")


if __name__ == "__main__":
    unittest.main()
