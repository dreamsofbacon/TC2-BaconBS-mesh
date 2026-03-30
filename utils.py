import logging
import base64
import re
import time
import uuid

user_states = {}


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
    message = f"BULLETIN|{board}|{sender_short_name}|{subject}|{content}|{unique_id}"
    for node_id in bbs_nodes:
        send_sync_message(message, node_id, interface)


def send_mail_to_bbs_nodes(sender_id, sender_short_name, recipient_id, subject, content, unique_id, bbs_nodes,
                           interface):
    message = f"MAIL|{sender_id}|{sender_short_name}|{recipient_id}|{subject}|{content}|{unique_id}"
    logging.info(f"SERVER SYNC: Syncing new mail message {subject} sent from {sender_short_name} to other BBS systems.")
    for node_id in bbs_nodes:
        send_sync_message(message, node_id, interface)


def send_delete_bulletin_to_bbs_nodes(unique_id, bbs_nodes, interface):
    message = f"DELETE_BULLETIN|{unique_id}"
    for node_id in bbs_nodes:
        send_sync_message(message, node_id, interface)


def send_delete_mail_to_bbs_nodes(unique_id, bbs_nodes, interface):
    message = f"DELETE_MAIL|{unique_id}"
    logging.info(f"SERVER SYNC: Sending delete mail sync message with unique_id: {unique_id}")
    for node_id in bbs_nodes:
        send_sync_message(message, node_id, interface)


def send_channel_to_bbs_nodes(name, url, bbs_nodes, interface):
    message = f"CHANNEL|{name}|{url}"
    for node_id in bbs_nodes:
        send_sync_message(message, node_id, interface)


def send_sync_message(message, destination, interface, raw_chunk_size=90, pause_seconds=0.75):
    """Send sync payload safely; long payloads are framed as reassemblable chunks."""
    try:
        message_bytes = message.encode('utf-8')
    except Exception:
        logging.info("SYNC SEND ERROR: Unable to encode payload")
        return

    # Small payloads are sent as a single legacy message for compatibility.
    if len(message) <= 180:
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
        return

    chunks = [message_bytes[i:i + raw_chunk_size] for i in range(0, len(message_bytes), raw_chunk_size)]
    total = len(chunks)
    message_id = uuid.uuid4().hex[:12]

    for index, chunk in enumerate(chunks, start=1):
        payload = base64.b64encode(chunk).decode('ascii')
        framed = f"SYNCCHUNK|{message_id}|{index}|{total}|{payload}"
        try:
            interface.sendText(
                text=framed,
                destinationId=destination,
                wantAck=True,
                wantResponse=False,
            )
        except Exception as e:
            logging.info(f"SYNC SEND ERROR {e}")

        time.sleep(pause_seconds)
