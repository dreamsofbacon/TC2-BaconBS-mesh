import logging
import base64
import hashlib
import os
import re
import time
import configparser
from datetime import datetime
from typing import Optional

user_states = {}

# Conservative single-packet byte ceiling for Meshtastic TEXT_MESSAGE packets.
# Most LoRa/Meshtastic configurations cap the data payload at 228 bytes; we stay
# under 220 to leave room for packet-layer overhead and multi-byte UTF-8 chars.
_MESHTASTIC_MAX_BYTES = 220

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
WIRE_CAPABILITIES: tuple = ('cck', 'epoch', 'scc', 'nob64', 'bmgap')  # 'cck'=compact channel-comment keys, 'epoch'=epoch timestamps, 'scc'=single-char scope codes, 'nob64'=drop base64 on text fields, 'bmgap'=bitmap-base85 gap-fill encoding

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


def local_capabilities_token() -> str:
    """Return the ``vN:cap1,cap2`` token to append to outbound SYNCSTATE frames."""
    return f"v{WIRE_PROTOCOL_VERSION}:{','.join(WIRE_CAPABILITIES)}"


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


def get_sync_pause_seconds() -> float:
    turbo = _is_sync_turbo_enabled()
    default = 0.02 if turbo else 0.75
    if os.getenv("BBS_SYNC_PAUSE_SECONDS") is not None:
        return _env_float("BBS_SYNC_PAUSE_SECONDS", default)
    return _config_float("sync", "sync_pause_seconds", default)


def get_hash_repair_pause_seconds() -> float:
    turbo = _is_sync_turbo_enabled()
    default = 0.0 if turbo else 0.1
    if os.getenv("BBS_HASH_REPAIR_PAUSE_SECONDS") is not None:
        return _env_float("BBS_HASH_REPAIR_PAUSE_SECONDS", default)
    return _config_float("sync", "hash_repair_pause_seconds", default)


def get_hash_chunk_pause_seconds() -> float:
    """Minimum airtime gap between consecutive HASHZ manifest chunks and HASHREQ
    frames sent to the same peer.

    Multi-chunk HASHZ manifests and HASHREQ bursts (one per scope) MUST clear the
    receiver's RX path between frames or LoRa drops the trailing frames. This
    floor applies regardless of ``sync_turbo`` because back-to-back small frames
    cause silent manifest loss that breaks the entire reconcile cycle. Lower
    only when running on a non-LoRa transport (e.g. simulator)."""
    if os.getenv("BBS_HASH_CHUNK_PAUSE_SECONDS") is not None:
        return _env_float("BBS_HASH_CHUNK_PAUSE_SECONDS", 1.5)
    return _config_float("sync", "hash_chunk_pause_seconds", 1.5)


def get_full_sync_delay_ms() -> int:
    turbo = _is_sync_turbo_enabled()
    default = 0 if turbo else 500
    if os.getenv("BBS_FULL_SYNC_DELAY_MS") is not None:
        return _env_int("BBS_FULL_SYNC_DELAY_MS", default)
    return _config_int("sync", "full_sync_delay_ms", default)


def get_repair_cycle_seconds() -> int:
    """Minimum seconds between SYNCSTATE-triggered repair cycles for the same peer/scope.

    Lower values converge mismatches faster but risk repair-storms on busy meshes.
    Turbo mode shrinks this aggressively for small (e.g. 2-node) deployments.
    """
    turbo = _is_sync_turbo_enabled()
    default = 15 if turbo else 90
    if os.getenv("BBS_REPAIR_CYCLE_SECONDS") is not None:
        return _env_int("BBS_REPAIR_CYCLE_SECONDS", default)
    return _config_int("sync", "repair_cycle_seconds", default)


def get_reconcile_max_per_pass() -> int:
    """Cap on records pulled (HASHMISS) or pushed per single reconcile pass.

    Higher values converge larger mismatches in one cycle but tie up the receive
    callback longer. Turbo mode raises this for small meshes where collisions
    are rare.
    """
    turbo = _is_sync_turbo_enabled()
    default = 100 if turbo else 20
    if os.getenv("BBS_RECONCILE_MAX_PER_PASS") is not None:
        return _env_int("BBS_RECONCILE_MAX_PER_PASS", default)
    return _config_int("sync", "reconcile_max_per_pass", default)


def get_sync_runtime_settings() -> dict:
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
    """Split text into chunks of at most max_len chars, breaking at sentence boundaries."""
    # Collapse 3+ consecutive newlines to at most 2, and runs of spaces/tabs to one space
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = text.strip()

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        segment = text[:max_len]

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
                split_pos = sp + 1 if sp > 10 else max_len

        chunks.append(text[:split_pos].rstrip())
        text = text[split_pos:].lstrip()

    return chunks


def send_message(message, destination, interface):
    for chunk in _split_into_chunks(message):
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
            logging.info(f"REPLY SEND ERROR {e}")

        time.sleep(2)


def get_node_info(interface, short_name):
    nodes = [{'num': node_id, 'shortName': node['user']['shortName'], 'longName': node['user']['longName']}
             for node_id, node in interface.nodes.items()
             if node['user']['shortName'].lower() == short_name]
    return nodes


