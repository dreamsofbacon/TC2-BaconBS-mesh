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


def get_full_sync_delay_ms() -> int:
    turbo = _is_sync_turbo_enabled()
    default = 0 if turbo else 500
    if os.getenv("BBS_FULL_SYNC_DELAY_MS") is not None:
        return _env_int("BBS_FULL_SYNC_DELAY_MS", default)
    return _config_int("sync", "full_sync_delay_ms", default)


def get_sync_runtime_settings() -> dict:
    return {
        "sync_turbo": _is_sync_turbo_enabled(),
        "sync_pause_seconds": get_sync_pause_seconds(),
        "hash_repair_pause_seconds": get_hash_repair_pause_seconds(),
        "full_sync_delay_ms": get_full_sync_delay_ms(),
        "env_overrides": {
            "sync_turbo": os.getenv("BBS_SYNC_TURBO") is not None,
            "sync_pause_seconds": os.getenv("BBS_SYNC_PAUSE_SECONDS") is not None,
            "hash_repair_pause_seconds": os.getenv("BBS_HASH_REPAIR_PAUSE_SECONDS") is not None,
            "full_sync_delay_ms": os.getenv("BBS_FULL_SYNC_DELAY_MS") is not None,
        },
    }


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


def send_bulletin_to_bbs_nodes(board, sender_short_name, subject, content, unique_id, bbs_nodes, interface):
    header = f"BULLETIN|{board}|{sender_short_name}|{subject}|"
    footer = f"|{unique_id}"
    _send_sync_with_cont(
        header, footer, content, unique_id,
        cont_prefix=f"BULLETINCONT|{unique_id}|",
        meta_prefix=f"BULLETINMETA|{unique_id}|",
        bbs_nodes=bbs_nodes, interface=interface,
    )


def send_mail_to_bbs_nodes(sender_id, sender_short_name, recipient_id, subject, content, unique_id, bbs_nodes,
                           interface):
    logging.info(f"SERVER SYNC: Syncing new mail message '{subject}' from {sender_short_name} to peers.")
    header = f"MAIL|{sender_id}|{sender_short_name}|{recipient_id}|{subject}|"
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


def send_channel_to_bbs_nodes(name, url, bbs_nodes, interface):
    message = f"CHANNEL|{name}|{url}"
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
    """Ask peers to send per-record hash manifests for selective repair."""
    message = f"HASHREQ|{scope}"
    for node_id in bbs_nodes:
        _send_one_sync(message, node_id, interface)


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


def send_zork_save_to_bbs_nodes(user_id, game_id, save_data, updated_at, bbs_nodes, interface, pause_seconds=None):
    """Send binary zork save payload as chunked base64 sync frames."""
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

    for idx, chunk in enumerate(chunks):
        message = f"{prefix}{idx}|{total_chunks}|{chunk}"
        for node_id in bbs_nodes:
            _send_one_sync(message, node_id, interface, pause_seconds)


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
