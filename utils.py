import logging
import base64
import hashlib
import os
import random
import re
import time
import threading
import configparser
from datetime import datetime
from typing import Optional

# Consecutive send failure tracking — triggers os._exit so the process can be
# restarted (by systemd on Linux, or manually on Windows) after a TCP drop.
_consecutive_send_failures: int = 0
_MAX_CONSECUTIVE_SEND_FAILURES: int = 10

user_states = {}

# Conservative single-packet byte ceiling for Meshtastic TEXT_MESSAGE packets.
# Most LoRa/Meshtastic configurations cap the data payload at 228 bytes; we stay
# under 220 to leave room for packet-layer overhead and multi-byte UTF-8 chars.
_MESHTASTIC_MAX_BYTES = 220


def get_max_text_bytes(interface=None) -> int:
    """Return the active transport's safe single-message UTF-8 byte limit."""
    try:
        value = int(getattr(interface, 'max_text_bytes', _MESHTASTIC_MAX_BYTES))
    except (TypeError, ValueError):
        value = _MESHTASTIC_MAX_BYTES
    return max(32, value)

# ---------------------------------------------------------------------------
# Wire protocol version + capability advertisement
# ---------------------------------------------------------------------------
#
# A trailing ``vN:cap1,cap2,...`` token on every SYNCSTATE frame lets peers
# negotiate optional wire-format trims (compact channel keys, epoch timestamps,
# single-char scope codes, plain UTF-8 text, bitmap gap-fill).  Each new
# capability is added to ``WIRE_CAPABILITIES`` in the PR that ships it; senders
# gate the new format on ``db_operations.peer_supports(peer_id, cap)``.  Old
# peers ignore the trailing field, new peers ignore unknown caps — so the
# rollout is loss-free in either direction.
WIRE_PROTOCOL_VERSION: int = 2
WIRE_CAPABILITIES: tuple = ('cck', 'epoch', 'scc', 'nob64', 'bmgap', 'cuid', 'pgos')  # 'cck'=compact channel-comment keys, 'epoch'=epoch timestamps, 'scc'=single-char scope codes, 'nob64'=drop base64 on text fields, 'bmgap'=bitmap-base85 gap-fill encoding, 'cuid'=compact UUIDs in CONT/META frames, 'pgos'=peer-gossip (relay known peers' sync state)

# Single-char scope codes used by the 'scc' wire capability.  Senders gate
# encoding on peers_all_support(peers, 'scc'); receivers always pass tokens
# through decode_scope which maps codes back to long names but leaves long
# names untouched (so legacy senders Just Work).
SCOPE_TO_CODE: dict = {
    'bulletins': 'b',
    'mail': 'm',
    'channels': 'c',
    'channel_comments': 'C',  # capital C distinguishes from 'channels'
    'profiles': 'p',
    'zork_saves': 'z',
    'game_scores': 'g',
    'tombstones': 't',
}
CODE_TO_SCOPE: dict = {v: k for k, v in SCOPE_TO_CODE.items()}


def encode_scope(scope: str, use_codes: bool = False) -> str:
    """Return single-char code for *scope* when ``use_codes`` and a mapping
    exists; otherwise return the scope name unchanged.  Unknown scopes (e.g.
    the literal 'all' used by HASHREQ) always pass through unchanged.
    """
    if not scope:
        return scope or ''
    if use_codes:
        return SCOPE_TO_CODE.get(scope, scope)
    return scope


def decode_scope(token: str) -> str:
    """Return long scope name for a single-char ``token`` when known;
    otherwise return ``token`` unchanged so legacy long-name senders work.
    """
    if not token:
        return token or ''
    return CODE_TO_SCOPE.get(token, token)


# ---------------------------------------------------------------------------
# 'cuid' wire capability: compact UUIDs on the wire.
#
# A canonical UUID is 36 chars ("550e8400-e29b-41d4-a716-446655440000").
# Encoding its 16 raw bytes as unpadded URL-safe base64 yields 22 chars; a
# leading '*' sentinel marks the compacted form, for 23 chars total — saving
# 13 bytes everywhere a unique_id appears on the wire (CONT/META frames repeat
# it, so multi-packet records save 13B per frame).
#
# The '*' sentinel is not a hex digit, not in the URL-safe base64 alphabet, and
# not the pipe delimiter, so it is unambiguous. unique_ids that are NOT valid
# UUIDs (legacy / externally-sourced rows) are sent verbatim — decode is a
# lossless round-trip to the canonical lowercase-hyphenated form, so the
# manifest/DB key never drifts.
# ---------------------------------------------------------------------------
_CUID_SENTINEL = '*'


def encode_uid(uid: str, use_cuid: bool = False) -> str:
    """Compact a canonical UUID to ``*<base64>`` when ``use_cuid`` and *uid* is
    a valid UUID; otherwise return *uid* unchanged."""
    if not use_cuid or not uid:
        return uid or ''
    try:
        import uuid as _uuid
        raw = _uuid.UUID(str(uid)).bytes
    except (ValueError, AttributeError, TypeError):
        return uid  # not a UUID — send verbatim
    return _CUID_SENTINEL + base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def decode_uid(token: str) -> str:
    """Expand a ``*<base64>`` compact UUID back to canonical form; pass any
    other token through unchanged (legacy full-UUID and non-UUID senders)."""
    if not token or not token.startswith(_CUID_SENTINEL):
        return token or ''
    body = token[1:]
    try:
        import uuid as _uuid
        padded = body + '=' * (-len(body) % 4)
        raw = base64.urlsafe_b64decode(padded.encode('ascii'))
        return str(_uuid.UUID(bytes=raw))
    except (ValueError, AttributeError, TypeError):
        return token  # malformed — leave as-is rather than corrupt the key


def local_capabilities_token() -> str:
    """Return the ``vN:cap1,cap2`` token to append to outbound SYNCSTATE frames.

    The static WIRE_CAPABILITIES are always advertised. ``apigw`` is advertised
    only when this node is configured as an API gateway ([gateway] enabled), so
    peers route APIREQ only to nodes that can actually fulfill them. It is kept
    out of the static tuple so the advertisement is config-driven (and so the
    static-tuple is a stable, testable constant)."""
    caps = list(WIRE_CAPABILITIES)
    if _config_bool('gateway', 'enabled', False):
        caps.append('apigw')
        caps.append('apigf')  # gateway can serve per-rid response gap-fill (Phase 3)
        caps.append('apimb')  # gateway offers store-and-forward mailbox via APIPOLL (Phase 2)
    return f"v{WIRE_PROTOCOL_VERSION}:{','.join(caps)}"


def parse_capabilities_token(token: str) -> tuple:
    """Parse a peer's ``vN:cap1,cap2`` advertisement.

    Returns ``(proto_v, caps_csv)`` where ``proto_v`` is the int version (0
    when missing/malformed) and ``caps_csv`` is the raw comma-separated cap
    list with no surrounding whitespace (empty string when absent).  This
    helper never raises — malformed input degrades to ``(0, '')`` so receivers
    can keep going.
    """
    if not token:
        return (0, '')
    s = str(token).strip()
    if not s.startswith('v') or ':' not in s:
        return (0, '')
    try:
        version_part, caps_part = s.split(':', 1)
        proto_v = int(version_part[1:])
    except (ValueError, IndexError):
        return (0, '')
    if proto_v < 0:
        return (0, '')
    caps_clean = ','.join(tok for tok in (c.strip() for c in caps_part.split(',')) if tok)
    return (proto_v, caps_clean)


def _get_config_path() -> str:
    return os.getenv("BBS_CONFIG_PATH", "config.ini")


def _load_runtime_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read(_get_config_path())
    return config


def _config_raw(section: str, option: str) -> Optional[str]:
    try:
        config = _load_runtime_config()
        if config.has_option(section, option):
            return config.get(section, option).strip()
    except Exception:
        return None
    return None


