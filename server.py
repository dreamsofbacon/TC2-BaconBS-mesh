#!/usr/bin/env python3

"""
TC²-BBS Server for Meshtastic by TheCommsChannel (TC²)
Date: 07/14/2024
Version: 0.1.6

Description:
The system allows for mail message handling, bulletin boards, and a channel
directory. It uses a configuration file for setup details and an SQLite3
database for data storage. Mail messages and bulletins are synced with
other BBS servers listed in the config.ini file.
"""

import logging
import json
import os
import configparser
import threading
import time
from datetime import datetime, timezone
from app_paths import resolve_app_path

from config_init import initialize_config, get_interface, init_cli_parser, merge_config
from db_operations import (
    initialize_database,
    install_connection_log_handler,
    sync_full_database_to_nodes,
    sync_priority_data_to_nodes,
    sync_mail_to_nodes,
    sync_bulletins_to_nodes,
    sync_channels_to_nodes,
    sync_profiles_to_nodes,
    sync_game_data_to_nodes,
    get_sync_progress,
    get_mismatched_peer_nodes,
    get_mismatched_peer_scopes,
    get_local_record_counts,
    mark_peer_phase_synced,
    clear_peer_phases_complete,
    clear_all_peer_phases_complete,
    get_peers_with_phase_complete,
    get_incomplete_record_uids,
    set_local_node_id,
)
from js8call_integration import JS8CallClient
from message_processing import (
    on_receive,
    is_hashreq_pending_for_peer_scope,
    start_zork_save_best_candidate_resolution,
    process_pending_candidate_resolutions,
    process_stale_sync_buffers,
    get_candidate_resolution_snapshot,
)
from pubsub import pub
from utils import send_hash_request_to_bbs_nodes, send_sync_state_to_bbs_nodes, select_syncstate_peers_to_notify

# General logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# JS8Call logging
js8call_logger = logging.getLogger('js8call')
js8call_logger.setLevel(logging.DEBUG)
js8call_handler = logging.StreamHandler()
js8call_handler.setLevel(logging.DEBUG)
js8call_formatter = logging.Formatter('%(asctime)s - JS8Call - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S')
js8call_handler.setFormatter(js8call_formatter)
js8call_logger.addHandler(js8call_handler)


def get_runtime_diagnostics_path() -> str:
    return resolve_app_path(os.getenv('BBS_RUNTIME_DIAG_PATH'), 'runtime_diagnostics.json')


def get_manual_sync_trigger_path() -> str:
    return resolve_app_path(os.getenv('BBS_MANUAL_SYNC_TRIGGER_PATH'), 'manual_sync.trigger')


def get_force_check_trigger_path() -> str:
    return resolve_app_path(os.getenv('BBS_FORCE_CHECK_TRIGGER_PATH'), 'force_check.trigger')


def get_peer_resync_trigger_path() -> str:
    return resolve_app_path(os.getenv('BBS_PEER_RESYNC_TRIGGER_PATH'), 'resync_peer.trigger')


def get_zork_save_resolve_trigger_path() -> str:
    return resolve_app_path(os.getenv('BBS_ZORK_SAVE_RESOLVE_TRIGGER_PATH'), 'resolve_zork_save.trigger')


def get_record_resolve_trigger_path() -> str:
    return resolve_app_path(os.getenv('BBS_RECORD_RESOLVE_TRIGGER_PATH'), 'resolve_record.trigger')


