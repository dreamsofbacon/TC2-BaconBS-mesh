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
import time
from datetime import datetime, timezone

from config_init import initialize_config, get_interface, init_cli_parser, merge_config
from db_operations import initialize_database, sync_full_database_to_nodes
from js8call_integration import JS8CallClient
from message_processing import on_receive
from pubsub import pub

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


def write_runtime_diagnostics_snapshot(interface, system_config: dict) -> None:
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
        synced_nodes = set()  # Track which nodes have been synced with
        
        while True:
            # Check and sync diagnostics every 30 seconds
            if time.time() >= next_diagnostics_write:
                write_runtime_diagnostics_snapshot(interface, system_config)
                next_diagnostics_write = time.time() + 30
            
            # Check for new BBS nodes to sync with every 60 seconds
            if time.time() >= next_node_sync_check:
                current_bbs_nodes = set(interface.bbs_nodes or [])
                new_nodes = current_bbs_nodes - synced_nodes
                
                if new_nodes:
                    logging.info(f"Detected {len(new_nodes)} new BBS node(s): {new_nodes}")
                    try:
                        result = sync_full_database_to_nodes(list(new_nodes), interface, delay_ms=500)
                        logging.info(f"Full database sync complete: {result['total_messages']} messages sent to new node(s)")
                        synced_nodes.update(new_nodes)
                    except Exception as e:
                        logging.error(f"Error syncing database to new nodes: {e}")
                        # Don't mark as synced on error; we'll retry next cycle
                
                next_node_sync_check = time.time() + 60
            
            time.sleep(1)

    except KeyboardInterrupt:
        logging.info("Shutting down the server...")
        interface.close()
        if js8call_client.connected:
            js8call_client.close()

if __name__ == "__main__":
    main()