def _config_bool(section: str, option: str, default: bool) -> bool:
    raw = _config_raw(section, option)
    if raw is None or raw == "":
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _config_float(section: str, option: str, default: float) -> float:
    raw = _config_raw(section, option)
    if raw is None or raw == "":
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _config_int(section: str, option: str, default: int) -> int:
    raw = _config_raw(section, option)
    if raw is None or raw == "":
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _is_sync_turbo_enabled() -> bool:
    env_value = os.getenv("BBS_SYNC_TURBO")
    if env_value is not None:
        return str(env_value).strip().lower() in ("1", "true", "yes", "on")
    return _config_bool("sync", "sync_turbo", False)


def _effective_turbo(interface=None) -> bool:
    """True if THIS call's pacing should use turbo defaults.

    An interface that reports itself as low-latency (currently only
    MqttInterface -- MQTT has none of LoRa's payload-size or half-duplex
    constraints) always gets turbo pacing for its own sync/repair calls,
    independent of the global [sync] sync_turbo flag. This is what lets a
    mixed LoRa+MQTT bridge node run its fragile radio at normal pacing
    while its MQTT link runs fast, simultaneously -- the global-only flag
    can't express that combination. interface=None (every call site before
    this existed) falls straight through to the global flag, unchanged.
    """
    if interface is not None and getattr(interface, 'is_low_latency', False):
        return True
    return _is_sync_turbo_enabled()


def _env_float(name: str, default: float) -> float:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.0, value)


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def get_sync_pause_seconds(interface=None) -> float:
    turbo = _effective_turbo(interface)
    default = 0.02 if turbo else 0.75
    if os.getenv("BBS_SYNC_PAUSE_SECONDS") is not None:
        return _env_float("BBS_SYNC_PAUSE_SECONDS", default)
    return _config_float("sync", "sync_pause_seconds", default)


def get_hash_repair_pause_seconds(interface=None) -> float:
    turbo = _effective_turbo(interface)
    default = 0.0 if turbo else 0.1
    if os.getenv("BBS_HASH_REPAIR_PAUSE_SECONDS") is not None:
        return _env_float("BBS_HASH_REPAIR_PAUSE_SECONDS", default)
    return _config_float("sync", "hash_repair_pause_seconds", default)


def get_hash_chunk_pause_seconds(interface=None) -> float:
    """Minimum airtime gap between consecutive HASHZ manifest chunks and HASHREQ
    frames sent to the same peer.

    Multi-chunk HASHZ manifests and HASHREQ bursts (one per scope) MUST clear the
    receiver's RX path between frames or LoRa drops the trailing frames. This
    1.5s floor applies on LoRa regardless of ``sync_turbo`` because back-to-back
    small frames cause silent manifest loss that breaks the entire reconcile
    cycle. A low-latency transport (e.g. MQTT, which has none of LoRa's
    half-duplex/collision constraints) gets a near-zero default instead, the
    same way the other pacing getters in this module do -- see
    ``_effective_turbo``."""
    turbo = _effective_turbo(interface)
    default = 0.0 if turbo else 1.5
    if os.getenv("BBS_HASH_CHUNK_PAUSE_SECONDS") is not None:
        return _env_float("BBS_HASH_CHUNK_PAUSE_SECONDS", default)
    return _config_float("sync", "hash_chunk_pause_seconds", default)


def get_full_sync_delay_ms(interface=None) -> int:
    turbo = _effective_turbo(interface)
    default = 0 if turbo else 500
    if os.getenv("BBS_FULL_SYNC_DELAY_MS") is not None:
        return _env_int("BBS_FULL_SYNC_DELAY_MS", default)
    return _config_int("sync", "full_sync_delay_ms", default)


def get_repair_cycle_seconds(interface=None) -> int:
    """Minimum seconds between SYNCSTATE-triggered repair cycles for the same peer/scope.

    Lower values converge mismatches faster but risk repair-storms on busy meshes.
    Turbo mode shrinks this aggressively for small (e.g. 2-node) deployments.
    """
    turbo = _effective_turbo(interface)
    default = 15 if turbo else 90
    if os.getenv("BBS_REPAIR_CYCLE_SECONDS") is not None:
        return _env_int("BBS_REPAIR_CYCLE_SECONDS", default)
    return _config_int("sync", "repair_cycle_seconds", default)


def get_reconcile_max_per_pass(interface=None) -> int:
    """Cap on records pulled (HASHMISS) or pushed per single reconcile pass.

    Higher values converge larger mismatches in one cycle but tie up the receive
    callback longer. Turbo mode raises this for small meshes where collisions
    are rare.
    """
    turbo = _effective_turbo(interface)
    default = 100 if turbo else 20
    if os.getenv("BBS_RECONCILE_MAX_PER_PASS") is not None:
        return _env_int("BBS_RECONCILE_MAX_PER_PASS", default)
    return _config_int("sync", "reconcile_max_per_pass", default)


def get_sync_runtime_settings() -> dict:
    # Global diagnostics snapshot -- no interface context, so this always
    # reports the un-overridden [sync] settings even on a node with a
    # low-latency (e.g. MQTT) link active. See _effective_turbo.
    return {
        "sync_turbo": _is_sync_turbo_enabled(),
        "sync_pause_seconds": get_sync_pause_seconds(),
        "hash_repair_pause_seconds": get_hash_repair_pause_seconds(),
        "hash_chunk_pause_seconds": get_hash_chunk_pause_seconds(),
        "full_sync_delay_ms": get_full_sync_delay_ms(),
        "repair_cycle_seconds": get_repair_cycle_seconds(),
        "reconcile_max_per_pass": get_reconcile_max_per_pass(),
        "env_overrides": {
            "sync_turbo": os.getenv("BBS_SYNC_TURBO") is not None,
            "sync_pause_seconds": os.getenv("BBS_SYNC_PAUSE_SECONDS") is not None,
            "hash_repair_pause_seconds": os.getenv("BBS_HASH_REPAIR_PAUSE_SECONDS") is not None,
            "hash_chunk_pause_seconds": os.getenv("BBS_HASH_CHUNK_PAUSE_SECONDS") is not None,
            "full_sync_delay_ms": os.getenv("BBS_FULL_SYNC_DELAY_MS") is not None,
            "repair_cycle_seconds": os.getenv("BBS_REPAIR_CYCLE_SECONDS") is not None,
            "reconcile_max_per_pass": os.getenv("BBS_RECONCILE_MAX_PER_PASS") is not None,
        },
    }


def get_syncstate_heartbeat_seconds() -> int:
    if os.getenv("BBS_SYNCSTATE_HEARTBEAT_SECONDS") is not None:
        return _env_int("BBS_SYNCSTATE_HEARTBEAT_SECONDS", 1800)
    return _config_int("sync", "syncstate_heartbeat_seconds", 1800)


def is_zork_save_sync_enabled() -> bool:
    env_value = os.getenv("BBS_SYNC_ZORK_SAVES")
    if env_value is not None:
        return str(env_value).strip().lower() in ("1", "true", "yes", "on")
    return _config_bool("sync", "sync_zork_saves", False)


def get_zork_save_sync_notice() -> str:
    if is_zork_save_sync_enabled():
        return ""
    return (
        "Warning: this node does not sync game saves. Progress is saved only on this node "
        "and will not follow you to other BBS nodes."
    )


def select_syncstate_peers_to_notify(peer_node_ids, counts, sent_cache, now=None, force=False, heartbeat_seconds=None):
    """Return peers that should receive a SYNCSTATE broadcast and update the cache.

    Peers are notified when forced, when local counts/hashes changed, or when the
    heartbeat interval expires so remote nodes still get an occasional refresh.
    """
    if now is None:
        now = time.time()
    if heartbeat_seconds is None:
        heartbeat_seconds = get_syncstate_heartbeat_seconds()

    normalized_peers = sorted({str(peer).strip() for peer in (peer_node_ids or []) if str(peer).strip()})
    live_peers = set(normalized_peers)
    for peer_id in list(sent_cache.keys()):
        if peer_id not in live_peers:
            sent_cache.pop(peer_id, None)

    signature = tuple(
        str(counts.get(key, ""))
        for key in (
            "bulletins", "mail", "channels", "zork_saves", "profiles", "game_scores",
            "bulletins_hash", "mail_hash", "channels_hash", "zork_saves_hash", "profiles_hash", "game_scores_hash",
        )
    )
    destinations = []
    for peer_id in normalized_peers:
        last_state = sent_cache.get(peer_id, {})
        last_signature = last_state.get("signature")
        last_sent_at = float(last_state.get("sent_at", 0.0) or 0.0)
        heartbeat_due = heartbeat_seconds <= 0 or (now - last_sent_at) >= float(heartbeat_seconds)
        if force or last_signature != signature or not last_state or heartbeat_due:
            sent_cache[peer_id] = {"signature": signature, "sent_at": now}
            destinations.append(peer_id)
    return destinations


