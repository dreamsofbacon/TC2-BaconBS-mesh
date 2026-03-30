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

from config_init import initialize_config, get_interface, init_cli_parser, merge_config
from db_operations import (
    initialize_database,
    sync_full_database_to_nodes,
    get_sync_progress,
    get_mismatched_peer_nodes,
    get_mismatched_peer_scopes,
)
from js8call_integration import JS8CallClient
from message_processing import on_receive
from pubsub import pub
from utils import send_hash_request_to_bbs_nodes

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
    return os.getenv('BBS_RUNTIME_DIAG_PATH', 'runtime_diagnostics.json')


def get_manual_sync_trigger_path() -> str:
    return os.getenv('BBS_MANUAL_SYNC_TRIGGER_PATH', 'manual_sync.trigger')


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
            if user.get('id'):
                snapshot['local_node_id'] = str(user.get('id'))
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
    write_runtime_diagnostics_snapshot(interface, system_config)

    logging.info(f"TC²-BBS is running on {system_config['interface_type']} interface...")

    initialize_database()

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
        # Empty on startup — receivers use unique_id idempotency, so re-syncing is safe
        synced_nodes: set = set()
        pending_sync_nodes: set = set()
        sync_interval_minutes = int(system_config.get('sync_interval_minutes', 5))
        last_schedule_epoch = 0
        last_manual_trigger_mtime = 0.0
        mismatch_resync_cooldown_seconds = 120
        last_mismatch_resync_at = {}
        mismatch_attempt_counts = {}
        system_config['sync_last_trigger_reason'] = 'scheduled'
        system_config['sync_interval_minutes_runtime'] = sync_interval_minutes
        system_config['sync_next_run_epoch'] = int(time.time())
        system_config['sync_mismatch_retry_at'] = {}

        def _run_sync(node):
            """Background thread: sync db to a single peer, then record completion."""
            try:
                result = sync_full_database_to_nodes([node], interface, delay_ms=500)
                logging.info(f"DB sync complete for {node}: {result['total_messages']} messages sent")
                synced_nodes.add(node)
            except Exception as exc:
                logging.error(f"Error syncing database to {node}: {exc}")
                # Not added to synced_nodes; will retry on next check cycle
            finally:
                pending_sync_nodes.discard(node)

        while True:
            now = time.time()

            # Refresh diagnostics snapshot (5 s while syncing, 30 s otherwise)
            if now >= next_diagnostics_write:
                write_runtime_diagnostics_snapshot(interface, system_config)
                sync_progress = get_sync_progress()
                next_diagnostics_write = now + (5 if sync_progress.get('in_progress') else 30)

            # Check/launch sync work frequently so manual triggers feel responsive
            if now >= next_node_sync_check:
                refresh_peer_lists_from_config(config_path, interface, system_config)
                sync_interval_minutes = read_sync_interval_minutes(config_path, default_minutes=5)
                system_config['sync_interval_minutes_runtime'] = sync_interval_minutes
                current_bbs_nodes = set(getattr(interface, 'bbs_nodes', []) or [])

                manual_triggered = False
                try:
                    if os.path.exists(trigger_path):
                        trigger_mtime = os.path.getmtime(trigger_path)
                        if trigger_mtime > last_manual_trigger_mtime:
                            manual_triggered = True
                            last_manual_trigger_mtime = trigger_mtime
                            os.remove(trigger_path)
                except Exception as exc:
                    logging.debug(f"Unable to process manual sync trigger: {exc}")

                sync_due = (last_schedule_epoch == 0) or (now >= (last_schedule_epoch + (sync_interval_minutes * 60)))

                if (manual_triggered or sync_due) and not pending_sync_nodes:
                    synced_nodes.clear()
                    last_schedule_epoch = now
                    system_config['sync_last_trigger_reason'] = 'manual' if manual_triggered else 'scheduled'
                    if manual_triggered:
                        logging.info("Manual sync trigger received from web admin")
                    else:
                        logging.info(f"Scheduled sync interval reached ({sync_interval_minutes} minutes)")

                # If diagnostics reports mismatch, force targeted re-sync for those peers.
                if not pending_sync_nodes:
                    mismatch_nodes = get_mismatched_peer_nodes(current_bbs_nodes)
                    mismatch_scopes_by_peer = get_mismatched_peer_scopes(current_bbs_nodes)
                    eligible = {
                        node for node in mismatch_nodes
                        if (now - float(last_mismatch_resync_at.get(node, 0))) >= mismatch_resync_cooldown_seconds
                    }
                    if eligible:
                        for node in sorted(eligible, key=str):
                            scopes = mismatch_scopes_by_peer.get(node, ['all'])
                            for scope in scopes:
                                send_hash_request_to_bbs_nodes([node], interface, scope=scope)
                        full_sync_fallback_nodes = set()
                        for node in eligible:
                            last_mismatch_resync_at[node] = now
                            system_config['sync_mismatch_retry_at'][str(node)] = datetime.now(timezone.utc).isoformat()
                            mismatch_attempt_counts[node] = int(mismatch_attempt_counts.get(node, 0)) + 1
                            # Every 3rd mismatch cycle, fall back to full per-peer sync.
                            if mismatch_attempt_counts[node] % 3 == 0:
                                full_sync_fallback_nodes.add(node)
                        if full_sync_fallback_nodes:
                            synced_nodes -= full_sync_fallback_nodes
                            logging.info(f"Mismatch persisted; running full-sync fallback for nodes: {full_sync_fallback_nodes}")
                        system_config['sync_last_trigger_reason'] = 'mismatch'
                        logging.info(f"Peer mismatch detected; requested hash manifests from nodes: {eligible}")

                next_run_epoch = int(last_schedule_epoch + (sync_interval_minutes * 60)) if last_schedule_epoch else int(now)
                system_config['sync_next_run_epoch'] = next_run_epoch

                new_nodes = current_bbs_nodes - synced_nodes - pending_sync_nodes

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
