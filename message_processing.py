import logging
import base64
import hashlib
import time

from meshtastic import BROADCAST_NUM

from command_handlers import (
    handle_mail_command, handle_bulletin_command, handle_help_command, handle_stats_command, handle_fortune_command,
    handle_bb_steps, handle_mail_steps, handle_stats_steps, handle_wall_of_shame_command,
    handle_channel_directory_command, handle_channel_directory_steps, handle_send_mail_command,
    handle_read_mail_command, handle_check_mail_command, handle_delete_mail_confirmation, handle_post_bulletin_command,
    handle_check_bulletin_command, handle_read_bulletin_command, handle_read_channel_command,
    handle_post_channel_command, handle_list_channels_command, handle_quick_help_command,
    handle_zork_command, handle_zork_steps,
    handle_games_command, handle_games_steps,
    handle_scoreboard_command, handle_scoreboard_steps,
    handle_profile_command, handle_profile_steps,
)
from db_operations import (
    add_bulletin, add_mail, delete_bulletin, delete_mail, add_channel,
    append_bulletin_content, append_mail_content,
    auto_upsert_user_profile, log_connection_event, upsert_peer_sync_state,
    upsert_synced_user_profile, upsert_synced_game_score,
    upsert_synced_zork_save,
    get_record_hash_manifest,
    get_bulletin_by_unique_id,
    get_mail_by_unique_id,
    get_channel_by_manifest_key,
    get_profile_by_user_id,
    get_game_score_by_user_and_game,
    get_zork_save_row_by_user_and_game,
    has_sync_tombstone,
)
from js8call_integration import handle_js8call_command, handle_js8call_steps, handle_group_message_selection
from utils import (
    get_user_state, get_node_short_name, get_node_id_from_num, send_message,
    send_bulletin_to_bbs_nodes, send_mail_to_bbs_nodes, send_channel_to_bbs_nodes,
    send_profile_to_bbs_nodes, send_game_score_to_bbs_nodes, send_zork_save_to_bbs_nodes,
    send_delete_bulletin_to_bbs_nodes, send_delete_mail_to_bbs_nodes,
    _send_one_sync,
)

main_menu_handlers = {
    "q": handle_quick_help_command,
    "b": lambda sender_id, interface: handle_help_command(sender_id, interface, 'bbs'),
    "u": lambda sender_id, interface: handle_help_command(sender_id, interface, 'utilities'),
    "p": handle_profile_command,
    "x": handle_help_command
}

bbs_menu_handlers = {
    "m": handle_mail_command,
    "b": handle_bulletin_command,
    "c": handle_channel_directory_command,
    "j": handle_js8call_command,
    "x": handle_help_command
}


utilities_menu_handlers = {
    "s": handle_stats_command,
    "f": handle_fortune_command,
    "w": handle_wall_of_shame_command,
    "g": handle_games_command,
    "z": handle_games_command,  # legacy alias
    "x": handle_help_command
}


board_action_handlers = {
    "r": lambda sender_id, interface, state: handle_bb_steps(sender_id, 'r', 2, state, interface, None),
    "p": lambda sender_id, interface, state: handle_bb_steps(sender_id, 'p', 2, state, interface, None),
    "x": handle_help_command
}


_zork_save_chunk_buffers = {}
_ZORK_SAVE_BUFFER_MAX_AGE_SECONDS = 600
_peer_hash_manifest_buffers = {}
_SUPPORTED_HASH_SCOPES = ["bulletins", "mail", "channels", "profiles", "game_scores", "zork_saves", "tombstones"]


def _prune_old_zork_save_chunks() -> None:
    now = time.time()
    stale_keys = [k for k, v in _zork_save_chunk_buffers.items()
                  if now - v.get('updated_at', now) > _ZORK_SAVE_BUFFER_MAX_AGE_SECONDS]
    for key in stale_keys:
        _zork_save_chunk_buffers.pop(key, None)


def _send_hash_manifest_to_peer(scope: str, destination_node_id: str, interface) -> None:
    manifest = get_record_hash_manifest(scope)
    for key, rec_hash in manifest.items():
        _send_one_sync(f"HASHREC|{scope}|{key}|{rec_hash}", destination_node_id, interface, pause_seconds=0.1)
    _send_one_sync(f"HASHEND|{scope}|{len(manifest)}", destination_node_id, interface, pause_seconds=0.1)