def _take_prefix_within_bytes(text: str, max_bytes: int) -> tuple[str, str]:
    """Return the largest UTF-8-safe prefix that fits max_bytes and the remainder."""
    if max_bytes <= 0 or not text:
        return "", text
    used = 0
    idx = 0
    for idx, ch in enumerate(text):
        b = len(ch.encode('utf-8'))
        if used + b > max_bytes:
            return text[:idx], text[idx:]
        used += b
    return text, ""


def update_user_state(user_id, state):
    user_states[user_id] = state


def get_user_state(user_id):
    return user_states.get(user_id, None)


def _split_into_chunks(text, max_len=200):
    """Split text by UTF-8 bytes, preferring sentence and word boundaries."""
    # Collapse 3+ consecutive newlines to at most 2, and runs of spaces/tabs to one space
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = text.strip()

    chunks = []
    while text:
        if len(text.encode('utf-8')) <= max_len:
            chunks.append(text)
            break

        segment, overflow = _take_prefix_within_bytes(text, max_len)

        # Find the last sentence-ending punctuation followed by whitespace in the segment
        best = -1
        for m in re.finditer(r'[.!?]["\']?(?=\s)', segment):
            best = m.end()

        if best > 10:
            split_pos = best
        else:
            # Fall back to last newline
            nl = segment.rfind('\n')
            if nl > 10:
                split_pos = nl + 1
            else:
                # Fall back to last space
                sp = segment.rfind(' ')
                split_pos = sp + 1 if sp > 10 else len(segment)

        chunks.append(text[:split_pos].rstrip())
        text = (segment[split_pos:] + overflow).lstrip()

    return chunks


def send_message(message, destination, interface) -> bool:
    """Send (chunked to the transport's limit). True if every chunk went.

    The return value matters for anything delivered asynchronously: a
    gateway reply arriving a minute after the question has no other way to
    learn it never landed, and the user is left staring at silence.
    Callers that send inline can go on ignoring it.
    """
    chunks = _split_into_chunks(message, max_len=get_max_text_bytes(interface))
    if not chunks:
        # An empty or whitespace-only body yields no chunks, so the loop
        # below never runs: nothing is sent, nothing is raised, and the
        # caller is told it succeeded. Whoever asked to send this meant to
        # say something, so treat it as a failed send and say where from.
        logging.warning(
            f"send_message called with an empty body for {destination}; "
            f"nothing was sent")
        return False

    delivered = True
    for chunk in chunks:
        try:
            d = interface.sendText(
                text=chunk,
                destinationId=destination,
                wantAck=True,
                wantResponse=False
            )
            destid = get_node_id_from_num(destination, interface)
            log_chunk = chunk.replace('\n', '\\n')
            logging.info(f"Sending message to user '{get_node_short_name(destid, interface)}' ({destid}) with sendID {d.id}: \"{log_chunk}\"")
        except Exception as e:
            delivered = False
            # WARNING, and name the transport: this was a bare INFO line
            # with no context, so a reply that failed to reach one radio
            # left nothing in the log saying which radio, or for whom.
            protocol = getattr(interface, 'protocol_name', 'unknown')
            logging.warning(
                f"REPLY SEND ERROR to {destination} over {protocol}: {e}")

        time.sleep(2)
    return delivered


def get_node_info(interface, short_name):
    nodes = [{'num': node_id, 'shortName': node['user']['shortName'], 'longName': node['user']['longName']}
             for node_id, node in interface.nodes.items()
             if node['user']['shortName'].lower() == short_name]
    return nodes


def get_node_id_from_num(node_num, interface):
    resolver = getattr(interface, 'node_id_from_num', None)
    if callable(resolver):
        resolved = resolver(node_num)
        if resolved:
            return resolved
    for node_id, node in interface.nodes.items():
        if node['num'] == node_num:
            return node_id
    return None


def home_network(node_id) -> str:
    """Return 'meshtastic', 'meshcore', or 'mqtt' based on node-id string shape.

    Meshtastic node ids are '!'-prefixed hex (e.g. '!04058ac8'); MeshCore
    node ids are bare hex public keys/prefixes with no '!' (see
    meshcore_interface.py's _clean_key and the README's node-id docs);
    MQTT-bridged node ids are always prefixed 'mqtt:' (see
    mqtt_interface.py's _mqtt_node_id) specifically so they classify here
    instead of silently falling into the 'meshcore' default the way an
    unrecognized shape otherwise would. This is a cheap, zero-new-state way
    to tell which network a peer id belongs to -- used by dual-radio/
    multi-link bridge mode (server.py's RadioLink routing) as a defensive
    fallback, not the primary safety mechanism (each link's own
    bbs_nodes/subscriber_nodes list, read from a separate config section,
    is what actually keeps different networks' peers from being conflated;
    see server._link_for_node's peer-list-membership-first match).
    """
    text = str(node_id or '').strip()
    if text.startswith('!'):
        return 'meshtastic'
    if text.startswith('mqtt:'):
        return 'mqtt'
    return 'meshcore'


def get_node_short_name(node_id, interface):
    node_info = interface.nodes.get(node_id)
    if node_info:
        return node_info['user']['shortName']
    return None


def resolve_display_name(node_id, interface):
    """Like get_node_short_name, but if node_id is linked to a multi-device
    account with a non-empty alias, returns the alias instead. Falls
    through to exactly get_node_short_name's behavior (including returning
    None) when there's no account link or no alias set -- an unlinked
    node's display name is completely unaffected by the account system.

    Resolved once, at authorship time, by every caller that captures a
    sender's name into a bulletin/mail/channel-comment row -- there is no
    live join at display/read time anywhere in this codebase, so (matching
    how a node's own short_name change today never rewrites already-posted
    content) an alias change never retroactively rewrites old posts either.

    db_operations is imported lazily here to avoid a circular import (it
    already imports several send_* helpers from this module).
    """
    try:
        from db_operations import get_account_id_for_node, get_account_alias
        account_id = get_account_id_for_node(node_id)
        if account_id:
            alias = get_account_alias(account_id)
            if alias:
                return alias
    except Exception:
        pass
    return get_node_short_name(node_id, interface)


def send_bulletin_to_bbs_nodes(board, sender_short_name, subject, content, unique_id, bbs_nodes, interface, date=None, source_node_id=None, source_timestamp=None):
    header = f"BULLETIN|{board}|{sender_short_name}|{subject}|"
    _use_epoch = peers_all_support(bbs_nodes, 'epoch')
    if source_node_id and source_timestamp:
        date_str = encode_ts_minute(date, _use_epoch) if date else ''
        src_ts_str = encode_ts_second(source_timestamp, _use_epoch)
        footer = f"|{unique_id}|{date_str}|{source_node_id}|{src_ts_str}"
    elif date:
        footer = f"|{unique_id}|{encode_ts_minute(date, _use_epoch)}"
    else:
        footer = f"|{unique_id}"
    # Compact the uid in the repeated CONT/META frames when peers support 'cuid'.
    _wire_uid = encode_uid(unique_id, peers_all_support(bbs_nodes, 'cuid'))
    _send_sync_with_cont(
        header, footer, content, unique_id,
        cont_prefix=f"BULLETINCONT|{_wire_uid}|",
        meta_prefix=f"BULLETINMETA|{_wire_uid}|",
        bbs_nodes=bbs_nodes, interface=interface,
        pause_seconds=get_sync_pause_seconds(interface),
    )


