"""Minimal protobuf primitives (a tiny subset) for the Meshtastic stream API.

Hand-rolled because CircuitPython has no protobuf library and we only need a
handful of message shapes (ToRadio/FromRadio with a text MeshPacket). Pure
Python with only ``struct`` from the stdlib, so it runs unchanged on both
CPython (for the test suite) and CircuitPython (on the Pico).

Wire types used: 0 = varint, 2 = length-delimited (bytes / sub-message),
5 = 32-bit fixed. That covers every field Meshtastic uses for text messaging.
"""

import struct

WT_VARINT = 0
WT_FIXED64 = 1
WT_LEN = 2
WT_FIXED32 = 5


def encode_varint(value):
    """Encode an unsigned int as a base-128 varint."""
    value &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def decode_varint(buf, pos):
    """Decode a varint at *pos*. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _tag(field, wire_type):
    return encode_varint((field << 3) | wire_type)


def field_varint(field, value):
    return _tag(field, WT_VARINT) + encode_varint(value)


def field_fixed32(field, value):
    return _tag(field, WT_FIXED32) + struct.pack("<I", value & 0xFFFFFFFF)


def field_bytes(field, data):
    return _tag(field, WT_LEN) + encode_varint(len(data)) + bytes(data)


def field_message(field, sub):
    # A sub-message is encoded identically to a length-delimited bytes field.
    return field_bytes(field, sub)


def iter_fields(buf):
    """Yield (field_number, wire_type, value) for every field in *buf*.

    value is an int for varint/fixed types and a bytes object for
    length-delimited fields. Unknown wire types raise ValueError."""
    pos = 0
    n = len(buf)
    while pos < n:
        key, pos = decode_varint(buf, pos)
        field = key >> 3
        wt = key & 0x07
        if wt == WT_VARINT:
            val, pos = decode_varint(buf, pos)
        elif wt == WT_FIXED32:
            val = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
        elif wt == WT_FIXED64:
            val = struct.unpack_from("<Q", buf, pos)[0]
            pos += 8
        elif wt == WT_LEN:
            ln, pos = decode_varint(buf, pos)
            val = bytes(buf[pos:pos + ln])
            pos += ln
        else:
            raise ValueError("unsupported wire type %d" % wt)
        yield field, wt, val
