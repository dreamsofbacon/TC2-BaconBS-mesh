"""Whether a relayed mail actually reached the recipient's radio.

Offline mail relay used to call a mail delivered the moment the BBS's own
radio accepted it. Nothing checked the recipient's radio got it.

MeshCore answers that reliably: the library waits for the recipient's ACK and
the BBS raises RecipientNoAck when none comes (meshcore_interface.sendText).

Meshtastic ACKs are not reliable enough to depend on, so they are a bonus
there: server.deliver_due_mail_dms marks a Meshtastic mail delivered straight
away when every packet was acknowledged, and otherwise sends it once more the
next time the recipient is heard. This module watches for those ACKs.

It listens to routing packets itself rather than passing an onResponse
callback to sendText. The library keeps one handler per packet and drops it
after the first routing reply, and the first reply is often an implicit ACK --
our own radio hearing a neighbour rebroadcast the packet -- which says nothing
about the recipient. The real ACK would then arrive with no handler left.

No imports beyond the standard library, so the delivery loop and the MeshCore
interface can both use RecipientNoAck without importing each other.
"""

import threading
import time


class RecipientNoAck(IOError):
    """The message left our radio but the recipient's radio never acknowledged it."""


# Long enough for a Meshtastic DM's own retransmissions to finish and a
# multi-hop ACK to come back.
ACK_WINDOW_SECONDS = 180
_TTL_SECONDS = 6 * 3600

_lock = threading.Lock()
_tracked = {}   # packet id -> {"dest": int, "at": float, "acked": bool}
_subscribed = False


def _node_num(dest):
    if isinstance(dest, int):
        return dest
    text = str(dest or '').strip()
    if text.startswith('!'):
        try:
            return int(text[1:], 16)
        except ValueError:
            return None
    try:
        return int(text)
    except ValueError:
        return None


def track(packet_id, dest) -> None:
    """Remember a sent packet so its ACK can be recognised."""
    if packet_id in (None, '', 0):
        return
    now = time.monotonic()
    with _lock:
        for key in [k for k, v in _tracked.items() if now - v["at"] > _TTL_SECONDS]:
            _tracked.pop(key, None)
        _tracked[str(packet_id)] = {"dest": _node_num(dest), "at": now, "acked": False}


def acked(packet_ids) -> bool:
    """True only when every one of these packets was acknowledged by its recipient."""
    ids = [str(p) for p in (packet_ids or []) if p not in (None, '')]
    if not ids:
        return False
    with _lock:
        return all(_tracked.get(p, {}).get("acked") for p in ids)


def on_routing_packet(packet, interface) -> None:
    """pubsub listener for meshtastic.receive.routing.

    Both arguments required: pypubsub makes a subtopic listener take every
    argument its parent topic requires, and the BBS's meshtastic.receive
    listener requires interface. An optional interface here made the
    subscription itself raise.
    """
    try:
        decoded = packet.get("decoded") or {}
        request_id = decoded.get("requestId")
        if request_id is None:
            return
        routing = decoded.get("routing") or {}
        if routing.get("errorReason", "NONE") != "NONE":
            return  # a NAK is simply "not confirmed"
        with _lock:
            entry = _tracked.get(str(request_id))
            # Only the recipient's own ACK counts. An implicit ACK comes from
            # our radio, having heard a relay rebroadcast the packet.
            if entry is not None and entry["dest"] is not None \
                    and packet.get("from") == entry["dest"]:
                entry["acked"] = True
    except Exception:
        return


def subscribe(topic: str = "meshtastic.receive.routing") -> None:
    """Start listening, once per process."""
    global _subscribed
    if _subscribed:
        return
    from pubsub import pub
    pub.subscribe(on_routing_packet, topic)
    _subscribed = True


def reset() -> None:
    """Forget everything tracked. For tests."""
    with _lock:
        _tracked.clear()