def send_mail_to_bbs_nodes(sender_id, sender_short_name, recipient_id, subject, content, unique_id, bbs_nodes,
                           interface, date=None, source_node_id=None, source_timestamp=None):
    logging.info(f"SERVER SYNC: Syncing new mail message '{subject}' from {sender_short_name} to peers.")
    header = f"MAIL|{sender_id}|{sender_short_name}|{recipient_id}|{subject}|"
    _use_epoch = peers_all_support(bbs_nodes, 'epoch')
    if source_node_id and source_timestamp:
        date_str = encode_ts_minute(date, _use_epoch) if date else ''
        src_ts_str = encode_ts_second(source_timestamp, _use_epoch)
        footer = f"|{unique_id}|{date_str}|{source_node_id}|{src_ts_str}"
    elif date:
        footer = f"|{unique_id}|{encode_ts_minute(date, _use_epoch)}"
    else:
        footer = f"|{unique_id}"
    _wire_uid = encode_uid(unique_id, peers_all_support(bbs_nodes, 'cuid'))
    _send_sync_with_cont(
        header, footer, content, unique_id,
        cont_prefix=f"MAILCONT|{_wire_uid}|",
        meta_prefix=f"MAILMETA|{_wire_uid}|",
        bbs_nodes=bbs_nodes, interface=interface,
        pause_seconds=get_sync_pause_seconds(interface),
    )


def send_delete_bulletin_to_bbs_nodes(unique_id, bbs_nodes, interface):
    message = f"DELETE_BULLETIN|{unique_id}"
    for node_id in bbs_nodes:
        _send_one_sync(message, node_id, interface)


def send_delete_mail_to_bbs_nodes(unique_id, bbs_nodes, interface):
    message = f"DELETE_MAIL|{unique_id}"
    logging.info(f"SERVER SYNC: Sending delete mail sync for unique_id: {unique_id}")
    for node_id in bbs_nodes:
        _send_one_sync(message, node_id, interface)


def send_delete_zork_save_to_bbs_nodes(user_id, game_id, deleted_at, bbs_nodes, interface):
    if not is_zork_save_sync_enabled():
        return
    _use_epoch = peers_all_support(bbs_nodes, 'epoch')
    _use_plain = peers_all_support(bbs_nodes, 'nob64')
    deleted_at_wire = encode_ts_second(deleted_at, _use_epoch)
    message = f"DELETE_ZORKSAVE|{encode_text(str(user_id), _use_plain)}|{encode_text(str(game_id), _use_plain)}|{deleted_at_wire}"
    logging.info(f"SERVER SYNC: Sending delete zork save sync for user_id={user_id} game_id={game_id}")
    for node_id in bbs_nodes:
        _send_one_sync(message, node_id, interface)


def send_channel_to_bbs_nodes(name, url, bbs_nodes, interface):
    message = f"CHANNEL|{name}|{url}"
    for node_id in bbs_nodes:
        _send_one_sync(message, node_id, interface)


def compact_channel_manifest_key(full_key: str) -> str:
    """Return an 8-char compact hash key for use in CHANNELCOMMENT wire frames
    when the full base64(name+url) key would make the frame exceed the packet limit.
    Prefixed with '~' so it is unambiguous and cannot be mistaken for a full key.
    """
    digest = hashlib.blake2b(full_key.encode('ascii'), digest_size=6).digest()
    return '~' + base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')


# ---------------------------------------------------------------------------
# Epoch timestamp helpers (PR 2 — 'epoch' capability)
# ---------------------------------------------------------------------------
# Wire form:
#   m<seconds>   - minute-precision timestamp; decodes to "YYYY-MM-DD HH:MM"
#                  (used for content dates in BULLETIN/MAIL/CHANNELCOMMENT)
#   s<seconds>   - second-precision timestamp; decodes to "YYYY-MM-DDTHH:MM:SS"
#                  (used for ISO source_timestamp trailers and *SYNC fields)
# Distinct prefixes let the receiver's positional parser disambiguate even when
# multiple optional timestamp fields are present.  Senders emit epoch form
# only when *all* peers in the call support the 'epoch' capability; legacy
# peers keep receiving ISO strings byte-for-byte unchanged.

_EPOCH_MIN_PATTERN = re.compile(r'^m\d+$')
_EPOCH_SEC_PATTERN = re.compile(r'^s\d+$')