def get_node_id_from_num(node_num, interface):
    for node_id, node in interface.nodes.items():
        if node['num'] == node_num:
            return node_id
    return None


def get_node_short_name(node_id, interface):
    node_info = interface.nodes.get(node_id)
    if node_info:
        return node_info['user']['shortName']
    return None


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
    _send_sync_with_cont(
        header, footer, content, unique_id,
        cont_prefix=f"BULLETINCONT|{unique_id}|",
        meta_prefix=f"BULLETINMETA|{unique_id}|",
        bbs_nodes=bbs_nodes, interface=interface,
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
    _send_sync_with_cont(
        header, footer, content, unique_id,
        cont_prefix=f"MAILCONT|{unique_id}|",
        meta_prefix=f"MAILMETA|{unique_id}|",
        bbs_nodes=bbs_nodes, interface=interface,
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
        > _MESHTASTIC_MAX_BYTES - _MIN_CHANNEL_COMMENT_CONTENT_BYTES
    )

    if len(short_header.encode('utf-8')) + len(footer.encode('utf-8')) > _MESHTASTIC_MAX_BYTES:
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
        _send_sync_with_cont(
            short_header, footer, content, unique_id,
            cont_prefix=f"CHANNELCOMMENTCONT|{unique_id}|",
            meta_prefix=f"CHANNELCOMMENTMETA|{unique_id}|",
            bbs_nodes=cck_peers, interface=interface,
        )

    if legacy_peers:
        legacy_header = short_header if full_too_big else full_header
        _send_sync_with_cont(
            legacy_header, footer, content, unique_id,
            cont_prefix=f"CHANNELCOMMENTCONT|{unique_id}|",
            meta_prefix=f"CHANNELCOMMENTMETA|{unique_id}|",
            bbs_nodes=legacy_peers, interface=interface,
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
            frame = build_have_frame(local_node_id, peer_id=node_id)
            if frame:
                _send_one_sync(frame, node_id, interface, pause_seconds=get_hash_repair_pause_seconds())
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
    message = f"HASHREQ|{scope}"
    pause = max(get_sync_pause_seconds(), get_hash_chunk_pause_seconds())
    for node_id in bbs_nodes:
        # Per-peer scope-code encoding via 'scc' capability.  The literal
        # 'all' has no code so it always passes through unchanged.
        if scope != 'all' and peers_all_support([node_id], 'scc'):
            per_peer_msg = f"HASHREQ|{encode_scope(scope, True)}"
        else:
            per_peer_msg = message
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
    available = _MESHTASTIC_MAX_BYTES - overhead
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
    for idx, chunk in enumerate(chunks):
        if only_set is not None and idx not in only_set:
            continue
        message = f"{prefix}{idx}|{total_chunks}|{chunk}"
        for node_id in bbs_nodes:
            logging.info(
                f"ZORKSAVE send chunk save_id={save_id} idx={idx}/{total_chunks} "
                f"frame_bytes={len(message.encode('utf-8'))} -> {node_id}"
            )
            _send_one_sync(message, node_id, interface, pause_seconds)
        sent_count += 1
    logging.info(f"ZORKSAVE send end save_id={save_id} chunks_sent={sent_count}/{total_chunks}")


def _send_one_sync(message, destination, interface, pause_seconds=None):
    """Send a single sync packet directly to destination (no chunking)."""
    if pause_seconds is None:
        pause_seconds = get_sync_pause_seconds()
    msg_len = len(message.encode('utf-8'))
    if msg_len > _MESHTASTIC_MAX_BYTES:
        logging.warning(f"SYNC frame exceeds {_MESHTASTIC_MAX_BYTES} bytes ({msg_len}); dropping frame")
        return
    try:
        interface.sendText(
            text=message,
            destinationId=destination,
            wantAck=True,
            wantResponse=False,
        )
        # Log transmission for sync stats (lazy import to avoid circular dependency)
        try:
            from db_operations import log_sync_transmission
            log_sync_transmission(message, destination, msg_len, is_continuation=False)
        except Exception as e:
            logging.debug(f"Failed to log sync transmission: {e}")
    except Exception as e:
        logging.info(f"SYNC SEND ERROR {e}")
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
    max_first = _MESHTASTIC_MAX_BYTES - len(header_bytes) - len(footer_bytes)
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
    max_cont = _MESHTASTIC_MAX_BYTES - len(cont_prefix_bytes) - _OFFSET_OVERHEAD
    if max_cont <= 0:
        logging.warning("Sync continuation prefix exceeds packet limit; skipping continuations")
        return

    content_char_offset = len(first_content)  # chars already stored by the first packet

    while remaining:
        chunk, remaining = _take_prefix_within_bytes(remaining, max_cont)
        # Format: BULLETINCONT|uid|<char_offset>|<chunk>
        cont_msg = cont_prefix + str(content_char_offset) + "|" + chunk
        for node_id in bbs_nodes:
            _send_one_sync(cont_msg, node_id, interface, pause_seconds)
        content_char_offset += len(chunk)
