"""Meshtastic serial (stream) protocol — just enough to send/receive text.

The Meshtastic firmware's Serial module in PROTO mode frames each protobuf
message on the wire as:

    0x94 0xC3 <len_hi> <len_lo> <protobuf bytes>

Outbound we send ``ToRadio`` messages; inbound we receive ``FromRadio``. We
only encode/decode the fields needed to exchange TEXT_MESSAGE_APP packets and
to learn our own node number at startup. Field numbers below come from
Meshtastic's mesh.proto.

Pure Python (only ``minipb`` + ``struct``); no hardware imports, so the test
suite exercises it under CPython. The Pico's code.py supplies a real UART.
"""

import minipb

START1 = 0x94
START2 = 0xC3
PORT_TEXT_MESSAGE = 1  # PortNum.TEXT_MESSAGE_APP

# --- mesh.proto field numbers (subset) ---
# ToRadio:   packet=1 (MeshPacket), want_config_id=3 (uint32)
# FromRadio: packet=2 (MeshPacket), my_info=3 (MyNodeInfo), config_complete_id=7
# MeshPacket: from=1(fx32), to=2(fx32), channel=3, decoded=4(Data), id=6(fx32),
#             want_ack=11
# Data:      portnum=1, payload=2(bytes)
# MyNodeInfo: my_node_num=1


def frame(payload):
    """Wrap a protobuf payload in the Meshtastic stream framing."""
    n = len(payload)
    return bytes([START1, START2, (n >> 8) & 0xFF, n & 0xFF]) + bytes(payload)


def build_text_toradio(dest_num, text, packet_id, channel=0, want_ack=True):
    """A ToRadio carrying a text MeshPacket addressed to *dest_num*.

    ``from`` is left unset — the radio stamps its own node number."""
    data = minipb.field_varint(1, PORT_TEXT_MESSAGE)
    data += minipb.field_bytes(2, text.encode("utf-8"))
    mp = minipb.field_fixed32(2, dest_num)          # to
    if channel:
        mp += minipb.field_varint(3, channel)
    mp += minipb.field_message(4, data)             # decoded (Data)
    mp += minipb.field_fixed32(6, packet_id & 0xFFFFFFFF)  # id
    if want_ack:
        mp += minipb.field_varint(11, 1)
    return frame(minipb.field_message(1, mp))       # ToRadio.packet


def build_want_config(config_id):
    """ToRadio.want_config_id — asks the radio to stream its config, which ends
    with MyNodeInfo + config_complete_id so we can learn our own node number."""
    return frame(minipb.field_varint(3, config_id & 0xFFFFFFFF))


def _parse_data(buf):
    d = {}
    for field, wt, val in minipb.iter_fields(buf):
        if field == 1:
            d["portnum"] = val
        elif field == 2:
            d["payload"] = val
    return d


def _parse_meshpacket(buf):
    pkt = {}
    for field, wt, val in minipb.iter_fields(buf):
        if field == 1:
            pkt["from"] = val
        elif field == 2:
            pkt["to"] = val
        elif field == 4 and wt == minipb.WT_LEN:
            pkt["decoded"] = _parse_data(val)
        elif field == 6:
            pkt["id"] = val
    return pkt


def parse_fromradio(payload):
    """Decode a FromRadio payload into a dict with any of:
    'text' (str) + 'from' + 'to' for a text packet, 'my_node_num',
    'config_complete_id'."""
    result = {}
    for field, wt, val in minipb.iter_fields(payload):
        if field == 2 and wt == minipb.WT_LEN:          # packet
            pkt = _parse_meshpacket(val)
            dec = pkt.get("decoded")
            if dec and dec.get("portnum") == PORT_TEXT_MESSAGE and "payload" in dec:
                try:
                    result["text"] = dec["payload"].decode("utf-8")
                except Exception:
                    result["text"] = None
                result["from"] = pkt.get("from")
                result["to"] = pkt.get("to")
        elif field == 3 and wt == minipb.WT_LEN:        # my_info
            for f2, w2, v2 in minipb.iter_fields(val):
                if f2 == 1:
                    result["my_node_num"] = v2
        elif field == 7:                                # config_complete_id
            result["config_complete_id"] = val
    return result


class StreamReader:
    """Accumulates raw UART bytes and yields complete FromRadio payloads.

    Resyncs on the 0x94 0xC3 magic so a mid-stream connection or a corrupt
    frame can't wedge the parser."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        """Add received bytes; return a list of complete FromRadio payloads."""
        if data:
            self.buf.extend(data)
        out = []
        while True:
            # Find the frame start magic, discarding any leading garbage.
            start = -1
            limit = len(self.buf) - 1
            i = 0
            while i < limit:
                if self.buf[i] == START1 and self.buf[i + 1] == START2:
                    start = i
                    break
                i += 1
            if start == -1:
                # No magic yet; keep at most the last byte (could be a partial magic).
                if len(self.buf) > 1:
                    self.buf = self.buf[-1:]
                return out
            if start:
                del self.buf[:start]  # drop garbage before the magic
            if len(self.buf) < 4:
                return out  # need the length header
            n = (self.buf[2] << 8) | self.buf[3]
            if len(self.buf) < 4 + n:
                return out  # full payload not arrived yet
            payload = bytes(self.buf[4:4 + n])
            del self.buf[:4 + n]
            out.append(payload)