def _ts_to_epoch_seconds(value) -> Optional[int]:
    """Best-effort: convert input → epoch seconds int. Returns None on failure."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    # Already in epoch form?
    if _EPOCH_MIN_PATTERN.match(s) or _EPOCH_SEC_PATTERN.match(s):
        return int(s[1:])
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    return None


def encode_ts_minute(value, use_epoch: bool = False) -> str:
    """Encode a content-date value for the wire.

    When ``use_epoch`` is True and the value can be parsed, returns
    ``m<seconds>`` rounded down to the minute (matching ISO ``YYYY-MM-DD HH:MM``
    precision).  Otherwise returns the original value as a string.
    """
    if value is None:
        return ""
    if not use_epoch:
        return str(value)
    sec = _ts_to_epoch_seconds(value)
    if sec is None:
        return str(value)
    return f"m{(sec // 60) * 60}"


def encode_ts_second(value, use_epoch: bool = False) -> str:
    """Encode a second-precision (ISO with T) timestamp for the wire.

    When ``use_epoch`` is True and the value can be parsed, returns
    ``s<seconds>``; otherwise returns the original value as a string.
    """
    if value is None:
        return ""
    if not use_epoch:
        return str(value)
    sec = _ts_to_epoch_seconds(value)
    if sec is None:
        return str(value)
    return f"s{sec}"


def decode_ts_minute(token: str) -> str:
    """Decode a wire timestamp token to ``YYYY-MM-DD HH:MM`` if it's epoch form;
    otherwise return the token unchanged (pass-through for legacy ISO senders).
    """
    if token and _EPOCH_MIN_PATTERN.match(token):
        try:
            return datetime.fromtimestamp(int(token[1:])).strftime("%Y-%m-%d %H:%M")
        except (OSError, ValueError, OverflowError):
            return token
    return token


def decode_ts_second(token: str) -> str:
    """Decode a wire timestamp token to ``YYYY-MM-DDTHH:MM:SS`` if it's epoch
    form; otherwise return the token unchanged.
    """
    if token and _EPOCH_SEC_PATTERN.match(token):
        try:
            return datetime.fromtimestamp(int(token[1:])).strftime("%Y-%m-%dT%H:%M:%S")
        except (OSError, ValueError, OverflowError):
            return token
    return token


def peers_all_support(peer_ids, cap: str) -> bool:
    """True iff every peer in ``peer_ids`` has advertised the given cap via SYNCSTATE.

    Empty / falsy peer set returns False — we never assume support without proof.
    Resolves ``db_operations.peer_supports`` lazily to avoid circular imports.
    """
    if not peer_ids:
        return False
    try:
        from db_operations import peer_supports
    except Exception:
        return False
    try:
        return all(peer_supports(p, cap) for p in peer_ids)
    except Exception:
        return False


def send_channel_comment_to_bbs_nodes(channel_key, sender_short_name, comment_date, content, unique_id, bbs_nodes, interface, source_node_id=None, source_timestamp=None):
    _use_epoch = peers_all_support(bbs_nodes, 'epoch')
    _use_plain = peers_all_support(bbs_nodes, 'nob64')
    comment_date_wire = encode_ts_minute(comment_date, _use_epoch) if comment_date else (comment_date or "")
    if source_node_id and source_timestamp:
        footer = f"|{unique_id}|{source_node_id}|{encode_ts_second(source_timestamp, _use_epoch)}"
    else:
        footer = f"|{unique_id}"
    _sender_wire = encode_text(sender_short_name, _use_plain)
    full_header = f"CHANNELCOMMENT|{channel_key}|{_sender_wire}|{comment_date_wire}|"
    short_key = compact_channel_manifest_key(channel_key)
    short_header = f"CHANNELCOMMENT|{short_key}|{_sender_wire}|{comment_date_wire}|"

    # 'cck' capability: peer advertises that it prefers compact channel keys,
    # so always send the short header to it (saves 40-90 bytes per frame).
    # Non-cck peers keep the legacy behaviour: full key when it fits, compact
    # key only when the full header would crowd content below the minimum
    # single-packet threshold.
    _MIN_CHANNEL_COMMENT_CONTENT_BYTES = 8
    full_too_big = (
        len(full_header.encode('utf-8')) + len(footer.encode('utf-8'))
        > get_max_text_bytes(interface) - _MIN_CHANNEL_COMMENT_CONTENT_BYTES
    )

    if len(short_header.encode('utf-8')) + len(footer.encode('utf-8')) > get_max_text_bytes(interface):
        logging.warning(
            f"CHANNELCOMMENT header exceeds packet limit even with compact key for uid={unique_id}; skipping"
        )
        return

    try:
        from db_operations import peer_supports
    except Exception:
        peer_supports = lambda *_a, **_k: False  # noqa: E731 — circular-import safe fallback

    cck_peers = []
    legacy_peers = []
    for node_id in bbs_nodes:
        if peer_supports(node_id, 'cck'):
            cck_peers.append(node_id)
        else:
            legacy_peers.append(node_id)

    if cck_peers:
        _cck_uid = encode_uid(unique_id, peers_all_support(cck_peers, 'cuid'))
        _send_sync_with_cont(
            short_header, footer, content, unique_id,
            cont_prefix=f"CHANNELCOMMENTCONT|{_cck_uid}|",
            meta_prefix=f"CHANNELCOMMENTMETA|{_cck_uid}|",
            bbs_nodes=cck_peers, interface=interface,
            pause_seconds=get_sync_pause_seconds(interface),
        )

    if legacy_peers:
        legacy_header = short_header if full_too_big else full_header
        _legacy_uid = encode_uid(unique_id, peers_all_support(legacy_peers, 'cuid'))
        _send_sync_with_cont(
            legacy_header, footer, content, unique_id,
            cont_prefix=f"CHANNELCOMMENTCONT|{_legacy_uid}|",
            meta_prefix=f"CHANNELCOMMENTMETA|{_legacy_uid}|",
            bbs_nodes=legacy_peers, interface=interface,
            pause_seconds=get_sync_pause_seconds(interface),
        )


def send_delete_channel_comment_to_bbs_nodes(unique_id, bbs_nodes, interface):
    message = f"DELETE_CHANNELCOMMENT|{unique_id}"
    for node_id in bbs_nodes:
        _send_one_sync(message, node_id, interface)


def send_sync_state_to_bbs_nodes(counts, bbs_nodes, interface):
    """Send compact local record counts and hashes to peers for mismatch detection."""
    message = (
        f"SYNCSTATE|{int(counts.get('bulletins', 0))}|{int(counts.get('mail', 0))}|"
        f"{int(counts.get('channels', 0))}|{int(counts.get('zork_saves', 0))}|"
        f"{int(counts.get('profiles', 0))}|{int(counts.get('game_scores', 0))}|"
        f"{str(counts.get('bulletins_hash', ''))}|{str(counts.get('mail_hash', ''))}|"
        f"{str(counts.get('channels_hash', ''))}|{str(counts.get('zork_saves_hash', ''))}|"
        f"{str(counts.get('profiles_hash', ''))}|{str(counts.get('game_scores_hash', ''))}|"
        f"{int(counts.get('tombstones', 0))}|"
        f"{local_capabilities_token()}"
    )
    for node_id in bbs_nodes:
        _send_one_sync(message, node_id, interface)


def build_peer_gossip_frames(local_node_id: str, recipient_id: str):
    """Build PEERGOSSIP frames relaying what we know about OTHER peers.

    Wire format: ``PEERGOSSIP|<peer_id>|b|m|c|z|p|g|t|<age_secs>``
    where age_secs is how long ago we last heard that peer's SYNCSTATE. The
    recipient translates age into its own clock so this works across unsynced
    node clocks. We never relay the recipient's own state back to it, nor our
    own (that travels via SYNCSTATE). One frame per known peer; each is a
    self-contained single packet.
    """
    frames = []
    try:
        from db_operations import get_peer_sync_states
        from datetime import datetime as _dt
        rows = get_peer_sync_states()
    except Exception:
        return frames
    now = _dt.now()
    for row in rows:
        peer_id = str(row[0])
        if not peer_id or peer_id == recipient_id or peer_id == local_node_id:
            continue
        try:
            reported_at = str(row[13]) if len(row) > 13 else ''
            age = 0
            if reported_at:
                age = max(0, int((now - _dt.strptime(reported_at, '%Y-%m-%d %H:%M:%S')).total_seconds()))
            tomb = int(row[14]) if len(row) > 14 and row[14] is not None else -1
            frame = (
                f"PEERGOSSIP|{peer_id}|{int(row[1] or 0)}|{int(row[2] or 0)}|"
                f"{int(row[3] or 0)}|{int(row[4] or 0)}|{int(row[5] or 0)}|"
                f"{int(row[6] or 0)}|{tomb}|{age}"
            )
        except (ValueError, TypeError, IndexError):
            continue
        frames.append(frame)
    return frames


def send_peer_gossip_to_bbs_nodes(local_node_id: str, bbs_nodes, interface) -> None:
    """Relay known peer sync-states to each pgos-capable peer (one hop per cycle)."""
    if not local_node_id or not bbs_nodes:
        return
    for node_id in bbs_nodes:
        if not peers_all_support([node_id], 'pgos'):
            continue
        for frame in build_peer_gossip_frames(local_node_id, node_id):
            _send_one_sync(frame, node_id, interface, pause_seconds=get_hash_repair_pause_seconds(interface))


# ── API gateway (apigw) ──────────────────────────────────────────────────────

# Pending API requests awaiting a response, keyed by request id (rid) →
# {'sender_id': <waiting user node>, 'created_at': ts}. Shared between the
# command handler (registers on submit) and message_processing (resolves on
# response / timeout sweep). Lives here in utils to avoid an import cycle.
_apigw_lock = threading.Lock()
_apigw_pending: dict = {}


def register_api_request(rid: str, sender_id, gateway_node_id=None, kind=None) -> None:
    with _apigw_lock:
        _apigw_pending[rid] = {
            'sender_id': sender_id,
            'created_at': time.time(),
            'gateway': gateway_node_id,
            'last_gap_req': 0.0,
            'kind': kind,  # 'r' (AI relay) | 'h' (HTTP GET) | None -- lets
                           # _deliver_api_response show the Project Nomad
                           # ask-another-question follow-up only for 'r'.
        }


def pop_api_request(rid: str):
    """Return the waiting user's node id for *rid* (and clear it), or None."""
    with _apigw_lock:
        entry = _apigw_pending.pop(rid, None)
    return entry['sender_id'] if entry else None


def get_api_request(rid: str):
    """Return a shallow copy of the pending entry for *rid* (or None) without
    clearing it — used by the gap-fill sweep to find the gateway peer."""
    with _apigw_lock:
        entry = _apigw_pending.get(rid)
        return dict(entry) if entry else None


def list_pending_api_requests() -> list:
    """Snapshot [(rid, entry_copy), ...] of outstanding requests for the sweep."""
    with _apigw_lock:
        return [(rid, dict(e)) for rid, e in _apigw_pending.items()]


def mark_api_gap_request(rid: str) -> None:
    """Record that we just asked the gateway to refill *rid* (cooldown clock)."""
    with _apigw_lock:
        e = _apigw_pending.get(rid)
        if e is not None:
            e['last_gap_req'] = time.time()


