"""Host-side tests for the Pico node's protocol core (no hardware).

The Pico's protobuf/framing/wire logic is plain Python, so we exercise the
hardest part — hand-rolled Meshtastic stream framing + protobuf + chunked
response reassembly — entirely under CPython. code.py (which imports
board/busio) is device-only and intentionally not imported here.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pico_node"))

import minipb
import meshtastic_link as link
import wire


def _build_fromradio_text(from_num, to_num, text):
    """Construct a FromRadio payload carrying a text packet (for receive tests)."""
    data = minipb.field_varint(1, link.PORT_TEXT_MESSAGE) + minipb.field_bytes(2, text.encode())
    mp = (minipb.field_fixed32(1, from_num) + minipb.field_fixed32(2, to_num)
          + minipb.field_message(4, data) + minipb.field_fixed32(6, 123))
    return minipb.field_message(2, mp)  # FromRadio.packet = field 2


def _build_fromradio_mynodeinfo(num):
    info = minipb.field_varint(1, num)
    return minipb.field_message(3, info)  # FromRadio.my_info = field 3


class MiniPbTests(unittest.TestCase):
    def test_varint_roundtrip(self):
        for v in (0, 1, 127, 128, 300, 16384, 67403640, 0xFFFFFFFF):
            enc = minipb.encode_varint(v)
            dec, pos = minipb.decode_varint(enc, 0)
            self.assertEqual(dec, v)
            self.assertEqual(pos, len(enc))

    def test_fields_roundtrip(self):
        buf = (minipb.field_varint(1, 5) + minipb.field_fixed32(2, 0x0408b778)
               + minipb.field_bytes(3, b"hi") + minipb.field_message(4, b"\x08\x01"))
        got = {f: (wt, v) for f, wt, v in minipb.iter_fields(buf)}
        self.assertEqual(got[1], (minipb.WT_VARINT, 5))
        self.assertEqual(got[2], (minipb.WT_FIXED32, 0x0408b778))
        self.assertEqual(got[3], (minipb.WT_LEN, b"hi"))
        self.assertEqual(got[4][1], b"\x08\x01")


class FramingTests(unittest.TestCase):
    def test_frame_has_magic_and_length(self):
        f = link.frame(b"abc")
        self.assertEqual(f[0], 0x94)
        self.assertEqual(f[1], 0xC3)
        self.assertEqual((f[2] << 8) | f[3], 3)
        self.assertEqual(f[4:], b"abc")

    def test_stream_reader_reassembles_across_chunks_and_garbage(self):
        payload = _build_fromradio_text(1, 2, "hello")
        framed = link.frame(payload)
        reader = link.StreamReader()
        # Leading garbage, then the frame split across two feeds.
        self.assertEqual(reader.feed(b"\x00\xff"), [])
        first, second = framed[:3], framed[3:]
        self.assertEqual(reader.feed(first), [])
        out = reader.feed(second)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0], payload)

    def test_two_frames_in_one_feed(self):
        a = link.frame(_build_fromradio_text(1, 2, "one"))
        b = link.frame(_build_fromradio_text(1, 2, "two"))
        reader = link.StreamReader()
        out = reader.feed(a + b)
        self.assertEqual(len(out), 2)
        self.assertEqual(link.parse_fromradio(out[0])["text"], "one")
        self.assertEqual(link.parse_fromradio(out[1])["text"], "two")


class EncodeDecodeTests(unittest.TestCase):
    def test_text_toradio_encodes_dest_and_text(self):
        f = link.build_text_toradio(0x0408b778, "APIPOLL|!1234", packet_id=7)
        # Strip framing, decode ToRadio.packet (field 1) -> MeshPacket.
        self.assertEqual(f[0], 0x94)
        payload = f[4:]
        fields = {fld: v for fld, wt, v in minipb.iter_fields(payload)}
        self.assertIn(1, fields)  # ToRadio.packet
        mp = link._parse_meshpacket(fields[1])
        self.assertEqual(mp["to"], 0x0408b778)
        self.assertEqual(mp["decoded"]["portnum"], link.PORT_TEXT_MESSAGE)
        self.assertEqual(mp["decoded"]["payload"].decode(), "APIPOLL|!1234")

    def test_parse_fromradio_text(self):
        msg = link.parse_fromradio(_build_fromradio_text(0x11, 0x22, "hi there"))
        self.assertEqual(msg["text"], "hi there")
        self.assertEqual(msg["from"], 0x11)
        self.assertEqual(msg["to"], 0x22)

    def test_parse_fromradio_my_node_num(self):
        msg = link.parse_fromradio(_build_fromradio_mynodeinfo(0x0408b778))
        self.assertEqual(msg["my_node_num"], 0x0408b778)


class WireTests(unittest.TestCase):
    def test_node_id_num_roundtrip(self):
        self.assertEqual(wire.node_id_to_num("!0408b778"), 0x0408b778)
        self.assertEqual(wire.num_to_node_id(0x0408b778), "!0408b778")
        self.assertEqual(wire.node_id_to_num("0408b778"), 0x0408b778)

    def test_builders(self):
        self.assertEqual(wire.build_ai_request("r1", "!u", "hello"),
                         "APIREQ|r1|!u|r|ai\x1fhello")
        self.assertEqual(wire.build_apipoll("!u"), "APIPOLL|!u")
        self.assertEqual(wire.build_apirespgap("r1", "5-10"), "APIRESPGAP|r1|5-10")

    def test_single_packet_response(self):
        a = wire.ResponseAssembler()
        done = a.feed("APIRESP|r1|200|5|hello")
        self.assertEqual(done, ("r1", "200", "hello"))

    def test_chunked_response_reassembles(self):
        a = wire.ResponseAssembler()
        self.assertIsNone(a.feed("APIRESP|r2|200|15|AAAAA"))
        self.assertIsNone(a.feed("APIRESPMETA|r2|15"))
        self.assertIsNone(a.feed("APIRESPCONT|r2|5|BBBBB"))
        done = a.feed("APIRESPCONT|r2|10|CCCCC")
        self.assertEqual(done, ("r2", "200", "AAAAABBBBBCCCCC"))

    def test_gap_spec_for_hole(self):
        a = wire.ResponseAssembler()
        a.feed("APIRESP|r3|200|15|AAAAA")   # 0-5
        a.feed("APIRESPCONT|r3|10|CCCCC")   # 10-15, hole 5-10
        self.assertEqual(a.gap_spec("r3"), "5-10")

    def test_gap_spec_unknown_length_requests_all(self):
        a = wire.ResponseAssembler()
        a.feed("APIRESPCONT|r4|0|AAA")  # no expected length learned yet
        self.assertEqual(a.gap_spec("r4"), "*")

    def test_matching_overlap_does_not_false_complete(self):
        a = wire.ResponseAssembler()
        a.feed("APIRESP|r5|200|10|ABCDE")

        self.assertIsNone(a.feed("APIRESPCONT|r5|3|DEFGH"))
        self.assertEqual(a.gap_spec("r5"), "8-10")

        self.assertEqual(a.feed("APIRESPCONT|r5|8|IJ"),
                         ("r5", "200", "ABCDEFGHIJ"))

    def test_conflicting_overlap_requests_full_resend_and_new_header_repairs(self):
        a = wire.ResponseAssembler()
        a.feed("APIRESP|r6|200|10|ABCDE")

        self.assertIsNone(a.feed("APIRESPCONT|r6|3|XX"))
        self.assertEqual(a.gap_spec("r6"), "*")

        self.assertIsNone(a.feed("APIRESP|r6|200|10|VWXYZ"))
        self.assertEqual(a.feed("APIRESPCONT|r6|5|12345"),
                         ("r6", "200", "VWXYZ12345"))

    def test_emoji_uses_character_offsets(self):
        a = wire.ResponseAssembler()
        a.feed("APIRESP|r7|200|4|A🙂B")

        self.assertEqual(a.feed("APIRESPCONT|r7|3|C"),
                         ("r7", "200", "A🙂BC"))


if __name__ == "__main__":
    unittest.main()