def _send_requested_record(scope: str, key: str, destination_node_id: str, interface) -> None:
    if scope == 'bulletins':
        row = get_bulletin_by_unique_id(key)
        if row:
            send_bulletin_to_bbs_nodes(row[0], row[1], row[2], row[3], row[4], [destination_node_id], interface)
    elif scope == 'mail':
        row = get_mail_by_unique_id(key)
        if row:
            send_mail_to_bbs_nodes(row[0], row[1], row[2], row[3], row[4], row[5], [destination_node_id], interface)
    elif scope == 'channels':
        row = get_channel_by_manifest_key(key)
        if row:
            send_channel_to_bbs_nodes(row[0], row[1], [destination_node_id], interface)
    elif scope == 'profiles':
        row = get_profile_by_user_id(key)
        if row:
            send_profile_to_bbs_nodes(row[0], row[1], row[2], row[3], row[4], row[5], row[6], [destination_node_id], interface)
    elif scope == 'game_scores':
        if ':' not in key:
            return
        user_id, game_id = key.split(':', 1)
        row = get_game_score_by_user_and_game(user_id, game_id)
        if row:
            send_game_score_to_bbs_nodes(row[0], row[1], row[2], row[3], row[4], row[5], row[6], [destination_node_id], interface)
    elif scope == 'zork_saves':
        if ':' not in key:
            return
        user_id, game_id = key.split(':', 1)
        row = get_zork_save_row_by_user_and_game(user_id, game_id)
        if row:
            send_zork_save_to_bbs_nodes(row[0], row[1], row[2], row[3], [destination_node_id], interface, pause_seconds=0.1)
    elif scope == 'tombstones':
        if key.startswith('bulletins:'):
            unique_id = key.split(':', 1)[1]
            send_delete_bulletin_to_bbs_nodes(unique_id, [destination_node_id], interface)
        elif key.startswith('mail:'):
            unique_id = key.split(':', 1)[1]
            send_delete_mail_to_bbs_nodes(unique_id, [destination_node_id], interface)


def _auto_update_profile(sender_id, interface):
    try:
        node_id = get_node_id_from_num(sender_id, interface)
        if node_id and node_id in interface.nodes:
            user = interface.nodes[node_id].get('user', {})
            short_name = user.get('shortName', '')
            long_name = user.get('longName', '')
            auto_upsert_user_profile(sender_id, short_name, long_name)
    except Exception:
        pass