def expire_api_requests(timeout_sec: float) -> list:
    """Remove and return [(rid, sender_id), ...] for requests older than timeout."""
    now = time.time()
    out = []
    with _apigw_lock:
        stale = [r for r, e in _apigw_pending.items() if now - e['created_at'] > timeout_sec]
        for rid in stale:
            out.append((rid, _apigw_pending.pop(rid)['sender_id']))
    return out


# --- Gateway side: retain sent responses briefly so we can refill dropped
# --- chunks when a requester sends APIRESPGAP (Phase 3 reliability). -------
_apigw_sent: dict = {}  # rid -> {'status', 'body', 'dest', 'created_at'}


def _retain_sent_api_response(rid, status, body, dest_node_id) -> None:
    with _apigw_lock:
        _apigw_sent[rid] = {
            'status': str(status),
            'body': str(body or ""),
            'dest': dest_node_id,
            'created_at': time.time(),
        }


def expire_sent_api_responses(ttl_sec: float) -> int:
    """Drop retained responses older than ttl. Returns count removed."""
    now = time.time()
    with _apigw_lock:
        stale = [r for r, e in _apigw_sent.items() if now - e['created_at'] > ttl_sec]
        for rid in stale:
            _apigw_sent.pop(rid, None)
    return len(stale)


def send_api_poll(local_node_id, interface) -> int:
    """Requester side (Phase 2): ask every gateway peer that offers a store-and-
    forward mailbox ('apimb') to flush any responses queued for us. Returns the
    number of polls sent. Cheap to call on startup / after a reconnect."""
    if not local_node_id:
        return 0
    sent = 0
    for peer in (getattr(interface, 'bbs_nodes', []) or []):
        if peers_all_support([peer], 'apimb'):
            _send_one_sync(f"APIPOLL|{local_node_id}", peer, interface,
                           pause_seconds=get_sync_pause_seconds(interface))
            sent += 1
    return sent


def _parse_gap_ranges(spec: str, total: int) -> list:
    """Parse an APIRESPGAP range spec into [(start, end), ...] clamped to total.
    '*' (or empty) means the whole body. Ranges are 'start-end' (end exclusive),
    comma-separated."""
    spec = (spec or "").strip()
    if spec in ("", "*"):
        return [(0, total)]
    out = []
    for tok in spec.split(","):
        tok = tok.strip()
        if "-" not in tok:
            continue
        a, _, b = tok.partition("-")
        try:
            start, end = int(a), int(b)
        except ValueError:
            continue
        start = max(0, min(start, total))
        end = max(start, min(end, total))
        if end > start:
            out.append((start, end))
    return out


def resend_api_response_ranges(rid, range_spec, interface) -> bool:
    """Gateway side: re-send the requested byte ranges of a retained response as
    APIRESPCONT frames (offset 0 range re-sends the APIRESP header too, so a lost
    header is recoverable). Returns False if the rid is no longer retained."""
    with _apigw_lock:
        entry = _apigw_sent.get(rid)
        snap = dict(entry) if entry else None
    if not snap:
        return False
    body = snap['body']
    total = len(body)
    ranges = _parse_gap_ranges(range_spec, total)
    if not ranges:
        return False
    pause = get_sync_pause_seconds(interface)
    for start, end in ranges:
        chunk = body[start:end]
        if start == 0:
            # Re-send the header frame (carries status + total length).
            _send_one_sync(
                f"APIRESP|{rid}|{snap['status']}|{total}|{chunk}",
                snap['dest'], interface, pause_seconds=pause,
            )
        else:
            _send_one_sync(
                f"APIRESPCONT|{rid}|{start}|{chunk}",
                snap['dest'], interface, pause_seconds=pause,
            )
    # Always (re)send META so the requester knows the authoritative total length
    # even if the original header frame is the one that was lost.
    _send_one_sync(f"APIRESPMETA|{rid}|{total}", snap['dest'], interface, pause_seconds=pause)
    return True


def select_gateway_peer(interface):
    """Return the first peer in bbs_nodes that advertises the 'apigw' capability,
    or None if no gateway is reachable."""
    for peer in (getattr(interface, 'bbs_nodes', []) or []):
        if peers_all_support([peer], 'apigw'):
            return peer
    return None


def send_api_request(rid, requester_id, kind, payload, gateway_node_id, interface):
    """Send a single-packet APIREQ to a gateway peer. Returns False (and sends
    nothing) if the request exceeds one LoRa packet — callers should surface a
    'request too long' message. (Response chunking is handled separately; API
    requests are short by design — a URL or a prompt.)"""
    frame = f"APIREQ|{rid}|{requester_id}|{kind}|{payload}"
    if len(frame.encode('utf-8')) > get_max_text_bytes(interface):
        return False
    _send_one_sync(frame, gateway_node_id, interface, pause_seconds=get_sync_pause_seconds(interface))
    return True


def send_api_response(rid, status, body, dest_node_id, interface):
    """Send an APIRESP back to the requester, chunked via the shared CONT/META
    machinery (responses routinely exceed one packet)."""
    content = str(body or "")
    # Retain the full response briefly so we can refill dropped chunks if the
    # requester sends APIRESPGAP (Phase 3). Cleared by expire_sent_api_responses.
    _retain_sent_api_response(rid, status, content, dest_node_id)
    # Total length goes in the header so the receiver knows the expected size even
    # for a single-packet response (which carries no META frame).
    _send_sync_with_cont(
        header=f"APIRESP|{rid}|{status}|{len(content)}|",
        footer="",
        content=content,
        unique_id=rid,
        cont_prefix=f"APIRESPCONT|{rid}|",
        meta_prefix=f"APIRESPMETA|{rid}|",
        bbs_nodes=[dest_node_id],
        interface=interface,
        pause_seconds=get_sync_pause_seconds(interface),
    )


def send_have_to_bbs_nodes(local_node_id: str, bbs_nodes, interface) -> None:
    """Broadcast a HAVE frame advertising local op_log heads to all peers.

    This is the Phase-2 replacement for the SYNCSTATE heartbeat for op-log
    scopes.  SYNCSTATE is still sent alongside for backward compatibility.
    """
    if not local_node_id or not bbs_nodes:
        return
    try:
        from op_sync import build_have_frame
        for node_id in bbs_nodes:
            frame = build_have_frame(local_node_id, peer_id=node_id, interface=interface)
            if frame:
                _send_one_sync(frame, node_id, interface, pause_seconds=get_hash_repair_pause_seconds(interface))
    except Exception as exc:
        logging.warning('send_have_to_bbs_nodes failed: %s', exc)


def send_hash_request_to_bbs_nodes(bbs_nodes, interface, scope='all'):
    """Ask peers to send per-record hash manifests for selective repair.

    Uses the hash-chunk pause floor between frames so callers that loop one
    HASHREQ per scope (one per call) still leave enough RF gap for the
    receiver's radio to clear between frames. Without this, multi-scope
    HASHREQ bursts (e.g. four scopes back-to-back) lose trailing frames on
    LoRa and the peer never replies for the missing scopes.
    """
    # When requesting channels, also request the channel_comments sub-scope so
    # both halves are reconciled together without a separate call.
    scopes_to_request = [scope]
    if scope == 'channels':
        scopes_to_request.append('channel_comments')

    pause = max(get_sync_pause_seconds(interface), get_hash_chunk_pause_seconds(interface))
    for node_id in bbs_nodes:
        for _scope in scopes_to_request:
            if _scope != 'all' and peers_all_support([node_id], 'scc'):
                per_peer_msg = f"HASHREQ|{encode_scope(_scope, True)}"
            else:
                per_peer_msg = f"HASHREQ|{_scope}"
            _send_one_sync(per_peer_msg, node_id, interface, pause_seconds=pause)


