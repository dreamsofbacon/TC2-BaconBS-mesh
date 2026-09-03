"""Normalize passive public-channel radio traffic for durable history."""

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


RETENTION_HOURS = 168

# Destinations that mean "everyone", across the transports this BBS speaks.
# Meshtastic broadcasts to 0xFFFFFFFF (meshtastic.BROADCAST_NUM); MeshCore
# and the MQTT bridge synthesise packets addressed to 0.
#
# Accepting only (0, 255) is what made public chatter look MeshCore-only: it
# matched the synthesised convention and never the value a real Meshtastic
# radio actually sends, so every LongFast message was dropped here.
#
# Deliberately a literal rather than importing meshtastic's constant. The
# test suite stubs that module with BROADCAST_NUM = 0, so importing it would
# make the tests agree with a number the radios never send -- which is how
# this got through in the first place.
BROADCAST_ADDRESSES = (0, 255, 0xFFFFFFFF)

CONTROL_PREFIXES = (
    'BULLETIN|', 'MAIL|', 'DELETE_', 'CHANNEL|', 'CHANNELCOMMENT|',
    'CHANNELCOMMENTCONT|', 'CHANNELCOMMENTMETA|', 'BULLETINCONT|',
    'MAILCONT|', 'BULLETINMETA|', 'MAILMETA|', 'SYNCSTATE|',
    'PROFILESYNC|', 'RELAYPREF|', 'SCORESYNC|', 'ZORKSAVE|',
    'ZORKGAP|', 'CANDREQ|', 'CANDRSP|', 'HASHREQ|', 'HASHREC|',
    'HASHEND|', 'HASHMISS|', 'HASHZ|', 'HASHZGAP|', 'HAVE|',
    'WANT|', 'EVENT|', 'PEERGOSSIP|', 'APIREQ|', 'APIRESP|',
    'APIRESPCONT|', 'APIRESPMETA|', 'APIRESPGAP|', 'APIPOLL|',
    'PCHAT|', 'PCHATCONT|', 'PCHATMETA|',
    'FLEETVER|', 'FLEETVERCONT|', 'NODEVER|', 'FLEETSTATUS|',
)