def process_message(sender_id, message, interface, is_sync_message=False, sender_node_id=None):
    state = get_user_state(sender_id)
    message_lower = message.lower().strip()
    message_strip = message.strip()

    if not is_sync_message:
        _auto_update_profile(sender_id, interface)

    bbs_nodes = interface.bbs_nodes

    # Handle repeated characters for single character commands using a prefix
    if len(message_lower) == 2 and message_lower[1] == 'x':
        message_lower = message_lower[0]

    if is_sync_message:
        if message.startswith("BULLETIN|"):
            parts = message.split("|", 5)
            if len(parts) != 6:
                logging.warning(f"Malformed BULLETIN sync message ignored: {message}")
                return
            board, sender_short_name, subject, content, unique_id = parts[1], parts[2], parts[3], parts[4], parts[5]
            add_bulletin(board, sender_short_name, subject, content, [], interface, unique_id=unique_id)

            if board.lower() == "urgent":
                notification_message = f"💥NEW URGENT BULLETIN💥\nFrom: {sender_short_name}\nTitle: {subject}\nDM 'CB,,Urgent' to view"
                send_message(notification_message, BROADCAST_NUM, interface)
        elif message.startswith("MAIL|"):
            parts = message.split("|", 6)
            if len(parts) != 7:
                logging.warning(f"Malformed MAIL sync message ignored: {message}")
                return
            sync_sender_id, sender_short_name, recipient_id, subject, content, unique_id = parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
            add_mail(sync_sender_id, sender_short_name, recipient_id, subject, content, [], interface, unique_id=unique_id)
        elif message.startswith("DELETE_BULLETIN|"):
            parts = message.split("|", 1)
            if len(parts) != 2 or not parts[1]:
                logging.warning(f"Malformed DELETE_BULLETIN sync message ignored: {message}")
                return
            unique_id = parts[1]
            delete_bulletin(unique_id, [], interface)
        elif message.startswith("DELETE_MAIL|"):
            parts = message.split("|", 1)
            if len(parts) != 2 or not parts[1]:
                logging.warning(f"Malformed DELETE_MAIL sync message ignored: {message}")
                return
            unique_id = parts[1]
            logging.info(f"Processing delete mail with unique_id: {unique_id}")
            delete_mail(unique_id, None, [], interface)
        elif message.startswith("CHANNEL|"):
            parts = message.split("|", 2)
            if len(parts) != 3:
                logging.warning(f"Malformed CHANNEL sync message ignored: {message}")
                return
            channel_name, channel_url = parts[1], parts[2]
            add_channel(channel_name, channel_url)
        elif message.startswith("BULLETINCONT|"):
            parts = message.split("|", 3)
            if len(parts) < 3 or not parts[1]:
                logging.warning(f"Malformed BULLETINCONT sync message ignored: {message}")
                return
            if len(parts) == 4:
                try:
                    offset = int(parts[2])
                except ValueError:
                    logging.warning(f"Malformed BULLETINCONT offset ignored: {message}")
                    return
                append_bulletin_content(parts[1], offset, parts[3])
            else:
                # Legacy format without offset — blind append
                append_bulletin_content(parts[1], None, parts[2])
        elif message.startswith("MAILCONT|"):
            parts = message.split("|", 3)
            if len(parts) < 3 or not parts[1]:
                logging.warning(f"Malformed MAILCONT sync message ignored: {message}")
                return
            if len(parts) == 4:
                try:
                    offset = int(parts[2])
                except ValueError:
                    logging.warning(f"Malformed MAILCONT offset ignored: {message}")
                    return
                append_mail_content(parts[1], offset, parts[3])
            else:
                # Legacy format without offset — blind append
                append_mail_content(parts[1], None, parts[2])
        elif message.startswith("SYNCSTATE|"):
            parts = message.split("|")
            if len(parts) not in (5, 7, 13):
                logging.warning(f"Malformed SYNCSTATE sync message ignored: {message}")
                return
            try:
                bulletins = int(parts[1])
                mail = int(parts[2])
                channels = int(parts[3])
                zork_saves = int(parts[4])
                profiles = int(parts[5]) if len(parts) >= 7 else 0
                game_scores = int(parts[6]) if len(parts) >= 7 else 0
            except ValueError:
                logging.warning(f"Invalid SYNCSTATE values ignored: {message}")
                return
            bulletins_hash = parts[7] if len(parts) >= 13 else ''
            mail_hash = parts[8] if len(parts) >= 13 else ''
            channels_hash = parts[9] if len(parts) >= 13 else ''
            zork_saves_hash = parts[10] if len(parts) >= 13 else ''
            profiles_hash = parts[11] if len(parts) >= 13 else ''
            game_scores_hash = parts[12] if len(parts) >= 13 else ''
            if sender_node_id:
                upsert_peer_sync_state(
                    sender_node_id,
                    bulletins,
                    mail,
                    channels,
                    zork_saves,
                    profiles,
                    game_scores,
                    bulletins_hash,
                    mail_hash,
                    channels_hash,
                    zork_saves_hash,
                    profiles_hash,
                    game_scores_hash,
                )
            else:
                logging.warning("SYNCSTATE ignored due to missing sender_node_id")
        elif message.startswith("HASHREQ|"):
            if not sender_node_id:
                logging.warning("HASHREQ ignored due to missing sender_node_id")
                return
            requested = message.split("|", 1)[1].strip().lower() if "|" in message else "all"
            scopes = _SUPPORTED_HASH_SCOPES if requested == 'all' else [requested]
            for scope in scopes:
                if scope in _SUPPORTED_HASH_SCOPES:
                    _send_hash_manifest_to_peer(scope, sender_node_id, interface)
        elif message.startswith("HASHREC|"):
            if not sender_node_id:
                return
            parts = message.split("|", 3)
            if len(parts) != 4:
                logging.warning(f"Malformed HASHREC ignored: {message}")
                return
            scope, key, rec_hash = parts[1], parts[2], parts[3]
            if scope not in _SUPPORTED_HASH_SCOPES:
                return
            buf_key = (sender_node_id, scope)
            if buf_key not in _peer_hash_manifest_buffers:
                _peer_hash_manifest_buffers[buf_key] = {}
            _peer_hash_manifest_buffers[buf_key][key] = rec_hash
        elif message.startswith("HASHEND|"):
            if not sender_node_id:
                return
            parts = message.split("|", 2)
            if len(parts) != 3:
                logging.warning(f"Malformed HASHEND ignored: {message}")
                return
            scope = parts[1]
            if scope not in _SUPPORTED_HASH_SCOPES:
                return
            remote = _peer_hash_manifest_buffers.pop((sender_node_id, scope), {})
            local = get_record_hash_manifest(scope)
            missing_or_mismatched = [
                key for key, remote_hash in remote.items()
                if local.get(key) != remote_hash
            ]
            for key in missing_or_mismatched:
                if scope in ('bulletins', 'mail') and key not in local and has_sync_tombstone(scope, key):
                    _send_one_sync(f"HASHMISS|tombstones|{scope}:{key}", sender_node_id, interface, pause_seconds=0.1)
                else:
                    _send_one_sync(f"HASHMISS|{scope}|{key}", sender_node_id, interface, pause_seconds=0.1)
        elif message.startswith("HASHMISS|"):
            if not sender_node_id:
                return
            parts = message.split("|", 2)
            if len(parts) != 3:
                logging.warning(f"Malformed HASHMISS ignored: {message}")
                return
            scope, key = parts[1], parts[2]
            if scope not in _SUPPORTED_HASH_SCOPES:
                return
            _send_requested_record(scope, key, sender_node_id, interface)
        elif message.startswith("PROFILESYNC|"):
            parts = message.split("|", 7)
            if len(parts) != 8:
                logging.warning(f"Malformed PROFILESYNC ignored: {message}")
                return
            try:
                short_name = base64.b64decode(parts[2].encode('ascii')).decode('utf-8')
                long_name = base64.b64decode(parts[3].encode('ascii')).decode('utf-8')
                messages_sent = int(parts[6])
                bio = base64.b64decode(parts[7].encode('ascii')).decode('utf-8')
            except Exception:
                logging.warning(f"Malformed PROFILESYNC payload ignored: {message}")
                return
            upsert_synced_user_profile(parts[1], short_name, long_name, parts[4], parts[5], messages_sent, bio)
        elif message.startswith("SCORESYNC|"):
            parts = message.split("|", 7)
            if len(parts) != 8:
                logging.warning(f"Malformed SCORESYNC ignored: {message}")
                return
            try:
                short_name = base64.b64decode(parts[3].encode('ascii')).decode('utf-8')
                score = int(parts[4])
                max_score = int(parts[5])
                moves = int(parts[6])
            except Exception:
                logging.warning(f"Malformed SCORESYNC payload ignored: {message}")
                return
            upsert_synced_game_score(parts[1], parts[2], short_name, score, max_score, moves, parts[7])
        elif message.startswith("ZORKSAVE|"):
            # Legacy: ZORKSAVE|save_id|user_b64|game_b64|updated_at|chunk_idx|total_chunks|chunk_b64
            # New:    ZORKSAVE|save_id|user_b64|game_b64|updated_at|payload_hash|chunk_idx|total_chunks|chunk_b64
            parts = message.split("|", 8)
            if len(parts) not in (8, 9):
                logging.warning(f"Malformed ZORKSAVE ignored: {message}")
                return
            save_id, user_b64, game_b64, updated_at = parts[1], parts[2], parts[3], parts[4]
            payload_hash = parts[5] if len(parts) == 9 else ''
            try:
                chunk_idx = int(parts[6] if len(parts) == 9 else parts[5])
                total_chunks = int(parts[7] if len(parts) == 9 else parts[6])
            except ValueError:
                logging.warning(f"Malformed ZORKSAVE indices ignored: {message}")
                return
            if total_chunks <= 0 or chunk_idx < 0 or chunk_idx >= total_chunks:
                logging.warning(f"Malformed ZORKSAVE chunk bounds ignored: {message}")
                return

            _prune_old_zork_save_chunks()
            sender_key = sender_node_id or "unknown"
            key = (sender_key, save_id)
            buf = _zork_save_chunk_buffers.get(key)
            if buf is None:
                buf = {
                    'user_b64': user_b64,
                    'game_b64': game_b64,
                    'updated_at_str': updated_at,
                    'payload_hash': payload_hash,
                    'total': total_chunks,
                    'chunks': {},
                    'updated_at': time.time(),
                }
                _zork_save_chunk_buffers[key] = buf
            elif buf.get('total') != total_chunks:
                # Conflicting frame set for same save id; reset buffer.
                buf = {
                    'user_b64': user_b64,
                    'game_b64': game_b64,
                    'updated_at_str': updated_at,
                    'payload_hash': payload_hash,
                    'total': total_chunks,
                    'chunks': {},
                    'updated_at': time.time(),
                }
                _zork_save_chunk_buffers[key] = buf

            buf['updated_at'] = time.time()
            if chunk_idx not in buf['chunks']:
                buf['chunks'][chunk_idx] = parts[8] if len(parts) == 9 else parts[7]

            if len(buf['chunks']) == buf['total']:
                try:
                    ordered = ''.join(buf['chunks'][i] for i in range(buf['total']))
                    user_id = base64.b64decode(buf['user_b64'].encode('ascii')).decode('utf-8')
                    game_id = base64.b64decode(buf['game_b64'].encode('ascii')).decode('utf-8')
                    save_data = base64.b64decode(ordered.encode('ascii'))
                    expected_hash = str(buf.get('payload_hash', '') or '')
                    if expected_hash:
                        actual_hash = base64.urlsafe_b64encode(
                            hashlib.blake2b(save_data, digest_size=8).digest()
                        ).decode('ascii').rstrip('=')
                        if actual_hash != expected_hash:
                            logging.warning(
                                f"ZORKSAVE hash mismatch for save_id {save_id}: expected {expected_hash}, got {actual_hash}"
                            )
                            _zork_save_chunk_buffers.pop(key, None)
                            return
                    upsert_synced_zork_save(user_id, game_id, save_data, buf['updated_at_str'])
                except Exception:
                    logging.warning(f"Malformed ZORKSAVE payload ignored: {message}")
                finally:
                    _zork_save_chunk_buffers.pop(key, None)
    else:
        if message_lower.startswith("sm,,"):
            handle_send_mail_command(sender_id, message_strip, interface, bbs_nodes)
        elif message_lower.startswith("cm"):
            handle_check_mail_command(sender_id, interface)
        elif message_lower.startswith("pb,,"):
            handle_post_bulletin_command(sender_id, message_strip, interface, bbs_nodes)
        elif message_lower.startswith("cb,,"):
            handle_check_bulletin_command(sender_id, message_strip, interface)
        elif message_lower.startswith("chp,,"):
            handle_post_channel_command(sender_id, message_strip, interface)
        elif message_lower.startswith("chl"):
            handle_list_channels_command(sender_id, interface)
        else:
            if state and state['command'] == 'MENU':
                menu_name = state['menu']
                if menu_name == 'bbs':
                    handlers = bbs_menu_handlers
                elif menu_name == 'utilities':
                    handlers = utilities_menu_handlers
                    number_alias = {
                        '1': 's',
                        '2': 'f',
                        '3': 'w',
                        '4': 'g',
                        '0': 'x',
                    }
                    message_lower = number_alias.get(message_lower, message_lower)
                else:
                    handlers = main_menu_handlers
            elif state and state['command'] == 'BULLETIN_MENU':
                if message_lower == 'x':
                    handle_help_command(sender_id, interface)
                else:
                    handle_bb_steps(sender_id, message_strip, 1, state, interface, bbs_nodes)
                return
            elif state and state['command'] == 'BULLETIN_ACTION':
                handlers = board_action_handlers
            elif state and state['command'] == 'JS8CALL_MENU':
                handle_js8call_steps(sender_id, message, state['step'], interface, state)
                return
            elif state and state['command'] == 'GROUP_MESSAGES':
                handle_group_message_selection(sender_id, message, state['step'], state, interface)
                return
            else:
                handlers = main_menu_handlers

            if message_lower == 'x' and not (state and state.get('command') == 'ZORK'):
                # Reset to main menu state
                handle_help_command(sender_id, interface)
                return

            if message_lower in handlers:
                if state and state['command'] in ['BULLETIN_ACTION', 'BULLETIN_READ', 'BULLETIN_POST', 'BULLETIN_POST_CONTENT']:
                    handlers[message_lower](sender_id, interface, state)
                else:
                    handlers[message_lower](sender_id, interface)
            elif state:
                command = state['command']
                step = state['step']

                if command == 'MAIL':
                    handle_mail_steps(sender_id, message, step, state, interface, bbs_nodes)
                elif command == 'BULLETIN':
                    handle_bb_steps(sender_id, message, step, state, interface, bbs_nodes)
                elif command == 'STATS':
                    handle_stats_steps(sender_id, message, step, interface)
                elif command == 'CHANNEL_DIRECTORY':
                    handle_channel_directory_steps(sender_id, message, step, state, interface)
                elif command == 'CHECK_MAIL':
                    if step == 1:
                        handle_read_mail_command(sender_id, message, state, interface)
                    elif step == 2:
                        handle_delete_mail_confirmation(sender_id, message, state, interface, bbs_nodes)
                elif command == 'CHECK_BULLETIN':
                    if step == 1:
                        handle_read_bulletin_command(sender_id, message, state, interface)
                elif command == 'CHECK_CHANNEL':
                    if step == 1:
                        handle_read_channel_command(sender_id, message, state, interface)
                elif command == 'LIST_CHANNELS':
                    if step == 1:
                        handle_read_channel_command(sender_id, message, state, interface)
                elif command == 'BULLETIN_POST':
                    handle_bb_steps(sender_id, message, 4, state, interface, bbs_nodes)
                elif command == 'BULLETIN_POST_CONTENT':
                    handle_bb_steps(sender_id, message, 5, state, interface, bbs_nodes)
                elif command == 'BULLETIN_READ':
                    handle_bb_steps(sender_id, message, 3, state, interface, bbs_nodes)
                elif command == 'JS8CALL_MENU':
                    handle_js8call_steps(sender_id, message, step, interface, state)
                elif command == 'GROUP_MESSAGES':
                    handle_group_message_selection(sender_id, message, step, state, interface)
                elif command == 'GAMES_MENU':
                    handle_games_steps(sender_id, message, interface)
                elif command == 'ZORK':
                    handle_zork_steps(sender_id, message, interface)
                elif command == 'SCOREBOARD':
                    handle_scoreboard_steps(sender_id, message, interface)
                elif command == 'PROFILE':
                    handle_profile_steps(sender_id, message, interface)
                else:
                    handle_help_command(sender_id, interface)
            else:
                handle_help_command(sender_id, interface)


