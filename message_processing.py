import logging
import base64
import hashlib
import json
import os
import random
import re
import time
import zlib
import uuid
import threading

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
    handle_apigw_command, handle_apigw_steps,
    handle_ask_nomad_command, handle_ask_nomad_steps,
    handle_account_steps,
    handle_settings_command, handle_settings_steps,
    handle_active_users_command,
    deliver_ask_nomad_reply,
    number_alias, MAIN_NUMBER_MAP, BBS_NUMBER_MAP, UTILITIES_NUMBER_MAP,
)
from db_operations import (
    add_bulletin, add_mail, delete_bulletin, delete_mail, add_channel,
    add_channel_comment_by_manifest_key, delete_channel_comment, delete_channel,
    decode_channel_manifest_key, make_channel_manifest_key,
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
    get_scopes_to_request_repair,
    get_record_hash_manifest,
    get_local_record_counts,
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
    get_user_state, update_user_state, get_node_short_name, resolve_display_name, get_node_id_from_num, send_message,
    send_bulletin_to_bbs_nodes, send_mail_to_bbs_nodes, send_channel_to_bbs_nodes,
    send_channel_comment_to_bbs_nodes,
    send_profile_to_bbs_nodes, send_game_score_to_bbs_nodes, send_zork_save_to_bbs_nodes,
    send_delete_bulletin_to_bbs_nodes, send_delete_mail_to_bbs_nodes,
    send_delete_channel_comment_to_bbs_nodes,
    send_delete_channel_to_bbs_nodes,
    send_delete_zork_save_to_bbs_nodes,
    send_hash_request_to_bbs_nodes,
    send_sync_state_to_bbs_nodes,
    get_hash_repair_pause_seconds,
    get_hash_chunk_pause_seconds,
    get_repair_cycle_seconds,
    get_reconcile_max_per_pass,
    is_zork_save_sync_enabled,
    decode_ts_minute, decode_ts_second,
    encode_scope, decode_scope, decode_uid, peers_all_support,
    encode_text, decode_text,
    pack_missing, unpack_missing,
    compact_channel_manifest_key,
    send_api_response, pop_api_request, get_api_request,
    _send_one_sync, get_max_text_bytes,
)

# Digit shortcuts, derived from the same label tables build_menu renders, so
# a menu entry can never show a number the input handler then ignores.
_MAIN_NUMBER_ALIAS = number_alias(MAIN_NUMBER_MAP)
_BBS_NUMBER_ALIAS = number_alias(BBS_NUMBER_MAP)
_UTILITIES_NUMBER_ALIAS = number_alias(UTILITIES_NUMBER_MAP)
# The API Gateway moved to the main menu, so it no longer has a Utilities
# number -- but 5 meant "API Gateway" here for a long time, so keep honoring
# it rather than silently dropping people back to the menu.
_UTILITIES_NUMBER_ALIAS.setdefault('5', 'a')

main_menu_handlers = {
    "q": handle_quick_help_command,
    "b": lambda sender_id, interface: handle_help_command(sender_id, interface, 'bbs'),
    "u": lambda sender_id, interface: handle_help_command(sender_id, interface, 'utilities'),
    "p": handle_profile_command,
    "n": handle_ask_nomad_command,
    "a": handle_apigw_command,
    "s": handle_settings_command,
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
    "a": handle_apigw_command,
    "x": handle_help_command
}


board_action_handlers = {
    "r": lambda sender_id, interface, state: handle_bb_steps(sender_id, 'r', 2, state, interface, None),
    "p": lambda sender_id, interface, state: handle_bb_steps(sender_id, 'p', 2, state, interface, None),
    "x": handle_help_command
}


_zork_save_chunk_buffers = {}
_ZORK_SAVE_BUFFER_MAX_AGE_SECONDS = 600
# Max ZORKGAP retry rounds before falling back to a full HASHMISS retransmit.
_ZORK_GAP_FILL_MAX_ATTEMPTS = 3
# Max HASHZGAP retry rounds before falling back to dropping the buffer and
# issuing a fresh HASHREQ (which makes the sender start over from scratch).
# Raised from 3 to 6: retrying individual missing chunks is much cheaper than
# restarting the full manifest exchange, especially on lossy LoRa links.
_HASHZ_GAP_FILL_MAX_ATTEMPTS = 6
_peer_hash_manifest_buffers = {}
_peer_hash_compressed_buffers = {}

# ---------------------------------------------------------------------------
# Striped HASSMISS: collect manifests from multiple peers for a brief window
# before reconciling, then distribute HASSMISS requests round-robin so each
# peer only handles a fraction of the missing records.  This roughly halves
# recovery time with 2 peers, thirds it with 3, etc.
# ---------------------------------------------------------------------------
# How long (seconds) to wait for additional peer manifests before reconciling.
# On LoRa timescales (single packets take 1-3 s) a 5 s window comfortably
# catches both peers' manifests without adding meaningful latency.
# Set BBS_STRIPE_COLLECT_SECONDS=0 to reconcile synchronously on each manifest
# (no batching) — used by tests for deterministic behaviour.
_STRIPE_COLLECT_SECONDS: float = float(os.environ.get("BBS_STRIPE_COLLECT_SECONDS", "5"))
# scope → {peer_id: {key: hash}}  — manifests waiting for the timer to fire
_pending_stripe_manifests: dict = {}
# scope → threading.Timer
_stripe_timers: dict = {}
_stripe_lock = threading.Lock()
# Cache of recently-sent HASHZ manifest chunks so we can replay specific
# indices when a peer asks for gap-fill via HASHZGAP. Keyed by
# (destination_node_id, scope, manifest_id). Entry shape:
#   {'chunks': [b64_str, ...], 'total': int, 'created_at': float}
_outgoing_hash_manifest_cache = {}
_hash_buffer_lock = threading.RLock()
_SUPPORTED_HASH_SCOPES = ["bulletins", "mail", "channels", "channel_comments", "profiles", "game_scores", "zork_saves", "tombstones"]
_HASH_BUFFER_MAX_AGE_SECONDS = 600
# After this many seconds with no new chunk for a HASHZ manifest, drop the
# partial buffer and re-issue HASHREQ. Without this, a single dropped chunk
# would otherwise leave the buffer stuck for ``_HASH_BUFFER_MAX_AGE_SECONDS``
# (10 minutes) before pruning, during which no retry happens on either side
# and sync appears stalled.
# Raised from 15s to 35s: gives more time for all chunks to arrive before
# triggering gap-fill. On a 3-node mesh with a 6-chunk manifest, 15s was
# too aggressive — gap-fills were firing before the last chunks landed,
# flooding the channel with redundant requests.
_HASH_BUFFER_RETRY_AFTER_SECONDS = 35
_recent_hashmiss_requests = {}
# Transient API-gateway response reassembly buffers, keyed by request id (rid) →
# {'status', 'expected', 'parts': {offset: chunk}}. In-memory only (responses are
# not DB records); guarded by a lock (radio thread writes, sweep reads).
_apigw_response_buffers: dict = {}
_apigw_buffers_lock = threading.Lock()
# Peer-AGNOSTIC in-flight record-request guard. Keyed by (scope, key) with no
# peer id, so once we ask ANY peer for a record we don't also ask the others
# for the same record until it arrives or the TTL lapses. This is what stops
# the "both peers answer the same HASHMISS → record received twice" duplicate.
# Retry-on-loss is preserved: after the TTL the guard expires and the next
# reconcile may pick a different peer.
_inflight_record_requests: dict = {}
# Per-pass HASHMISS pull/push caps and SYNCSTATE repair TTL are tunable via the
# [sync] config section (reconcile_max_per_pass, repair_cycle_seconds) or the
# corresponding BBS_* environment variables. Turbo mode lifts these defaults.
_recent_syncstate_repairs = {}
# Pattern for the optional original-date field appended to BULLETIN/MAIL wire
# frames.  Matches both legacy ISO "YYYY-MM-DD HH:MM" and PR 2 epoch form
# "m<seconds>".  Distinct prefix 'm' (minute precision) disambiguates from the
# 's' (second precision) source_timestamp tokens even when both are present.
_SYNC_DATE_PATTERN = re.compile(r'^(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}|m\d+)$')
# Pattern for the optional source_timestamp field.  Matches both legacy ISO
# "YYYY-MM-DDTHH:MM:SS[...]" and PR 2 epoch form "s<seconds>".
_SYNC_ISO_TIMESTAMP_PATTERN = re.compile(r'^(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}|s\d+$)')
_candidate_resolution_requests = {}
_recent_candidate_resolution_results = []
_CANDIDATE_REQUEST_TIMEOUT_SECONDS = 15.0
# Track in-flight HASHREQ exchanges so we don't flood a peer with duplicate requests
# while their manifest response is still being assembled.
_pending_hashreq = {}  # (peer_id, scope) -> float timestamp
_PENDING_HASHREQ_TIMEOUT = 25


def _prune_old_zork_save_chunks() -> None:
    now = time.time()
    stale_keys = [k for k, v in _zork_save_chunk_buffers.items()
                  if now - v.get('updated_at', now) > _ZORK_SAVE_BUFFER_MAX_AGE_SECONDS]
    for key in stale_keys:
        _zork_save_chunk_buffers.pop(key, None)


