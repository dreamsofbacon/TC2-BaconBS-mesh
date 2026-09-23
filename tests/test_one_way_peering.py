"""A peer that never answers this node is reported, not endured.

Peering is per node: a node acts only on sync frames whose sender is in its
own bbs_nodes list. So a link configured on one side only looks healthy from
the side that did the configuring. The peer's broadcasts arrive, its counts
are recorded, the mismatch is noticed, and repair is requested every cycle --
into silence.

That is what the VPS node did for a day: 0 bulletins, 0 mail, 0 channels,
while asking three peers for them several times a minute. Every log line it
wrote said "mismatch; requesting targeted repair", which reads like work in
progress rather than a fault.

Two things make it detectable. A frame ADDRESSED to us proves the peer acts
on what we send, where a broadcast proves nothing -- a peer that ignores us
still sends those. And the silence has to last: three questions in three
seconds is a burst, not a misconfiguration.
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_operations
import radio_stubs

radio_stubs.install()
import server  # noqa: E402

PEER = "mqtt:baconbbsvt:Burlington-NNE"
OTHER = "mqtt:baconbbsvt:BaconBBS-VT2"
LATER = datetime.now(timezone.utc) + timedelta(hours=1)


class _Health(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def ask(self, peer=PEER, times=3):
        for _ in range(times):
            db_operations.record_peer_request(peer)

    def health(self, peer=PEER, now=LATER):
        for row in db_operations.peer_link_health(now=now):
            if row["peer_node_id"] == peer:
                return row
        return None


class CountingTests(_Health):
    def test_a_peer_we_have_not_asked_is_not_listed(self):
        self.assertEqual([], db_operations.peer_link_health())

    def test_one_unanswered_request_is_not_yet_a_fault(self):
        self.ask(times=1)
        self.assertFalse(self.health()["one_way"])

    def test_a_burst_of_questions_right_now_is_not_a_fault(self):
        """Three in three seconds is a busy cycle, not a broken link."""
        self.ask(times=5)
        self.assertFalse(self.health(now=datetime.now(timezone.utc))["one_way"])

    def test_questions_unanswered_for_an_hour_are(self):
        self.ask()
        row = self.health()
        self.assertTrue(row["one_way"])
        self.assertEqual(3, row["requests"])

    def test_a_reply_clears_it(self):
        self.ask(times=5)
        db_operations.record_peer_reply(PEER)
        self.assertFalse(self.health()["one_way"])

    def test_a_reply_before_any_request_is_fine(self):
        db_operations.record_peer_reply(PEER)
        self.assertFalse(self.health()["one_way"])

    def test_a_peer_that_answered_and_then_stopped_is_caught(self):
        """A peer list edited later leaves exactly this state, and the first
        version of this check would have called it healthy for ever."""
        db_operations.record_peer_reply(PEER)
        self.ask()
        self.assertTrue(self.health()["one_way"])

    def test_peers_are_tracked_separately(self):
        self.ask(PEER)
        self.ask(OTHER)
        db_operations.record_peer_reply(OTHER)
        self.assertTrue(self.health(PEER)["one_way"])
        self.assertFalse(self.health(OTHER)["one_way"])


class ReportingTests(_Health):
    def test_the_warning_names_the_peer_and_the_fix(self):
        self.ask()
        with self.assertLogs("root", level="WARNING") as logged:
            reported = server.report_one_way_peers("mqtt:baconbbsvt:bbs", now=LATER)
        self.assertEqual([PEER], reported)
        text = "\n".join(logged.output)
        self.assertIn(PEER, text)
        self.assertIn("bbs_nodes", text)
        self.assertIn("mqtt:baconbbsvt:bbs", text)

    def test_it_is_said_once_not_every_cycle(self):
        self.ask()
        server.report_one_way_peers("us", now=LATER)
        self.assertEqual([], server.report_one_way_peers("us", now=LATER))

    def test_it_is_said_again_if_the_peer_falls_silent_after_answering(self):
        self.ask()
        server.report_one_way_peers("us", now=LATER)
        db_operations.record_peer_reply(PEER)
        self.ask()
        self.assertEqual([PEER], server.report_one_way_peers("us", now=LATER))

    def test_a_healthy_fleet_says_nothing(self):
        for _ in range(3):
            db_operations.record_peer_request(PEER)
            db_operations.record_peer_reply(PEER)
        self.assertEqual([], server.report_one_way_peers("us", now=LATER))

    def test_a_broken_store_does_not_break_the_loop(self):
        with mock.patch.object(db_operations, "peer_link_health",
                               side_effect=RuntimeError("database is locked")):
            self.assertEqual([], server.report_one_way_peers("us"))


class WhatCountsAsAnAnswerTests(unittest.TestCase):
    """A broadcast is not an answer. This is the whole distinction."""

    def test_a_directed_frame_counts(self):
        import message_processing as mp
        self.assertFalse(mp._is_broadcast_destination(123456))

    def test_a_group_message_does_not(self):
        import message_processing as mp
        for value in (None, 0, "^all", 0xffffffff, "broadcast"):
            with self.subTest(value=value):
                self.assertTrue(mp._is_broadcast_destination(value))


if __name__ == "__main__":
    unittest.main()