def on_receive(packet, interface):
    try:
        if 'decoded' in packet and packet['decoded']['portnum'] == 'TEXT_MESSAGE_APP':
            message_bytes = packet['decoded']['payload']
            message_string = message_bytes.decode('utf-8')
            sender_id = packet['from']
            to_id = packet.get('to')
            sender_node_id = packet['fromId']

            sender_short_name = get_node_short_name(sender_node_id, interface)
            receiver_short_name = get_node_short_name(get_node_id_from_num(to_id, interface),
                                                      interface) if to_id else "Group Chat"
            logging.info(f"Received message from user '{sender_short_name}' ({sender_node_id}) to {receiver_short_name}: {message_string}")

            bbs_nodes = interface.bbs_nodes
            is_sync_message = any(message_string.startswith(prefix) for prefix in
                                  ["BULLETIN|", "MAIL|", "DELETE_BULLETIN|", "DELETE_MAIL|",
                                   "CHANNEL|", "BULLETINCONT|", "MAILCONT|", "SYNCSTATE|",
                                   "PROFILESYNC|", "SCORESYNC|", "ZORKSAVE|",
                                   "HASHREQ|", "HASHREC|", "HASHEND|", "HASHMISS|"])

            msg_type = "sync" if is_sync_message else "user"
            log_connection_event(
                sender_id,
                sender_node_id,
                sender_short_name,
                to_id,
                msg_type,
                f"RX {msg_type} message to {to_id if to_id is not None else 'group'}",
            )

            if sender_node_id in bbs_nodes:
                if is_sync_message:
                    log_connection_event(sender_id, sender_node_id, sender_short_name, to_id, "sync", "Accepted sync message")
                    process_message(sender_id, message_string, interface, is_sync_message=True, sender_node_id=sender_node_id)
                else:
                    log_connection_event(sender_id, sender_node_id, sender_short_name, to_id, "drop", "Ignored non-sync from BBS node")
                    logging.info("Ignoring non-sync message from known BBS node")
            elif to_id is not None and to_id != 0 and to_id != 255 and to_id == interface.myInfo.my_node_num:
                log_connection_event(sender_id, sender_node_id, sender_short_name, to_id, "direct", "Accepted direct message")
                process_message(sender_id, message_string, interface, is_sync_message=False, sender_node_id=sender_node_id)
            else:
                log_connection_event(sender_id, sender_node_id, sender_short_name, to_id, "drop", "Ignored group/unknown message")
                logging.info("Ignoring message sent to group chat or from unknown node")
    except KeyError as e:
        logging.error(f"Error processing packet: {e}")
