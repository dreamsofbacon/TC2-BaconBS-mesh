"""A record originating on an MQTT node was parsed with its fields shifted.

The optional trailing source_node_id on BULLETIN / MAIL / CHANNELCOMMENT
frames was recognised by a leading '!' -- a Meshtastic radio id. An MQTT
node is mqtt:<topic>:<name>, so the field was never stripped, the parse
shifted by one, and the SOURCE NODE ID was written into unique_id.

Two nodes then disagreed about that record's manifest key permanently: a
drift no amount of hash repair can close, because each keeps offering a key
the other has never agreed to. It showed up live as a bulletin stored with
unique_id 'mqtt:baconbbsvt:Burlington-NNE'.
"""
import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations
import message_processing


class _Iface:
    def __init__(self):
        self.sent = []
        self.bbs_nodes = []
        self.allowed_nodes = []
        self.nodes = {}

    def sendText(self, text=None, destinationId=None, wantAck=True, wantResponse=False, **kwargs):
        self.sent.append((destinationId, text))
        return types.SimpleNamespace(id=len(self.sent))


class SourceNodeIdRecognitionTests(unittest.TestCase):
    def test_a_meshtastic_radio_id_is_recognised(self):
        self.assertTrue(message_processing._looks_like_source_node_id("!04058ac8"))

    def test_an_mqtt_node_id_is_recognised(self):
        """The case that was missing."""
        self.assertTrue(
            message_processing._looks_like_source_node_id("mqtt:baconbbsvt:Burlington-NNE"))

    def test_ordinary_content_is_not_mistaken_for_a_node_id(self):
        self.assertFalse(message_processing._looks_like_source_node_id("some content"))
        self.assertFalse(message_processing._looks_like_source_node_id(""))

    def test_a_bare_hex_meshcore_key_is_deliberately_not_matched(self):
        """It has no distinguishing prefix, so accepting it would mean
        treating ordinary content as a node id."""
        self.assertFalse(
            message_processing._looks_like_source_node_id("78cb1cc70466915f74e30e70"))


class MqttOriginatedRecordParsingTests(unittest.TestCase):
    UID = "a403b5c0-2e5f-4e8a-8ac8-74b4651cf5ad"
    SRC = "mqtt:baconbbsvt:Burlington-NNE"

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.iface = _Iface()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _bulletins(self):
        conn = db_operations.thread_local.connection
        return conn.execute(
            "SELECT unique_id, subject, source_node_id FROM bulletins").fetchall()

    def test_an_mqtt_sourced_bulletin_keeps_its_real_unique_id(self):
        message_processing.process_message(
            1,
            f"BULLETIN|General|BACN|glorious test|body text|{self.UID}|2026-08-24 21:37|"
            f"{self.SRC}|2026-08-24T21:37:00",
            self.iface, is_sync_message=True, sender_node_id=self.SRC)
        rows = self._bulletins()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], self.UID,
                         "source node id was stored as the unique_id")

    def test_the_source_node_id_lands_in_its_own_column(self):
        message_processing.process_message(
            1,
            f"BULLETIN|General|BACN|glorious test|body text|{self.UID}|2026-08-24 21:37|"
            f"{self.SRC}|2026-08-24T21:37:00",
            self.iface, is_sync_message=True, sender_node_id=self.SRC)
        self.assertEqual(self._bulletins()[0][2], self.SRC)

    def test_a_radio_sourced_bulletin_still_parses(self):
        """The path that always worked must not regress."""
        message_processing.process_message(
            1,
            f"BULLETIN|General|BACN|radio test|body|{self.UID}|2026-08-24 21:37|"
            f"!04058ac8|2026-08-24T21:37:00",
            self.iface, is_sync_message=True, sender_node_id="!04058ac8")
        rows = self._bulletins()
        self.assertEqual(rows[0][0], self.UID)
        self.assertEqual(rows[0][2], "!04058ac8")

    def test_the_same_record_from_two_nodes_does_not_duplicate(self):
        """The live symptom: one copy keyed correctly, one keyed by the
        source node id, so the nodes never agreed the record was the same."""
        frame = (f"BULLETIN|General|BACN|glorious test|body text|{self.UID}|"
                 f"2026-08-24 21:37|{self.SRC}|2026-08-24T21:37:00")
        message_processing.process_message(
            1, frame, self.iface, is_sync_message=True, sender_node_id=self.SRC)
        message_processing.process_message(
            1, frame, self.iface, is_sync_message=True, sender_node_id=self.SRC)
        rows = self._bulletins()
        self.assertEqual(len(rows), 1, f"record duplicated: {rows}")
        self.assertEqual(rows[0][0], self.UID)


if __name__ == "__main__":
    unittest.main()
