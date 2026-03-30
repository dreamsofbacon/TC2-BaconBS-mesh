import logging
import re
import time

user_states = {}

# Conservative single-packet byte ceiling for Meshtastic TEXT_MESSAGE packets.
# Most LoRa/Meshtastic configurations cap the data payload at 228 bytes; we stay
# under 220 to leave room for packet-layer overhead and multi-byte UTF-8 chars.
_MESHTASTIC_MAX_BYTES = 220


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


def _send_one_sync(message, destination, interface, pause_seconds=0.75):
    """Send a single sync packet directly to destination (no chunking)."""
    try:
        interface.sendText(
            text=message,
            destinationId=destination,
            wantAck=True,
            wantResponse=False,
        )
    except Exception as e:
        logging.info(f"SYNC SEND ERROR {e}")
    time.sleep(pause_seconds)


def _send_sync_with_cont(header, footer, content, unique_id, cont_prefix, bbs_nodes, interface, pause_seconds=0.75):
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
    content_bytes = content.encode('utf-8')

    # How many content bytes can fit in the first (primary) packet?
    max_first = _MESHTASTIC_MAX_BYTES - len(header_bytes) - len(footer_bytes)
    max_first = max(10, max_first)

    first_content_bytes = content_bytes[:max_first]
    # Decode back safely; 'replace' avoids splitting a multi-byte char
    first_content = first_content_bytes.decode('utf-8', errors='replace')
    first_msg = header + first_content + footer

    for node_id in bbs_nodes:
        _send_one_sync(first_msg, node_id, interface, pause_seconds)

    # Send continuation packets for any remaining content
    remaining = content_bytes[max_first:]
    max_cont = _MESHTASTIC_MAX_BYTES - len(cont_prefix_bytes)
    max_cont = max(10, max_cont)

    while remaining:
        chunk = remaining[:max_cont].decode('utf-8', errors='replace')
        remaining = remaining[max_cont:]
        cont_msg = cont_prefix + chunk
        for node_id in bbs_nodes:
            _send_one_sync(cont_msg, node_id, interface, pause_seconds)