def _retry_stale_zork_save_buffers(interface) -> None:
    """Recover from LoRa frame loss during a multi-chunk ZORKSAVE push.

    For a partial buffer that has stopped advancing for
    ``_HASH_BUFFER_RETRY_AFTER_SECONDS``, send a targeted gap-fill request
    (``ZORKGAP|save_id|user_b64|game_b64|csv``) listing only the missing
    indices. The sender re-emits just those frames. The partial buffer is
    preserved so already-received chunks are not retransmitted.

    After ``_ZORK_GAP_FILL_MAX_ATTEMPTS`` unproductive gap-fill rounds the
    buffer is dropped and a full HASHMISS retransmit is requested as a
    fallback, in case the buffer or the sender's record got out of sync.
    """
    if not _zork_save_chunk_buffers:
        return
    now = time.time()
    candidates = []
    for (peer_id, save_id), buf in list(_zork_save_chunk_buffers.items()):
        if now - float(buf.get('updated_at', now)) <= _HASH_BUFFER_RETRY_AFTER_SECONDS:
            continue
        chunks = buf.get('chunks', {})
        total = int(buf.get('total', 0))
        if total <= 0 or len(chunks) >= total:
            continue
        candidates.append((peer_id, save_id, buf, chunks, total))
    for peer_id, save_id, buf, chunks, total in candidates:
        last_count = int(buf.get('last_retry_received', -1))
        received = len(chunks)
        # Reset attempt counter whenever new chunks arrived since the last retry.
        if received != last_count:
            buf['gap_attempts'] = 0
        buf['last_retry_received'] = received
        attempts = int(buf.get('gap_attempts', 0))
        missing = sorted(set(range(total)) - set(int(i) for i in chunks.keys()))

        if attempts >= _ZORK_GAP_FILL_MAX_ATTEMPTS:
            # Gap-fill not working; drop buffer and fall back to full retransmit.
            _zork_save_chunk_buffers.pop((peer_id, save_id), None)
            logging.warning(
                f"ZORKSAVE gap-fill exhausted (peer={peer_id} save_id={save_id} "
                f"{received}/{total} chunks, missing={missing}); falling back to full HASHMISS"
            )
            try:
                _send_one_sync(
                    f"HASHMISS|zork_saves|{save_id}",
                    peer_id,
                    interface,
                    pause_seconds=get_hash_repair_pause_seconds(interface),
                )
            except Exception as exc:
                logging.warning(f"Failed to re-request ZORKSAVE {peer_id}/{save_id}: {exc}")
            continue

        # Send a ZORKGAP request for only the missing frames.
        user_b64 = str(buf.get('user_b64', ''))
        game_b64 = str(buf.get('game_b64', ''))
        csv = pack_missing(missing, total, peers_all_support([peer_id], 'bmgap'))
        gap_msg = f"ZORKGAP|{save_id}|{user_b64}|{game_b64}|{csv}"
        if len(gap_msg.encode('utf-8')) > 200:
            # CSV too large to fit; fall back immediately.
            buf['gap_attempts'] = _ZORK_GAP_FILL_MAX_ATTEMPTS
            buf['updated_at'] = now
            logging.warning(
                f"ZORKSAVE gap list too large for one frame (peer={peer_id} save_id={save_id} "
                f"missing={len(missing)}); will fall back to HASHMISS next tick"
            )
            continue
        buf['gap_attempts'] = attempts + 1
        buf['updated_at'] = now  # don't immediately retry next tick
        logging.warning(
            f"ZORKSAVE buffer stalled (peer={peer_id} save_id={save_id} "
            f"{received}/{total} chunks, missing={missing}); sending ZORKGAP attempt "
            f"{attempts + 1}/{_ZORK_GAP_FILL_MAX_ATTEMPTS}"
        )
        try:
            _send_one_sync(
                gap_msg,
                peer_id,
                interface,
                pause_seconds=get_hash_repair_pause_seconds(interface),
            )
        except Exception as exc:
            logging.warning(f"Failed to send ZORKGAP {peer_id}/{save_id}: {exc}")


def _prune_old_hash_manifest_chunks() -> None:
    now = time.time()
    with _hash_buffer_lock:
        stale_keys = [
            k for k, v in _peer_hash_compressed_buffers.items()
            if now - v.get('updated_at', now) > _HASH_BUFFER_MAX_AGE_SECONDS
        ]
        for key in stale_keys:
            _peer_hash_compressed_buffers.pop(key, None)
        stale_out = [
            k for k, v in _outgoing_hash_manifest_cache.items()
            if now - v.get('created_at', now) > _HASH_BUFFER_MAX_AGE_SECONDS
        ]
        for key in stale_out:
            _outgoing_hash_manifest_cache.pop(key, None)


_last_retry_stale_hash_ts: float = 0.0


def _retry_stale_hash_manifest_buffers(interface) -> None:
    """Recover from LoRa frame loss during a multi-chunk HASHZ manifest stream.

    Same failure mode as ZORKSAVE: a stalled partial buffer means one or more
    chunk frames were dropped on the air. Instead of re-issuing HASHREQ (which
    makes the sender replay the *entire* manifest and is very likely to lose
    the same chunks again), send HASHZGAP listing only the missing indices.
    The sender replays just those frames using cached chunk data keyed by
    manifest_id, so the receiver's partial buffer remains valid.

    After ``_HASHZ_GAP_FILL_MAX_ATTEMPTS`` unproductive gap-fill rounds, fall
    back to the original behavior: drop the buffer and re-issue HASHREQ.
    """
    global _last_retry_stale_hash_ts
    with _hash_buffer_lock:
        if not _peer_hash_compressed_buffers:
            return
    now = time.time()
    # Rate-limit to once per _HASH_BUFFER_RETRY_AFTER_SECONDS to avoid
    # sending HASHZGAPs for stale buffers mid-stream (which would collide
    # with ongoing HASHZ chunk delivery on half-duplex LoRa).
    if now - _last_retry_stale_hash_ts < _HASH_BUFFER_RETRY_AFTER_SECONDS:
        return
    _last_retry_stale_hash_ts = now
    candidates = []
    with _hash_buffer_lock:
        for (peer_id, scope, manifest_id), buf in list(_peer_hash_compressed_buffers.items()):
            if now - float(buf.get('updated_at', now)) <= _HASH_BUFFER_RETRY_AFTER_SECONDS:
                continue
            chunks = dict(buf.get('chunks', {}))
            total = int(buf.get('total', 0))
            if total <= 0 or len(chunks) >= total:
                continue
            candidates.append((peer_id, scope, manifest_id, len(chunks), total, chunks))

    for peer_id, scope, manifest_id, received, total, chunks_snapshot in candidates:
        with _hash_buffer_lock:
            buf = _peer_hash_compressed_buffers.get((peer_id, scope, manifest_id))
            if not buf:
                continue
            current_chunks = dict(buf.get('chunks', {}))
            if len(current_chunks) != received:
                continue
            last_count = int(buf.get('last_retry_received', -1))
            if received != last_count:
                buf['gap_attempts'] = 0
            buf['last_retry_received'] = received
            attempts = int(buf.get('gap_attempts', 0))
            missing = sorted(set(range(total)) - set(int(i) for i in current_chunks.keys()))

            if attempts >= _HASHZ_GAP_FILL_MAX_ATTEMPTS:
                # If we already have the majority of chunks, keep retrying rather
                # than discarding the partial buffer and starting over from scratch.
                # Resetting gap_attempts here lets the HASHZGAP loop continue.
                # However, if the buffer has been alive too long it means the
                # sender has lost the manifest from its cache and HASHZGAP will
                # never succeed — so cap majority-retries by wall-clock age.
                buffer_age = now - float(buf.get('created_at', now))
                majority_and_young = (
                    received > 0
                    and total > 0
                    and received >= (total + 1) // 2
                    and buffer_age < _HASH_BUFFER_MAX_AGE_SECONDS // 5
                )
                if majority_and_young:
                    buf['gap_attempts'] = 0
                    buf['updated_at'] = now
                    fallback_to_hashreq = False
                    _scope_wire = encode_scope(scope, peers_all_support([peer_id], 'scc'))
                    csv = pack_missing(missing, total, peers_all_support([peer_id], 'bmgap'))
                    gap_msg = f"HASHZGAP|{_scope_wire}|{manifest_id}|{csv}"
                    if len(gap_msg.encode('utf-8')) > 200:
                        gap_msg = ""
                else:
                    _peer_hash_compressed_buffers.pop((peer_id, scope, manifest_id), None)
                    _clear_hashreq_pending(peer_id, scope)
                    fallback_to_hashreq = True
                    gap_msg = ""
            else:
                _scope_wire = encode_scope(scope, peers_all_support([peer_id], 'scc'))
                csv = pack_missing(missing, total, peers_all_support([peer_id], 'bmgap'))
                gap_msg = f"HASHZGAP|{_scope_wire}|{manifest_id}|{csv}"
                if len(gap_msg.encode('utf-8')) > 200:
                    buf['gap_attempts'] = _HASHZ_GAP_FILL_MAX_ATTEMPTS
                    buf['updated_at'] = now
                    gap_msg = ""
                    fallback_to_hashreq = False
                else:
                    buf['gap_attempts'] = attempts + 1
                    buf['updated_at'] = now
                    fallback_to_hashreq = False

        if fallback_to_hashreq:
            logging.warning(
                f"HASHZ gap-fill exhausted (peer={peer_id} scope={scope} manifest_id={manifest_id} "
                f"{received}/{total} chunks, missing={missing}); falling back to full HASHREQ"
            )
            try:
                send_hash_request_to_bbs_nodes([peer_id], interface, scope=scope)
                _mark_hashreq_pending(peer_id, scope)
            except Exception as exc:
                logging.warning(f"Failed to re-request HASHZ for {peer_id}/{scope}: {exc}")
            continue

        if not gap_msg:
            logging.warning(
                f"HASHZ gap list too large for one frame (peer={peer_id} scope={scope} "
                f"manifest_id={manifest_id} missing={len(missing)}); will fall back to HASHREQ next tick"
            )
            continue

        logging.warning(
            f"HASHZ buffer stalled (peer={peer_id} scope={scope} manifest_id={manifest_id} "
            f"{received}/{total} chunks, missing={missing}); sending HASHZGAP"
        )
        try:
            _send_one_sync(
                gap_msg,
                peer_id,
                interface,
                pause_seconds=get_hash_repair_pause_seconds(interface),
            )
        except Exception as exc:
            logging.warning(f"Failed to send HASHZGAP for {peer_id}/{scope}: {exc}")


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
    _use_plain = peers_all_support([destination_node_id], 'nob64')
    user_b64 = encode_text(str(user_id), _use_plain)
    game_b64 = encode_text(str(game_id), _use_plain)
    kind = str(candidate.get('kind', 'missing'))
    updated_at = str(candidate.get('updated_at', '') or '')
    size = int(candidate.get('size', 0) or 0)
    payload_hash = str(candidate.get('payload_hash', '') or '')
    _scope_wire = encode_scope(scope, peers_all_support([destination_node_id], 'scc'))
    message = f"CANDRSP|{_scope_wire}|{request_id}|{user_b64}|{game_b64}|{kind}|{updated_at}|{size}|{payload_hash}"
    _send_one_sync(message, destination_node_id, interface, pause_seconds=get_hash_repair_pause_seconds(interface))


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
    user_b64 = normalized_user
    game_b64 = normalized_game
    for peer_id in sorted(peers):
        _use_plain = peers_all_support([peer_id], 'nob64')
        _scope_wire = encode_scope('zork_saves', peers_all_support([peer_id], 'scc'))
        _u = encode_text(user_b64, _use_plain)
        _g = encode_text(game_b64, _use_plain)
        message = f"CANDREQ|{_scope_wire}|{request_id}|{_u}|{_g}"
        _send_one_sync(message, peer_id, interface, pause_seconds=get_hash_repair_pause_seconds(interface))
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
            _send_one_sync(f"HASHMISS|zork_saves|{key}", source_peer, interface, pause_seconds=get_hash_repair_pause_seconds(interface))
            action = f"pull-save:{source_peer}"
            result_text = f"Best candidate save requested from {source_peer} @ {best.get('updated_at', '')} ({best.get('size', 0)} bytes)"
        elif source_peer != 'local' and kind == 'tombstone':
            _send_one_sync(f"HASHMISS|tombstones|zork_saves:{key}", source_peer, interface, pause_seconds=get_hash_repair_pause_seconds(interface))
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


