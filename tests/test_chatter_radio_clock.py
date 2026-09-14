"""A radio with the wrong time must not silence Public Chatter.

bbs.local's Meshtastic radio came back on 2026-09-12 about 48 days slow. A
Meshtastic packet's rxTime is the radio's own clock, so every broadcast it
heard looked seven weeks old, fell outside the 168-hour retention window and
was dropped without a log line -- baconnet and LongFast alike, for two days.
"""
import logging
import sqlite3
import sys
import types
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import public_chatter
from public_chatter import normalize_broadcast


class RadioClockTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        public_chatter._last_clock_warning.clear()
        self.interface = SimpleNamespace(
            protocol_name="Meshtastic",
            public_chatter_channels=[0, 1],
            public_chatter_capture_node_id="!bbslocal",
            channel_names={0: "LongFast", 1: "baconnet"},
        )
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def tearDown(self):
        db_operations.thread_local.connection.close()
        del db_operations.thread_local.connection

    def packet(self, rx_time, **changes):
        packet = {
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"Yo yo yo"},
            "fromId": "!04058ac8",
            "to": 0xFFFFFFFF,
            "channel": 1,
            "id": 987654,
            "rxTime": int(rx_time.timestamp()),
        }
        packet.update(changes)
        return packet

    def observe(self, rx_time, **changes):
        return normalize_broadcast(self.packet(rx_time, **changes),
                                   self.interface, captured_at=self.now)

    def test_a_radio_48_days_slow_still_captures_baconnet(self):
        """The live failure."""
        observation = self.observe(self.now - timedelta(days=48))
        self.assertIsNotNone(observation)
        self.assertEqual(observation["channel_name"], "baconnet")
        self.assertTrue(db_operations.add_public_chatter(**observation))

    def test_it_is_stamped_with_this_nodes_time_and_lives_a_full_week(self):
        observation = self.observe(self.now - timedelta(days=48))
        self.assertEqual(observation["message_timestamp"], public_chatter._iso(self.now))
        self.assertEqual(observation["expires_at"],
                         public_chatter._iso(self.now + timedelta(hours=168)))

    def test_a_radio_far_ahead_is_corrected_too(self):
        observation = self.observe(self.now + timedelta(days=400))
        self.assertEqual(observation["message_timestamp"], public_chatter._iso(self.now))

    def test_a_correct_radio_clock_is_kept(self):
        rx = self.now - timedelta(seconds=13)
        self.assertEqual(self.observe(rx)["message_timestamp"], public_chatter._iso(rx))

    def test_a_genuine_backlog_keeps_its_older_time(self):
        """A radio hands a returning client the packets it held. Those times
        are right, and hours old is not a broken clock."""
        rx = self.now - timedelta(hours=20)
        self.assertEqual(self.observe(rx)["message_timestamp"], public_chatter._iso(rx))

    def test_the_boundaries(self):
        inside_past = self.now - timedelta(days=1)
        inside_ahead = self.now + timedelta(minutes=10)
        self.assertEqual(self.observe(inside_past)["message_timestamp"],
                         public_chatter._iso(inside_past))
        self.assertEqual(self.observe(inside_ahead)["message_timestamp"],
                         public_chatter._iso(inside_ahead))
        self.assertEqual(self.observe(inside_past - timedelta(seconds=1))["message_timestamp"],
                         public_chatter._iso(self.now))
        self.assertEqual(self.observe(inside_ahead + timedelta(seconds=1))["message_timestamp"],
                         public_chatter._iso(self.now))

    def test_the_message_id_ignores_the_correction(self):
        """Every node hearing the packet must agree on its id. A MeshCore
        channel message has no native id, so its id hashes the sender's time
        -- which each node receives identically, however wrong."""
        interface = SimpleNamespace(protocol_name="MeshCore", public_chatter_channels=[0],
                                    public_chatter_capture_node_id="mc")
        sent = self.now - timedelta(days=48)
        packet = {"decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"brown dog: hi"},
                  "to": 0, "channel": 0, "sender_timestamp": int(sent.timestamp())}
        first = normalize_broadcast(dict(packet), interface, captured_at=self.now)
        later = normalize_broadcast(dict(packet), interface,
                                    captured_at=self.now + timedelta(seconds=40))
        self.assertEqual(first["unique_id"], later["unique_id"])
        self.assertEqual(first["unique_id"], public_chatter.make_message_id(
            "meshcore", 0, None, None, sent, "brown dog: hi"))

    def test_the_operator_is_told_once_an_hour(self):
        with self.assertLogs(level=logging.WARNING) as logs:
            for _ in range(5):
                self.observe(self.now - timedelta(days=48))
            logging.warning("sentinel")
        clock = [line for line in logs.output if "clock" in line]
        self.assertEqual(len(clock), 1)
        self.assertIn("48.0 days behind", clock[0])

    def test_the_first_warning_comes_on_a_machine_just_booted(self):
        """monotonic() counts from boot. CI runners and a freshly rebooted Pi
        are under an hour old, and the first warning used to wait for it."""
        with mock.patch.object(public_chatter.time, "monotonic", return_value=120.0):
            with self.assertLogs(level=logging.WARNING) as logs:
                self.observe(self.now - timedelta(days=48))
                logging.warning("sentinel")
        self.assertEqual(len([line for line in logs.output if "clock" in line]), 1)

    def test_it_warns_again_after_an_hour(self):
        with self.assertLogs(level=logging.WARNING) as logs:
            for uptime in (120.0, 3719.0, 3720.0):
                with mock.patch.object(public_chatter.time, "monotonic", return_value=uptime):
                    self.observe(self.now - timedelta(days=48))
        self.assertEqual(len([line for line in logs.output if "clock" in line]), 2)

    def test_a_correct_clock_says_nothing(self):
        with self.assertLogs(level=logging.WARNING) as logs:
            self.observe(self.now - timedelta(seconds=5))
            logging.warning("sentinel")
        self.assertEqual([line for line in logs.output if "clock" in line], [])


if __name__ == "__main__":
    unittest.main()
