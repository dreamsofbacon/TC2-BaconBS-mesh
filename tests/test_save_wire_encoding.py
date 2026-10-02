"""Game saves cross the air in Ascii85 to peers that understand it.

Base64 spends four characters on every three bytes; Ascii85 spends five on
four. It is a small saving, and it must not cost correctness: a peer on
older code has to keep getting base64, and a gap-fill resend has to cut the
payload at the same places the first send did.
"""
import base64
import os
import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    meshtastic_stub = types.ModuleType("meshtastic")
    setattr(meshtastic_stub, "BROADCAST_NUM", 0)
    sys.modules["meshtastic"] = meshtastic_stub

import db_operations
import message_processing
import utils
from utils import (ZORKSAVE_ASCII85_PREFIX, decode_save_payload, encode_save_payload,
                   send_zork_save_to_bbs_nodes)

NEW, OLD = "!newpeer1", "!oldpeer1"


class _Radio:
    def __init__(self):
        self.sent = []          # (destination, text)
        self.bbs_nodes = []

    def sendText(self, text, destinationId, wantAck, wantResponse):
        self.sent.append((destinationId, text))

    def to(self, destination):
        return [text for dest, text in self.sent if dest == destination]


def supports_new(peer_ids, cap):
    """Only NEW has advertised the capability."""
    return cap == 'zs85' and list(peer_ids) == [NEW]


class EncodingTests(unittest.TestCase):
    def test_every_byte_pattern_round_trips_in_both_forms(self):
        for data in (b"", b"\x00", b"\x00" * 9, bytes(range(256)), os.urandom(613),
                     b'\x01' + os.urandom(200)):
            for ascii85, save_id in ((True, ZORKSAVE_ASCII85_PREFIX + "abc"), (False, "abc")):
                self.assertEqual(data, decode_save_payload(
                    save_id, encode_save_payload(data, ascii85)))

    def test_ascii85_never_contains_the_field_separator(self):
        """A '|' in a chunk would be read as the end of a field."""
        for _ in range(200):
            self.assertNotIn("|", encode_save_payload(os.urandom(257), True))
        self.assertNotIn("|", encode_save_payload(bytes(range(256)) * 4, True))

    def test_it_is_plain_ascii_with_no_whitespace(self):
        text = encode_save_payload(os.urandom(500), True)
        self.assertTrue(text.isascii())
        self.assertEqual(text, "".join(text.split()))

    def test_ascii85_is_shorter(self):
        data = os.urandom(204)
        self.assertLess(len(encode_save_payload(data, True)),
                        len(encode_save_payload(data, False)))
        self.assertEqual(255, len(encode_save_payload(data, True)))
        self.assertEqual(272, len(encode_save_payload(data, False)))

    def test_the_capability_is_advertised(self):
        self.assertIn('zs85', utils.WIRE_CAPABILITIES)


class SendTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        message_processing._zork_save_chunk_buffers.clear()
        self.addCleanup(self._close)
        for patch in (mock.patch.dict(os.environ, {"BBS_SYNC_ZORK_SAVES": "true"}),
                      mock.patch.object(utils, 'peers_all_support', supports_new)):
            patch.start()
            self.addCleanup(patch.stop)
        self.payload = b'\x01' + bytes((i * 7) % 256 for i in range(700))

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def send(self, peers, only_indices=None):
        radio = _Radio()
        send_zork_save_to_bbs_nodes("4242", "hamurabi", self.payload,
                                    "2026-10-02 12:00:00", peers, radio,
                                    pause_seconds=0, only_indices=only_indices)
        return radio

    def receive(self, frames, sender):
        for frame in frames:
            message_processing.process_message(
                sender_id=1, message=frame, interface=_Radio(),
                is_sync_message=True, sender_node_id=sender)

    def test_a_peer_on_older_code_still_gets_base64(self):
        frames = self.send([OLD]).to(OLD)
        self.assertTrue(frames)
        for frame in frames:
            self.assertFalse(frame.split("|")[1].startswith(ZORKSAVE_ASCII85_PREFIX))
        payload = "".join(frame.split("|", 8)[8] for frame in frames)
        self.assertEqual(self.payload, base64.b64decode(payload))

    def test_a_peer_that_understands_it_gets_ascii85(self):
        frames = self.send([NEW]).to(NEW)
        for frame in frames:
            self.assertTrue(frame.split("|")[1].startswith(ZORKSAVE_ASCII85_PREFIX))
        payload = "".join(frame.split("|", 8)[8] for frame in frames)
        self.assertEqual(self.payload, base64.a85decode(payload))

    def test_the_newer_form_carries_a_short_save_id(self):
        """The id rides in every frame. The older form's is the whole of
        "user:game:time:length" in base64; the newer one is eight
        characters of a hash of the same string."""
        radio = self.send([OLD, NEW])
        old_id = radio.to(OLD)[0].split("|")[1]
        new_id = radio.to(NEW)[0].split("|")[1]
        self.assertEqual(len(ZORKSAVE_ASCII85_PREFIX) + 8, len(new_id))
        self.assertGreater(len(old_id), 30)
        self.assertNotIn("|", new_id)

    def test_the_short_id_is_the_same_on_a_resend_and_differs_between_saves(self):
        first = self.send([NEW]).to(NEW)[0].split("|")[1]
        self.assertEqual(first, self.send([NEW]).to(NEW)[0].split("|")[1])
        self.payload += b"changed"
        self.assertNotEqual(first, self.send([NEW]).to(NEW)[0].split("|")[1])

    def test_a_small_save_fits_one_packet_in_the_newer_form(self):
        self.payload = b'\x01' + os.urandom(95)
        radio = self.send([OLD, NEW])
        self.assertEqual(1, len(radio.to(NEW)))
        self.assertEqual(2, len(radio.to(OLD)))

    def test_fewer_bytes_go_to_the_peer_that_understands_it(self):
        radio = self.send([OLD, NEW])
        old_bytes = sum(len(f.encode()) for f in radio.to(OLD))
        new_bytes = sum(len(f.encode()) for f in radio.to(NEW))
        self.assertLess(new_bytes, old_bytes)

    def test_in_a_mixed_fleet_each_peer_gets_its_own_form_and_both_arrive(self):
        radio = self.send([OLD, NEW])
        for peer in (OLD, NEW):
            db_operations.get_db_connection().execute("DELETE FROM zork_saves")
            self.receive(radio.to(peer), peer)
            self.assertEqual(self.payload,
                             bytes(db_operations.get_zork_save("4242", "hamurabi")), peer)

    def test_every_frame_fits_a_packet(self):
        radio = self.send([OLD, NEW])
        for _dest, frame in radio.sent:
            self.assertLessEqual(len(frame.encode("utf-8")), utils._MESHTASTIC_MAX_BYTES)

    def test_a_gap_fill_resends_the_same_chunks_the_first_send_cut(self):
        """The resend goes to one peer. Its encoding is chosen from that
        peer alone, so it is the same as it was in the first send -- even
        though that send also went to a peer on older code."""
        first = self.send([OLD, NEW]).to(NEW)
        self.assertGreater(len(first), 2)
        again = self.send([NEW], only_indices=[1]).to(NEW)
        self.assertEqual([first[1]], again)

    def test_a_lost_chunk_is_recovered_by_the_resend(self):
        first = self.send([OLD, NEW]).to(NEW)
        self.receive(first[:1] + first[2:], NEW)            # chunk 1 lost
        self.assertIsNone(db_operations.get_zork_save("4242", "hamurabi"))
        self.receive(self.send([NEW], only_indices=[1]).to(NEW), NEW)
        self.assertEqual(self.payload, bytes(db_operations.get_zork_save("4242", "hamurabi")))

    def test_a_damaged_ascii85_save_is_rejected_by_its_hash(self):
        frames = self.send([NEW]).to(NEW)
        head, _sep, chunk = frames[0].rpartition("|")
        swapped = ("!" if chunk[0] != "!" else '"') + chunk[1:]
        self.receive([head + "|" + swapped] + frames[1:], NEW)
        self.assertIsNone(db_operations.get_zork_save("4242", "hamurabi"))

    def test_a_compressed_game_save_survives_the_whole_trip(self):
        """What actually travels now: a compact, compressed door save."""
        import door_kit
        state = {"market": {f"good{i}": {"price": 100 + i, "stock": i} for i in range(16)}}
        self.payload = door_kit.encode_save(state)
        self.receive(self.send([NEW]).to(NEW), NEW)
        self.assertEqual(state, door_kit.decode_save(
            db_operations.get_zork_save("4242", "hamurabi")))


if __name__ == "__main__":
    unittest.main()
