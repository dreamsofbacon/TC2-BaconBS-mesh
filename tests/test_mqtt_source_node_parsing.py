"""A record originating elsewhere was parsed with its fields shifted.

The optional trailing source_node_id on BULLETIN / MAIL / CHANNELCOMMENT
frames used to be recognised by guessing at its shape -- a leading '!' for a
Meshtastic radio, later 'mqtt:' as well. A MeshCore key is bare hex with no
prefix to recognise, so for those the guess still failed while the timestamp
before it had already been consumed: the parse shifted by one field and the
SOURCE NODE ID was written into unique_id.

Two nodes then held one record under two identities and could never
converge, because each kept offering a key the other had never agreed to. It
showed up live as a mail row with unique_id 'mqtt:baconbbsvt:Burlington-NNE'
whose content field held the real uid and date, and as a channel comment
under 'mqtt:baconbbsvt:Chattanooga'. Both had to be deleted by hand.

The guess is gone. source_node_id and source_timestamp are written together
or not at all, and content is always followed by |unique_id, so the last
field of a well-formed frame is never content -- a trailing timestamp is
proof that the field before it is the node id, whatever it looks like.
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


class SourceFieldSplitTests(unittest.TestCase):
    """MAIL shape: sender|short|recipient|subject|content|unique_id -- five
    structural pipes before the optional pair."""

    HEADER = "s|sh|r|subj|content|uid"

    def test_the_pair_is_peeled_off_when_present(self):
        body, node, ts = message_processing._split_source_fields(
            self.HEADER + "|!04058ac8|2026-08-30T10:24:00", 5)
        self.assertEqual(body, self.HEADER)
        self.assertEqual(node, "!04058ac8")
        self.assertEqual(ts, "2026-08-30T10:24:00")

    def test_an_mqtt_node_id_is_peeled(self):
        _body, node, _ts = message_processing._split_source_fields(
            self.HEADER + "|mqtt:baconbbsvt:Burlington-NNE|2026-08-30T10:24:00", 5)
        self.assertEqual(node, "mqtt:baconbbsvt:Burlington-NNE")

    def test_a_bare_hex_meshcore_key_is_peeled(self):
        """The case the shape guess could never cover: no prefix to match."""
        key = "78cb1cc70466915f74e30e7050162165d52cbb3f6574287dad0607986ecc1a72"
        body, node, _ts = message_processing._split_source_fields(
            self.HEADER + "|" + key + "|2026-08-30T10:24:00", 5)
        self.assertEqual(node, key)
        self.assertEqual(body, self.HEADER)

    def test_an_epoch_encoded_timestamp_anchors_just_as_well(self):
        _body, node, ts = message_processing._split_source_fields(
            self.HEADER + "|" + "a" * 12 + "|s1788094380", 5)
        self.assertEqual(node, "a" * 12)
        self.assertTrue(ts)

    def test_nothing_is_consumed_when_the_pair_is_absent(self):
        for frame in (self.HEADER, self.HEADER + "|2026-08-30 10:24"):
            with self.subTest(frame=frame):
                body, node, ts = message_processing._split_source_fields(frame, 5)
                self.assertEqual(body, frame)
                self.assertIsNone(node)
                self.assertIsNone(ts)

    def test_a_truncated_frame_is_left_alone_rather_than_eaten_into(self):
        """The back-out: peeling here would leave too few fields for the
        header, so the trailing text is not the pair."""
        body, node, ts = message_processing._split_source_fields(
            "only|three|2026-08-30T10:24:00", 5)
        self.assertEqual(body, "only|three|2026-08-30T10:24:00")
        self.assertIsNone(node)
        self.assertIsNone(ts)

    def test_a_date_is_not_mistaken_for_a_source_timestamp(self):
        """The minute-precision date has no T and no seconds.

        Deliberately given more fields than the header needs, so the
        back-out guard cannot be what saves it -- only the timestamp anchor
        can. With fewer fields this passes whether or not the anchor is
        checked, which makes it prove nothing."""
        frame = "s|sh|r|subj|con|tent|uid|2026-08-30 10:24"
        body, node, ts = message_processing._split_source_fields(frame, 5)
        self.assertIsNone(ts)
        self.assertIsNone(node)
        self.assertEqual(body, frame)

    def test_a_plain_unique_id_is_not_peeled_as_a_timestamp(self):
        """Same shape, no trailing date at all."""
        frame = "s|sh|r|subj|con|tent|a403b5c0-2e5f-4e8a-8ac8-74b4651cf5ad"
        body, node, ts = message_processing._split_source_fields(frame, 5)
        self.assertEqual(body, frame)
        self.assertIsNone(node)
        self.assertIsNone(ts)


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

    def test_a_meshcore_sourced_bulletin_keeps_its_real_unique_id(self):
        """The gap the shape guess left open. A MeshCore key is bare hex, so
        it was never recognised and its node id landed in unique_id."""
        key = "78cb1cc70466915f74e30e7050162165d52cbb3f6574287dad0607986ecc1a72"
        message_processing.process_message(
            1,
            f"BULLETIN|General|BACN|meshcore test|body|{self.UID}|2026-08-24 21:37|"
            f"{key}|2026-08-24T21:37:00",
            self.iface, is_sync_message=True, sender_node_id=key)
        rows = self._bulletins()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], self.UID,
                         "MeshCore node id was stored as the unique_id")
        self.assertEqual(rows[0][2], key)

    def test_a_meshcore_sourced_mail_keeps_its_real_unique_id(self):
        """The live wreckage was a mail row, so cover that frame too -- it
        has one more header field than a bulletin."""
        key = "78cb1cc70466915f74e30e7050162165d52cbb3f6574287dad0607986ecc1a72"
        message_processing.process_message(
            1,
            f"MAIL|{key}|BACN|!04058ac8|subject here|body text|{self.UID}|"
            f"2026-08-24 21:37|{key}|2026-08-24T21:37:00",
            self.iface, is_sync_message=True, sender_node_id=key)
        rows = db_operations.thread_local.connection.execute(
            "SELECT unique_id, subject, content, source_node_id FROM mail").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], self.UID)
        self.assertEqual(rows[0][1], "subject here")
        # The real uid and date used to be swept in here behind the content.
        self.assertEqual(rows[0][2], "body text")
        self.assertEqual(rows[0][3], key)

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
