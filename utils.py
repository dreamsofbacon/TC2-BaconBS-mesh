import logging
import base64
import hashlib
import os
import re
import time
import configparser
from typing import Optional

user_states = {}

# Conservative single-packet byte ceiling for Meshtastic TEXT_MESSAGE packets.
# Most LoRa/Meshtastic configurations cap the data payload at 228 bytes; we stay
# under 220 to leave room for packet-layer overhead and multi-byte UTF-8 chars.
_MESHTASTIC_MAX_BYTES = 220


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
    return _config_bool("sync", "sync_zork_saves", True)


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
    if source_node_id and source_timestamp:
        date_str = str(date) if date else ''
        footer = f"|{unique_id}|{date_str}|{source_node_id}|{source_timestamp}"
    elif date:
        footer = f"|{unique_id}|{date}"
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
    if source_node_id and source_timestamp:
        date_str = str(date) if date else ''
        footer = f"|{unique_id}|{date_str}|{source_node_id}|{source_timestamp}"
    elif date:
        footer = f"|{unique_id}|{date}"
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
    message = f"DELETE_ZORKSAVE|{_b64(str(user_id))}|{_b64(str(game_id))}|{deleted_at}"
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


def send_channel_comment_to_bbs_nodes(channel_key, sender_short_name, comment_date, content, unique_id, bbs_nodes, interface, source_node_id=None, source_timestamp=None):
    if source_node_id and source_timestamp:
        footer = f"|{unique_id}|{source_node_id}|{source_timestamp}"
    else:
        footer = f"|{unique_id}"
    header = f"CHANNELCOMMENT|{channel_key}|{_b64(sender_short_name)}|{comment_date}|"
    # Use compact key if the full key leaves fewer than 8 content bytes in the base packet.
    # A base packet carrying only 1-7 bytes of content requires multi-packet sequences that
    # are fragile under radio packet loss — compact key shrinks the header enough to fit
    # most short comments in a single packet.
    _MIN_CHANNEL_COMMENT_CONTENT_BYTES = 8
    if len(header.encode('utf-8')) + len(footer.encode('utf-8')) > _MESHTASTIC_MAX_BYTES - _MIN_CHANNEL_COMMENT_CONTENT_BYTES:
        # Full channel manifest key leaves too little room; fall back to a compact hash.
        short_key = compact_channel_manifest_key(channel_key)
        header = f"CHANNELCOMMENT|{short_key}|{_b64(sender_short_name)}|{comment_date}|"
        if len(header.encode('utf-8')) + len(footer.encode('utf-8')) > _MESHTASTIC_MAX_BYTES:
            logging.warning(
                f"CHANNELCOMMENT header exceeds packet limit even with compact key for uid={unique_id}; skipping"
            )
            return
    _send_sync_with_cont(
        header, footer, content, unique_id,
        cont_prefix=f"CHANNELCOMMENTCONT|{unique_id}|",
        meta_prefix=f"CHANNELCOMMENTMETA|{unique_id}|",
        bbs_nodes=bbs_nodes, interface=interface,
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
        f"{str(counts.get('profiles_hash', ''))}|{str(counts.get('game_scores_hash', ''))}"
    )
    for node_id in bbs_nodes:
        _send_one_sync(message, node_id, interface)


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
        _send_one_sync(message, node_id, interface, pause_seconds=pause)


def _b64(text: str) -> str:
    return base64.b64encode((text or "").encode("utf-8")).decode("ascii")


def send_profile_to_bbs_nodes(user_id, short_name, long_name, first_seen, last_seen, messages_sent, bio, bbs_nodes, interface):
    message = (
        f"PROFILESYNC|{user_id}|{_b64(short_name)}|{_b64(long_name)}|"
        f"{first_seen}|{last_seen}|{int(messages_sent)}|{_b64(bio)}"
    )
    for node_id in bbs_nodes:
        _send_one_sync(message, node_id, interface)


def send_game_score_to_bbs_nodes(user_id, game_id, short_name, score, max_score, moves, achieved_at, bbs_nodes, interface):
    message = (
        f"SCORESYNC|{user_id}|{game_id}|{_b64(short_name)}|"
        f"{int(score)}|{int(max_score)}|{int(moves)}|{achieved_at}"
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
    user_b64 = _b64(str(user_id))
    game_b64 = _b64(str(game_id))
    # Deterministic save_id allows retries/re-sync to reuse the same identity.
    save_id_raw = f"{user_id}:{game_id}:{updated_at}:{len(payload_b64)}"
    save_id = base64.urlsafe_b64encode(save_id_raw.encode("utf-8")).decode("ascii").rstrip("=")

    prefix = f"ZORKSAVE|{save_id}|{user_b64}|{game_b64}|{updated_at}|{payload_hash}|"
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