def _b64(text: str) -> str:
    return base64.b64encode((text or "").encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# PR 4 — 'nob64' wire capability: plain UTF-8 text on the wire instead of
# base64, distinguished by a leading '~' sentinel.  The '~' char is not in the
# base64 alphabet, so the decoder can tell new frames from legacy ones with no
# version field needed.
#
# Inside the sentinel-prefixed payload, literal '|' (the wire delimiter) and
# '\\' (the escape char) are encoded as '\\p' and '\\\\' respectively.  This
# keeps the encoded form free of unescaped '|' so the outer split-on-pipe
# parser never mis-segments the frame.
# ---------------------------------------------------------------------------

def pipe_escape(s: str) -> str:
    """Escape ``\\`` and ``|`` so the result is safe inside a pipe-delimited frame.

    ``\\`` becomes ``\\\\`` and ``|`` becomes ``\\p``.  Result never contains
    an unescaped ``|`` character.
    """
    if s is None:
        return ''
    return str(s).replace('\\', '\\\\').replace('|', '\\p')


def pipe_unescape(s: str) -> str:
    """Reverse :func:`pipe_escape` in a single pass.

    Unknown ``\\X`` sequences (other than ``\\\\`` and ``\\p``) drop the
    backslash and keep ``X`` so a future codepoint can be added without
    breaking older receivers.
    """
    if s is None:
        return ''
    s = str(s)
    out = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == '\\' and i + 1 < n:
            nxt = s[i + 1]
            if nxt == 'p':
                out.append('|')
            elif nxt == '\\':
                out.append('\\')
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


def encode_text(s, use_plain: bool = False) -> str:
    """Encode a user-supplied text field for the wire.

    When ``use_plain`` is True returns ``'~' + pipe_escape(s)`` (the new
    'nob64' form); otherwise returns standard base64 (legacy form).  None and
    empty inputs always return ``''`` (kept consistent with the legacy
    ``_b64`` helper which also produced empty output for empty input).
    """
    if s is None or s == '':
        return ''
    s = str(s)
    if use_plain:
        return '~' + pipe_escape(s)
    return base64.b64encode(s.encode('utf-8')).decode('ascii')


def decode_text(token) -> str:
    """Decode a wire text field, auto-detecting plain vs base64 form.

    A leading ``~`` sentinel marks the new pipe-escaped UTF-8 form; absence
    means legacy base64.  Empty / None input returns ``''``.  Best-effort:
    if base64 decoding fails (e.g. token was actually plain but missing the
    sentinel) the raw token is returned unchanged so an unparseable wire frame
    still surfaces *something* rather than throwing.
    """
    if token is None or token == '':
        return ''
    t = str(token)
    if t.startswith('~'):
        return pipe_unescape(t[1:])
    try:
        return base64.b64decode(t.encode('ascii')).decode('utf-8')
    except Exception:
        return t


# ---------------------------------------------------------------------------
# PR 5 — 'bmgap' wire capability: compact gap-fill encoding.
#
# Gap-fill messages (HASHZGAP, ZORKGAP) carry a list of missing chunk indices.
# Legacy peers receive a raw CSV like "1,3,5,7"; bmgap-capable peers receive
# whichever of "csv:1,3,5,7" or "bm:<base85-bitmap>" is shorter.  The base85
# bitmap shines when many indices are missing from a large manifest (each
# byte covers 8 indices vs CSV which needs 2-7 chars per index).
# ---------------------------------------------------------------------------

def pack_missing(missing, total, prefer_bitmap: bool = True) -> str:
    """Encode a missing-indices set for a gap-fill frame.

    When ``prefer_bitmap`` is False (peer doesn't support 'bmgap'), returns
    a bare CSV with no prefix — identical to the legacy wire form.

    When True, returns ``'bm:' + b85`` or ``'csv:' + csv`` whichever is
    shorter; both are explicitly prefixed so the receiver can dispatch.
    """
    missing_sorted = sorted(int(x) for x in (missing or []))
    csv = ",".join(str(i) for i in missing_sorted)
    if not prefer_bitmap:
        return csv
    bits = max(int(total or 0), (missing_sorted[-1] + 1) if missing_sorted else 0)
    if bits <= 0:
        return "csv:" + csv
    bm_bytes = bytearray((bits + 7) // 8)
    for idx in missing_sorted:
        if 0 <= idx < bits:
            bm_bytes[idx >> 3] |= 1 << (idx & 7)
    bm = "bm:" + base64.b85encode(bytes(bm_bytes)).decode('ascii')
    csv_full = "csv:" + csv
    return bm if len(bm) < len(csv_full) else csv_full


def unpack_missing(token, total: int = 0) -> list:
    """Decode a gap-fill missing-indices field.

    Accepts the three on-wire forms:
      * ``'bm:<base85>'`` — bitmap-base85 (new 'bmgap' form)
      * ``'csv:1,3,5'``   — explicit CSV (new 'bmgap' form)
      * ``'1,3,5'``       — bare CSV (legacy)

    When ``total`` > 0 indices ≥ total are filtered out.  Returns a sorted
    list of unique non-negative ints.
    """
    if token is None or token == '':
        return []
    t = str(token)
    if t.startswith('bm:'):
        try:
            raw = base64.b85decode(t[3:].encode('ascii'))
        except Exception:
            return []
        out = []
        for byte_idx, b in enumerate(raw):
            if not b:
                continue
            base_idx = byte_idx << 3
            for bit in range(8):
                if b & (1 << bit):
                    idx = base_idx + bit
                    if total <= 0 or idx < total:
                        out.append(idx)
        return sorted(set(out))
    if t.startswith('csv:'):
        t = t[4:]
    try:
        out = sorted({int(x) for x in t.split(',') if x.strip() != ''})
    except Exception:
        return []
    if total > 0:
        out = [i for i in out if 0 <= i < total]
    return out


def send_profile_to_bbs_nodes(user_id, short_name, long_name, first_seen, last_seen, messages_sent, bio, bbs_nodes, interface):
    _use_epoch = peers_all_support(bbs_nodes, 'epoch')
    _use_plain = peers_all_support(bbs_nodes, 'nob64')
    message = (
        f"PROFILESYNC|{user_id}|{encode_text(short_name, _use_plain)}|{encode_text(long_name, _use_plain)}|"
        f"{encode_ts_second(first_seen, _use_epoch)}|{encode_ts_second(last_seen, _use_epoch)}|"
        f"{int(messages_sent)}|{encode_text(bio, _use_plain)}"
    )
    for node_id in bbs_nodes:
        _send_one_sync(message, node_id, interface)


def send_game_score_to_bbs_nodes(user_id, game_id, short_name, score, max_score, moves, achieved_at, bbs_nodes, interface):
    _use_epoch = peers_all_support(bbs_nodes, 'epoch')
    _use_plain = peers_all_support(bbs_nodes, 'nob64')
    message = (
        f"SCORESYNC|{user_id}|{game_id}|{encode_text(short_name, _use_plain)}|"
        f"{int(score)}|{int(max_score)}|{int(moves)}|{encode_ts_second(achieved_at, _use_epoch)}"
    )
    for node_id in bbs_nodes:
        _send_one_sync(message, node_id, interface)


def send_zork_save_to_bbs_nodes(user_id, game_id, save_data, updated_at, bbs_nodes, interface, pause_seconds=None, only_indices=None):
    """Send binary zork save payload as chunked base64 sync frames.

    When ``only_indices`` is provided (iterable of int), only those chunk
    indices are emitted — used to satisfy gap-fill (ZORKGAP) requests so we
    don't re-stream the entire payload just to recover a couple of dropped
    frames.
    """
    if not is_zork_save_sync_enabled():
        return
    only_set = set(int(i) for i in only_indices) if only_indices is not None else None
    payload_b64 = base64.b64encode(save_data or b"").decode("ascii")
    payload_hash = base64.urlsafe_b64encode(hashlib.blake2b(save_data or b"", digest_size=8).digest()).decode("ascii").rstrip("=")
    _use_epoch = peers_all_support(bbs_nodes, 'epoch')
    _use_plain = peers_all_support(bbs_nodes, 'nob64')
    user_b64 = encode_text(str(user_id), _use_plain)
    game_b64 = encode_text(str(game_id), _use_plain)
    updated_at_wire = encode_ts_second(updated_at, _use_epoch)
    # Deterministic save_id allows retries/re-sync to reuse the same identity.
    # save_id derives from the *original* updated_at value (not the epoch-encoded
    # wire form) so the identity is stable regardless of encoding choice.
    save_id_raw = f"{user_id}:{game_id}:{updated_at}:{len(payload_b64)}"
    save_id = base64.urlsafe_b64encode(save_id_raw.encode("utf-8")).decode("ascii").rstrip("=")

    prefix = f"ZORKSAVE|{save_id}|{user_b64}|{game_b64}|{updated_at_wire}|{payload_hash}|"
    # Reserve enough room for chunk index and total chunk counters.
    overhead = len(prefix.encode("utf-8")) + len("999999|999999|".encode("utf-8"))
    available = get_max_text_bytes(interface) - overhead
    if available <= 0:
        logging.warning("ZORKSAVE framing prefix too large for packet limit; skipping save sync frame")
        return
    max_chunk = available

    chunks = [payload_b64[i:i + max_chunk] for i in range(0, len(payload_b64), max_chunk)]
    if not chunks:
        chunks = [""]
    total_chunks = len(chunks)

    logging.info(
        f"ZORKSAVE send begin save_id={save_id} user={user_id} game={game_id} "
        f"bytes={len(save_data or b'')} b64_len={len(payload_b64)} chunks={total_chunks} "
        f"payload_hash={payload_hash} pause={pause_seconds} peers={list(bbs_nodes)} "
        f"only_indices={sorted(only_set) if only_set is not None else 'all'}"
    )
    sent_count = 0
    # Per-chunk pause jitter breaks the deterministic half-duplex collision
    # pattern where chunk_idx=1 was systematically lost because the receiver's
    # ACK for chunk 0 collided with the sender's transmission of chunk 1 at a
    # fixed predictable offset.  See _send_hash_manifest_to_peer for the
    # matching fix on HASHZ.
    base_pause = pause_seconds if pause_seconds is not None else 0.0
    for idx, chunk in enumerate(chunks):
        if only_set is not None and idx not in only_set:
            continue
        message = f"{prefix}{idx}|{total_chunks}|{chunk}"
        jitter = random.uniform(0, base_pause) if base_pause > 0 else 0.0
        effective_pause = base_pause + jitter if pause_seconds is not None else None
        for node_id in bbs_nodes:
            logging.info(
                f"ZORKSAVE send chunk save_id={save_id} idx={idx}/{total_chunks} "
                f"frame_bytes={len(message.encode('utf-8'))} -> {node_id}"
            )
            _send_one_sync(message, node_id, interface, effective_pause)
        sent_count += 1
    logging.info(f"ZORKSAVE send end save_id={save_id} chunks_sent={sent_count}/{total_chunks}")


# Serial interfaces are slower to ack sends than TCP — raise the hard timeout
# to 15s so RPi nodes don't falsely declare the connection dead while waiting
# for a serial Heltec that is just slow under heavy receive load.
_SEND_THREAD_TIMEOUT_SECONDS: float = float(os.environ.get("BBS_SEND_TIMEOUT_SECONDS", "15"))


def _send_one_sync(message, destination, interface, pause_seconds=None):
    """Send a single sync packet directly to destination (no chunking).

    Radio libraries may block while waiting for an acknowledgement. Running
    the send in a daemon thread with a bounded, transport-aware join timeout
    keeps the main loop responsive when a connection dies.
    """
    global _consecutive_send_failures
    if pause_seconds is None:
        pause_seconds = get_sync_pause_seconds(interface)
    msg_len = len(message.encode('utf-8'))
    max_text_bytes = get_max_text_bytes(interface)
    if msg_len > max_text_bytes:
        logging.warning(f"SYNC frame exceeds {max_text_bytes} bytes ({msg_len}); dropping frame")
        return

    _result: list = [None]   # True on success, Exception on error
    def _do_send():
        try:
            interface.sendText(
                text=message,
                destinationId=destination,
                wantAck=True,
                wantResponse=False,
            )
            _result[0] = True
        except Exception as exc:
            _result[0] = exc

    t = threading.Thread(target=_do_send, daemon=True)
    t.start()
    send_timeout = max(
        _SEND_THREAD_TIMEOUT_SECONDS,
        float(getattr(interface, 'send_timeout_seconds', 0) or 0),
    )
    t.join(timeout=send_timeout)

    if t.is_alive():
        # Send is still blocking — ack never arrived, connection is dead.
        _consecutive_send_failures += 1
        logging.info(f"SYNC SEND ERROR: send blocked for >{send_timeout:.0f}s — connection likely dead")
    elif isinstance(_result[0], Exception):
        _consecutive_send_failures += 1
        logging.info(f"SYNC SEND ERROR {_result[0]}")
    else:
        _consecutive_send_failures = 0
        try:
            from db_operations import log_sync_transmission
            log_sync_transmission(message, destination, msg_len, is_continuation=False)
        except Exception as e:
            logging.debug(f"Failed to log sync transmission: {e}")

    if _consecutive_send_failures >= _MAX_CONSECUTIVE_SEND_FAILURES:
        logging.warning(
            f"SYNC SEND ERROR: {_consecutive_send_failures} consecutive failures — signalling reconnect."
        )
        _consecutive_send_failures = 0
        try:
            import server as _server
            _server.signal_reconnect(interface)
        except Exception:
            os._exit(2)  # fallback if import fails

    time.sleep(pause_seconds)


def _send_sync_with_cont(header, footer, content, unique_id, cont_prefix, bbs_nodes, interface, pause_seconds=0.75, meta_prefix=None):
    """
    Send a sync message whose content may exceed one Meshtastic packet.

    Strategy (graceful degradation — no all-or-nothing failure):
      1. Pack as much content as fits into the first packet alongside the
         mandatory header/footer fields.  That packet is always a fully valid,
         immediately parseable sync record.
      2. Any remaining content is sent as independent BULLETINCONT / MAILCONT
         follow-up packets.  Each is self-contained; if one is lost only a
         slice of content is missing, not the entire record.
    """
    header_bytes = header.encode('utf-8')
    footer_bytes = footer.encode('utf-8')
    cont_prefix_bytes = cont_prefix.encode('utf-8')
    # Reserve bytes for the offset field appended to each continuation packet
    # e.g. "BULLETINCONT|uid|" + "1234|" + chunk  → reserve 10 chars for offset+pipe
    _OFFSET_OVERHEAD = 10

    # How many content bytes can fit in the first (primary) packet?
    max_text_bytes = get_max_text_bytes(interface)
    max_first = max_text_bytes - len(header_bytes) - len(footer_bytes)
    if max_first <= 0:
        logging.warning("Sync frame header/footer exceed packet limit; skipping message")
        return

    first_content, remaining_text = _take_prefix_within_bytes(content, max_first)
    first_msg = header + first_content + footer

    for node_id in bbs_nodes:
        _send_one_sync(first_msg, node_id, interface, pause_seconds)

    if remaining_text and meta_prefix:
        meta_msg = f"{meta_prefix}{len(content)}"
        for node_id in bbs_nodes:
            _send_one_sync(meta_msg, node_id, interface, pause_seconds)

    # Send continuation packets for any remaining content
    remaining = remaining_text
    max_cont = max_text_bytes - len(cont_prefix_bytes) - _OFFSET_OVERHEAD
    if max_cont <= 0:
        logging.warning("Sync continuation prefix exceeds packet limit; skipping continuations")
        return

    content_char_offset = len(first_content)  # chars already stored by the first packet

    while remaining:
        chunk, remaining = _take_prefix_within_bytes(remaining, max_cont)
        # Format: BULLETINCONT|uid|<char_offset>|<chunk>
        cont_msg = cont_prefix + str(content_char_offset) + "|" + chunk
        # Jitter each continuation packet's pause so repeated retries don't
        # land at the same timing and hit the same LoRa half-duplex loss window.
        jitter = random.uniform(0.0, pause_seconds * 0.4)
        for node_id in bbs_nodes:
            _send_one_sync(cont_msg, node_id, interface, pause_seconds + jitter)
        content_char_offset += len(chunk)