def read_sync_interval_minutes(config_path: str, default_minutes: int = 5) -> int:
    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    raw = cfg.get('sync', 'sync_interval_minutes', fallback=str(default_minutes)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default_minutes
    return max(1, value)


def refresh_peer_lists_from_config(config_path: str, interface, system_config: dict) -> None:
    cfg = configparser.ConfigParser()
    cfg.read(config_path)

    bbs_nodes = cfg.get('sync', 'bbs_nodes', fallback='').split(',')
    bbs_nodes = [node.strip() for node in bbs_nodes if node.strip()]

    allowed_nodes = cfg.get('allow_list', 'allowed_nodes', fallback='').split(',')
    allowed_nodes = [node.strip() for node in allowed_nodes if node.strip()]

    interface.bbs_nodes = bbs_nodes
    interface.allowed_nodes = allowed_nodes
    system_config['bbs_nodes'] = bbs_nodes
    system_config['allowed_nodes'] = allowed_nodes


def write_runtime_diagnostics_snapshot(interface, system_config: dict) -> None:
    sync_progress = get_sync_progress()
    mismatch_retry_at = system_config.get('sync_mismatch_retry_at', {})
    if not isinstance(mismatch_retry_at, dict):
        mismatch_retry_at = {}
    snapshot = {
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'interface_attached': True,
        'interface_type': interface.__class__.__name__,
        'mesh_node_count': None,
        'local_node_id': None,
        'local_short_name': None,
        'local_long_name': None,
        'bbs_nodes': list(getattr(interface, 'bbs_nodes', system_config.get('bbs_nodes', [])) or []),
        'allowed_nodes': list(getattr(interface, 'allowed_nodes', system_config.get('allowed_nodes', [])) or []),
        'sync_in_progress': bool(sync_progress.get('in_progress', False)),
        'sync_progress_percent': int(sync_progress.get('progress_percent', 0)),
        'sync_completed_items': int(sync_progress.get('completed_items', 0)),
        'sync_total_items': int(sync_progress.get('total_items', 0)),
        'sync_remaining_items': int(sync_progress.get('remaining_items', 0)),
        'sync_current_phase': str(sync_progress.get('current_phase', 'never_run')),
        'sync_target_nodes': list(sync_progress.get('target_nodes', [])),
        'sync_started_at': str(sync_progress.get('started_at', '')),
        'sync_last_updated_at': str(sync_progress.get('last_updated_at', '')),
        'sync_last_result': str(sync_progress.get('last_result', '')),
        'sync_interval_minutes': int(system_config.get('sync_interval_minutes_runtime', system_config.get('sync_interval_minutes', 5))),
        'sync_next_run_epoch': int(system_config.get('sync_next_run_epoch', 0)),
        'sync_last_trigger_reason': str(system_config.get('sync_last_trigger_reason', 'scheduled')),
        'sync_mismatch_retry_at': dict(mismatch_retry_at),
        'candidate_resolution': get_candidate_resolution_snapshot(),
        'error': '',
    }

    try:
        nodes = getattr(interface, 'nodes', None)
        if isinstance(nodes, dict):
            snapshot['mesh_node_count'] = len(nodes)

        my_info = None
        get_my_info = getattr(interface, 'getMyNodeInfo', None)
        if callable(get_my_info):
            my_info = get_my_info()

        if isinstance(my_info, dict):
            node_num = my_info.get('num')
            user = my_info.get('user', {}) if isinstance(my_info.get('user'), dict) else {}
            if node_num is not None:
                snapshot['local_node_id'] = str(node_num)
                set_local_node_id(str(node_num))
            if user.get('id'):
                snapshot['local_node_id'] = str(user.get('id'))
                set_local_node_id(str(user.get('id')))
            if user.get('shortName'):
                snapshot['local_short_name'] = str(user.get('shortName'))
            if user.get('longName'):
                snapshot['local_long_name'] = str(user.get('longName'))
    except Exception as exc:
        snapshot['error'] = f'Runtime snapshot collection failed: {exc}'

    snapshot_path = get_runtime_diagnostics_path()
    tmp_path = f"{snapshot_path}.tmp"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as snapshot_file:
            json.dump(snapshot, snapshot_file)
        os.replace(tmp_path, snapshot_path)
    except Exception as exc:
        logging.debug(f"Unable to write runtime diagnostics snapshot: {exc}")

def display_banner():
    banner = """
██████╗  █████╗  ██████╗ ██████╗ ███╗   ██╗    ██████╗ ██████╗ ███████╗
██╔══██╗██╔══██╗██╔════╝██╔═══██╗████╗  ██║    ██╔══██╗██╔══██╗██╔════╝
██████╔╝███████║██║     ██║   ██║██╔██╗ ██║    ██████╔╝██████╔╝███████╗
██╔══██╗██╔══██║██║     ██║   ██║██║╚██╗██║    ██╔══██╗██╔══██╗╚════██║
██████╔╝██║  ██║╚██████╗╚██████╔╝██║ ╚████║    ██████╔╝██████╔╝███████║
╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝    ╚═════╝ ╚═════╝ ╚══════╝
Meshtastic Version
"""
    print(banner)

def main():
    display_banner()
    args = init_cli_parser()
    config_file = None
    if args.config is not None:
        config_file = args.config
    system_config = initialize_config(config_file)

    merge_config(system_config, args)

    interface = get_interface(system_config)
    interface.bbs_nodes = system_config['bbs_nodes']
    interface.allowed_nodes = system_config['allowed_nodes']
    config_path = system_config.get('config_file', 'config.ini')
    trigger_path = get_manual_sync_trigger_path()
    force_check_trigger_path = get_force_check_trigger_path()
    peer_resync_trigger_path = get_peer_resync_trigger_path()
    zork_save_resolve_trigger_path = get_zork_save_resolve_trigger_path()
    record_resolve_trigger_path = get_record_resolve_trigger_path()
    write_runtime_diagnostics_snapshot(interface, system_config)

    logging.info(f"TC²-BBS is running on {system_config['interface_type']} interface...")

    initialize_database()
    install_connection_log_handler()

    def receive_packet(packet, interface):
        on_receive(packet, interface)

    pub.subscribe(receive_packet, system_config['mqtt_topic'])

    # Initialize and start JS8Call Client if configured
    js8call_client = JS8CallClient(interface)
    js8call_client.logger = js8call_logger

    if js8call_client.db_conn:
        js8call_client.connect()

    try:
        next_diagnostics_write = 0.0
        next_node_sync_check = 0.0
        next_incomplete_repair = 0.0
        # Empty on startup — receivers use unique_id idempotency, so re-syncing is safe
        mail_synced_nodes: set = set()       # P1: direct mail
        bulletins_synced_nodes: set = set()  # P2: bulletin board posts
        channels_synced_nodes: set = set()   # P3: channel directory
        profiles_synced_nodes: set = set()   # P4: user profiles
        game_synced_nodes: set = set()       # P5: game scores + zork saves (lowest priority)
        synced_nodes: set = set()            # alias: P1+P2 both complete (used for new-node detection)
        pending_sync_nodes: set = set()

        # Seed phase-completion sets from the persistent DB record so a server
        # restart does NOT re-trigger a full push to peers already synced.
        _db_mail_peers = get_peers_with_phase_complete('mail')
        _db_bulletin_peers = get_peers_with_phase_complete('bulletins')
        _db_channel_peers = get_peers_with_phase_complete('channels')
        _db_profile_peers = get_peers_with_phase_complete('profiles')
        _db_game_peers = get_peers_with_phase_complete('game')
        mail_synced_nodes.update(_db_mail_peers)
        bulletins_synced_nodes.update(_db_bulletin_peers)
        channels_synced_nodes.update(_db_channel_peers)
        profiles_synced_nodes.update(_db_profile_peers)
        game_synced_nodes.update(_db_game_peers)
        synced_nodes.update(_db_mail_peers & _db_bulletin_peers)
        if _db_mail_peers:
            logging.info(
                f"Resumed from DB: {len(_db_mail_peers)} peer(s) already fully synced — "
                "skipping full re-push, using hash-repair for any drift."
            )
        syncstate_advertisement_cache: dict = {}
        sync_interval_minutes = int(system_config.get('sync_interval_minutes', 5))
        last_schedule_epoch = 0
        last_manual_trigger_mtime = 0.0
        last_force_check_trigger_mtime = 0.0
        last_peer_resync_trigger_mtime = 0.0
        last_zork_save_resolve_trigger_mtime = 0.0
        last_record_resolve_trigger_mtime = 0.0
        system_config['sync_last_trigger_reason'] = 'scheduled'
        system_config['sync_interval_minutes_runtime'] = sync_interval_minutes
        system_config['sync_next_run_epoch'] = int(time.time())
        system_config['sync_mismatch_retry_at'] = {}

        def _run_sync(node):
            """Background thread: five-phase sync to a single peer in priority order.
            P1 mail → P2 bulletins → P3 channels → P4 profiles → P5 game data.
            Each phase is tracked independently so a mismatch fallback only re-sends
            the scopes that need repair without restarting lower-priority phases.

            Before each phase the peer's most-recently-advertised SYNCSTATE is checked.
            If both count and hash already match local, the full-push for that scope is
            skipped and the hash-repair protocol is trusted to handle any residual drift,
            dramatically reducing airtime when a peer is already mostly up-to-date.
            """
            local_counts = get_local_record_counts()
            peer_scopes_mismatched = set(get_mismatched_peer_scopes({node}).get(node, []))

            # P1 — mail (highest priority; abort remaining phases on failure)
            if 'mail' not in peer_scopes_mismatched:
                logging.info(f"P1 mail skipped for {node}: SYNCSTATE counts/hash match, trusting hash-repair for drift")
                mail_synced_nodes.add(node)
                mark_peer_phase_synced(node, 'mail')
            else:
                try:
                    m = sync_mail_to_nodes([node], interface)
                    logging.info(f"P1 mail sync done for {node}: {m['mail_synced']} sent")
                    mail_synced_nodes.add(node)
                    mark_peer_phase_synced(node, 'mail')
                except Exception as exc:
                    logging.error(f"P1 mail sync failed for {node}: {exc}")
                    pending_sync_nodes.discard(node)
                    return

            # P2 — bulletins
            if 'bulletins' not in peer_scopes_mismatched:
                logging.info(f"P2 bulletins skipped for {node}: SYNCSTATE counts/hash match")
                bulletins_synced_nodes.add(node)
                synced_nodes.add(node)
                mark_peer_phase_synced(node, 'bulletins')
            else:
                try:
                    b = sync_bulletins_to_nodes([node], interface)
                    logging.info(f"P2 bulletin sync done for {node}: {b['bulletins_synced']} sent")
                    bulletins_synced_nodes.add(node)
                    synced_nodes.add(node)  # P1+P2 complete: stop new-node re-trigger
                    mark_peer_phase_synced(node, 'bulletins')
                except Exception as exc:
                    logging.error(f"P2 bulletin sync failed for {node}: {exc}")
                    pending_sync_nodes.discard(node)
                    return

            # P3 — channels (failure does not block profiles or game data)
            if 'channels' not in peer_scopes_mismatched:
                logging.info(f"P3 channels skipped for {node}: SYNCSTATE counts/hash match")
                channels_synced_nodes.add(node)
                mark_peer_phase_synced(node, 'channels')
            else:
                try:
                    ch = sync_channels_to_nodes([node], interface)
                    logging.info(f"P3 channel sync done for {node}: {ch['channels_synced']} sent")
                    channels_synced_nodes.add(node)
                    mark_peer_phase_synced(node, 'channels')
                except Exception as exc:
                    logging.error(f"P3 channel sync failed for {node}: {exc}")

            # P4 — profiles
            if 'profiles' not in peer_scopes_mismatched:
                logging.info(f"P4 profiles skipped for {node}: SYNCSTATE counts/hash match")
                profiles_synced_nodes.add(node)
                mark_peer_phase_synced(node, 'profiles')
            else:
                try:
                    pr = sync_profiles_to_nodes([node], interface)
                    logging.info(f"P4 profile sync done for {node}: {pr['profiles_synced']} sent")
                    profiles_synced_nodes.add(node)
                    mark_peer_phase_synced(node, 'profiles')
                except Exception as exc:
                    logging.error(f"P4 profile sync failed for {node}: {exc}")

            # Send SYNCSTATE so peers can compare counts before game data starts.
            try:
                local_counts = get_local_record_counts()
                destinations = select_syncstate_peers_to_notify([node], local_counts, syncstate_advertisement_cache, now=time.time(), force=True)
                if destinations:
                    send_sync_state_to_bbs_nodes(local_counts, destinations, interface)
            except Exception as exc:
                logging.warning(f"SYNCSTATE ping after P4 failed for {node}: {exc}")

            # P5 — game data (lowest priority)
            game_scopes_match = ('game_scores' not in peer_scopes_mismatched
                                 and 'zork_saves' not in peer_scopes_mismatched)
            if game_scopes_match:
                logging.info(f"P5 game data skipped for {node}: SYNCSTATE counts/hash match")
                game_synced_nodes.add(node)
                mark_peer_phase_synced(node, 'game')
                pending_sync_nodes.discard(node)
            else:
                try:
                    g = sync_game_data_to_nodes([node], interface)
                    logging.info(
                        f"P5 game data sync done for {node}: "
                        f"scores={g['game_scores_synced']}, saves={g['zork_saves_synced']}"
                    )
                    game_synced_nodes.add(node)
                    mark_peer_phase_synced(node, 'game')
                except Exception as exc:
                    logging.error(f"P5 game data sync failed for {node}: {exc}")
                    # game_synced_nodes not updated; will retry next eligible cycle
                finally:
                    pending_sync_nodes.discard(node)

        while True:
            now = time.time()
            force_mismatch_check = False
            process_pending_candidate_resolutions(interface)
            # Drive stale-buffer retries (HASHZGAP / ZORKGAP) on a steady tick so
            # a dropped chunk in the middle of a manifest or zork save stream
            # always triggers a gap-fill request even when no further frames
            # arrive from the peer to drive the opportunistic retry path.
            process_stale_sync_buffers(interface)

            # Refresh diagnostics snapshot (5 s while syncing, 30 s otherwise)
            if now >= next_diagnostics_write:
                write_runtime_diagnostics_snapshot(interface, system_config)
                sync_progress = get_sync_progress()
                next_diagnostics_write = now + (5 if sync_progress.get('in_progress') else 30)

            # Periodic scan: request repair of records whose content arrived truncated.
            # Only runs when not actively syncing so it doesn't pile on top of a flood.
            if now >= next_incomplete_repair and not get_sync_progress().get('in_progress'):
                _incomplete = get_incomplete_record_uids()
                _repair_targets = [
                    (scope, uid)
                    for scope in ('bulletins', 'mail', 'channels')
                    for uid in _incomplete.get(scope, [])
                ]
                _repair_peers = set(getattr(interface, 'bbs_nodes', []) or [])
                if _repair_targets and _repair_peers:
                    from utils import _send_one_sync, get_hash_repair_pause_seconds
                    logging.info(
                        f"Incomplete-content repair: requesting {len(_repair_targets)} record(s) "
                        f"from {len(_repair_peers)} peer(s)"
                    )
                    for _scope, _uid in _repair_targets:
                        for _peer in sorted(_repair_peers):
                            _send_one_sync(
                                f"HASHMISS|{_scope}|{_uid}",
                                _peer,
                                interface,
                                pause_seconds=get_hash_repair_pause_seconds(),
                            )
                # Re-check sooner when incomplete records are known; back off when all complete.
                next_incomplete_repair = now + (120 if _repair_targets else 600)

            # Check/launch sync work frequently so manual triggers feel responsive
            if now >= next_node_sync_check:
                refresh_peer_lists_from_config(config_path, interface, system_config)
                sync_interval_minutes = read_sync_interval_minutes(config_path, default_minutes=5)
                system_config['sync_interval_minutes_runtime'] = sync_interval_minutes
                current_bbs_nodes = set(getattr(interface, 'bbs_nodes', []) or [])

                manual_triggered = False
                force_check_triggered = False
                peer_resync_triggered_node = None
                resolve_zork_save_request = None
                resolve_record_request = None
                try:
                    if os.path.exists(trigger_path):
                        trigger_mtime = os.path.getmtime(trigger_path)
                        if trigger_mtime > last_manual_trigger_mtime:
                            manual_triggered = True
                            last_manual_trigger_mtime = trigger_mtime
                            os.remove(trigger_path)
                except Exception as exc:
                    logging.debug(f"Unable to process manual sync trigger: {exc}")

                try:
                    if os.path.exists(force_check_trigger_path):
                        trigger_mtime = os.path.getmtime(force_check_trigger_path)
                        if trigger_mtime > last_force_check_trigger_mtime:
                            force_check_triggered = True
                            last_force_check_trigger_mtime = trigger_mtime
                            os.remove(force_check_trigger_path)
                except Exception as exc:
                    logging.debug(f"Unable to process force-check trigger: {exc}")

                try:
                    if os.path.exists(peer_resync_trigger_path):
                        trigger_mtime = os.path.getmtime(peer_resync_trigger_path)
                        if trigger_mtime > last_peer_resync_trigger_mtime:
                            last_peer_resync_trigger_mtime = trigger_mtime
                            with open(peer_resync_trigger_path, 'r') as _f:
                                _peer_id = _f.read().strip()
                            os.remove(peer_resync_trigger_path)
                            if _peer_id:
                                peer_resync_triggered_node = _peer_id
                except Exception as exc:
                    logging.debug(f"Unable to process peer resync trigger: {exc}")

                try:
                    if os.path.exists(zork_save_resolve_trigger_path):
                        trigger_mtime = os.path.getmtime(zork_save_resolve_trigger_path)
                        if trigger_mtime > last_zork_save_resolve_trigger_mtime:
                            last_zork_save_resolve_trigger_mtime = trigger_mtime
                            with open(zork_save_resolve_trigger_path, 'r', encoding='utf-8') as _f:
                                resolve_zork_save_request = _f.read().strip()
                            os.remove(zork_save_resolve_trigger_path)
                except Exception as exc:
                    logging.debug(f"Unable to process zork save resolver trigger: {exc}")

                try:
                    if os.path.exists(record_resolve_trigger_path):
                        trigger_mtime = os.path.getmtime(record_resolve_trigger_path)
                        if trigger_mtime > last_record_resolve_trigger_mtime:
                            last_record_resolve_trigger_mtime = trigger_mtime
                            with open(record_resolve_trigger_path, 'r', encoding='utf-8') as _f:
                                resolve_record_request = _f.read().strip()
                            os.remove(record_resolve_trigger_path)
                except Exception as exc:
                    logging.debug(f"Unable to process record resolve trigger: {exc}")

                sync_due = (last_schedule_epoch == 0) or (now >= (last_schedule_epoch + (sync_interval_minutes * 60)))

                if force_check_triggered:
                    force_mismatch_check = True
                    system_config['sync_last_trigger_reason'] = 'force_check'
                    logging.info("Force mismatch check requested from web admin")

                if peer_resync_triggered_node:
                    mail_synced_nodes.discard(peer_resync_triggered_node)
                    bulletins_synced_nodes.discard(peer_resync_triggered_node)
                    channels_synced_nodes.discard(peer_resync_triggered_node)
                    profiles_synced_nodes.discard(peer_resync_triggered_node)
                    game_synced_nodes.discard(peer_resync_triggered_node)
                    synced_nodes.discard(peer_resync_triggered_node)
                    pending_sync_nodes.discard(peer_resync_triggered_node)
                    syncstate_advertisement_cache.pop(peer_resync_triggered_node, None)
                    clear_peer_phases_complete(peer_resync_triggered_node)
                    force_mismatch_check = True
                    system_config['sync_last_trigger_reason'] = 'peer_resync'
                    logging.info(f"Peer full-resync requested for {peer_resync_triggered_node}; cleared from synced/game sets")

                if resolve_zork_save_request:
                    try:
                        payload = json.loads(resolve_zork_save_request)
                        user_id = str(payload.get('user_id', '')).strip()
                        game_id = str(payload.get('game_id', '')).strip()
                        if user_id and game_id:
                            request_id = start_zork_save_best_candidate_resolution(user_id, game_id, list(current_bbs_nodes), interface)
                            system_config['sync_last_trigger_reason'] = 'candidate_resolver'
                            logging.info(f"Started zork save best-candidate resolver request {request_id} for {user_id}:{game_id}")
                    except Exception as exc:
                        logging.warning(f"Unable to start zork save resolver request: {exc}")

                if resolve_record_request:
                    try:
                        payload = json.loads(resolve_record_request)
                        scope = str(payload.get('scope', '')).strip().lower()
                        key = str(payload.get('key', '')).strip()
                        if scope and key:
                            from utils import _send_one_sync, get_hash_repair_pause_seconds
                            for peer_id in sorted(current_bbs_nodes):
                                send_hash_request_to_bbs_nodes([peer_id], interface, scope=scope)
                                _send_one_sync(f"HASHMISS|{scope}|{key}", peer_id, interface, pause_seconds=get_hash_repair_pause_seconds())
                            system_config['sync_last_trigger_reason'] = 'record_resolver'
                            logging.info(f"Queued per-record repair for {scope}:{key} to peers: {sorted(current_bbs_nodes)}")
                    except Exception as exc:
                        logging.warning(f"Unable to start record resolver request: {exc}")

                if (manual_triggered or sync_due) and not pending_sync_nodes:
                    last_schedule_epoch = now
                    local_counts = get_local_record_counts()
                    if manual_triggered:
                        destinations = select_syncstate_peers_to_notify(current_bbs_nodes, local_counts, syncstate_advertisement_cache, now=now, force=True)
                        if destinations:
                            send_sync_state_to_bbs_nodes(local_counts, destinations, interface)
                        # Manual sync clears all phase sets so every phase reruns from scratch.
                        mail_synced_nodes.clear()
                        bulletins_synced_nodes.clear()
                        channels_synced_nodes.clear()
                        profiles_synced_nodes.clear()
                        game_synced_nodes.clear()
                        synced_nodes.clear()
                        clear_all_peer_phases_complete()
                        # Also force immediate mismatch re-check for currently configured peers.
                        force_mismatch_check = True
                        system_config['sync_last_trigger_reason'] = 'manual'
                        logging.info("Manual sync trigger received from web admin")
                    else:
                        destinations = select_syncstate_peers_to_notify(current_bbs_nodes, local_counts, syncstate_advertisement_cache, now=now, force=False)
                        # Scheduled cycle is lightweight; mismatch path requests targeted repairs.
                        system_config['sync_last_trigger_reason'] = 'scheduled'
                        if destinations:
                            send_sync_state_to_bbs_nodes(local_counts, destinations, interface)
                            logging.info(
                                f"Scheduled sync interval reached ({sync_interval_minutes} minutes); "
                                f"sent SYNCSTATE to {len(destinations)} peer(s)"
                            )
                        else:
                            logging.info(
                                f"Scheduled sync interval reached ({sync_interval_minutes} minutes); "
                                "local state unchanged, skipping SYNCSTATE broadcast"
                            )

                # Automatic scheduled SYNCSTATE already prompts peers to request targeted repair.
                # Reserve proactive local HASHREQs for explicit force-check actions to avoid
                # duplicate manifest exchanges on both sides of the link.
                if force_mismatch_check and not pending_sync_nodes:
                    mismatch_nodes = get_mismatched_peer_nodes(current_bbs_nodes)
                    mismatch_scopes_by_peer = get_mismatched_peer_scopes(current_bbs_nodes)
                    eligible = set(mismatch_nodes)
                    if eligible:
                        for node in sorted(eligible, key=str):
                            scopes = mismatch_scopes_by_peer.get(node, ['all'])
                            for scope in scopes:
                                if not is_hashreq_pending_for_peer_scope(node, scope):
                                    send_hash_request_to_bbs_nodes([node], interface, scope=scope)
                        for node in eligible:
                            system_config['sync_mismatch_retry_at'][str(node)] = datetime.now(timezone.utc).isoformat()
                        system_config['sync_last_trigger_reason'] = 'mismatch'
                        logging.info(f"Peer mismatch detected; requested hash manifests from nodes: {eligible}")

                next_run_epoch = int(last_schedule_epoch + (sync_interval_minutes * 60)) if last_schedule_epoch else int(now)
                system_config['sync_next_run_epoch'] = next_run_epoch

                # A node needs a sync thread if it hasn't completed even P1 (mail) yet.
                new_nodes = current_bbs_nodes - mail_synced_nodes - pending_sync_nodes

                if new_nodes:
                    logging.info(f"Detected {len(new_nodes)} new BBS node(s) to sync: {new_nodes}")
                    pending_sync_nodes.update(new_nodes)
                    for new_node in sorted(new_nodes, key=str):
                        t = threading.Thread(target=_run_sync, args=(new_node,), daemon=True)
                        t.start()

                next_node_sync_check = now + 5
            
            time.sleep(1)

    except KeyboardInterrupt:
        logging.info("Shutting down the server...")
        interface.close()
        if js8call_client.connected:
            js8call_client.close()

if __name__ == "__main__":
    main()