def _utc_datetime(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and value > 0:
        parsed = datetime.fromtimestamp(value, tz=timezone.utc)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            parsed = fallback
    else:
        parsed = fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def make_message_id(
    network: str,
    channel_index: int,
    sender_node_id: Optional[str],
    native_id: Any,
    message_timestamp: datetime,
    content: str,
) -> str:
    """Build an ID shared by every BBS node hearing the same RF packet."""
    if native_id not in (None, ''):
        identity = f'native:{native_id}'
    else:
        identity = f'time:{int(message_timestamp.timestamp())}|text:{content}'
    material = (
        f'{network.casefold()}|{int(channel_index)}|{sender_node_id or ""}|{identity}'
    ).encode('utf-8')
    return 'pch:' + hashlib.blake2b(material, digest_size=16).hexdigest()


# MeshCore packs the path length into the low 6 bits of a byte, so a path
# cannot exceed this. 255 is not a very long path -- it is the sentinel for
# direct (non-flood) routing.
_MESHCORE_MAX_PATH = 63
_MESHCORE_DIRECT_SENTINEL = 255


# MeshCore channel messages carry no sender identity whatsoever. A channel
# is encrypted with a shared key and the frame has no pubkey field at all --
# unlike a MeshCore direct message, which does. So "unknown sender" was
# accurate rather than broken.
#
# What clients do instead is write the sender's name into the body as a
# "Name: " prefix. That prefix is the ONLY sender information that exists on
# this transport, which is why it is worth parsing despite being a
# convention rather than a protocol guarantee.
_MESHCORE_SENDER_MAX = 32


def split_meshcore_sender(content: str):
    """Split a MeshCore channel body into (sender_name, message).

    Returns ('', content) unchanged whenever the body does not clearly carry
    a name. Guessing wrong attributes somebody's message to a person who
    never sent it, so every ambiguous case declines to parse.

    Splits on the FIRST colon-space and nothing else. Real traffic is full of
    later colons -- a 'MM:YhW9uyPnvQ:36.23537,-86.71230' location payload, an
    '@ 18:28' timestamp, a 'Route:' continuation line -- and any of them
    would be mistaken for the delimiter by a rightmost or bare-colon split.
    Requiring the space is what separates 'brown dog: Test' from 'MM:YhW9'.
    """
    head, separator, tail = content.partition(': ')
    if not separator:
        return '', content
    name = head.strip()
    body = tail.strip()
    # A name is one line, non-empty, and short. Names legitimately contain
    # spaces and emoji ('brown dog', 'N4NOV 🏠', '🔋'), so neither can be
    # excluded -- length and line count are what remain to judge on.
    if not name or not body or '\n' in head or len(name) > _MESHCORE_SENDER_MAX:
        return '', content
    return name, body


def hops_used(packet: dict):
    """How many hops a broadcast travelled, or None when that is unknowable.

    The two transports count in completely different ways, which is why this
    cannot be a single subtraction:

    Meshtastic sends a TTL. hop_start is what the sender set out with,
    hop_limit is what was left when we heard it, so the difference is hops
    traversed. Absence is common and means unknown, not zero: firmware
    before 2.x never sent hop_start, and protobuf omits zero-valued fields
    entirely, so a packet heard direct can arrive with no hop_limit key.

    MeshCore instead has each repeater append its own hash to the packet's
    path, so the path length is the hop count directly, already parsed by
    the library. There is no TTL to subtract, which is why reading only the
    Meshtastic pair reported every MeshCore message as unknown.

    A packet that reached this node over MQTT rather than the air carries hop
    fields describing somebody else's radio path. Reporting that as our hop
    count would be worse than reporting nothing.
    """
    if packet.get('viaMqtt') or packet.get('via_mqtt'):
        return None

    path_len = packet.get('path_len')
    if path_len is not None:
        try:
            hops = int(path_len)
        except (TypeError, ValueError):
            return None
        if hops == _MESHCORE_DIRECT_SENTINEL:
            return None
        return hops if 0 <= hops <= _MESHCORE_MAX_PATH else None

    start = packet.get('hopStart', packet.get('hop_start'))
    limit = packet.get('hopLimit', packet.get('hop_limit'))
    if start is None or limit is None:
        return None
    try:
        hops = int(start) - int(limit)
    except (TypeError, ValueError):
        return None
    # Meshtastic caps the TTL at 7; anything outside that is a malformed or
    # rewritten packet rather than a very long path.
    return hops if 0 <= hops <= 7 else None


def normalize_broadcast(
    packet: dict,
    interface,
    *,
    captured_at: Optional[datetime] = None,
) -> Optional[dict]:
    """Return a storage-ready observation, or None when capture is disallowed."""
    decoded = packet.get('decoded') or {}
    if decoded.get('portnum') != 'TEXT_MESSAGE_APP':
        return None
    if packet.get('to') not in BROADCAST_ADDRESSES:
        return None

    allowed = {int(value) for value in getattr(interface, 'public_chatter_channels', [])}
    channel_index = int(packet.get('channel', packet.get('channel_index', 0)) or 0)
    if channel_index not in allowed:
        return None

    payload = decoded.get('payload', b'')
    if isinstance(payload, bytes):
        content = payload.decode('utf-8', errors='replace').strip()
    else:
        content = str(payload).strip()
    if (
        not content
        or bool(packet.get('public_chatter_sync'))
        or content.startswith(CONTROL_PREFIXES)
    ):
        return None

    now = captured_at or datetime.now(timezone.utc)
    now = _utc_datetime(now, datetime.now(timezone.utc))
    source_time = _utc_datetime(
        packet.get('rxTime', packet.get('sender_timestamp')),
        now,
    )
    expires = source_time + timedelta(hours=RETENTION_HOURS)
    if expires <= now:
        return None

    network = str(getattr(interface, 'protocol_name', 'Meshtastic')).casefold()
    # The radio's own name for the channel, in preference to its number.
    # MeshCore stamps this onto the packet from names it read at connect;
    # Meshtastic packets carry no name, so the table server.py built from
    # the local node's channel config is consulted here instead.
    channel_name = str(packet.get('channel_name') or '')
    if not channel_name:
        known = getattr(interface, 'channel_names', None) or {}
        try:
            channel_name = str(known.get(channel_index) or '')
        except Exception:
            channel_name = ''
    if not channel_name and channel_index == 0:
        # An unnamed primary channel. Both transports have a conventional
        # name for it; db_operations.channel_name_placeholders knows these
        # are stand-ins, so a real name learned later replaces them.
        channel_name = 'Public' if network == 'meshcore' else 'LongFast'
    sender_node_id = packet.get('fromId') or None
    sender_name = str(packet.get('sender_name') or '')
    native_id = packet.get('id', packet.get('message_hash'))
    # Derive the id from the body EXACTLY as it arrived, before any prefix is
    # stripped. make_message_id falls back to hashing the content when a
    # packet carries no native id, so parsing first would make a node running
    # this code and one running the old code disagree about which packet
    # they are looking at -- and every MeshCore message would sync twice.
    unique_id = make_message_id(
        network, channel_index, sender_node_id, native_id, source_time, content
    )
    if network == 'meshcore' and not sender_node_id and not sender_name:
        sender_name, content = split_meshcore_sender(content)
    return {
        'unique_id': unique_id,
        'network': network,
        'channel_index': channel_index,
        'channel_name': channel_name,
        'sender_node_id': str(sender_node_id) if sender_node_id else None,
        'sender_name': sender_name,
        'content': content,
        'message_timestamp': _iso(source_time),
        'captured_at': _iso(now),
        'capture_node_id': str(getattr(interface, 'public_chatter_capture_node_id', '') or ''),
        'expires_at': _iso(expires),
        'hops': hops_used(packet),
    }


def capture_broadcast(packet: dict, interface) -> bool:
    observation = normalize_broadcast(packet, interface)
    if observation is None:
        return False
    from db_operations import add_public_chatter

    return add_public_chatter(**observation)