def process_stale_sync_buffers(interface) -> None:
    """Periodic tick to drive stale-buffer retries when no sync messages arrive.

    The retry helpers ``_retry_stale_hash_manifest_buffers`` and
    ``_retry_stale_zork_save_buffers`` are also invoked opportunistically on
    incoming SYNCSTATE/HASHZ/ZORKSAVE frames, but if the peer simply stops
    sending (because of dropped chunks) there's no event to drive them. This
    function is meant to be called from the server's main loop on a steady
    cadence so a stalled partial buffer always gets a HASHZGAP/ZORKGAP after
    ``_HASH_BUFFER_RETRY_AFTER_SECONDS``.
    """
    try:
        _retry_stale_hash_manifest_buffers(interface)
    except Exception as exc:
        logging.debug(f"process_stale_sync_buffers: hash retry tick failed: {exc}")
    try:
        _retry_stale_zork_save_buffers(interface)
    except Exception as exc:
        logging.debug(f"process_stale_sync_buffers: zork retry tick failed: {exc}")


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


def _should_request_record(scope: str, key: str) -> bool:
    """Peer-agnostic guard: return True (and mark in-flight) only if no request
    for this (scope, key) has gone to ANY peer within the TTL. Prevents pulling
    the same missing record from multiple peers, which otherwise returns the
    record once per peer that has it."""
    now = time.time()
    ttl = _get_hashmiss_request_ttl_seconds()
    if ttl <= 0:
        return True
    # Opportunistic prune so the dict can't grow unbounded.
    stale = [k for k, ts in _inflight_record_requests.items() if now - float(ts) > ttl]
    for k in stale:
        _inflight_record_requests.pop(k, None)
    sig = (str(scope), str(key))
    last = _inflight_record_requests.get(sig)
    if last is not None and (now - float(last)) < ttl:
        return False
    _inflight_record_requests[sig] = now
    return True


def _apigw_apply_chunk(rid, offset, chunk, status=None, expected=None, source=None):
    """Accumulate an API-response chunk by absolute char offset. Returns
    (status, body) once the assembled length reaches the expected total, else
    None. Mirrors the bulletin CONT reassembly but in transient memory."""
    with _apigw_buffers_lock:
        buf = _apigw_response_buffers.setdefault(
            rid, {'status': '', 'expected': None, 'parts': {}, 'source': None, 'created_at': time.time()}
        )
        if status is not None:
            buf['status'] = status
        if expected is not None:
            buf['expected'] = int(expected)
        if source is not None:
            buf['source'] = source
        buf['parts'][int(offset)] = chunk
        assembled_len = sum(len(c) for c in buf['parts'].values())
        if buf['expected'] is not None and assembled_len >= buf['expected']:
            body = ''.join(buf['parts'][o] for o in sorted(buf['parts']))
            st = buf['status']
            _apigw_response_buffers.pop(rid, None)
            return (st, body)
    return None


def _compute_response_gaps(parts: dict, expected: int) -> list:
    """Given received {offset: chunk} and the expected total length, return the
    list of missing (start, end) byte ranges (end exclusive). Assumes contiguous
    chunks laid at their offsets; any uncovered span is a gap to refill."""
    gaps = []
    cursor = 0
    for off in sorted(parts):
        if off > cursor:
            gaps.append((cursor, off))  # hole before this chunk
        cursor = max(cursor, off + len(parts[off]))
    if cursor < expected:
        gaps.append((cursor, expected))  # trailing tail never arrived
    return gaps


def request_pending_api_gaps(interface, max_age=12.0, cooldown=10.0, no_response_age=45.0):
    """Requester side sweep (driven by the main loop): for each outstanding API
    request whose response is missing or incomplete, ask the gateway to refill
    the gaps via APIRESPGAP. Only targets gateways advertising 'apigf'. Returns
    the number of gap requests sent.

    Two distinct timeouts:
    - ``max_age``: once we hold a *partial* response, how long a hole may sit
      before we ask the gateway to refill it (short — packets are in flight).
    - ``no_response_age``: when *nothing* has arrived yet, how long to wait
      before asking for a full resend. Must exceed the gateway's processing
      time (e.g. an AI call), otherwise every request emits a wasteful full
      resend before the gateway has even produced an answer."""
    from utils import (list_pending_api_requests, mark_api_gap_request,
                       _send_one_sync, get_sync_pause_seconds)
    from db_operations import peer_supports
    now = time.time()
    sent = 0
    for rid, entry in list_pending_api_requests():
        gateway = entry.get('gateway')
        # Locate any partial buffer (it may carry the live gateway source).
        with _apigw_buffers_lock:
            buf = _apigw_response_buffers.get(rid)
            buf_snap = None
            if buf is not None:
                buf_snap = {
                    'expected': buf.get('expected'),
                    'parts': dict(buf.get('parts', {})),
                    'source': buf.get('source'),
                    'created_at': buf.get('created_at', entry.get('created_at', now)),
                }
        target = (buf_snap or {}).get('source') or gateway
        if not target or not peer_supports(target, 'apigf'):
            continue
        # Respect a per-request cooldown so we don't spam refill requests.
        if now - float(entry.get('last_gap_req', 0.0)) < cooldown:
            continue
        if buf_snap is None:
            # No response at all yet — wait long enough for the gateway to finish
            # its work (AI call, HTTP fetch) before asking for a full resend, so
            # we don't burn airtime nudging a gateway that's still computing.
            if now - float(entry.get('created_at', now)) < no_response_age:
                continue
            spec = "*"  # resend everything (header may have been the lost frame)
        else:
            if now - float(buf_snap['created_at']) < max_age:
                continue
            expected = buf_snap['expected']
            if not expected:
                spec = "*"  # never learned the total length; ask for a full resend
            else:
                gaps = _compute_response_gaps(buf_snap['parts'], int(expected))
                if not gaps:
                    continue
                spec = ",".join(f"{a}-{b}" for a, b in gaps)
        _send_one_sync(f"APIRESPGAP|{rid}|{spec}", target, interface,
                       pause_seconds=get_sync_pause_seconds(interface))
        mark_api_gap_request(rid)
        sent += 1
    return sent


def _deliver_api_response(rid, status, body, interface):
    """Resolve a completed API response to the waiting user and DM it.

    Peeks the pending entry's 'kind' BEFORE popping it (pop_api_request
    clears the entry) so an AI-relay (Project Nomad) response can offer the
    same ask-another-question follow-up here as the local-gateway fast path
    in command_handlers._apigw_submit -- an HTTP GET response gets none,
    matching the original one-shot flow."""
    pending = get_api_request(rid)
    sender_id = pop_api_request(rid)
    if sender_id is None:
        return  # no waiter (already timed out / unknown rid)
    prefix = "" if str(status) in ("200", "OK") else f"[{status}] "
    text = f"{prefix}{body}"
    if pending and pending.get('kind') == 'r':
        # Answer and invitation in ONE message: two DMs two seconds apart
        # race each other's relay traffic on a multi-hop mesh, and the
        # second one loses. See command_handlers.deliver_ask_nomad_reply.
        deliver_ask_nomad_reply(text, sender_id, interface)
    else:
        send_message(text, sender_id, interface)


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


