import logging
import base64
import hashlib
import json
import os
import re
import time
import zlib
import uuid

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
    add_channel_comment_by_manifest_key, delete_channel_comment,
    append_bulletin_content, append_mail_content,
    append_channel_comment_content,
    flush_pending_bulletin_continuations, flush_pending_mail_continuations,
    flush_pending_channel_comment_continuations,
    apply_bulletin_expected_content_length, apply_mail_expected_content_length,
    apply_channel_comment_expected_content_length,
    auto_upsert_user_profile, log_connection_event, upsert_peer_sync_state,
    log_sync_transmission,
    upsert_synced_user_profile, upsert_synced_game_score,
    upsert_synced_zork_save,
    apply_synced_zork_save_delete,
    get_mismatched_peer_scopes,
    get_record_hash_manifest,
    get_sync_progress,
    get_bulletin_by_unique_id,
    get_mail_by_unique_id,
    get_channel_by_manifest_key,
    get_channel_comment_by_unique_id,
    make_channel_manifest_key,
    get_profile_by_user_id,
    get_game_score_by_user_and_game,
    get_zork_save_row_by_user_and_game,
    get_sync_tombstone_deleted_at,
    get_recent_sync_tombstones,
    has_sync_tombstone,
)
from js8call_integration import handle_js8call_command, handle_js8call_steps, handle_group_message_selection
from utils import (
    get_user_state, get_node_short_name, get_node_id_from_num, send_message,
    send_bulletin_to_bbs_nodes, send_mail_to_bbs_nodes, send_channel_to_bbs_nodes,
    send_channel_comment_to_bbs_nodes,
    send_profile_to_bbs_nodes, send_game_score_to_bbs_nodes, send_zork_save_to_bbs_nodes,
    send_delete_bulletin_to_bbs_nodes, send_delete_mail_to_bbs_nodes,
    send_delete_channel_comment_to_bbs_nodes,
    send_delete_zork_save_to_bbs_nodes,
    send_hash_request_to_bbs_nodes,
    get_hash_repair_pause_seconds,
    is_zork_save_sync_enabled,
    _send_one_sync, _MESHTASTIC_MAX_BYTES,
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
_peer_hash_compressed_buffers = {}
_SUPPORTED_HASH_SCOPES = ["bulletins", "mail", "channels", "profiles", "game_scores", "zork_saves", "tombstones"]
_HASH_BUFFER_MAX_AGE_SECONDS = 600
_recent_hashmiss_requests = {}
# Maximum records pulled (HASHMISS) or pushed per single reconcile pass to avoid
# flooding LoRa and causing the packet loss that stalls convergence.
_RECONCILE_MAX_PULL_PER_PASS = 20
_RECONCILE_MAX_PUSH_PER_PASS = 20
_recent_syncstate_repairs = {}
_SYNCSTATE_REPAIR_TTL_SECONDS = 90
# Pattern for the optional original-date field appended to BULLETIN/MAIL wire frames.
_SYNC_DATE_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')
_candidate_resolution_requests = {}
_recent_candidate_resolution_results = []
_CANDIDATE_REQUEST_TIMEOUT_SECONDS = 15.0
# Track in-flight HASHREQ exchanges so we don't flood a peer with duplicate requests
# while their manifest response is still being assembled.
_pending_hashreq = {}  # (peer_id, scope) -> float timestamp
_PENDING_HASHREQ_TIMEOUT = 60


def _prune_old_zork_save_chunks() -> None:
    now = time.time()
    stale_keys = [k for k, v in _zork_save_chunk_buffers.items()
                  if now - v.get('updated_at', now) > _ZORK_SAVE_BUFFER_MAX_AGE_SECONDS]
    for key in stale_keys:
        _zork_save_chunk_buffers.pop(key, None)


def _prune_old_hash_manifest_chunks() -> None:
    now = time.time()
    stale_keys = [
        k for k, v in _peer_hash_compressed_buffers.items()
        if now - v.get('updated_at', now) > _HASH_BUFFER_MAX_AGE_SECONDS
    ]
    for key in stale_keys:
        _peer_hash_compressed_buffers.pop(key, None)


def _candidate_payload_hash(payload: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.blake2b(payload or b'', digest_size=8).digest()).decode('ascii').rstrip('=')


def _candidate_request_key(user_id: str, game_id: str) -> str:
    return f"{user_id}:{game_id}"


def _build_local_zork_save_candidate(user_id: str, game_id: str, source_peer: str = 'local') -> dict:
    row = get_zork_save_row_by_user_and_game(user_id, game_id)
    tombstone_deleted_at = get_sync_tombstone_deleted_at('zork_saves', _candidate_request_key(user_id, game_id))
    if row:
        payload = row[2] or b''
        save_candidate = {
            'kind': 'save',
            'updated_at': str(row[3] or ''),
            'size': len(payload),
            'payload_hash': _candidate_payload_hash(payload),
            'source_peer': str(source_peer),
        }
        if tombstone_deleted_at and tombstone_deleted_at >= save_candidate['updated_at']:
            return {
                'kind': 'tombstone',
                'updated_at': str(tombstone_deleted_at),
                'size': 0,
                'payload_hash': '',
                'source_peer': str(source_peer),
            }
        return save_candidate
    if tombstone_deleted_at:
        return {
            'kind': 'tombstone',
            'updated_at': str(tombstone_deleted_at),
            'size': 0,
            'payload_hash': '',
            'source_peer': str(source_peer),
        }
    return {
        'kind': 'missing',
        'updated_at': '',
        'size': 0,
        'payload_hash': '',
        'source_peer': str(source_peer),
    }


def _candidate_rank(candidate: dict) -> tuple:
    kind = str(candidate.get('kind', 'missing'))
    updated_at = str(candidate.get('updated_at', '') or '')
    if kind == 'tombstone':
        return (3, updated_at, 0, str(candidate.get('payload_hash', '') or ''), str(candidate.get('source_peer', '') or ''))
    if kind == 'save':
        return (2, updated_at, int(candidate.get('size', 0) or 0), str(candidate.get('payload_hash', '') or ''), str(candidate.get('source_peer', '') or ''))
    return (1, updated_at, 0, '', str(candidate.get('source_peer', '') or ''))


def _select_best_candidate(candidates: list[dict]):
    valid = [candidate for candidate in candidates if isinstance(candidate, dict)]
    if not valid:
        return None
    return max(valid, key=_candidate_rank)


def _record_candidate_resolution_result(result: dict) -> None:
    _recent_candidate_resolution_results.append(dict(result))
    if len(_recent_candidate_resolution_results) > 12:
        del _recent_candidate_resolution_results[:-12]


def get_candidate_resolution_snapshot() -> dict:
    active = []
    for req_id, state in sorted(_candidate_resolution_requests.items(), key=lambda item: item[1].get('started_at', 0.0), reverse=True):
        active.append({
            'request_id': str(req_id),
            'key': str(state.get('key', '')),
            'status': str(state.get('status', 'collecting')),
            'requested_at': str(state.get('requested_at', '')),
            'responses': len(state.get('responses', {})),
            'expected': len(state.get('expected_peers', set())),
        })
    return {
        'active': active,
        'recent': list(_recent_candidate_resolution_results),
    }


def _send_candidate_response(scope: str, request_id: str, user_id: str, game_id: str, candidate: dict, destination_node_id: str, interface) -> None:
    user_b64 = base64.b64encode(str(user_id).encode('utf-8')).decode('ascii')
    game_b64 = base64.b64encode(str(game_id).encode('utf-8')).decode('ascii')
    kind = str(candidate.get('kind', 'missing'))
    updated_at = str(candidate.get('updated_at', '') or '')
    size = int(candidate.get('size', 0) or 0)
    payload_hash = str(candidate.get('payload_hash', '') or '')
    message = f"CANDRSP|{scope}|{request_id}|{user_b64}|{game_b64}|{kind}|{updated_at}|{size}|{payload_hash}"
    _send_one_sync(message, destination_node_id, interface, pause_seconds=get_hash_repair_pause_seconds())


def start_zork_save_best_candidate_resolution(user_id: str, game_id: str, peer_node_ids: list[str], interface) -> str:
    normalized_user = str(user_id).strip()
    normalized_game = str(game_id).strip()
    key = _candidate_request_key(normalized_user, normalized_game)
    peers = {str(peer).strip() for peer in (peer_node_ids or []) if str(peer).strip()}
    request_id = uuid.uuid4().hex[:12]
    now = time.time()
    _candidate_resolution_requests[request_id] = {
        'scope': 'zork_saves',
        'request_id': request_id,
        'key': key,
        'user_id': normalized_user,
        'game_id': normalized_game,
        'expected_peers': peers,
        'responses': {'local': _build_local_zork_save_candidate(normalized_user, normalized_game, source_peer='local')},
        'status': 'collecting',
        'requested_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
        'started_at': now,
        'result': '',
    }
    user_b64 = base64.b64encode(normalized_user.encode('utf-8')).decode('ascii')
    game_b64 = base64.b64encode(normalized_game.encode('utf-8')).decode('ascii')
    message = f"CANDREQ|zork_saves|{request_id}|{user_b64}|{game_b64}"
    for peer_id in sorted(peers):
        _send_one_sync(message, peer_id, interface, pause_seconds=get_hash_repair_pause_seconds())
    if not peers:
        _finalize_candidate_resolution_request(request_id, interface, timed_out=False)
    return request_id


def _finalize_candidate_resolution_request(request_id: str, interface, timed_out: bool) -> None:
    state = _candidate_resolution_requests.pop(request_id, None)
    if not state:
        return
    candidates = list(state.get('responses', {}).values())
    best = _select_best_candidate(candidates)
    key = str(state.get('key', ''))
    result_text = 'no candidates received'
    action = 'none'
    if best:
        kind = str(best.get('kind', 'missing'))
        source_peer = str(best.get('source_peer', 'local') or 'local')
        if source_peer != 'local' and kind == 'save':
            _send_one_sync(f"HASHMISS|zork_saves|{key}", source_peer, interface, pause_seconds=get_hash_repair_pause_seconds())
            action = f"pull-save:{source_peer}"
            result_text = f"Best candidate save requested from {source_peer} @ {best.get('updated_at', '')} ({best.get('size', 0)} bytes)"
        elif source_peer != 'local' and kind == 'tombstone':
            _send_one_sync(f"HASHMISS|tombstones|zork_saves:{key}", source_peer, interface, pause_seconds=get_hash_repair_pause_seconds())
            action = f"pull-tombstone:{source_peer}"
            result_text = f"Best candidate tombstone requested from {source_peer} @ {best.get('updated_at', '')}"
        elif source_peer == 'local' and kind == 'save':
            action = 'local-save-best'
            result_text = f"Local save already best candidate @ {best.get('updated_at', '')} ({best.get('size', 0)} bytes)"
        elif source_peer == 'local' and kind == 'tombstone':
            action = 'local-tombstone-best'
            result_text = f"Local tombstone already best candidate @ {best.get('updated_at', '')}"
        else:
            action = 'no-record'
            result_text = 'No peer reported a usable save candidate'
    result = {
        'request_id': str(request_id),
        'key': key,
        'status': 'timed_out' if timed_out else 'completed',
        'action': action,
        'result': result_text,
        'requested_at': str(state.get('requested_at', '')),
        'completed_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'responses': len(state.get('responses', {})),
        'expected': len(state.get('expected_peers', set())),
    }
    _record_candidate_resolution_result(result)


def process_pending_candidate_resolutions(interface) -> None:
    now = time.time()
    ready = []
    for request_id, state in list(_candidate_resolution_requests.items()):
        expected_peers = set(state.get('expected_peers', set()))
        responded = {peer for peer in state.get('responses', {}).keys() if peer != 'local'}
        if expected_peers and responded >= expected_peers:
            ready.append((request_id, False))
        elif (now - float(state.get('started_at', now))) >= _CANDIDATE_REQUEST_TIMEOUT_SECONDS:
            ready.append((request_id, True))
    for request_id, timed_out in ready:
        _finalize_candidate_resolution_request(request_id, interface, timed_out=timed_out)


def _hash_manifest_compression_enabled() -> bool:
    # Compression is ON by default; set BBS_HASH_MANIFEST_COMPRESSION=0 to disable.
    return str(os.getenv("BBS_HASH_MANIFEST_COMPRESSION", "1")).strip().lower() not in ("0", "false", "no", "off")


def _prune_recent_hashmiss_requests() -> None:
    now = time.time()
    ttl_seconds = _get_hashmiss_request_ttl_seconds()
    stale_keys = [
        k for k, last_sent in _recent_hashmiss_requests.items()
        if now - float(last_sent) > ttl_seconds
    ]
    for key in stale_keys:
        _recent_hashmiss_requests.pop(key, None)


def _get_hashmiss_request_ttl_seconds() -> float:
    raw = str(os.getenv("BBS_HASHMISS_REQUEST_TTL_SECONDS", "30")).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 30.0


def _should_send_hashmiss(sender_node_id: str, scope: str, key: str, local_hash: str, remote_hash: str) -> bool:
    _prune_recent_hashmiss_requests()
    now = time.time()
    ttl_seconds = _get_hashmiss_request_ttl_seconds()
    sig = (str(sender_node_id), str(scope), str(key), str(local_hash), str(remote_hash))
    last_sent = _recent_hashmiss_requests.get(sig)
    if ttl_seconds > 0 and last_sent is not None and (now - float(last_sent)) < ttl_seconds:
        return False
    _recent_hashmiss_requests[sig] = now
    return True


def _mark_hashreq_pending(peer_id: str, scope: str) -> None:
    _pending_hashreq[(str(peer_id), str(scope))] = time.time()


def _clear_hashreq_pending(peer_id: str, scope: str) -> None:
    _pending_hashreq.pop((str(peer_id), str(scope)), None)


def _is_hashreq_pending(peer_id: str, scope: str) -> bool:
    key = (str(peer_id), str(scope))
    ts = _pending_hashreq.get(key)
    if ts is None:
        return False
    if time.time() - float(ts) > _PENDING_HASHREQ_TIMEOUT:
        _pending_hashreq.pop(key, None)
        return False
    return True


def is_hashreq_pending_for_peer_scope(peer_id: str, scope: str) -> bool:
    """Public accessor for server.py to check before sending its own HASHREQs."""
    return _is_hashreq_pending(peer_id, scope)


def _prune_recent_syncstate_repairs() -> None:
    now = time.time()
    stale_keys = [
        k for k, last_sent in _recent_syncstate_repairs.items()
        if now - float(last_sent) > _SYNCSTATE_REPAIR_TTL_SECONDS
    ]
    for key in stale_keys:
        _recent_syncstate_repairs.pop(key, None)


def _request_targeted_repair_if_needed(sender_node_id: str, interface) -> None:
    # Don't pile hash-repair on top of an active full sync — it overwhelms LoRa.
    if get_sync_progress().get('in_progress'):
        return

    by_peer = get_mismatched_peer_scopes({sender_node_id})
    scopes = by_peer.get(str(sender_node_id), [])
    if not scopes:
        return

    _prune_recent_syncstate_repairs()
    now = time.time()
    repair_sig = (str(sender_node_id), tuple(sorted(scopes)))
    last_sent = _recent_syncstate_repairs.get(repair_sig)
    if last_sent is not None and (now - float(last_sent)) < _SYNCSTATE_REPAIR_TTL_SECONDS:
        return

    _recent_syncstate_repairs[repair_sig] = now
    requested_scopes = [s for s in scopes if not _is_hashreq_pending(sender_node_id, s)]
    if not requested_scopes:
        logging.debug(f"SYNCSTATE mismatch from {sender_node_id} but all scopes already have in-flight HASHREQ; skipping")
        return
    logging.info(f"SYNCSTATE mismatch from {sender_node_id}; requesting targeted repair for scopes: {', '.join(requested_scopes)}")
    for scope in requested_scopes:
        send_hash_request_to_bbs_nodes([sender_node_id], interface, scope=scope)
        _mark_hashreq_pending(sender_node_id, scope)


def _reconcile_remote_manifest(scope: str, sender_node_id: str, interface) -> None:
    if scope == 'zork_saves' and not is_zork_save_sync_enabled():
        _peer_hash_manifest_buffers.pop((sender_node_id, scope), None)
        return
    remote = _peer_hash_manifest_buffers.pop((sender_node_id, scope), {})
    local = get_record_hash_manifest(scope)
    remote_keys = set(remote.keys())
    local_keys = set(local.keys())

    # Ask peer for keys we do not have, plus keys that exist on both sides but differ.
    need_from_remote = set(remote_keys - local_keys)
    need_from_remote.update(key for key in (remote_keys & local_keys) if local.get(key) != remote.get(key))
    push_keys = sorted(local_keys - remote_keys)
    logging.info(
        f"Reconciling manifest scope={scope} peer={sender_node_id} "
        f"remote_keys={len(remote_keys)} local_keys={len(local_keys)} "
        f"pull={len(need_from_remote)} push={len(push_keys)}"
    )

    # Cap HASHMISS requests per pass to avoid flooding the LoRa channel, which causes
    # the very packet loss that stalls convergence.  Deferred keys will be retried on
    # the next SYNCSTATE → HASHREQ → reconcile cycle.
    pull_sent = 0
    for key in sorted(need_from_remote):
        if pull_sent >= _RECONCILE_MAX_PULL_PER_PASS:
            logging.info(
                f"Reconcile pull cap reached ({_RECONCILE_MAX_PULL_PER_PASS}) for scope={scope} peer={sender_node_id}; "
                f"{len(need_from_remote) - pull_sent} key(s) deferred to next repair cycle"
            )
            break
        local_hash = str(local.get(key, ""))
        remote_hash = str(remote.get(key, ""))
        if not _should_send_hashmiss(sender_node_id, scope, key, local_hash, remote_hash):
            continue
        if scope in ('bulletins', 'mail', 'zork_saves', 'channels') and key not in local and has_sync_tombstone(scope, key):
            logging.info(f"Requesting tombstone replay from {sender_node_id} for {scope}:{key}")
            _send_one_sync(f"HASHMISS|tombstones|{scope}:{key}", sender_node_id, interface, pause_seconds=get_hash_repair_pause_seconds())
        else:
            logging.info(f"Requesting record from {sender_node_id} scope={scope} key={key}")
            _send_one_sync(f"HASHMISS|{scope}|{key}", sender_node_id, interface, pause_seconds=get_hash_repair_pause_seconds())
        pull_sent += 1

    # Proactively push records the peer is missing to converge in one cycle.
    # Also capped per pass to avoid blocking the receive callback for too long.
    push_sent = 0
    for key in push_keys:
        if push_sent >= _RECONCILE_MAX_PUSH_PER_PASS:
            logging.info(
                f"Reconcile push cap reached ({_RECONCILE_MAX_PUSH_PER_PASS}) for scope={scope} peer={sender_node_id}; "
                f"{len(push_keys) - push_sent} key(s) deferred to next repair cycle"
            )
            break
        logging.info(f"Pushing local-only record to {sender_node_id} scope={scope} key={key}")
        _send_requested_record(scope, key, sender_node_id, interface)
        push_sent += 1


def _send_hash_manifest_to_peer(scope: str, destination_node_id: str, interface) -> None:
    manifest = get_record_hash_manifest(scope)
    logging.info(f"Sending hash manifest to {destination_node_id} scope={scope} count={len(manifest)} compressed={_hash_manifest_compression_enabled()}")
    if not _hash_manifest_compression_enabled():
        for key, rec_hash in manifest.items():
            _send_one_sync(f"HASHREC|{scope}|{key}|{rec_hash}", destination_node_id, interface, pause_seconds=get_hash_repair_pause_seconds())
        _send_one_sync(f"HASHEND|{scope}|{len(manifest)}", destination_node_id, interface, pause_seconds=get_hash_repair_pause_seconds())
        return

    payload = json.dumps(manifest, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    compressed = zlib.compress(payload, level=6)
    b64 = base64.urlsafe_b64encode(compressed).decode("ascii")
    manifest_id = str(int(time.time() * 1000))
    prefix = f"HASHZ|{scope}|{manifest_id}|"
    max_chunk = _MESHTASTIC_MAX_BYTES - len(prefix.encode("utf-8")) - len("999999|999999|".encode("utf-8"))
    if max_chunk <= 0:
        logging.warning("HASHZ prefix too large for packet limit; falling back to HASHREC")
        for key, rec_hash in manifest.items():
            _send_one_sync(f"HASHREC|{scope}|{key}|{rec_hash}", destination_node_id, interface, pause_seconds=get_hash_repair_pause_seconds())
        _send_one_sync(f"HASHEND|{scope}|{len(manifest)}", destination_node_id, interface, pause_seconds=get_hash_repair_pause_seconds())
        return

    chunks = [b64[i:i + max_chunk] for i in range(0, len(b64), max_chunk)] or [""]
    total = len(chunks)
    for idx, chunk in enumerate(chunks):
        _send_one_sync(f"{prefix}{idx}|{total}|{chunk}", destination_node_id, interface, pause_seconds=get_hash_repair_pause_seconds())


def _send_requested_record(scope: str, key: str, destination_node_id: str, interface) -> None:
    if scope == 'bulletins':
        row = get_bulletin_by_unique_id(key)
        if row:
            # row: (board, sender_short_name, date, subject, content, unique_id)
            logging.info(f"Sending requested bulletin to {destination_node_id} key={key}")
            send_bulletin_to_bbs_nodes(row[0], row[1], row[3], row[4], row[5], [destination_node_id], interface, date=row[2])
        else:
            logging.warning(f"Requested bulletin missing locally for resend key={key}")
    elif scope == 'mail':
        row = get_mail_by_unique_id(key)
        if row:
            # row: (sender, sender_short_name, recipient, date, subject, content, unique_id)
            logging.info(f"Sending requested mail to {destination_node_id} key={key}")
            send_mail_to_bbs_nodes(row[0], row[1], row[2], row[4], row[5], row[6], [destination_node_id], interface, date=row[3])
        else:
            logging.warning(f"Requested mail missing locally for resend key={key}")
    elif scope == 'channels':
        if str(key).startswith('comment:'):
            unique_id = str(key).split(':', 1)[1]
            row = get_channel_comment_by_unique_id(unique_id)
            if row:
                logging.info(f"Sending requested channel comment to {destination_node_id} key={key}")
                send_channel_comment_to_bbs_nodes(
                    make_channel_manifest_key(row[0], row[1]),
                    row[2], row[3], row[4], row[5], [destination_node_id], interface,
                )
            else:
                logging.warning(f"Requested channel comment missing locally for resend key={key}")
        else:
            row = get_channel_by_manifest_key(key)
            if row:
                logging.info(f"Sending requested channel to {destination_node_id} key={key}")
                send_channel_to_bbs_nodes(row[0], row[1], [destination_node_id], interface)
            else:
                logging.warning(f"Requested channel missing locally for resend key={key}")
    elif scope == 'profiles':
        row = get_profile_by_user_id(key)
        if row:
            logging.info(f"Sending requested profile to {destination_node_id} key={key}")
            send_profile_to_bbs_nodes(row[0], row[1], row[2], row[3], row[4], row[5], row[6], [destination_node_id], interface)
    elif scope == 'game_scores':
        if ':' not in key:
            return
        user_id, game_id = key.split(':', 1)
        row = get_game_score_by_user_and_game(user_id, game_id)
        if row:
            logging.info(f"Sending requested game score to {destination_node_id} key={key}")
            send_game_score_to_bbs_nodes(row[0], row[1], row[2], row[3], row[4], row[5], row[6], [destination_node_id], interface)
    elif scope == 'zork_saves':
        if ':' not in key:
            return
        user_id, game_id = key.split(':', 1)
        row = get_zork_save_row_by_user_and_game(user_id, game_id)
        if row:
            logging.info(f"Sending requested zork save to {destination_node_id} key={key}")
            send_zork_save_to_bbs_nodes(row[0], row[1], row[2], row[3], [destination_node_id], interface, pause_seconds=get_hash_repair_pause_seconds())
    elif scope == 'tombstones':
        if key.startswith('bulletins:'):
            unique_id = key.split(':', 1)[1]
            logging.info(f"Replaying bulletin delete to {destination_node_id} key={key}")
            send_delete_bulletin_to_bbs_nodes(unique_id, [destination_node_id], interface)
        elif key.startswith('mail:'):
            unique_id = key.split(':', 1)[1]
            logging.info(f"Replaying mail delete to {destination_node_id} key={key}")
            send_delete_mail_to_bbs_nodes(unique_id, [destination_node_id], interface)
        elif key.startswith('channels:comment:'):
            unique_id = key.split(':', 2)[2]
            logging.info(f"Replaying channel comment delete to {destination_node_id} key={key}")
            send_delete_channel_comment_to_bbs_nodes(unique_id, [destination_node_id], interface)
        elif key.startswith('zork_saves:'):
            remainder = key.split(':', 1)[1]
            if ':' not in remainder:
                return
            user_id, game_id = remainder.split(':', 1)
            deleted_at = get_sync_tombstone_deleted_at('zork_saves', f"{user_id}:{game_id}")
            if not deleted_at:
                return
            logging.info(f"Replaying zork save delete to {destination_node_id} key={key}")
            send_delete_zork_save_to_bbs_nodes(user_id, game_id, deleted_at, [destination_node_id], interface)


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
            # Use rsplit from the right so content with embedded '|' is handled correctly.
            # Wire format: BULLETIN|board|sender|subject|content|unique_id[|date]
            body = message[len("BULLETIN|"):]
            # Try to strip optional date from the far right.
            tail = body.rsplit("|", 2)
            if len(tail) == 3 and _SYNC_DATE_PATTERN.match(tail[2]):
                original_date, unique_id = tail[2], tail[1]
                header_content = tail[0]
            else:
                tail2 = body.rsplit("|", 1)
                original_date = None
                unique_id = tail2[1] if len(tail2) == 2 else body
                header_content = tail2[0] if len(tail2) == 2 else ""
            hparts = header_content.split("|", 3)
            if len(hparts) != 4:
                logging.warning(f"Malformed BULLETIN sync message ignored: {message}")
                return
            board, sender_short_name, subject, content = hparts[0], hparts[1], hparts[2], hparts[3]
            add_bulletin(board, sender_short_name, subject, content, [], interface, unique_id=unique_id, date=original_date)
            flush_pending_bulletin_continuations(unique_id)

            if board.lower() == "urgent":
                notification_message = f"💥NEW URGENT BULLETIN💥\nFrom: {sender_short_name}\nTitle: {subject}\nDM 'CB,,Urgent' to view"
                send_message(notification_message, BROADCAST_NUM, interface)
        elif message.startswith("MAIL|"):
            # Wire format: MAIL|sender_id|sender_short|recipient_id|subject|content|unique_id[|date]
            body = message[len("MAIL|"):]
            tail = body.rsplit("|", 2)
            if len(tail) == 3 and _SYNC_DATE_PATTERN.match(tail[2]):
                original_date, unique_id = tail[2], tail[1]
                header_content = tail[0]
            else:
                tail2 = body.rsplit("|", 1)
                original_date = None
                unique_id = tail2[1] if len(tail2) == 2 else body
                header_content = tail2[0] if len(tail2) == 2 else ""
            hparts = header_content.split("|", 4)
            if len(hparts) != 5:
                logging.warning(f"Malformed MAIL sync message ignored: {message}")
                return
            sync_sender_id, sender_short_name, recipient_id, subject, content = hparts[0], hparts[1], hparts[2], hparts[3], hparts[4]
            add_mail(sync_sender_id, sender_short_name, recipient_id, subject, content, [], interface, unique_id=unique_id, date=original_date)
            flush_pending_mail_continuations(unique_id)
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
        elif message.startswith("DELETE_CHANNELCOMMENT|"):
            parts = message.split("|", 1)
            if len(parts) != 2 or not parts[1]:
                logging.warning(f"Malformed DELETE_CHANNELCOMMENT sync message ignored: {message}")
                return
            delete_channel_comment(parts[1], [], interface)
        elif message.startswith("DELETE_ZORKSAVE|"):
            if not is_zork_save_sync_enabled():
                logging.info("Ignoring DELETE_ZORKSAVE because zork save sync is disabled locally")
                return
            parts = message.split("|", 3)
            if len(parts) != 4 or not parts[1] or not parts[2] or not parts[3]:
                logging.warning(f"Malformed DELETE_ZORKSAVE sync message ignored: {message}")
                return
            try:
                user_id = base64.b64decode(parts[1].encode('ascii')).decode('utf-8')
                game_id = base64.b64decode(parts[2].encode('ascii')).decode('utf-8')
            except Exception:
                logging.warning(f"Malformed DELETE_ZORKSAVE payload ignored: {message}")
                return
            apply_synced_zork_save_delete(user_id, game_id, parts[3])
        elif message.startswith("CHANNEL|"):
            parts = message.split("|", 2)
            if len(parts) != 3:
                logging.warning(f"Malformed CHANNEL sync message ignored: {message}")
                return
            channel_name, channel_url = parts[1], parts[2]
            add_channel(channel_name, channel_url)
        elif message.startswith("CHANNELCOMMENT|"):
            # Wire format: CHANNELCOMMENT|{manifest_key}|{b64_sender}|{date}|{content}|{unique_id}
            # Use rsplit from the right so content with embedded '|' is handled correctly.
            body = message[len("CHANNELCOMMENT|"):]
            tail = body.rsplit("|", 1)
            if len(tail) != 2 or not tail[1]:
                logging.warning(f"Malformed CHANNELCOMMENT sync message ignored: {message}")
                return
            unique_id = tail[1]
            hparts = tail[0].split("|", 3)
            if len(hparts) != 4:
                logging.warning(f"Malformed CHANNELCOMMENT header ignored: {message}")
                return
            channel_key, b64_sender_raw, comment_date, content = hparts
            try:
                sender_short_name = base64.b64decode(b64_sender_raw.encode('ascii')).decode('utf-8')
            except Exception:
                logging.warning(f"Malformed CHANNELCOMMENT sender ignored: {message}")
                return
            add_channel_comment_by_manifest_key(channel_key, sender_short_name, comment_date, content, unique_id)
            flush_pending_channel_comment_continuations(unique_id)
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
        elif message.startswith("BULLETINMETA|"):
            parts = message.split("|", 2)
            if len(parts) != 3 or not parts[1]:
                logging.warning(f"Malformed BULLETINMETA sync message ignored: {message}")
                return
            try:
                expected_length = int(parts[2])
            except ValueError:
                logging.warning(f"Malformed BULLETINMETA length ignored: {message}")
                return
            apply_bulletin_expected_content_length(parts[1], expected_length)
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
        elif message.startswith("MAILMETA|"):
            parts = message.split("|", 2)
            if len(parts) != 3 or not parts[1]:
                logging.warning(f"Malformed MAILMETA sync message ignored: {message}")
                return
            try:
                expected_length = int(parts[2])
            except ValueError:
                logging.warning(f"Malformed MAILMETA length ignored: {message}")
                return
            apply_mail_expected_content_length(parts[1], expected_length)
        elif message.startswith("CHANNELCOMMENTCONT|"):
            parts = message.split("|", 3)
            if len(parts) < 3 or not parts[1]:
                logging.warning(f"Malformed CHANNELCOMMENTCONT sync message ignored: {message}")
                return
            if len(parts) == 4:
                try:
                    offset = int(parts[2])
                except ValueError:
                    logging.warning(f"Malformed CHANNELCOMMENTCONT offset ignored: {message}")
                    return
                append_channel_comment_content(parts[1], offset, parts[3])
            else:
                append_channel_comment_content(parts[1], None, parts[2])
        elif message.startswith("CHANNELCOMMENTMETA|"):
            parts = message.split("|", 2)
            if len(parts) != 3 or not parts[1]:
                logging.warning(f"Malformed CHANNELCOMMENTMETA sync message ignored: {message}")
                return
            try:
                expected_length = int(parts[2])
            except ValueError:
                logging.warning(f"Malformed CHANNELCOMMENTMETA length ignored: {message}")
                return
            apply_channel_comment_expected_content_length(parts[1], expected_length)
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
                _request_targeted_repair_if_needed(sender_node_id, interface)
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
        elif message.startswith("HASHZ|"):
            if not sender_node_id:
                return
            parts = message.split("|", 5)
            if len(parts) != 6:
                logging.warning(f"Malformed HASHZ ignored: {message}")
                return
            scope, manifest_id = parts[1], parts[2]
            if scope not in _SUPPORTED_HASH_SCOPES:
                return
            try:
                chunk_idx = int(parts[3])
                total_chunks = int(parts[4])
            except ValueError:
                logging.warning(f"Malformed HASHZ chunk index ignored: {message}")
                return
            if total_chunks <= 0 or chunk_idx < 0 or chunk_idx >= total_chunks:
                logging.warning(f"Malformed HASHZ chunk bounds ignored: {message}")
                return

            _prune_old_hash_manifest_chunks()
            buf_key = (sender_node_id, scope, manifest_id)
            buf = _peer_hash_compressed_buffers.get(buf_key)
            if buf is None or int(buf.get('total', -1)) != total_chunks:
                buf = {
                    'total': total_chunks,
                    'chunks': {},
                    'updated_at': time.time(),
                }
                _peer_hash_compressed_buffers[buf_key] = buf
            buf['updated_at'] = time.time()
            if chunk_idx not in buf['chunks']:
                buf['chunks'][chunk_idx] = parts[5]

            if len(buf['chunks']) == total_chunks:
                try:
                    payload_b64 = ''.join(buf['chunks'][i] for i in range(total_chunks))
                    payload_bytes = base64.urlsafe_b64decode(payload_b64.encode('ascii'))
                    manifest_obj = json.loads(zlib.decompress(payload_bytes).decode('utf-8'))
                    if isinstance(manifest_obj, dict):
                        normalized = {str(k): str(v) for k, v in manifest_obj.items()}
                        _peer_hash_manifest_buffers[(sender_node_id, scope)] = normalized
                        _clear_hashreq_pending(sender_node_id, scope)
                        _reconcile_remote_manifest(scope, sender_node_id, interface)
                except Exception:
                    logging.warning(f"Malformed HASHZ payload ignored: {message}")
                    _clear_hashreq_pending(sender_node_id, scope)
                finally:
                    _peer_hash_compressed_buffers.pop(buf_key, None)
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
            _clear_hashreq_pending(sender_node_id, scope)
            _reconcile_remote_manifest(scope, sender_node_id, interface)
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
        elif message.startswith("CANDREQ|"):
            if not is_zork_save_sync_enabled():
                return
            if not sender_node_id:
                return
            parts = message.split("|", 4)
            if len(parts) != 5:
                logging.warning(f"Malformed CANDREQ ignored: {message}")
                return
            scope, request_id = parts[1], parts[2]
            if scope != 'zork_saves':
                return
            try:
                user_id = base64.b64decode(parts[3].encode('ascii')).decode('utf-8')
                game_id = base64.b64decode(parts[4].encode('ascii')).decode('utf-8')
            except Exception:
                logging.warning(f"Malformed CANDREQ payload ignored: {message}")
                return
            candidate = _build_local_zork_save_candidate(user_id, game_id, source_peer='local')
            _send_candidate_response(scope, request_id, user_id, game_id, candidate, sender_node_id, interface)
        elif message.startswith("CANDRSP|"):
            if not is_zork_save_sync_enabled():
                return
            if not sender_node_id:
                return
            parts = message.split("|", 8)
            if len(parts) != 9:
                logging.warning(f"Malformed CANDRSP ignored: {message}")
                return
            scope, request_id = parts[1], parts[2]
            if scope != 'zork_saves':
                return
            try:
                user_id = base64.b64decode(parts[3].encode('ascii')).decode('utf-8')
                game_id = base64.b64decode(parts[4].encode('ascii')).decode('utf-8')
            except Exception:
                logging.warning(f"Malformed CANDRSP payload ignored: {message}")
                return
            state = _candidate_resolution_requests.get(request_id)
            if not state:
                return
            if str(state.get('key', '')) != _candidate_request_key(user_id, game_id):
                return
            try:
                size = int(parts[7] or 0)
            except ValueError:
                size = 0
            state.setdefault('responses', {})[str(sender_node_id)] = {
                'kind': str(parts[5] or 'missing'),
                'updated_at': str(parts[6] or ''),
                'size': size,
                'payload_hash': str(parts[8] or ''),
                'source_peer': str(sender_node_id),
            }
            process_pending_candidate_resolutions(interface)
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
            if not is_zork_save_sync_enabled():
                logging.info("Ignoring ZORKSAVE because zork save sync is disabled locally")
                return
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
            if state and state['command'] in ('MENU', 'MAIN_MENU'):
                menu_name = state.get('menu', 'main')
                if menu_name == 'bbs':
                    handlers = bbs_menu_handlers
                    _bbs_alias = {'1': 'm', '2': 'b', '3': 'c', '4': 'j', '0': 'x'}
                    message_lower = _bbs_alias.get(message_lower, message_lower)
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
                    _main_alias = {'1': 'q', '2': 'b', '3': 'u', '4': 'p', '0': 'x'}
                    message_lower = _main_alias.get(message_lower, message_lower)
            elif state and state['command'] == 'BULLETIN_MENU':
                if message_lower in ('x', '0'):
                    handle_help_command(sender_id, interface)
                else:
                    handle_bb_steps(sender_id, message_strip, 1, state, interface, bbs_nodes)
                return
            elif state and state['command'] == 'BULLETIN_ACTION':
                _bb_action_alias = {'1': 'r', '2': 'p', '0': 'x'}
                message_lower = _bb_action_alias.get(message_lower, message_lower)
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
                                  ["BULLETIN|", "MAIL|", "DELETE_BULLETIN|", "DELETE_MAIL|", "DELETE_ZORKSAVE|",
                                   "CHANNEL|", "CHANNELCOMMENT|", "CHANNELCOMMENTCONT|", "CHANNELCOMMENTMETA|", "DELETE_CHANNELCOMMENT|",
                                   "BULLETINCONT|", "MAILCONT|", "BULLETINMETA|", "MAILMETA|", "SYNCSTATE|",
                                   "PROFILESYNC|", "SCORESYNC|", "ZORKSAVE|", "CANDREQ|", "CANDRSP|",
                                   "HASHREQ|", "HASHREC|", "HASHEND|", "HASHMISS|", "HASHZ|"])

            msg_type = "sync" if is_sync_message else "user"
            sync_frame = message_string.split("|", 1)[0] if is_sync_message and "|" in message_string else (message_string[:24] if is_sync_message else "")
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
                    try:
                        log_sync_transmission(
                            message_string,
                            sender_node_id,
                            len(message_bytes),
                            is_continuation=message_string.startswith(("BULLETINCONT|", "MAILCONT|", "CHANNELCOMMENTCONT|")),
                            direction='rx',
                        )
                    except Exception as exc:
                        logging.debug(f"Failed to record received sync transmission: {exc}")
                    log_connection_event(sender_id, sender_node_id, sender_short_name, to_id, "sync", f"Accepted sync message ({sync_frame})")
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
