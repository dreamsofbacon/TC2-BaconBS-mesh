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
    channel_name = str(packet.get('channel_name') or '')
    if not channel_name and channel_index == 0:
        channel_name = 'Public' if network == 'meshcore' else 'LongFast'
    sender_node_id = packet.get('fromId') or None
    sender_name = str(packet.get('sender_name') or '')
    native_id = packet.get('id', packet.get('message_hash'))
    return {
        'unique_id': make_message_id(
            network, channel_index, sender_node_id, native_id, source_time, content
        ),
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
    }


def capture_broadcast(packet: dict, interface) -> bool:
    observation = normalize_broadcast(packet, interface)
    if observation is None:
        return False
    from db_operations import add_public_chatter

    return add_public_chatter(**observation)