def _prune_recent_syncstate_repairs(interface=None) -> None:
    now = time.time()
    ttl = get_repair_cycle_seconds(interface)
    stale_keys = [
        k for k, last_sent in _recent_syncstate_repairs.items()
        if now - float(last_sent) > ttl
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
    logging.info(f"SYNCSTATE-driven mismatch eval for {sender_node_id}: scopes={scopes}")

    _prune_recent_syncstate_repairs(interface)
    now = time.time()
    repair_sig = (str(sender_node_id), tuple(sorted(scopes)))
    last_sent = _recent_syncstate_repairs.get(repair_sig)
    if last_sent is not None and (now - float(last_sent)) < get_repair_cycle_seconds(interface):
        return

    _recent_syncstate_repairs[repair_sig] = now
    requested_scopes = [s for s in scopes if not _is_hashreq_pending(sender_node_id, s)]
    if not requested_scopes:
        logging.debug(f"SYNCSTATE mismatch from {sender_node_id} but all scopes already have in-flight HASHREQ; skipping")
        return
    # Only request HASHREQ for scopes where we have <= peer's count.  When we
    # have MORE records the peer should be the requester (it will see our higher
    # count in our SYNCSTATE and send HASHREQ to us).  Sending both directions
    # simultaneously causes bidirectional HASHZ storms on half-duplex LoRa.
    requested_scopes = get_scopes_to_request_repair(sender_node_id, requested_scopes)
    if not requested_scopes:
        # Local has more records in all mismatched scopes.  Send our SYNCSTATE
        # directly to this peer so it sees our counts and can send HASHREQ to
        # us.  Without this, mail/bulletins sit undelivered until the peer
        # happens to receive our next scheduled broadcast (which may be delayed
        # by the "state unchanged" skip-guard on lossy LoRa).
        logging.info(
            f"SYNCSTATE mismatch from {sender_node_id}: local has more records in all scopes; "
            f"poking peer with SYNCSTATE so it can request our manifest"
        )
        try:
            send_sync_state_to_bbs_nodes(get_local_record_counts(), [sender_node_id], interface)
        except Exception as exc:
            logging.warning(f"Failed to send poke SYNCSTATE to {sender_node_id}: {exc}")
        return
    # Deprioritize zork_saves: it carries the largest, most chunk-loss-prone
    # payloads and is the least important scope. If anything else is also out
    # of sync, repair those first and defer zork_saves to a later cycle so a
    # stuck multi-chunk save can't starve bulletins/mail/channels of airtime.
    # NOTE: tombstones is always appended alongside zork_saves, so we must
    # check for non-tombstone data scopes — otherwise zork is deferred forever.
    non_zork = [s for s in requested_scopes if s != 'zork_saves']
    data_non_zork = [s for s in non_zork if s != 'tombstones']
    if data_non_zork and len(non_zork) != len(requested_scopes):
        logging.info(
            f"Deferring zork_saves repair for {sender_node_id} until other scopes converge "
            f"(active: {', '.join(data_non_zork)})"
        )
        requested_scopes = non_zork
    logging.info(f"SYNCSTATE mismatch from {sender_node_id}; requesting targeted repair for scopes: {', '.join(requested_scopes)}")
    for scope in requested_scopes:
        send_hash_request_to_bbs_nodes([sender_node_id], interface, scope=scope)
        _mark_hashreq_pending(sender_node_id, scope)


def _queue_striped_reconcile(scope: str, sender_node_id: str, manifest: dict, interface) -> None:
    """Store an incoming manifest and (re)start the collection timer for this scope.

    When the timer fires, all manifests collected within the window are reconciled
    together with HASSMISS requests striped round-robin across peers.  If only one
    peer responded before the window closed, behaviour is identical to the original
    single-peer reconcile.
    """
    # Zero collection window → reconcile synchronously (no batching). Keeps the
    # single-peer path deterministic for tests and lets operators opt out of the
    # collect-and-stripe delay.
    if _STRIPE_COLLECT_SECONDS <= 0:
        _do_striped_reconcile(scope, {sender_node_id: manifest}, interface)
        return

    with _stripe_lock:
        if scope not in _pending_stripe_manifests:
            _pending_stripe_manifests[scope] = {}
        _pending_stripe_manifests[scope][sender_node_id] = manifest

        # Cancel any existing timer so the window resets on each new arrival.
        existing = _stripe_timers.pop(scope, None)
        if existing is not None:
            existing.cancel()

        # Capture interface in closure — avoid late-binding on a mutable var.
        _iface = interface

        def _fire():
            with _stripe_lock:
                manifests = _pending_stripe_manifests.pop(scope, {})
                _stripe_timers.pop(scope, None)
            if manifests:
                _do_striped_reconcile(scope, manifests, _iface)

        t = threading.Timer(_STRIPE_COLLECT_SECONDS, _fire)
        t.daemon = True
        t.start()
        _stripe_timers[scope] = t


def _do_striped_reconcile(scope: str, peer_manifests: dict, interface) -> None:
    """Reconcile missing records across multiple peers with striped HASSMISS.

    peer_manifests: {peer_id: {key: hash}}

    Pull strategy:
      - Build the union of keys missing locally across all peers
      - Sort deterministically, then assign round-robin: key[0]→peer[0],
        key[1]→peer[1], key[2]→peer[0], ...
      - Each peer only receives HASSMISS for its assigned slice

    Push strategy:
      - Local-only records are pushed to ALL peers (can't stripe pushes since
        every peer needs them)
    """
    local = get_record_hash_manifest(scope)
    local_keys = set(local.keys())
    max_per_pass = get_reconcile_max_per_pass(interface)
    if scope == 'zork_saves':
        max_per_pass = 1

    peers = sorted(peer_manifests.keys())  # deterministic ordering
    n_peers = len(peers)

    # Compute per-peer pull sets and the push set.
    all_need_from_remote: set = set()
    push_keys_set: set = set()
    for peer_id, remote in peer_manifests.items():
        remote_keys = set(remote.keys())
        need = (remote_keys - local_keys) | {k for k in (remote_keys & local_keys) if local.get(k) != remote.get(k)}
        all_need_from_remote |= need
        differing_shared = {k for k in (remote_keys & local_keys) if local.get(k) != remote.get(k)}
        push_keys_set |= (local_keys - remote_keys) | differing_shared

    total_pull = len(all_need_from_remote)
    logging.info(
        f"Striped reconcile scope={scope} peers={peers} "
        f"total_pull={total_pull} push={len(push_keys_set)}"
    )

    # Stripe pull keys round-robin across peers.
    sorted_pull_keys = sorted(all_need_from_remote)
    peer_pull_counts = {p: 0 for p in peers}
    pull_sent_total = 0

    for i, key in enumerate(sorted_pull_keys):
        if pull_sent_total >= max_per_pass * n_peers:
            logging.info(
                f"Striped reconcile pull cap reached ({max_per_pass * n_peers}) "
                f"for scope={scope}; {total_pull - pull_sent_total} key(s) deferred"
            )
            break

        # Assign to the peer whose manifest actually contains this key,
        # round-robining when multiple peers have it.
        eligible_peers = [p for p in peers if key in peer_manifests[p]]
        if not eligible_peers:
            continue
        # Pick the eligible peer with the fewest assignments so far.
        assigned_peer = min(eligible_peers, key=lambda p: peer_pull_counts[p])

        remote = peer_manifests[assigned_peer]
        local_hash = str(local.get(key, ""))
        remote_hash = str(remote.get(key, ""))
        if not _should_send_hashmiss(assigned_peer, scope, key, local_hash, remote_hash):
            continue
        # Peer-agnostic: skip if this record is already in-flight from any peer.
        if not _should_request_record(scope, key):
            continue

        _peer_scc = peers_all_support([assigned_peer], 'scc')
        _scope_wire = encode_scope(scope, _peer_scc)
        _tomb_wire = encode_scope('tombstones', _peer_scc)
        wire_key = key
        if scope == 'channels' and not str(key).startswith('comment:'):
            if len(f"HASHMISS|{_scope_wire}|{key}".encode('utf-8')) > get_max_text_bytes(interface):
                wire_key = compact_channel_manifest_key(key)
                logging.info(f"Channel key too long for HASHMISS frame; using compact key {wire_key}")

        # Tombstone lookup: channel_comments are stored under the channels scope
        # with a 'comment:' prefix — translate when checking.
        _tomb_scope = scope
        _tomb_key = key
        if scope == 'channel_comments':
            _tomb_scope = 'channels'
            _tomb_key = f"comment:{key}"

        if scope in ('bulletins', 'mail', 'zork_saves', 'channels', 'channel_comments') and key not in local and has_sync_tombstone(_tomb_scope, _tomb_key):
            logging.info(f"Requesting tombstone replay from {assigned_peer} for {scope}:{key}")
            _send_one_sync(f"HASHMISS|{_tomb_wire}|{_scope_wire}:{wire_key}", assigned_peer, interface,
                           pause_seconds=get_hash_repair_pause_seconds(interface))
            # Asking for a replay only stops US re-pulling it. The peer still
            # HAS the record and has never heard about the delete, so without
            # this both sides hold their ground forever: we suppress what it
            # offers, it keeps offering. Tell it directly.
            _push_delete_to_peer(_tomb_scope, _tomb_key, assigned_peer, interface)
        else:
            logging.info(f"Requesting record from {assigned_peer} scope={scope} key={wire_key} (striped {i % n_peers + 1}/{n_peers})")
            _send_one_sync(f"HASHMISS|{_scope_wire}|{wire_key}", assigned_peer, interface,
                           pause_seconds=get_hash_repair_pause_seconds(interface))

        peer_pull_counts[assigned_peer] += 1
        pull_sent_total += 1

    # Push local-only records to ALL peers.
    push_keys = sorted(push_keys_set)
    push_sent = 0
    for key in push_keys:
        if push_sent >= max_per_pass:
            logging.info(
                f"Striped reconcile push cap reached ({max_per_pass}) for scope={scope}; "
                f"{len(push_keys) - push_sent} key(s) deferred"
            )
            break
        for peer_id in peers:
            if key not in peer_manifests[peer_id]:
                logging.info(f"Pushing local-only record to {peer_id} scope={scope} key={key}")
                _send_requested_record(scope, key, peer_id, interface)
        push_sent += 1


def _reconcile_remote_manifest(scope: str, sender_node_id: str, interface) -> None:
    if scope == 'zork_saves' and not is_zork_save_sync_enabled():
        with _hash_buffer_lock:
            _peer_hash_manifest_buffers.pop((sender_node_id, scope), None)
        return
    with _hash_buffer_lock:
        remote = dict(_peer_hash_manifest_buffers.pop((sender_node_id, scope), {}))
    local = get_record_hash_manifest(scope)
    remote_keys = set(remote.keys())
    local_keys = set(local.keys())

    # Ask peer for keys we do not have, plus keys that exist on both sides but differ.
    need_from_remote = set(remote_keys - local_keys)
    need_from_remote.update(key for key in (remote_keys & local_keys) if local.get(key) != remote.get(key))
    # Push records the peer is missing entirely, AND records both sides have but
    # whose hashes differ.  When hashes differ for a shared key, the most common
    # cause is that one side received only some of a multi-frame record (base
    # frame landed, META or CONT was lost).  Re-pushing our copy lets the peer's
    # _merge_continuation_content extend its truncated row to our complete one.
    # The merge logic is idempotent — if the peer already has equal-or-longer
    # content the push is treated as a duplicate and ignored.  Without this,
    # records with mismatched hashes can never converge because the pull-only
    # path just round-trips the peer's truncated content back to us.
    differing_shared = {k for k in (remote_keys & local_keys) if local.get(k) != remote.get(k)}
    push_keys = sorted((local_keys - remote_keys) | differing_shared)
    logging.info(
        f"Reconciling manifest scope={scope} peer={sender_node_id} "
        f"remote_keys={len(remote_keys)} local_keys={len(local_keys)} "
        f"pull={len(need_from_remote)} push={len(push_keys)} "
        f"(of which {len(differing_shared)} shared-but-differ)"
    )

    # Cap HASHMISS requests per pass to avoid flooding the LoRa channel, which causes
    # the very packet loss that stalls convergence.  Deferred keys will be retried on
    # the next SYNCSTATE → HASHREQ → reconcile cycle.
    max_per_pass = get_reconcile_max_per_pass(interface)
    # zork_saves payloads are the largest and least important; cap them at 1
    # per pass so a stuck (or chunk-lossy) save can't blanket the channel and
    # block the other scopes from converging.
    if scope == 'zork_saves':
        max_per_pass = 1
    pull_sent = 0
    for key in sorted(need_from_remote):
        if pull_sent >= max_per_pass:
            logging.info(
                f"Reconcile pull cap reached ({max_per_pass}) for scope={scope} peer={sender_node_id}; "
                f"{len(need_from_remote) - pull_sent} key(s) deferred to next repair cycle"
            )
            break
        local_hash = str(local.get(key, ""))
        remote_hash = str(remote.get(key, ""))
        if not _should_send_hashmiss(sender_node_id, scope, key, local_hash, remote_hash):
            continue
        # Peer-agnostic: skip if this record is already in-flight from any peer.
        if not _should_request_record(scope, key):
            continue
        _peer_scc = peers_all_support([sender_node_id], 'scc')
        _scope_wire = encode_scope(scope, _peer_scc)
        _tomb_wire = encode_scope('tombstones', _peer_scc)
        # For channel non-comment keys, the full base64(name+url) manifest key
        # can exceed 200 chars for channels with long descriptions.  If the plain
        # HASHMISS frame would be over the packet cap, substitute the 9-char
        # compact key (~XXXXXXXX) so the request actually gets transmitted.
        # The sender will resolve the compact key back to the full channel record.
        wire_key = key
        if scope == 'channels' and not str(key).startswith('comment:'):
            if len(f"HASHMISS|{_scope_wire}|{key}".encode('utf-8')) > get_max_text_bytes(interface):
                wire_key = compact_channel_manifest_key(key)
                logging.info(f"Channel key too long for HASHMISS frame; using compact key {wire_key}")
        # Tombstone lookup: channel_comments stored under channels scope with 'comment:' prefix.
        _tomb_scope = 'channels' if scope == 'channel_comments' else scope
        _tomb_key = f"comment:{key}" if scope == 'channel_comments' else key
        if scope in ('bulletins', 'mail', 'zork_saves', 'channels', 'channel_comments') and key not in local and has_sync_tombstone(_tomb_scope, _tomb_key):
            logging.info(f"Requesting tombstone replay from {sender_node_id} for {scope}:{key}")
            _send_one_sync(f"HASHMISS|{_tomb_wire}|{_scope_wire}:{wire_key}", sender_node_id, interface, pause_seconds=get_hash_repair_pause_seconds(interface))
            _push_delete_to_peer(_tomb_scope, _tomb_key, sender_node_id, interface)
        else:
            logging.info(f"Requesting record from {sender_node_id} scope={scope} key={wire_key}")
            _send_one_sync(f"HASHMISS|{_scope_wire}|{wire_key}", sender_node_id, interface, pause_seconds=get_hash_repair_pause_seconds(interface))
        pull_sent += 1

    # Proactively push records the peer is missing to converge in one cycle.
    # Also capped per pass to avoid blocking the receive callback for too long.
    push_sent = 0
    for key in push_keys:
        if push_sent >= max_per_pass:
            logging.info(
                f"Reconcile push cap reached ({max_per_pass}) for scope={scope} peer={sender_node_id}; "
                f"{len(push_keys) - push_sent} key(s) deferred to next repair cycle"
            )
            break
        logging.info(f"Pushing local-only record to {sender_node_id} scope={scope} key={key}")
        _send_requested_record(scope, key, sender_node_id, interface)
        push_sent += 1


def _send_hash_manifest_to_peer(scope: str, destination_node_id: str, interface) -> None:
    manifest = get_record_hash_manifest(scope)
    logging.info(f"Sending hash manifest to {destination_node_id} scope={scope} count={len(manifest)} compressed={_hash_manifest_compression_enabled()}")
    # Manifest frames travel back-to-back; a chunk-pause floor (independent of
    # turbo) keeps trailing chunks from being dropped on the LoRa receive path.
    chunk_pause = max(get_hash_repair_pause_seconds(interface), get_hash_chunk_pause_seconds(interface))
    _scope_wire = encode_scope(scope, peers_all_support([destination_node_id], 'scc'))
    if not _hash_manifest_compression_enabled():
        for key, rec_hash in manifest.items():
            _send_one_sync(f"HASHREC|{_scope_wire}|{key}|{rec_hash}", destination_node_id, interface, pause_seconds=chunk_pause)
        _send_one_sync(f"HASHEND|{_scope_wire}|{len(manifest)}", destination_node_id, interface, pause_seconds=chunk_pause)
        return

    payload = json.dumps(manifest, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    compressed = zlib.compress(payload, level=6)
    b64 = base64.urlsafe_b64encode(compressed).decode("ascii")
    manifest_id = str(int(time.time() * 1000))
    prefix = f"HASHZ|{_scope_wire}|{manifest_id}|"
    max_chunk = get_max_text_bytes(interface) - len(prefix.encode("utf-8")) - len("999999|999999|".encode("utf-8"))
    if max_chunk <= 0:
        logging.warning("HASHZ prefix too large for packet limit; falling back to HASHREC")
        for key, rec_hash in manifest.items():
            _send_one_sync(f"HASHREC|{_scope_wire}|{key}|{rec_hash}", destination_node_id, interface, pause_seconds=chunk_pause)
        _send_one_sync(f"HASHEND|{_scope_wire}|{len(manifest)}", destination_node_id, interface, pause_seconds=chunk_pause)
        return

    chunks = [b64[i:i + max_chunk] for i in range(0, len(b64), max_chunk)] or [""]
    total = len(chunks)
    # Cache chunks so we can honor HASHZGAP gap-fill requests from this peer
    # without recomputing (and risking content drift if the local DB changes).
    _prune_old_hash_manifest_chunks()
    with _hash_buffer_lock:
        _outgoing_hash_manifest_cache[(destination_node_id, scope, manifest_id)] = {
            'chunks': list(chunks),
            'total': total,
            'created_at': time.time(),
        }
    for idx, chunk in enumerate(chunks):
        # Add random jitter to EVERY chunk's post-send pause so the gap before
        # the next chunk varies. Previously chunk 0's pause was un-jittered,
        # which made chunk 1 arrive at a deterministic offset from chunk 0 and
        # collide with the half-duplex ACK window -- chunk index 1 was then
        # consistently lost on every manifest. Apply jitter on chunk 0 as well
        # (the original intent of "don't jitter the very first send" was
        # misplaced -- the issue is the gap, not the send).
        jitter = random.uniform(0, chunk_pause)
        _send_one_sync(f"{prefix}{idx}|{total}|{chunk}", destination_node_id, interface,
                       pause_seconds=chunk_pause + jitter)


def _send_requested_record(scope: str, key: str, destination_node_id: str, interface) -> None:
    if scope == 'bulletins':
        row = get_bulletin_by_unique_id(key)
        if row:
            # row: (board, sender_short_name, date, subject, content, unique_id, source_node_id, source_timestamp)
            logging.info(f"Sending requested bulletin to {destination_node_id} key={key}")
            send_bulletin_to_bbs_nodes(row[0], row[1], row[3], row[4], row[5], [destination_node_id], interface, date=row[2],
                                       source_node_id=row[6] if len(row) > 6 else None,
                                       source_timestamp=row[7] if len(row) > 7 else None)
        else:
            logging.warning(f"Requested bulletin missing locally for resend key={key}")
    elif scope == 'mail':
        row = get_mail_by_unique_id(key)
        if row:
            # row: (sender, sender_short_name, recipient, date, subject, content, unique_id, source_node_id, source_timestamp)
            logging.info(f"Sending requested mail to {destination_node_id} key={key}")
            send_mail_to_bbs_nodes(row[0], row[1], row[2], row[4], row[5], row[6], [destination_node_id], interface, date=row[3],
                                   source_node_id=row[7] if len(row) > 7 else None,
                                   source_timestamp=row[8] if len(row) > 8 else None)
        else:
            logging.warning(f"Requested mail missing locally for resend key={key}")
    elif scope == 'channels':
        if str(key).startswith('comment:'):
            # Legacy: old peers still use 'comment:{uuid}' keys in the channels scope.
            unique_id = str(key).split(':', 1)[1]
            row = get_channel_comment_by_unique_id(unique_id)
            if row:
                logging.info(f"Sending requested channel comment to {destination_node_id} key={key}")
                send_channel_comment_to_bbs_nodes(
                    make_channel_manifest_key(row[0], row[1]),
                    row[2], row[3], row[4], row[5], [destination_node_id], interface,
                    source_node_id=row[8] if len(row) > 8 else None,
                    source_timestamp=row[9] if len(row) > 9 else None,
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
    elif scope == 'channel_comments':
        # New sub-scope: keys are plain UUIDs (no 'comment:' prefix).
        row = get_channel_comment_by_unique_id(str(key))
        if row:
            logging.info(f"Sending requested channel comment to {destination_node_id} key={key}")
            send_channel_comment_to_bbs_nodes(
                make_channel_manifest_key(row[0], row[1]),
                row[2], row[3], row[4], row[5], [destination_node_id], interface,
                source_node_id=row[8] if len(row) > 8 else None,
                source_timestamp=row[9] if len(row) > 9 else None,
            )
        else:
            logging.warning(f"Requested channel comment missing locally for resend key={key}")
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
            # Multi-chunk ZORKSAVE pushes need the same inter-frame floor as
            # HASHZ to keep LoRa from dropping trailing chunks under turbo.
            try:
                send_zork_save_to_bbs_nodes(
                    row[0], row[1], row[2], row[3], [destination_node_id], interface,
                    pause_seconds=max(get_hash_repair_pause_seconds(interface), get_hash_chunk_pause_seconds(interface)),
                )
            except Exception:
                import traceback
                logging.error(
                    f"Exception while sending zork save key={key} to {destination_node_id}:\n{traceback.format_exc()}"
                )
        else:
            logging.warning(f"Requested zork save missing locally for resend key={key}")
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
        elif key.startswith('channels:'):
            # Channel ENTRY (the 'comment:' case is handled above). This branch
            # was missing, so a request to replay a channel delete was answered
            # with silence.
            manifest_key = key.split(':', 1)[1]
            logging.info(f"Replaying channel delete to {destination_node_id} key={key}")
            send_delete_channel_to_bbs_nodes(manifest_key, [destination_node_id], interface)
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


def _looks_like_source_node_id(value: str) -> bool:
    """True if this trailing wire field is a node id, not record content.

    The optional source_node_id was matched on a leading '!' -- a Meshtastic
    radio id. An MQTT node is mqtt:<topic>:<name> and a MeshCore node is a
    bare hex key, so a record originating on either was never stripped: the
    parse shifted by one field and the SOURCE NODE ID was written into
    unique_id. Two nodes then disagreed about that record's manifest key
    forever, which is a drift no amount of repair can close.

    MeshCore's bare-hex ids are deliberately NOT matched here: they have no
    distinguishing prefix, so accepting them would mean treating ordinary
    content as a node id. Those still fall through, exactly as before.
    """
    text = str(value or "")
    return text.startswith('!') or text.startswith('mqtt:')


def _push_delete_to_peer(tomb_scope: str, tomb_key: str, peer_id: str, interface) -> None:
    """Tell one peer about a delete we hold a tombstone for.

    Reconciliation had suppression but no propagation: when we had deleted a
    record and the peer had not, we stopped ourselves re-pulling it and left
    it at that. The peer kept offering it and we kept refusing, forever --
    two nodes each holding their ground with no way to converge.

    Only scopes with a delete frame can be pushed. Anything else is left
    alone rather than half-handled.
    """
    try:
        if tomb_scope == 'bulletins':
            send_delete_bulletin_to_bbs_nodes(tomb_key, [peer_id], interface)
        elif tomb_scope == 'mail':
            send_delete_mail_to_bbs_nodes(tomb_key, [peer_id], interface)
        elif tomb_scope == 'channels' and str(tomb_key).startswith('comment:'):
            send_delete_channel_comment_to_bbs_nodes(str(tomb_key)[len('comment:'):], [peer_id], interface)
        elif tomb_scope == 'channels':
            send_delete_channel_to_bbs_nodes(tomb_key, [peer_id], interface)
        elif tomb_scope == 'zork_saves':
            remainder = str(tomb_key)
            if ':' not in remainder:
                return
            user_id, game_id = remainder.split(':', 1)
            deleted_at = get_sync_tombstone_deleted_at('zork_saves', remainder)
            if not deleted_at:
                return
            send_delete_zork_save_to_bbs_nodes(user_id, game_id, deleted_at, [peer_id], interface)
        else:
            return
        logging.info(f"Pushed delete for {tomb_scope}:{tomb_key} to {peer_id}")
    except Exception:
        logging.debug("could not push delete to peer", exc_info=True)


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
            # Wire format: BULLETIN|board|sender|subject|content|unique_id[|date[|source_node_id|source_timestamp]]
            body = message[len("BULLETIN|"):]
            source_timestamp = None
            source_node_id = None
            # Strip optional ISO source_timestamp from far right
            _tmp = body.rsplit("|", 1)
            if len(_tmp) == 2 and _SYNC_ISO_TIMESTAMP_PATTERN.match(_tmp[1]):
                source_timestamp = decode_ts_second(_tmp[1])
                body = _tmp[0]
                # Strip optional source_node_id (starts with '!')
                _tmp2 = body.rsplit("|", 1)
                if len(_tmp2) == 2 and _looks_like_source_node_id(_tmp2[1]):
                    source_node_id = _tmp2[1]
                    body = _tmp2[0]
            # Try to strip optional date from the far right.
            tail = body.rsplit("|", 2)
            if len(tail) == 3 and _SYNC_DATE_PATTERN.match(tail[2]):
                original_date, unique_id = decode_ts_minute(tail[2]), tail[1]
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
            add_bulletin(board, sender_short_name, subject, content, [], interface, unique_id=unique_id, date=original_date, source_node_id=source_node_id, source_timestamp=source_timestamp)
            flush_pending_bulletin_continuations(unique_id)

            if board.lower() == "urgent":
                notification_message = f"💥NEW URGENT BULLETIN💥\nFrom: {sender_short_name}\nTitle: {subject}\nDM 'CB,,Urgent' to view"
                send_message(notification_message, BROADCAST_NUM, interface)
        elif message.startswith("MAIL|"):
            # Wire format: MAIL|sender_id|sender_short|recipient_id|subject|content|unique_id[|date[|source_node_id|source_timestamp]]
            body = message[len("MAIL|"):]
            source_timestamp = None
            source_node_id = None
            # Strip optional ISO source_timestamp from far right
            _tmp = body.rsplit("|", 1)
            if len(_tmp) == 2 and _SYNC_ISO_TIMESTAMP_PATTERN.match(_tmp[1]):
                source_timestamp = decode_ts_second(_tmp[1])
                body = _tmp[0]
                _tmp2 = body.rsplit("|", 1)
                if len(_tmp2) == 2 and _looks_like_source_node_id(_tmp2[1]):
                    source_node_id = _tmp2[1]
                    body = _tmp2[0]
            tail = body.rsplit("|", 2)
            if len(tail) == 3 and _SYNC_DATE_PATTERN.match(tail[2]):
                original_date, unique_id = decode_ts_minute(tail[2]), tail[1]
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
            add_mail(sync_sender_id, sender_short_name, recipient_id, subject, content, [], interface, unique_id=unique_id, date=original_date, source_node_id=source_node_id, source_timestamp=source_timestamp)
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
        elif message.startswith("DELETE_CHANNEL|"):
            parts = message.split("|", 1)
            if len(parts) != 2 or not parts[1]:
                logging.warning(f"Malformed DELETE_CHANNEL sync message ignored: {message}")
                return
            decoded = decode_channel_manifest_key(parts[1])
            if not decoded:
                logging.warning(f"Undecodable DELETE_CHANNEL key ignored: {parts[1]}")
                return
            # No bbs_nodes/interface: applying a peer's delete must not
            # re-broadcast it, or two nodes bounce the same delete forever.
            delete_channel(decoded[0], decoded[1], [], None)
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
                user_id = decode_text(parts[1])
                game_id = decode_text(parts[2])
            except Exception:
                logging.warning(f"Malformed DELETE_ZORKSAVE payload ignored: {message}")
                return
            apply_synced_zork_save_delete(user_id, game_id, decode_ts_second(parts[3]))
        elif message.startswith("CHANNEL|"):
            parts = message.split("|", 2)
            if len(parts) != 3:
                logging.warning(f"Malformed CHANNEL sync message ignored: {message}")
                return
            channel_name, channel_url = parts[1], parts[2]
            # A peer that has not seen our delete keeps offering the channel.
            # Without this the record comes straight back and deleting it
            # again achieves nothing -- the reported repropagation loop.
            # Hash repair already checks this; the direct frame did not.
            if has_sync_tombstone('channels', make_channel_manifest_key(channel_name, channel_url)):
                logging.info(
                    f"Ignoring CHANNEL sync for deleted channel {channel_name!r} (tombstoned locally)")
                return
            add_channel(channel_name, channel_url)
        elif message.startswith("CHANNELCOMMENT|"):
            # Wire format: CHANNELCOMMENT|{manifest_key}|{b64_sender}|{date}|{content}|{unique_id}[|source_node_id|source_timestamp]
            # Use rsplit from the right so content with embedded '|' is handled correctly.
            body = message[len("CHANNELCOMMENT|"):]
            source_timestamp = None
            source_node_id = None
            # Strip optional ISO source_timestamp from far right
            _tmp = body.rsplit("|", 1)
            if len(_tmp) == 2 and _SYNC_ISO_TIMESTAMP_PATTERN.match(_tmp[1]):
                source_timestamp = decode_ts_second(_tmp[1])
                body = _tmp[0]
                _tmp2 = body.rsplit("|", 1)
                if len(_tmp2) == 2 and _looks_like_source_node_id(_tmp2[1]):
                    source_node_id = _tmp2[1]
                    body = _tmp2[0]
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
            comment_date = decode_ts_minute(comment_date)
            try:
                sender_short_name = decode_text(b64_sender_raw)
            except Exception:
                logging.warning(f"Malformed CHANNELCOMMENT sender ignored: {message}")
                return
            add_channel_comment_by_manifest_key(channel_key, sender_short_name, comment_date, content, unique_id, source_node_id=source_node_id, source_timestamp=source_timestamp)
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
                append_bulletin_content(decode_uid(parts[1]), offset, parts[3])
            else:
                # Legacy format without offset — blind append
                append_bulletin_content(decode_uid(parts[1]), None, parts[2])
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
            apply_bulletin_expected_content_length(decode_uid(parts[1]), expected_length)
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
                append_mail_content(decode_uid(parts[1]), offset, parts[3])
            else:
                # Legacy format without offset — blind append
                append_mail_content(decode_uid(parts[1]), None, parts[2])
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
            apply_mail_expected_content_length(decode_uid(parts[1]), expected_length)
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
                append_channel_comment_content(decode_uid(parts[1]), offset, parts[3])
            else:
                append_channel_comment_content(decode_uid(parts[1]), None, parts[2])
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
            apply_channel_comment_expected_content_length(decode_uid(parts[1]), expected_length)
        elif message.startswith("SYNCSTATE|"):
            parts = message.split("|")
            # 5,7,13,14 = legacy variants; 15 = v2+ (trailing vN:caps token).
            # Accept any len >=15 too so future fields tacked on don't break us.
            if len(parts) not in (5, 7, 13, 14) and len(parts) < 15:
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
            tombstones_peer = -1
            if len(parts) >= 14:
                try:
                    tombstones_peer = int(parts[13])
                except ValueError:
                    pass
            peer_proto_v = 0
            peer_caps_csv = ''
            if len(parts) >= 15:
                from utils import parse_capabilities_token
                peer_proto_v, peer_caps_csv = parse_capabilities_token(parts[14])
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
                    tombstones_peer,
                    proto_v=peer_proto_v,
                    caps=peer_caps_csv,
                )
                logging.info(
                    f"SYNCSTATE recv from {sender_node_id}: "
                    f"b={bulletins} m={mail} c={channels} z={zork_saves} p={profiles} g={game_scores} t={tombstones_peer} | "
                    f"bH={bulletins_hash} mH={mail_hash} cH={channels_hash} zH={zork_saves_hash} pH={profiles_hash} gH={game_scores_hash} | "
                    f"v={peer_proto_v} caps=[{peer_caps_csv}]"
                )
                _retry_stale_hash_manifest_buffers(interface)
                _retry_stale_zork_save_buffers(interface)
                _request_targeted_repair_if_needed(sender_node_id, interface)
            else:
                logging.warning("SYNCSTATE ignored due to missing sender_node_id")
        elif message.startswith("HASHREQ|"):
            if not sender_node_id:
                logging.warning("HASHREQ ignored due to missing sender_node_id")
                return
            requested = message.split("|", 1)[1].strip().lower() if "|" in message else "all"
            requested = decode_scope(requested) if requested != 'all' else 'all'
            scopes = _SUPPORTED_HASH_SCOPES if requested == 'all' else [requested]
            # When channels is requested, automatically include the channel_comments
            # sub-scope so both halves are reconciled in the same exchange.
            if 'channels' in scopes and 'channel_comments' not in scopes:
                idx = scopes.index('channels')
                scopes = list(scopes)
                scopes.insert(idx + 1, 'channel_comments')
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
            scope = decode_scope(scope)
            if scope not in _SUPPORTED_HASH_SCOPES:
                return
            buf_key = (sender_node_id, scope)
            with _hash_buffer_lock:
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
            scope = decode_scope(scope)
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
            _retry_stale_hash_manifest_buffers(interface)
            buf_key = (sender_node_id, scope, manifest_id)
            with _hash_buffer_lock:
                buf = _peer_hash_compressed_buffers.get(buf_key)
                if buf is None or int(buf.get('total', -1)) != total_chunks:
                    # Before creating a new buffer, check whether we already have
                    # a majority-complete buffer for this (peer, scope) with a
                    # different manifest_id.  If so, discard the new manifest's
                    # chunk to avoid fragmenting progress across competing streams.
                    skip_new = False
                    for existing_key, existing_buf in _peer_hash_compressed_buffers.items():
                        if existing_key[0] == sender_node_id and existing_key[1] == scope and existing_key[2] != manifest_id:
                            ex_received = len(existing_buf.get('chunks', {}))
                            ex_total = int(existing_buf.get('total', 0))
                            if ex_total > 0 and ex_received >= (ex_total + 1) // 2:
                                skip_new = True
                                break
                    if skip_new:
                        return
                    buf = {
                        'total': total_chunks,
                        'chunks': {},
                        'created_at': time.time(),
                        'updated_at': time.time(),
                    }
                    _peer_hash_compressed_buffers[buf_key] = buf
                buf['updated_at'] = time.time()
                if chunk_idx not in buf['chunks']:
                    buf['chunks'][chunk_idx] = parts[5]
                complete = len(buf['chunks']) == total_chunks
                if complete:
                    payload_b64 = ''.join(buf['chunks'][i] for i in range(total_chunks))
                else:
                    payload_b64 = ''

            if complete:
                try:
                    payload_bytes = base64.urlsafe_b64decode(payload_b64.encode('ascii'))
                    manifest_obj = json.loads(zlib.decompress(payload_bytes).decode('utf-8'))
                    if isinstance(manifest_obj, dict):
                        normalized = {str(k): str(v) for k, v in manifest_obj.items()}
                        with _hash_buffer_lock:
                            _peer_hash_manifest_buffers[(sender_node_id, scope)] = normalized
                        _clear_hashreq_pending(sender_node_id, scope)
                        _queue_striped_reconcile(scope, sender_node_id, normalized, interface)
                except Exception:
                    logging.warning(f"Malformed HASHZ payload ignored: {message}")
                    _clear_hashreq_pending(sender_node_id, scope)
                finally:
                    with _hash_buffer_lock:
                        _peer_hash_compressed_buffers.pop(buf_key, None)
        elif message.startswith("HASHZGAP|"):
            # Gap-fill request from a peer who received a partial HASHZ manifest.
            # Wire format: HASHZGAP|scope|manifest_id|csv_of_missing_indices
            if not sender_node_id:
                return
            parts = message.split("|", 3)
            if len(parts) != 4:
                logging.warning(f"Malformed HASHZGAP ignored: {message}")
                return
            scope, manifest_id, csv = parts[1], parts[2], parts[3]
            scope = decode_scope(scope)
            if scope not in _SUPPORTED_HASH_SCOPES:
                return
            cache_key = (sender_node_id, scope, manifest_id)
            with _hash_buffer_lock:
                entry = _outgoing_hash_manifest_cache.get(cache_key)
                cached_chunks = list(entry['chunks']) if entry else []
                total = int(entry['total']) if entry else 0
            try:
                missing = unpack_missing(csv, total)
            except Exception:
                logging.warning(f"Malformed HASHZGAP payload ignored: {message}")
                return
            if not entry:
                logging.info(
                    f"HASHZGAP from {sender_node_id} for unknown manifest scope={scope} "
                    f"manifest_id={manifest_id} (cache miss); peer will fall back to HASHREQ"
                )
                return
            _scope_wire = encode_scope(scope, peers_all_support([sender_node_id], 'scc'))
            prefix = f"HASHZ|{_scope_wire}|{manifest_id}|"
            chunk_pause = max(get_hash_repair_pause_seconds(interface), get_hash_chunk_pause_seconds(interface))
            logging.info(
                f"Honoring HASHZGAP from {sender_node_id} scope={scope} manifest_id={manifest_id} "
                f"missing={missing} total={total}"
            )
            for idx in missing:
                if 0 <= idx < total:
                    try:
                        jitter = random.uniform(0, chunk_pause)
                        _send_one_sync(
                            f"{prefix}{idx}|{total}|{cached_chunks[idx]}",
                            sender_node_id,
                            interface,
                            pause_seconds=chunk_pause + jitter,
                        )
                    except Exception as exc:
                        logging.warning(f"Failed to resend HASHZ chunk {idx} to {sender_node_id}: {exc}")
        elif message.startswith("HASHEND|"):
            if not sender_node_id:
                return
            parts = message.split("|", 2)
            if len(parts) != 3:
                logging.warning(f"Malformed HASHEND ignored: {message}")
                return
            scope = parts[1]
            scope = decode_scope(scope)
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
            scope = decode_scope(scope)
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
            scope = decode_scope(scope)
            if scope != 'zork_saves':
                return
            try:
                user_id = decode_text(parts[3])
                game_id = decode_text(parts[4])
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
            scope = decode_scope(scope)
            if scope != 'zork_saves':
                return
            try:
                user_id = decode_text(parts[3])
                game_id = decode_text(parts[4])
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
                short_name = decode_text(parts[2])
                long_name = decode_text(parts[3])
                messages_sent = int(parts[6])
                bio = decode_text(parts[7])
            except Exception:
                logging.warning(f"Malformed PROFILESYNC payload ignored: {message}")
                return
            upsert_synced_user_profile(parts[1], short_name, long_name,
                                       decode_ts_second(parts[4]), decode_ts_second(parts[5]),
                                       messages_sent, bio)
        elif message.startswith("SCORESYNC|"):
            parts = message.split("|", 7)
            if len(parts) != 8:
                logging.warning(f"Malformed SCORESYNC ignored: {message}")
                return
            try:
                short_name = decode_text(parts[3])
                score = int(parts[4])
                max_score = int(parts[5])
                moves = int(parts[6])
            except Exception:
                logging.warning(f"Malformed SCORESYNC payload ignored: {message}")
                return
            upsert_synced_game_score(parts[1], parts[2], short_name, score, max_score, moves,
                                     decode_ts_second(parts[7]))
        elif message.startswith("ZORKGAP|"):
            # Gap-fill request from a peer who received a partial ZORKSAVE.
            # Wire format: ZORKGAP|save_id|user_b64|game_b64|csv_of_missing_indices
            if not is_zork_save_sync_enabled():
                return
            parts = message.split("|", 4)
            if len(parts) != 5:
                logging.warning(f"Malformed ZORKGAP ignored: {message}")
                return
            save_id, user_b64, game_b64, csv = parts[1], parts[2], parts[3], parts[4]
            try:
                user_id = decode_text(user_b64)
                game_id = decode_text(game_b64)
                missing = unpack_missing(csv, 0)
            except Exception:
                logging.warning(f"Malformed ZORKGAP payload ignored: {message}")
                return
            row = get_zork_save_row_by_user_and_game(user_id, game_id)
            if not row:
                logging.warning(
                    f"ZORKGAP for unknown save user={user_id} game={game_id} from {sender_node_id}; "
                    f"falling back to full retransmit not possible (no row)"
                )
                return
            logging.info(
                f"Honoring ZORKGAP from {sender_node_id} save_id={save_id} user={user_id} "
                f"game={game_id} missing={missing}"
            )
            try:
                send_zork_save_to_bbs_nodes(
                    row[0], row[1], row[2], row[3], [sender_node_id], interface,
                    pause_seconds=max(get_hash_repair_pause_seconds(interface), get_hash_chunk_pause_seconds(interface)),
                    only_indices=missing,
                )
            except Exception:
                import traceback
                logging.error(
                    f"Exception honoring ZORKGAP save_id={save_id} to {sender_node_id}:\n{traceback.format_exc()}"
                )
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
            save_id, user_b64, game_b64, updated_at = parts[1], parts[2], parts[3], decode_ts_second(parts[4])
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
            _retry_stale_zork_save_buffers(interface)
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
                    'gap_attempts': 0,
                    'last_retry_received': -1,
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
                    'gap_attempts': 0,
                    'last_retry_received': -1,
                }
                _zork_save_chunk_buffers[key] = buf

            buf['updated_at'] = time.time()
            if chunk_idx not in buf['chunks']:
                buf['chunks'][chunk_idx] = parts[8] if len(parts) == 9 else parts[7]
            logging.info(
                f"ZORKSAVE recv chunk save_id={save_id} idx={chunk_idx}/{total_chunks} "
                f"have={len(buf['chunks'])}/{buf['total']} from={sender_key} "
                f"payload_hash={payload_hash or '(legacy)'}"
            )

            if len(buf['chunks']) == buf['total']:
                try:
                    ordered = ''.join(buf['chunks'][i] for i in range(buf['total']))
                    user_id = decode_text(buf['user_b64'])
                    game_id = decode_text(buf['game_b64'])
                    save_data = base64.b64decode(ordered.encode('ascii'))
                    expected_hash = str(buf.get('payload_hash', '') or '')
                    if expected_hash:
                        actual_hash = base64.urlsafe_b64encode(
                            hashlib.blake2b(save_data, digest_size=8).digest()
                        ).decode('ascii').rstrip('=')
                        if actual_hash != expected_hash:
                            logging.warning(
                                f"ZORKSAVE hash mismatch for save_id {save_id} user={user_id} game={game_id} "
                                f"expected={expected_hash} actual={actual_hash} "
                                f"bytes={len(save_data)} b64_len={len(ordered)} chunks={buf['total']} from={sender_key}"
                            )
                            _zork_save_chunk_buffers.pop(key, None)
                            return
                    logging.info(
                        f"ZORKSAVE assembled save_id={save_id} user={user_id} game={game_id} "
                        f"bytes={len(save_data)} chunks={buf['total']} from={sender_key}"
                    )
                    upsert_synced_zork_save(user_id, game_id, save_data, buf['updated_at_str'])
                except Exception:
                    logging.warning(f"Malformed ZORKSAVE payload ignored: {message}")
                finally:
                    _zork_save_chunk_buffers.pop(key, None)
        elif message.startswith("APIREQ|"):
            # Gateway side: a peer asks us to make an outbound call. Validate +
            # dispatch off-thread; reply via APIRESP back to the requester.
            import gateway
            if not gateway.is_gateway_enabled():
                return
            parts = message.split("|", 4)
            if len(parts) != 5 or not parts[1]:
                logging.warning(f"Malformed APIREQ ignored: {message}")
                return
            rid, requester_id, kind, payload = parts[1], parts[2], parts[3], parts[4]
            logging.info(f"APIREQ from {sender_node_id}: rid={rid} kind={kind}")
            _allowed = getattr(interface, 'allowed_nodes', None)
            _dest = sender_node_id

            def _reply(status, body, _rid=rid, _dest=_dest):
                # Persist for store-and-forward retrieval (Phase 2) so a node that
                # was asleep when we answered can still fetch it via APIPOLL, then
                # send immediately (best-effort) for nodes that are listening.
                try:
                    from db_operations import enqueue_api_response
                    enqueue_api_response(_rid, _dest, status, body)
                except Exception:
                    pass
                send_api_response(_rid, status, body, _dest, interface)

            gateway.handle_apireq(
                rid, requester_id, kind, payload, _allowed, reply_fn=_reply,
            )
        elif message.startswith("APIPOLL|"):
            # Gateway side (Phase 2): an intermittently-connected node asks for any
            # responses we queued for it while it was offline. Flush undelivered.
            parts = message.split("|", 1)
            poll_node = parts[1] if len(parts) == 2 and parts[1] else sender_node_id
            try:
                from db_operations import (fetch_undelivered_api_responses,
                                           mark_api_responses_delivered)
                pending = fetch_undelivered_api_responses(poll_node)
                for entry in pending:
                    send_api_response(entry['rid'], entry['status'], entry['body'],
                                      poll_node, interface)
                if pending:
                    mark_api_responses_delivered([e['id'] for e in pending])
                    logging.info(f"APIPOLL flushed {len(pending)} response(s) to {poll_node}")
            except Exception as exc:
                logging.warning(f"APIPOLL handling failed: {exc}")
        elif message.startswith("APIRESP|"):
            # Requester side: first (or only) frame of a response. Header carries
            # the total length so single-packet responses complete immediately.
            parts = message.split("|", 4)
            if len(parts) < 4 or not parts[1]:
                logging.warning(f"Malformed APIRESP ignored: {message}")
                return
            rid, status = parts[1], parts[2]
            try:
                expected = int(parts[3])
            except ValueError:
                logging.warning(f"Malformed APIRESP length ignored: {message}")
                return
            first_chunk = parts[4] if len(parts) == 5 else ""
            done = _apigw_apply_chunk(rid, 0, first_chunk, status=status,
                                      expected=expected, source=sender_node_id)
            if done is not None:
                _deliver_api_response(rid, done[0], done[1], interface)
        elif message.startswith("APIRESPMETA|"):
            parts = message.split("|", 2)
            if len(parts) != 3 or not parts[1]:
                return
            try:
                expected = int(parts[2])
            except ValueError:
                return
            # META only confirms expected length (offset 0 already holds the
            # header chunk); check for completion.
            done = None
            with _apigw_buffers_lock:
                buf = _apigw_response_buffers.get(parts[1])
                if buf is not None:
                    buf['expected'] = expected
                    if sum(len(c) for c in buf['parts'].values()) >= expected:
                        done = (buf['status'], ''.join(buf['parts'][o] for o in sorted(buf['parts'])))
                        _apigw_response_buffers.pop(parts[1], None)
            if done is not None:
                _deliver_api_response(parts[1], done[0], done[1], interface)
        elif message.startswith("APIRESPCONT|"):
            parts = message.split("|", 3)
            if len(parts) != 4 or not parts[1]:
                return
            try:
                offset = int(parts[2])
            except ValueError:
                return
            done = _apigw_apply_chunk(parts[1], offset, parts[3], source=sender_node_id)
            if done is not None:
                _deliver_api_response(parts[1], done[0], done[1], interface)
        elif message.startswith("APIRESPGAP|"):
            # Gateway side: a requester is missing part of a response it asked
            # for. Re-send the requested byte ranges from our retained copy.
            parts = message.split("|", 2)
            if len(parts) < 2 or not parts[1]:
                return
            rid = parts[1]
            spec = parts[2] if len(parts) == 3 else "*"
            from utils import resend_api_response_ranges
            if resend_api_response_ranges(rid, spec, interface):
                logging.info(f"APIRESPGAP refill for rid={rid} ranges={spec} to {sender_node_id}")
            else:
                logging.info(f"APIRESPGAP for unknown/expired rid={rid} from {sender_node_id}")
        elif message.startswith("PEERGOSSIP|"):
            # 'pgos': a neighbour relays what it last heard about ANOTHER peer's
            # sync state, so we become aware of (and can detect drift against)
            # peers we can't hear directly — including zork_saves.
            # Wire: PEERGOSSIP|<peer_id>|b|m|c|z|p|g|t|<age_secs>
            parts = message.split("|")
            if len(parts) != 10 or not parts[1]:
                logging.warning(f"Malformed PEERGOSSIP ignored: {message}")
                return
            relayed_peer = parts[1]
            try:
                from db_operations import get_local_node_id, merge_relayed_peer_state
                # Never let a relay overwrite our own authoritative state.
                #
                # A node has a DIFFERENT identity on every link -- a radio id
                # from get_local_node_id(), and mqtt:<topic>:<name> per MQTT
                # bridge. Checking only the radio id meant a peer gossiping
                # about us over MQTT was not recognised as us, so the node
                # recorded peer state for ITSELF and then listed itself as a
                # sync peer. Also compare the identity we hold on the link
                # this arrived on.
                # Check EVERY identity this node answers to, not just the
                # link this arrived on. Gossip about our baconbbs identity
                # reaches us over baconbbsvt, so a per-link check still let
                # us record sync state for ourselves.
                # Check EVERY identity this node answers to, not just the
                # link this arrived on: gossip about our baconbbs identity
                # reaches us over baconbbsvt, so a per-link check still let
                # us record sync state for ourselves -- and a node that
                # tracks itself sees a peer permanently behind and repairs
                # against it forever, which can never converge.
                from db_operations import get_local_link_identities
                _self_ids = get_local_link_identities()
                _self_ids.add(str(getattr(interface, 'self_node_id', '') or ''))
                if relayed_peer in _self_ids:
                    return
                counts = {
                    'bulletins': int(parts[2]), 'mail': int(parts[3]),
                    'channels': int(parts[4]), 'zork_saves': int(parts[5]),
                    'profiles': int(parts[6]), 'game_scores': int(parts[7]),
                    'tombstones': int(parts[8]),
                }
                age = int(parts[9])
            except (ValueError, TypeError):
                logging.warning(f"Invalid PEERGOSSIP values ignored: {message}")
                return
            if merge_relayed_peer_state(relayed_peer, counts, age):
                logging.info(
                    f"PEERGOSSIP from {sender_node_id}: learned fresher state for {relayed_peer} "
                    f"(z={counts['zork_saves']} b={counts['bulletins']} m={counts['mail']} c={counts['channels']}, age={age}s)"
                )
        elif message.startswith("HAVE|"):
            # Phase-2 op-log discovery: peer advertises its event heads per scope.
            if not sender_node_id:
                logging.warning("HAVE ignored: missing sender_node_id")
                return
            try:
                import op_sync
                from db_operations import get_local_node_id
                op_sync.handle_have(
                    message.split("|"),
                    sender_node_id=sender_node_id,
                    local_node_id=get_local_node_id() or '',
                    interface=interface,
                )
            except Exception as exc:
                logging.warning(f"HAVE handler failed: {exc}")
        elif message.startswith("WANT|"):
            # Phase-2 op-log discovery: peer requests EVENT frames for a scope/origin range.
            if not sender_node_id:
                logging.warning("WANT ignored: missing sender_node_id")
                return
            try:
                import op_sync
                from db_operations import get_local_node_id
                op_sync.handle_want(
                    message.split("|"),
                    sender_node_id=sender_node_id,
                    local_node_id=get_local_node_id() or '',
                    interface=interface,
                )
            except Exception as exc:
                logging.warning(f"WANT handler failed: {exc}")
        elif message.startswith("EVENT|"):
            # Phase-2 op-log discovery: peer delivers an op_log event; fetch record if missing.
            if not sender_node_id:
                logging.warning("EVENT ignored: missing sender_node_id")
                return
            try:
                import op_sync
                op_sync.handle_event(
                    message.split("|"),
                    sender_node_id=sender_node_id,
                    interface=interface,
                )
            except Exception as exc:
                logging.warning(f"EVENT handler failed: {exc}")
    else:
        if message_lower.startswith("sm,,"):
            handle_send_mail_command(sender_id, message_strip, interface, bbs_nodes)
        elif message_lower == "au":
            handle_active_users_command(sender_id, interface)
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
                    message_lower = _BBS_NUMBER_ALIAS.get(message_lower, message_lower)
                elif menu_name == 'utilities':
                    handlers = utilities_menu_handlers
                    message_lower = _UTILITIES_NUMBER_ALIAS.get(message_lower, message_lower)
                else:
                    handlers = main_menu_handlers
                    message_lower = _MAIN_NUMBER_ALIAS.get(message_lower, message_lower)
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
                elif command == 'ACCOUNT':
                    handle_account_steps(sender_id, message, interface, sender_node_id)
                elif command == 'SETTINGS':
                    handle_settings_steps(sender_id, message, interface, sender_node_id)
                elif command == 'APIGW':
                    handle_apigw_steps(sender_id, message, interface)
                elif command == 'ASK_NOMAD':
                    handle_ask_nomad_steps(sender_id, message, interface)
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

            sender_short_name = resolve_display_name(sender_node_id, interface)
            receiver_short_name = get_node_short_name(get_node_id_from_num(to_id, interface),
                                                      interface) if to_id else "Group Chat"
            logging.info(f"Received message from user '{sender_short_name}' ({sender_node_id}) to {receiver_short_name}: {message_string}")

            bbs_nodes = interface.bbs_nodes
            is_sync_message = any(message_string.startswith(prefix) for prefix in
                                  ["BULLETIN|", "MAIL|", "DELETE_BULLETIN|", "DELETE_MAIL|", "DELETE_ZORKSAVE|",
                                   "CHANNEL|", "DELETE_CHANNEL|", "CHANNELCOMMENT|", "CHANNELCOMMENTCONT|", "CHANNELCOMMENTMETA|", "DELETE_CHANNELCOMMENT|",
                                   "BULLETINCONT|", "MAILCONT|", "BULLETINMETA|", "MAILMETA|", "SYNCSTATE|",
                                   "PROFILESYNC|", "SCORESYNC|", "ZORKSAVE|", "ZORKGAP|", "CANDREQ|", "CANDRSP|",
                                   "HASHREQ|", "HASHREC|", "HASHEND|", "HASHMISS|", "HASHZ|", "HASHZGAP|",
                                   "HAVE|", "WANT|", "EVENT|", "PEERGOSSIP|",
                                   "APIREQ|", "APIRESP|", "APIRESPCONT|", "APIRESPMETA|",
                                   "APIRESPGAP|", "APIPOLL|"])

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
            elif (sender_node_id in getattr(interface, 'subscriber_nodes', []) or []
                  ) and message_string.startswith(("WANT|", "HASHMISS|")):
                # Pull-only subscriber (e.g. a Pico cache node): answer its op_log
                # WANT / record HASHMISS, but nothing that would let it push to us.
                # It is intentionally NOT in bbs_nodes, so the push/hash-repair
                # loops never target it — no reconcile churn.
                log_connection_event(sender_id, sender_node_id, sender_short_name, to_id, "subscriber",
                                     f"Answered subscriber pull ({sync_frame})")
                process_message(sender_id, message_string, interface, is_sync_message=True, sender_node_id=sender_node_id)
            elif to_id is not None and to_id != 0 and to_id != 255 and to_id == interface.myInfo.my_node_num:
                log_connection_event(sender_id, sender_node_id, sender_short_name, to_id, "direct", "Accepted direct message")
                process_message(sender_id, message_string, interface, is_sync_message=False, sender_node_id=sender_node_id)
            else:
                log_connection_event(sender_id, sender_node_id, sender_short_name, to_id, "drop", "Ignored group/unknown message")
                logging.info("Ignoring message sent to group chat or from unknown node")
    except KeyError as e:
        logging.error(f"Error processing packet: {e}")
