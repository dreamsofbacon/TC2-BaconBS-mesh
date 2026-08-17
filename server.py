#!/usr/bin/env python3

"""
Bacon BBS Server for Meshtastic + MeshCore (fork of TC²-BBS-mesh by TheCommsChannel)
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
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import configparser
import random
import threading
import time
from datetime import datetime, timezone
from app_paths import resolve_app_path

from config_init import (
    initialize_config, get_interface, get_secondary_interface, init_cli_parser, merge_config,
    get_mqtt_interfaces, get_mqtt_interface_by_name,
)
from radio_link import RadioLink
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
    get_local_node_id,
    run_op_log_backfill,
    get_peer_sync_states,
    upsert_mesh_clients,
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
from utils import send_hash_request_to_bbs_nodes, send_sync_state_to_bbs_nodes, send_have_to_bbs_nodes, send_peer_gossip_to_bbs_nodes, select_syncstate_peers_to_notify, home_network

# Per-link tick cadence constants (module-level so both main() and
# _run_link_tick, which runs once per active RadioLink, can share them).
_API_POLL_INTERVAL: float = 300.0
# Per-record incomplete-repair attempt counts. After several failed repair
# cycles a record's partial content is reset so a fresh, self-consistent
# resend can rebuild it (breaks the misaligned-chunk-boundary deadlock).
_INCOMPLETE_RESET_AFTER: int = 4  # ~3 min at the 45s repair cadence

# Reconnect signal: set by the consecutive-failure counter in utils.py (via
# signal_reconnect() below) or by the reader-thread liveness check in
# _run_link_tick when the TCP/serial link is detected dead. Each RadioLink
# has its own threading.Event (radio_link.py) so one dead radio's reconnect
# doesn't affect the other in dual-radio bridge mode -- _active_links is the
# registry signal_reconnect() uses to find which link a given interface
# object (passed up from a failed send in utils.py) belongs to.
_active_links: list = []

# Set by main() so the periodic diagnostics snapshot can report non-link
# services (JS8Call, API gateway) alongside the radio/MQTT links -- see
# _describe_services. Mirrors _active_links rather than threading another
# parameter through write_runtime_diagnostics_snapshot's call sites.
_js8call_client = None


def signal_reconnect(interface) -> None:
    """Mark the RadioLink that owns ``interface`` as needing reconnect.

    Called from utils.py's send-failure counter with whatever interface
    object the failing send was made on, so the *correct* link reconnects
    in dual-radio bridge mode instead of a single shared flag that couldn't
    tell which radio actually failed. No-ops (with a debug log) if the
    interface doesn't match any currently active link -- e.g. it was
    already swapped out by an in-flight reconnect.
    """
    for link in _active_links:
        if link.interface is interface:
            link.reconnect_needed.set()
            return
    logging.debug("signal_reconnect: interface did not match any active RadioLink; ignoring")

# Short socket send timeout so a dead TCP connection fails fast (5 s) instead
# of blocking for the library default (~30 s), keeping the main loop responsive.
_TCP_SEND_TIMEOUT_SECONDS: float = 5.0


def _apply_socket_timeout(iface) -> None:
    """Set a short send timeout on the TCP socket if present."""
    try:
        sock = getattr(iface, 'socket', None)
        if sock is not None:
            sock.settimeout(_TCP_SEND_TIMEOUT_SECONDS)
    except Exception:
        pass


def _is_interface_alive(iface) -> bool:
    """Return False if the active radio transport has disconnected.

    None is never "alive" -- it means this link either never connected at
    startup (get_interface/get_secondary_interface gave up after repeated
    failures, see config_init._open_interface) or is mid-reconnect. Treating
    it the same as a disconnected interface routes it through the exact same
    _run_link_tick -> reconnect_needed -> _reconnect_link path as a live
    interface that later drops, so a radio that fails at startup keeps
    retrying in the background instead of being permanently absent.
    """
    if iface is None:
        return False
    try:
        connected = getattr(iface, 'is_connected', None)
        if isinstance(connected, bool) and not connected:
            return False
        rx = getattr(iface, '_rxThread', None)
        if rx is not None and not rx.is_alive():
            return False
    except Exception:
        pass
    return True

# Liveness watchdog: the main loop bumps this on every iteration. A daemon
# thread restarts the process via os._exit(2) (systemd brings it back) if the
# main loop wedges, typically because Meshtastic's _sendToRadio blocked on a
# dead USB/serial link and every thread piles up behind it.
_last_main_loop_tick: float = time.time()
_WATCHDOG_STUCK_SECONDS: float = float(os.environ.get("BBS_WATCHDOG_STUCK_SECONDS", "180"))
_WATCHDOG_CHECK_INTERVAL: float = 15.0


def _watchdog_loop() -> None:
    import sys
    while True:
        try:
            time.sleep(_WATCHDOG_CHECK_INTERVAL)
            age = time.time() - _last_main_loop_tick
            if age > _WATCHDOG_STUCK_SECONDS:
                msg = (
                    f"Main-loop watchdog: no tick for {age:.0f}s "
                    f"(>{_WATCHDOG_STUCK_SECONDS:.0f}s); restarting process via os._exit(2). "
                    f"Probable cause: Meshtastic _sendToRadio wedged on dead serial link."
                )
                try:
                    logging.critical(msg)
                except Exception:
                    pass
                try:
                    sys.stderr.write(msg + "\n")
                    sys.stderr.flush()
                except Exception:
                    pass
                os._exit(2)
        except Exception:
            # Never let the watchdog die quietly.
            try:
                logging.exception("Watchdog tick failed; continuing")
            except Exception:
                pass


def _start_main_loop_watchdog() -> None:
    t = threading.Thread(target=_watchdog_loop, name="main-loop-watchdog", daemon=True)
    t.start()
    logging.info(
        f"Main-loop watchdog started (stuck_threshold={_WATCHDOG_STUCK_SECONDS:.0f}s, "
        f"check_interval={_WATCHDOG_CHECK_INTERVAL:.0f}s)"
    )

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


def get_link_reconnect_trigger_path() -> str:
    return resolve_app_path(os.getenv('BBS_LINK_RECONNECT_TRIGGER_PATH'), 'reconnect_link.trigger')


def apply_link_reconnect_request(links, target: str) -> list:
    """Flag the requested link(s) for reconnect. Returns the names matched.

    ``target`` is a link name or 'all'. Deliberately only sets the same
    reconnect_needed flag the automatic liveness check uses, so the existing
    _reconnect_link path does the work -- close, retry with backoff, rejoin --
    without restarting the process or touching any other link.
    """
    normalized = str(target or '').strip()
    if not normalized:
        return []
    matched = [link for link in links if normalized == 'all' or link.name == normalized]
    for link in matched:
        logging.info(f"[{link.name}] Reconnect requested from web admin.")
        link.reconnect_needed.set()
    if not matched:
        logging.warning(
            f"Reconnect requested for unknown link '{normalized}'; "
            f"active links are: {', '.join(link.name for link in links) or '(none)'}"
        )
    return [link.name for link in matched]


def read_sync_interval_minutes(config_path: str, default_minutes: int = 5) -> int:
    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    raw = cfg.get('sync', 'sync_interval_minutes', fallback=str(default_minutes)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default_minutes
    return max(1, value)


def refresh_peer_lists_from_config(
    config_path: str, interface, system_config: dict, *,
    sync_section: str = 'sync', allow_section: str = 'allow_list',
    bbs_nodes_key: str = 'bbs_nodes', allowed_nodes_key: str = 'allowed_nodes',
    subscriber_nodes_key: str = 'subscriber_nodes',
) -> None:
    """Re-read one radio's peer lists from its config sections.

    Single-radio callers (the default) use the original 'sync'/'allow_list'
    sections and 'bbs_nodes'/'allowed_nodes'/'subscriber_nodes' system_config
    keys -- byte-identical behavior to before dual-radio support existed.
    Dual-radio bridge mode's secondary RadioLink passes sync_section='sync2'
    etc. so the two radios' peer lists never overwrite each other.
    """
    cfg = configparser.ConfigParser()
    cfg.read(config_path)

    bbs_nodes = cfg.get(sync_section, 'bbs_nodes', fallback='').split(',')
    bbs_nodes = [node.strip() for node in bbs_nodes if node.strip()]

    allowed_nodes = cfg.get(allow_section, 'allowed_nodes', fallback='').split(',')
    allowed_nodes = [node.strip() for node in allowed_nodes if node.strip()]

    # Pull-only subscribers (e.g. a Pico cache node): we answer their WANT/HASHMISS
    # but never push-sync or hash-repair TO them (they can't reciprocate). Kept in
    # a separate list from bbs_nodes precisely so the push/repair loops skip them.
    subscriber_nodes = cfg.get(sync_section, 'subscriber_nodes', fallback='').split(',')
    subscriber_nodes = [node.strip() for node in subscriber_nodes if node.strip()]

    interface.bbs_nodes = bbs_nodes
    interface.allowed_nodes = allowed_nodes
    interface.subscriber_nodes = subscriber_nodes
    system_config[bbs_nodes_key] = bbs_nodes
    system_config[allowed_nodes_key] = allowed_nodes
    system_config[subscriber_nodes_key] = subscriber_nodes


def _describe_radio(interface, system_config: dict, *, bbs_nodes_key='bbs_nodes', allowed_nodes_key='allowed_nodes') -> dict:
    """Build one radio's diagnostics entry -- shared by the flat top-level
    fields (back-compat, mirrors the primary radio) and the 'radios' array."""
    entry = {
        # None here means this link never connected at startup (or dropped
        # and is mid-reconnect) -- see config_init._open_interface / main()'s
        # RadioLink construction, which now creates the link either way so
        # the background reconnect loop can pick it up.
        'interface_attached': interface is not None,
        'interface_type': interface.__class__.__name__,
        'radio_protocol': str(getattr(interface, 'protocol_name', 'Meshtastic')),
        'mesh_node_count': None,
        'local_node_id': None,
        'local_short_name': None,
        'local_long_name': None,
        'bbs_nodes': list(getattr(interface, 'bbs_nodes', system_config.get(bbs_nodes_key, [])) or []),
        'allowed_nodes': list(getattr(interface, 'allowed_nodes', system_config.get(allowed_nodes_key, [])) or []),
        'connected': interface is not None,
    }
    try:
        connected = getattr(interface, 'is_connected', None)
        if isinstance(connected, bool):
            entry['connected'] = connected

        nodes = getattr(interface, 'nodes', None)
        if isinstance(nodes, dict):
            entry['mesh_node_count'] = len(nodes)

        my_info = None
        get_my_info = getattr(interface, 'getMyNodeInfo', None)
        if callable(get_my_info):
            my_info = get_my_info()

        if isinstance(my_info, dict):
            node_num = my_info.get('num')
            user = my_info.get('user', {}) if isinstance(my_info.get('user'), dict) else {}
            if node_num is not None:
                entry['local_node_id'] = str(node_num)
            if user.get('id'):
                entry['local_node_id'] = str(user.get('id'))
            if user.get('shortName'):
                entry['local_short_name'] = str(user.get('shortName'))
            if user.get('longName'):
                entry['local_long_name'] = str(user.get('longName'))
            if entry['local_node_id']:
                set_local_node_id(entry['local_node_id'])
    except Exception as exc:
        entry['error'] = f'Runtime snapshot collection failed: {exc}'
    return entry


def _describe_services() -> list:
    """Non-link services this node runs, for the status display.

    These are NOT RadioLinks -- they carry no BBS sync traffic and have no
    reconnect path -- so they're reported separately from 'radios' and the
    web admin renders them without a Reconnect button. Only services that
    are actually configured appear; an unconfigured integration is absent
    rather than shown as a permanently-down service.

    Adding a future service here is the only change needed to surface it in
    both the nav-bar badges and Settings > Links & Services.
    """
    services = []
    try:
        from gateway import is_gateway_enabled
        if is_gateway_enabled():
            services.append({
                'name': 'gateway',
                'protocol': 'API Gateway',
                'connected': True,   # config-enabled; it has no socket to be "down"
                'reconnecting': False,
            })
    except Exception:
        pass
    try:
        client = _js8call_client
        # db_conn is JS8Call's own "is this configured at all" signal --
        # see js8call_integration.JS8CallClient.__init__.
        if client is not None and getattr(client, 'db_conn', None):
            services.append({
                'name': 'js8call',
                'protocol': 'JS8Call',
                'connected': bool(getattr(client, 'connected', False)),
                'reconnecting': False,
            })
    except Exception:
        pass
    return services


def write_runtime_diagnostics_snapshot(links, system_config: dict) -> None:
    """Write the diagnostics JSON web_admin.py reads. ``links`` is a list of
    one or two RadioLink -- the top-level fields mirror links[0] (the
    primary radio) for back-compat with any reader written before dual-radio
    bridge mode existed; a new 'radios' array carries every active radio."""
    if not isinstance(links, (list, tuple)):
        links = [links]  # tolerate a bare interface for back-compat callers
    sync_progress = get_sync_progress()
    mismatch_retry_at = system_config.get('sync_mismatch_retry_at', {})
    if not isinstance(mismatch_retry_at, dict):
        mismatch_retry_at = {}

    radios = []
    for link in links:
        iface = getattr(link, 'interface', link)
        name = getattr(link, 'name', 'primary')
        bbs_key = getattr(link, 'bbs_nodes_key', 'bbs_nodes')
        allowed_key = getattr(link, 'allowed_nodes_key', 'allowed_nodes')
        entry = _describe_radio(iface, system_config, bbs_nodes_key=bbs_key, allowed_nodes_key=allowed_key)
        entry['name'] = name
        entry['reconnecting'] = bool(getattr(link, 'reconnecting', False))
        radios.append(entry)

    primary = radios[0] if radios else _describe_radio(None, system_config)

    snapshot = {
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'interface_attached': primary.get('interface_attached', False),
        'interface_type': primary.get('interface_type'),
        'radio_protocol': primary.get('radio_protocol'),
        'mesh_node_count': primary.get('mesh_node_count'),
        'local_node_id': primary.get('local_node_id'),
        'local_short_name': primary.get('local_short_name'),
        'local_long_name': primary.get('local_long_name'),
        'bbs_nodes': primary.get('bbs_nodes', []),
        'allowed_nodes': primary.get('allowed_nodes', []),
        'radios': radios,
        'services': _describe_services(),
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
        'error': primary.get('error', ''),
    }

    snapshot_path = get_runtime_diagnostics_path()
    tmp_path = f"{snapshot_path}.tmp"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as snapshot_file:
            json.dump(snapshot, snapshot_file)
        os.replace(tmp_path, snapshot_path)
    except Exception as exc:
        logging.debug(f"Unable to write runtime diagnostics snapshot: {exc}")

    return snapshot


def publish_mqtt_status(links, snapshot: dict) -> None:
    """Publish this node's whole link-status tree to every active MQTT
    broker link -- see mqtt_interface.MqttInterface.publish_status for the
    actual topic design. Reuses the exact 'radios' entries
    write_runtime_diagnostics_snapshot just built (same call site, always
    passed its return value) so this, the Settings > Diagnostics page, and
    the nav-bar status badges' /api/status/links can never disagree.

    Each broker gets the SAME whole-node status -- every radio AND every
    MQTT link, not just itself -- so a peer bridging with this node on ONE
    broker can still see whether its other links are healthy, which
    matters for routing/monitoring decisions on their end.

    Best-effort throughout: entirely skipped if there are no MQTT links,
    and a publish failure on one link is logged, never allowed to affect
    the others or the caller (server.py's main loop).
    """
    radios = snapshot.get('radios', []) if isinstance(snapshot, dict) else []
    if not radios:
        return
    status = {
        'updated_at': snapshot.get('updated_at') if isinstance(snapshot, dict) else None,
        'links': {
            str(r.get('name', 'primary')): {
                'protocol': r.get('radio_protocol'),
                'connected': bool(r.get('connected', False)),
                'reconnecting': bool(r.get('reconnecting', False)),
                'mesh_node_count': r.get('mesh_node_count'),
            }
            for r in radios
        },
    }
    for link in links:
        publish = getattr(link.interface, 'publish_status', None)
        if callable(publish):
            try:
                publish(status)
            except Exception:
                logging.debug(f"[{link.name}] MQTT status publish failed", exc_info=True)


def persist_mesh_clients(links) -> None:
    """Durably record each active link's live node roster (interface.nodes)
    to the mesh_clients table -- see the schema comment in
    initialize_database() for why this exists. interface.nodes is
    transient (rebuilt from scratch on every reconnect), so without this a
    restart silently forgets every device the BBS has ever seen nearby.

    Excludes each interface's own local-node entry (a node is "in range OF"
    this device, not itself). The common fields (num, user.id/shortName/
    longName/hwModel/role) come from all three interface types uniformly;
    lastHeard and deviceMetrics.batteryLevel are Meshtastic-only today (see
    meshcore_interface.py / mqtt_interface.py -- neither populates them)
    and simply come through as None on other transports.

    Batches one upsert transaction per link (not per node) -- see
    db_operations.upsert_mesh_clients. Best-effort: a failure on one link
    is logged, never allowed to affect another link or the caller (the
    main loop).
    """
    for link in links:
        interface = link.interface
        nodes = getattr(interface, 'nodes', None)
        if not isinstance(nodes, dict) or not nodes:
            continue

        my_info = getattr(interface, 'myInfo', None)
        local_num = getattr(my_info, 'my_node_num', None)
        protocol = str(getattr(interface, 'protocol_name', 'Meshtastic'))

        rows = []
        for node_id, node in nodes.items():
            if not isinstance(node, dict):
                continue
            node_num = node.get('num')
            if local_num is not None and node_num == local_num:
                continue  # our own node, not a device in range of us
            user = node.get('user') if isinstance(node.get('user'), dict) else {}
            device_metrics = node.get('deviceMetrics') if isinstance(node.get('deviceMetrics'), dict) else {}
            rows.append({
                'link_name': link.name,
                'node_id': str(node_id),
                'node_num': node_num,
                'protocol': protocol,
                'short_name': user.get('shortName', ''),
                'long_name': user.get('longName', ''),
                'hw_model': user.get('hwModel', ''),
                'role': user.get('role', ''),
                'battery_level': device_metrics.get('batteryLevel'),
                'last_heard_epoch': node.get('lastHeard'),
            })

        if rows:
            try:
                upsert_mesh_clients(rows)
            except Exception:
                logging.debug(f"[{link.name}] mesh client roster persist failed", exc_info=True)


def display_banner():
    banner = """
██████╗  █████╗  ██████╗ ██████╗ ███╗   ██╗    ██████╗ ██████╗ ███████╗
██╔══██╗██╔══██╗██╔════╝██╔═══██╗████╗  ██║    ██╔══██╗██╔══██╗██╔════╝
██████╔╝███████║██║     ██║   ██║██╔██╗ ██║    ██████╔╝██████╔╝███████╗
██╔══██╗██╔══██║██║     ██║   ██║██║╚██╗██║    ██╔══██╗██╔══██╗╚════██║
██████╔╝██║  ██║╚██████╗╚██████╔╝██║ ╚████║    ██████╔╝██████╔╝███████║
╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝    ╚═════╝ ╚═════╝ ╚══════╝
Meshtastic + MeshCore Version
"""
    print(banner)

def _seed_link_from_db(link: RadioLink) -> None:
    """Seed phase-completion sets from the persistent DB record so a server
    restart does NOT re-trigger a full push to peers already synced. Reads
    the same global per-peer completion table regardless of which link is
    seeding -- harmless since a peer id only ever appears in one link's own
    bbs_nodes/pending-sync bookkeeping (peer-id shape is protocol-specific,
    see utils.home_network)."""
    db_mail_peers = get_peers_with_phase_complete('mail')
    db_bulletin_peers = get_peers_with_phase_complete('bulletins')
    db_channel_peers = get_peers_with_phase_complete('channels')
    db_profile_peers = get_peers_with_phase_complete('profiles')
    db_game_peers = get_peers_with_phase_complete('game')
    link.mail_synced_nodes.update(db_mail_peers)
    link.bulletins_synced_nodes.update(db_bulletin_peers)
    link.channels_synced_nodes.update(db_channel_peers)
    link.profiles_synced_nodes.update(db_profile_peers)
    link.game_synced_nodes.update(db_game_peers)
    link.synced_nodes.update(db_mail_peers & db_bulletin_peers)
    if db_mail_peers:
        logging.info(
            f"[{link.name}] Resumed from DB: {len(db_mail_peers)} peer(s) already fully synced — "
            "skipping full re-push, using hash-repair for any drift."
        )


def _link_for_node(links: list, node_id) -> RadioLink:
    """Pick the RadioLink a node id belongs to.

    Checks each link's own configured peer lists FIRST -- the only way to
    disambiguate multiple simultaneous same-protocol links (e.g. two MQTT
    links bridging two different remote sites both report network_key
    values that don't collapse to one shared bucket, but a bare
    home_network() shape check can't tell them apart either way). Falls
    back to the coarser home_network()==network_key match for a peer id no
    link's lists contain yet, then to links[0] -- exactly single-radio
    behavior when there's only one link."""
    text = str(node_id or '')
    for link in links:
        if text in link.bbs_nodes or text in link.allowed_nodes or text in link.subscriber_nodes:
            return link
    target = home_network(node_id)
    for link in links:
        if link.network_key == target:
            return link
    return links[0]


def _run_sync_for_link(link: RadioLink, node) -> None:
    """Background thread: five-phase sync to a single peer in priority order,
    over ONE radio link. P1 mail → P2 bulletins → P3 channels → P4 profiles
    → P5 game data. Each phase is tracked independently so a mismatch
    fallback only re-sends the scopes that need repair without restarting
    lower-priority phases.

    Two links run this concurrently (one thread per node, per link) without
    interfering with each other since all mutable state lives on `link`.

    Before each phase the peer's most-recently-advertised SYNCSTATE is checked.
    If both count and hash already match local, the full-push for that scope is
    skipped and the hash-repair protocol is trusted to handle any residual drift,
    dramatically reducing airtime when a peer is already mostly up-to-date.
    """
    interface = link.interface
    peer_scopes_mismatched = set(get_mismatched_peer_scopes({node}).get(node, []))

    # P1 — mail (highest priority; abort remaining phases on failure)
    if 'mail' not in peer_scopes_mismatched:
        logging.info(f"[{link.name}] P1 mail skipped for {node}: SYNCSTATE counts/hash match, trusting hash-repair for drift")
        link.mail_synced_nodes.add(node)
        mark_peer_phase_synced(node, 'mail')
    else:
        try:
            m = sync_mail_to_nodes([node], interface)
            logging.info(f"[{link.name}] P1 mail sync done for {node}: {m['mail_synced']} sent")
            link.mail_synced_nodes.add(node)
            mark_peer_phase_synced(node, 'mail')
        except Exception as exc:
            logging.error(f"[{link.name}] P1 mail sync failed for {node}: {exc}")
            link.pending_sync_nodes.discard(node)
            return

    # P2 — bulletins
    if 'bulletins' not in peer_scopes_mismatched:
        logging.info(f"[{link.name}] P2 bulletins skipped for {node}: SYNCSTATE counts/hash match")
        link.bulletins_synced_nodes.add(node)
        link.synced_nodes.add(node)
        mark_peer_phase_synced(node, 'bulletins')
    else:
        try:
            b = sync_bulletins_to_nodes([node], interface)
            logging.info(f"[{link.name}] P2 bulletin sync done for {node}: {b['bulletins_synced']} sent")
            link.bulletins_synced_nodes.add(node)
            link.synced_nodes.add(node)  # P1+P2 complete: stop new-node re-trigger
            mark_peer_phase_synced(node, 'bulletins')
        except Exception as exc:
            logging.error(f"[{link.name}] P2 bulletin sync failed for {node}: {exc}")
            link.pending_sync_nodes.discard(node)
            return

    # P3 — channels (failure does not block profiles or game data)
    if 'channels' not in peer_scopes_mismatched:
        logging.info(f"[{link.name}] P3 channels skipped for {node}: SYNCSTATE counts/hash match")
        link.channels_synced_nodes.add(node)
        mark_peer_phase_synced(node, 'channels')
    else:
        try:
            ch = sync_channels_to_nodes([node], interface)
            logging.info(f"[{link.name}] P3 channel sync done for {node}: {ch['channels_synced']} sent")
            link.channels_synced_nodes.add(node)
            mark_peer_phase_synced(node, 'channels')
        except Exception as exc:
            logging.error(f"[{link.name}] P3 channel sync failed for {node}: {exc}")

    # P4 — profiles
    if 'profiles' not in peer_scopes_mismatched:
        logging.info(f"[{link.name}] P4 profiles skipped for {node}: SYNCSTATE counts/hash match")
        link.profiles_synced_nodes.add(node)
        mark_peer_phase_synced(node, 'profiles')
    else:
        try:
            pr = sync_profiles_to_nodes([node], interface)
            logging.info(f"[{link.name}] P4 profile sync done for {node}: {pr['profiles_synced']} sent")
            link.profiles_synced_nodes.add(node)
            mark_peer_phase_synced(node, 'profiles')
        except Exception as exc:
            logging.error(f"[{link.name}] P4 profile sync failed for {node}: {exc}")

    # Send SYNCSTATE so peers can compare counts before game data starts.
    try:
        local_counts = get_local_record_counts()
        destinations = select_syncstate_peers_to_notify([node], local_counts, link.syncstate_advertisement_cache, now=time.time(), force=True)
        if destinations:
            send_sync_state_to_bbs_nodes(local_counts, destinations, interface)
    except Exception as exc:
        logging.warning(f"[{link.name}] SYNCSTATE ping after P4 failed for {node}: {exc}")

    # P5 — game data (lowest priority)
    game_scopes_match = ('game_scores' not in peer_scopes_mismatched
                         and 'zork_saves' not in peer_scopes_mismatched)
    if game_scopes_match:
        logging.info(f"[{link.name}] P5 game data skipped for {node}: SYNCSTATE counts/hash match")
        link.game_synced_nodes.add(node)
        mark_peer_phase_synced(node, 'game')
        link.pending_sync_nodes.discard(node)
    else:
        try:
            g = sync_game_data_to_nodes([node], interface)
            logging.info(
                f"[{link.name}] P5 game data sync done for {node}: "
                f"scores={g['game_scores_synced']}, saves={g['zork_saves_synced']}"
            )
            link.game_synced_nodes.add(node)
            mark_peer_phase_synced(node, 'game')
        except Exception as exc:
            logging.error(f"[{link.name}] P5 game data sync failed for {node}: {exc}")
            # game_synced_nodes not updated; will retry next eligible cycle
        finally:
            link.pending_sync_nodes.discard(node)


def _is_link_still_configured(link: RadioLink, system_config: dict) -> bool:
    """True if ``link`` is still supposed to exist per current config.ini,
    independent of whether its interface is currently connected.

    Needed because reconnect_fn (get_interface / get_secondary_interface /
    get_mqtt_interface_by_name) returns None for two very different reasons
    that _reconnect_link must NOT treat the same way:
      1. The link was removed/disabled in config.ini since startup -> stop
         retrying, it's gone for good.
      2. The link is still configured but this attempt cycle failed again
         (wedged radio, unreachable broker) -> keep retrying with backoff.
    Only (1) should ever abandon the reconnect loop.
    """
    if link.name == 'primary':
        return True  # no disable flag for the primary radio -- always configured
    if link.name == 'secondary':
        return bool(system_config.get('interface2_enabled'))
    # MQTT links: still configured iff still present in mqtt_links by name.
    return any(entry.get('name') == link.name for entry in system_config.get('mqtt_links', []))


def _reconnect_link(link: RadioLink, system_config: dict, config_path: str) -> None:
    """Dedicated per-link reconnect-with-backoff thread.

    Runs off the main loop (unlike the original single-radio code's inline
    blocking retry) so a dead radio degrades ONLY this link to unavailable
    instead of blocking sync work on any other active link. In single-radio
    deployments there is only ever one link, so this is a straight behavior-
    preserving move of the same retry loop onto its own thread.
    """
    import utils as _utils
    _utils._consecutive_send_failures = 0
    if link.interface is not None:
        logging.warning(f"[{link.name}] Reconnect signal received — closing dead interface...")
        try:
            link.interface.close()
        except Exception:
            pass
    else:
        # This link never connected at startup (get_interface/
        # get_secondary_interface gave up after repeated failures) -- there
        # is nothing to close, just start trying to connect.
        logging.warning(f"[{link.name}] Never connected at startup — attempting first connect...")
    logging.warning(f"[{link.name}] Attempting to reconnect to radio interface...")
    retry_delay = 5
    while True:
        try:
            time.sleep(retry_delay)
            if link.reconnect_fn is not None:
                new_iface = link.reconnect_fn(system_config)
            elif link.name == 'primary':
                new_iface = get_interface(system_config)
            else:
                new_iface = get_secondary_interface(system_config)
            if new_iface is None:
                if not _is_link_still_configured(link, system_config):
                    logging.warning(f"[{link.name}] interface no longer configured in config.ini; abandoning reconnect")
                    link.reconnecting = False
                    return
                # Still configured -- this attempt cycle just failed again
                # (e.g. get_interface's own bounded retry gave up on a still-
                # wedged radio, or the MQTT broker is still unreachable).
                # Keep retrying with backoff rather than abandoning the link.
                logging.warning(f"[{link.name}] Reconnect attempt still failing; retrying in {retry_delay}s...")
                retry_delay = min(retry_delay * 2, 60)
                continue
            _apply_socket_timeout(new_iface)
            refresh_peer_lists_from_config(
                config_path, new_iface, system_config,
                sync_section=link.sync_section, allow_section=link.allow_section,
                bbs_nodes_key=link.bbs_nodes_key, allowed_nodes_key=link.allowed_nodes_key,
                subscriber_nodes_key=link.subscriber_nodes_key,
            )
            start_receive = getattr(new_iface, 'start_receive', None)
            if callable(start_receive):
                start_receive()
            link.interface = new_iface
            link.reconnecting = False
            logging.info(f"[{link.name}] Reconnected to radio interface successfully.")
            return
        except Exception as exc:
            logging.warning(f"[{link.name}] Reconnect failed: {exc} — retrying in {retry_delay}s...")
            retry_delay = min(retry_delay * 2, 60)


def _run_link_tick(link: RadioLink, *, system_config: dict, config_path: str,
                    triggers: dict, now: float) -> None:
    """One radio's worth of per-tick work. Called once per active RadioLink
    from main()'s while loop, in sequence (not concurrently), so nothing
    here needs locking against another link's tick.

    NOTE: process_pending_candidate_resolutions / process_stale_sync_buffers /
    request_pending_api_gaps sweep GLOBAL pending-request state and aren't
    peer-aware, so calling them once per link (as here) can occasionally
    retry via the "wrong" radio for an item that actually belongs to the
    other network — harmless (the send just fails/logs against a peer that
    isn't reachable on that transport) but worth knowing about; a future
    pass could make these genuinely peer-routed if it becomes a problem in
    practice.
    """
    link.bump_tick()

    if link.reconnecting:
        # A dedicated thread (_reconnect_link) is retrying this link's
        # connection with backoff. Skip sync/send work for this link until
        # it finishes — every OTHER link keeps ticking normally.
        return

    if not _is_interface_alive(link.interface):
        logging.warning(f"[{link.name}] Radio reader thread has exited — connection lost, triggering reconnect.")
        link.reconnect_needed.set()

    if link.reconnect_needed.is_set():
        link.reconnect_needed.clear()
        link.reconnecting = True
        threading.Thread(
            target=_reconnect_link, args=(link, system_config, config_path),
            daemon=True, name=f"reconnect-{link.name}",
        ).start()
        return

    interface = link.interface

    process_pending_candidate_resolutions(interface)
    # Drive stale-buffer retries (HASHZGAP / ZORKGAP) on a steady tick so a
    # dropped chunk in the middle of a manifest or zork save stream always
    # triggers a gap-fill request even when no further frames arrive.
    process_stale_sync_buffers(interface)

    # Requester side (Phase 3): nudge the gateway to refill any dropped
    # response chunks before the request times out.
    try:
        from message_processing import request_pending_api_gaps
        request_pending_api_gaps(interface)
    except Exception:
        pass

    # Requester side (Phase 2): periodically poll this link's gateway
    # mailboxes for any responses queued while we were offline.
    if now >= link.next_api_poll:
        _polled = 0
        try:
            from utils import send_api_poll
            _polled = send_api_poll(get_local_node_id(), interface)
        except Exception:
            pass
        next_api_poll_interval = _API_POLL_INTERVAL if _polled else 30.0
        link.next_api_poll = now + next_api_poll_interval

    # Periodic scan: request repair of records whose content arrived truncated.
    # Only runs when not actively syncing so it doesn't pile on top of a flood.
    if now >= link.next_incomplete_repair and not get_sync_progress().get('in_progress'):
        _incomplete = get_incomplete_record_uids()
        _repair_targets = [
            (scope, uid)
            for scope in ('bulletins', 'mail', 'channels')
            for uid in _incomplete.get(scope, [])
        ]
        _repair_peers = set(link.bbs_nodes)
        # Prune attempt counters for records that are no longer incomplete.
        _still = {(s, u) for s in ('bulletins', 'mail', 'channels') for u in _incomplete.get(s, [])}
        for _k in [k for k in link.incomplete_attempts if k not in _still]:
            link.incomplete_attempts.pop(_k, None)
        if _repair_targets and _repair_peers:
            from utils import _send_one_sync, get_hash_repair_pause_seconds
            from message_processing import _should_request_record
            from db_operations import reset_incomplete_record
            _peer_pool = sorted(_repair_peers)
            _sent = 0
            for _scope, _uid in _repair_targets:
                _attempts = link.incomplete_attempts.get((_scope, _uid), 0) + 1
                link.incomplete_attempts[(_scope, _uid)] = _attempts
                if _attempts >= _INCOMPLETE_RESET_AFTER:
                    reset_incomplete_record(_scope, _uid)
                    link.incomplete_attempts[(_scope, _uid)] = 0
                # Peer-agnostic guard is process-wide (message_processing._should_request_record),
                # so even if BOTH links reach this point in the same tick for the
                # same record, only one actually sends a request.
                if not _should_request_record(_scope, _uid):
                    continue
                _peer = random.choice(_peer_pool)
                _send_one_sync(
                    f"HASHMISS|{_scope}|{_uid}", _peer, interface,
                    pause_seconds=get_hash_repair_pause_seconds(interface),
                )
                _sent += 1
            if _sent:
                logging.info(f"[{link.name}] Incomplete-content repair: requested {_sent} record(s), one peer each")
        link.next_incomplete_repair = now + (45 if _repair_targets else 600)

    # Check/launch sync work frequently so manual triggers feel responsive.
    if now < link.next_node_sync_check:
        return

    refresh_peer_lists_from_config(
        config_path, interface, system_config,
        sync_section=link.sync_section, allow_section=link.allow_section,
        bbs_nodes_key=link.bbs_nodes_key, allowed_nodes_key=link.allowed_nodes_key,
        subscriber_nodes_key=link.subscriber_nodes_key,
    )
    sync_interval_minutes = read_sync_interval_minutes(config_path, default_minutes=5)
    system_config['sync_interval_minutes_runtime'] = sync_interval_minutes
    current_bbs_nodes = set(link.bbs_nodes)

    force_mismatch_check = bool(triggers.get('force_check'))
    if triggers.get('force_check'):
        system_config['sync_last_trigger_reason'] = 'force_check'

    peer_resync_node = triggers.get('peer_resync_node')
    # Peer-list membership first (see _link_for_node) -- the only way to
    # route a resync to the right link when multiple share a protocol
    # bucket (e.g. two MQTT links); home_network()==network_key remains a
    # valid fallback for a peer not yet in any link's configured lists.
    peer_belongs_to_link = bool(peer_resync_node) and (
        peer_resync_node in link.bbs_nodes
        or peer_resync_node in link.allowed_nodes
        or peer_resync_node in link.subscriber_nodes
        or home_network(peer_resync_node) == link.network_key
    )
    if peer_belongs_to_link:
        link.mail_synced_nodes.discard(peer_resync_node)
        link.bulletins_synced_nodes.discard(peer_resync_node)
        link.channels_synced_nodes.discard(peer_resync_node)
        link.profiles_synced_nodes.discard(peer_resync_node)
        link.game_synced_nodes.discard(peer_resync_node)
        link.synced_nodes.discard(peer_resync_node)
        link.pending_sync_nodes.discard(peer_resync_node)
        link.syncstate_advertisement_cache.pop(peer_resync_node, None)
        clear_peer_phases_complete(peer_resync_node)
        force_mismatch_check = True
        system_config['sync_last_trigger_reason'] = 'peer_resync'
        logging.info(f"[{link.name}] Peer full-resync requested for {peer_resync_node}; cleared from synced/game sets")

    # Best-candidate/record resolution requests go out on EVERY active link's
    # own peers (not just one) since the freshest copy could be on either
    # network — see the project plan's discussion of eventual consistency.
    zork_payload = triggers.get('resolve_zork_save')
    if zork_payload and current_bbs_nodes:
        try:
            user_id, game_id = zork_payload
            request_id = start_zork_save_best_candidate_resolution(user_id, game_id, list(current_bbs_nodes), interface)
            system_config['sync_last_trigger_reason'] = 'candidate_resolver'
            logging.info(f"[{link.name}] Started zork save best-candidate resolver request {request_id} for {user_id}:{game_id}")
        except Exception as exc:
            logging.warning(f"[{link.name}] Unable to start zork save resolver request: {exc}")

    record_payload = triggers.get('resolve_record')
    if record_payload and current_bbs_nodes:
        try:
            scope, key = record_payload
            from utils import _send_one_sync, get_hash_repair_pause_seconds, encode_scope, peers_all_support
            for peer_id in sorted(current_bbs_nodes):
                send_hash_request_to_bbs_nodes([peer_id], interface, scope=scope)
                _scope_wire = encode_scope(scope, peers_all_support([peer_id], 'scc'))
                _send_one_sync(f"HASHMISS|{_scope_wire}|{key}", peer_id, interface, pause_seconds=get_hash_repair_pause_seconds(interface))
            system_config['sync_last_trigger_reason'] = 'record_resolver'
            logging.info(f"[{link.name}] Queued per-record repair for {scope}:{key} to peers: {sorted(current_bbs_nodes)}")
        except Exception as exc:
            logging.warning(f"[{link.name}] Unable to start record resolver request: {exc}")

    sync_due = (link.last_schedule_epoch == 0) or (now >= (link.last_schedule_epoch + (sync_interval_minutes * 60)))

    if (triggers.get('manual') or sync_due) and not link.pending_sync_nodes:
        link.last_schedule_epoch = now
        local_counts = get_local_record_counts()
        if triggers.get('manual'):
            destinations = select_syncstate_peers_to_notify(current_bbs_nodes, local_counts, link.syncstate_advertisement_cache, now=now, force=True)
            if destinations:
                send_sync_state_to_bbs_nodes(local_counts, destinations, interface)
                send_have_to_bbs_nodes(get_local_node_id(), list(destinations), interface)
                send_peer_gossip_to_bbs_nodes(get_local_node_id(), list(destinations), interface)
            # Manual sync clears all phase sets so every phase reruns from scratch.
            link.mail_synced_nodes.clear()
            link.bulletins_synced_nodes.clear()
            link.channels_synced_nodes.clear()
            link.profiles_synced_nodes.clear()
            link.game_synced_nodes.clear()
            link.synced_nodes.clear()
            # clear_all_peer_phases_complete() is global (every peer, both
            # links) — matches original single-radio "manual sync resyncs
            # everything" semantics; with two links active, a manual
            # trigger clears BOTH radios' phase-complete records, so both
            # fully re-push on their own next due tick too.
            clear_all_peer_phases_complete()
            force_mismatch_check = True
            system_config['sync_last_trigger_reason'] = 'manual'
            logging.info(f"[{link.name}] Manual sync trigger received from web admin")
        else:
            destinations = select_syncstate_peers_to_notify(current_bbs_nodes, local_counts, link.syncstate_advertisement_cache, now=now, force=False)
            system_config['sync_last_trigger_reason'] = 'scheduled'
            if destinations:
                send_sync_state_to_bbs_nodes(local_counts, destinations, interface)
                send_have_to_bbs_nodes(get_local_node_id(), list(destinations), interface)
                send_peer_gossip_to_bbs_nodes(get_local_node_id(), list(destinations), interface)
                logging.info(
                    f"[{link.name}] Scheduled sync interval reached ({sync_interval_minutes} minutes); "
                    f"sent SYNCSTATE to {len(destinations)} peer(s)"
                )
            else:
                # Even if our local state hasn't changed, peers that are
                # behind on records need our SYNCSTATE so they can request
                # the data they're missing. Check the peer_sync_state table
                # and force-broadcast to any peer with fewer records in any scope.
                _check_scopes = [
                    ('bulletins', 1), ('mail', 2), ('channels', 3),
                    ('zork_saves', 4), ('profiles', 5), ('game_scores', 6),
                ]
                _peer_rows = {str(r[0]): r for r in get_peer_sync_states()}
                behind_peers = set()
                for _pid in current_bbs_nodes:
                    _row = _peer_rows.get(str(_pid))
                    if _row is None:
                        continue
                    for _sk, _idx in _check_scopes:
                        if int(local_counts.get(_sk, 0)) > int(_row[_idx] or 0):
                            behind_peers.add(str(_pid))
                            break
                if behind_peers:
                    _forced = select_syncstate_peers_to_notify(
                        list(behind_peers), local_counts,
                        link.syncstate_advertisement_cache, now=now, force=True
                    )
                    if _forced:
                        send_sync_state_to_bbs_nodes(local_counts, _forced, interface)
                        send_have_to_bbs_nodes(get_local_node_id(), _forced, interface)
                        send_peer_gossip_to_bbs_nodes(get_local_node_id(), list(_forced), interface)
                        logging.info(
                            f"[{link.name}] Scheduled sync interval reached ({sync_interval_minutes} minutes); "
                            f"state unchanged but {len(behind_peers)} peer(s) behind — "
                            f"sent SYNCSTATE to {behind_peers}"
                        )
                else:
                    logging.info(
                        f"[{link.name}] Scheduled sync interval reached ({sync_interval_minutes} minutes); "
                        "local state unchanged, skipping SYNCSTATE broadcast"
                    )

    # Automatic scheduled SYNCSTATE already prompts peers to request targeted repair.
    # Reserve proactive local HASHREQs for explicit force-check actions to avoid
    # duplicate manifest exchanges on both sides of the link.
    if force_mismatch_check and not link.pending_sync_nodes:
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
                system_config.setdefault('sync_mismatch_retry_at', {})[str(node)] = datetime.now(timezone.utc).isoformat()
            system_config['sync_last_trigger_reason'] = 'mismatch'
            logging.info(f"[{link.name}] Peer mismatch detected; requested hash manifests from nodes: {eligible}")

    next_run_epoch = int(link.last_schedule_epoch + (sync_interval_minutes * 60)) if link.last_schedule_epoch else int(now)
    system_config.setdefault('sync_next_run_epoch_by_link', {})[link.name] = next_run_epoch
    if link.name == 'primary':
        # Back-compat: existing web_admin.py reads this single top-level key.
        system_config['sync_next_run_epoch'] = next_run_epoch

    # A node needs a sync thread if it hasn't completed even P1 (mail) yet.
    new_nodes = current_bbs_nodes - link.mail_synced_nodes - link.pending_sync_nodes

    if new_nodes:
        logging.info(f"[{link.name}] Detected {len(new_nodes)} new BBS node(s) to sync: {new_nodes}")
        link.pending_sync_nodes.update(new_nodes)
        for new_node in sorted(new_nodes, key=str):
            t = threading.Thread(target=_run_sync_for_link, args=(link, new_node), daemon=True)
            t.start()

    link.next_node_sync_check = now + 5


def main():
    display_banner()
    args = init_cli_parser()
    config_file = None
    if args.config is not None:
        config_file = args.config
    system_config = initialize_config(config_file)

    merge_config(system_config, args)

    config_path = system_config.get('config_file', 'config.ini')

    # primary_iface may be None here -- get_interface gives up (and returns
    # None) after repeated connect failures instead of killing the process
    # (see config_init._open_interface). The primary link is still created
    # either way so _run_link_tick's normal "interface not alive -> trigger
    # reconnect" path (_is_interface_alive treats None as not-alive) picks it
    # up in the background, exactly like a radio that drops mid-session.
    # This is what lets a wedged primary radio NOT block the secondary radio
    # (or MQTT links, or the rest of the node) from running.
    primary_iface = get_interface(system_config)
    links = [RadioLink('primary', primary_iface, reconnect_fn=get_interface)]
    if primary_iface is not None:
        _apply_socket_timeout(primary_iface)
        refresh_peer_lists_from_config(config_path, primary_iface, system_config)
    else:
        logging.warning(
            "[primary] Failed to connect at startup; continuing without it. "
            "A background reconnect loop will keep retrying -- see _reconnect_link."
        )

    # Optional second radio for dual-radio bridge mode (see radio_link.py and
    # the project plan). Absent/disabled in every deployment that doesn't
    # opt in via [interface2] -- links stays a single-element list and every
    # loop below behaves exactly as it did before dual-radio support existed.
    # interface2_enabled is checked directly (not "secondary_iface is not
    # None") so a secondary radio that's configured but failed to connect
    # still gets its own RadioLink and background reconnect loop, same as
    # primary -- only a genuinely absent/disabled [interface2] skips it.
    interface2_enabled = bool(system_config.get('interface2_enabled'))
    secondary_iface = get_secondary_interface(system_config) if interface2_enabled else None
    if interface2_enabled:
        secondary_link = RadioLink(
            'secondary', secondary_iface,
            sync_section='sync2', allow_section='allow_list2',
            bbs_nodes_key='bbs_nodes2', allowed_nodes_key='allowed_nodes2',
            subscriber_nodes_key='subscriber_nodes2',
            reconnect_fn=get_secondary_interface,
        )
        if secondary_iface is not None:
            _apply_socket_timeout(secondary_iface)
            refresh_peer_lists_from_config(
                config_path, secondary_iface, system_config,
                sync_section='sync2', allow_section='allow_list2',
                bbs_nodes_key='bbs_nodes2', allowed_nodes_key='allowed_nodes2',
                subscriber_nodes_key='subscriber_nodes2',
            )
        else:
            logging.warning(
                "[secondary] Failed to connect at startup; continuing without it. "
                "A background reconnect loop will keep retrying -- see _reconnect_link."
            )
        # Appended either way -- a secondary that failed to connect still
        # gets a background reconnect loop (see _is_interface_alive/
        # _run_link_tick), same as the primary.
        links.append(secondary_link)
        logging.info(
            f"Dual-radio bridge mode active: primary={system_config['interface_type']}, "
            f"secondary={system_config['interface2_type']}"
        )

    # Optional MQTT internet-bridge links -- 0, 1, or many simultaneous
    # connections, each bridging to a different remote site over an
    # internet-connected broker (see config_init.get_mqtt_interfaces and the
    # project plan). Purely additive: absent [mqttN] sections leaves links
    # exactly as built above.
    for mqtt_entry in get_mqtt_interfaces(system_config):
        mqtt_iface = mqtt_entry['interface']
        mqtt_link = RadioLink(
            mqtt_entry['name'], mqtt_iface,
            sync_section=mqtt_entry['sync_section'], allow_section=mqtt_entry['allow_section'],
            bbs_nodes_key=mqtt_entry['bbs_nodes_key'], allowed_nodes_key=mqtt_entry['allowed_nodes_key'],
            subscriber_nodes_key=mqtt_entry['subscriber_nodes_key'],
            reconnect_fn=lambda cfg, _name=mqtt_entry['name']: get_mqtt_interface_by_name(cfg, _name),
        )
        refresh_peer_lists_from_config(
            config_path, mqtt_iface, system_config,
            sync_section=mqtt_entry['sync_section'], allow_section=mqtt_entry['allow_section'],
            bbs_nodes_key=mqtt_entry['bbs_nodes_key'], allowed_nodes_key=mqtt_entry['allowed_nodes_key'],
            subscriber_nodes_key=mqtt_entry['subscriber_nodes_key'],
        )
        links.append(mqtt_link)
        logging.info(f"[{mqtt_entry['name']}] MQTT bridge link active: {mqtt_iface.protocol_name}")

    global _active_links
    _active_links = links

    trigger_path = get_manual_sync_trigger_path()
    force_check_trigger_path = get_force_check_trigger_path()
    peer_resync_trigger_path = get_peer_resync_trigger_path()
    zork_save_resolve_trigger_path = get_zork_save_resolve_trigger_path()
    record_resolve_trigger_path = get_record_resolve_trigger_path()
    link_reconnect_trigger_path = get_link_reconnect_trigger_path()
    publish_mqtt_status(links, write_runtime_diagnostics_snapshot(links, system_config))
    persist_mesh_clients(links)

    if len(links) > 1:
        link_descriptions = ", ".join(f"{l.name}={l.protocol_name}" for l in links)
        logging.info(f"Bacon BBS is running on {len(links)} active links (bridge mode): {link_descriptions}...")
    else:
        logging.info(f"Bacon BBS is running on {system_config['interface_type']} interface...")

    initialize_database()
    install_connection_log_handler()
    run_op_log_backfill()
    _start_main_loop_watchdog()

    def receive_packet(packet, interface):
        on_receive(packet, interface)

    pub.subscribe(receive_packet, system_config['mqtt_topic'])

    for link in links:
        start_receive = getattr(link.interface, 'start_receive', None)
        if callable(start_receive):
            start_receive()

    # Initialize and start JS8Call Client if configured. Bridges to one
    # physical audio/serial device, so it's tied to the primary radio only
    # regardless of dual-radio bridge mode.
    js8call_client = JS8CallClient(links[0].interface)
    js8call_client.logger = js8call_logger
    global _js8call_client
    _js8call_client = js8call_client

    if js8call_client.db_conn:
        js8call_client.connect()

    try:
        next_diagnostics_write = 0.0
        # DB maintenance: prune unbounded tables + WAL checkpoint on a slow cadence,
        # VACUUM even less often. First pass deferred so startup isn't slowed.
        from db_operations import get_maintenance_config, run_db_maintenance
        _maint_cfg = get_maintenance_config()
        next_maintenance = time.time() + 300.0  # first pass 5 min after start
        next_vacuum = time.time() + max(1, _maint_cfg['vacuum_interval_hours']) * 3600.0
        # How long a requester waits for an API-gateway reply before timing out:
        # the gateway's own request_timeout plus generous mesh round-trip slack.
        from utils import _config_int as _cfg_int
        _apigw_wait_timeout = _cfg_int('gateway', 'request_timeout', 20) + 90

        last_manual_trigger_mtime = 0.0
        last_force_check_trigger_mtime = 0.0
        last_peer_resync_trigger_mtime = 0.0
        last_zork_save_resolve_trigger_mtime = 0.0
        last_record_resolve_trigger_mtime = 0.0
        last_link_reconnect_trigger_mtime = 0.0
        system_config['sync_last_trigger_reason'] = 'scheduled'
        system_config['sync_interval_minutes_runtime'] = int(system_config.get('sync_interval_minutes', 5))
        system_config['sync_next_run_epoch'] = int(time.time())
        system_config['sync_mismatch_retry_at'] = {}

        for link in links:
            _seed_link_from_db(link)

        while True:
            global _last_main_loop_tick
            _last_main_loop_tick = time.time()
            now = time.time()

            # Gateway side: drop retained responses we no longer need to refill.
            # Global cleanup (not tied to any one interface).
            try:
                from utils import expire_sent_api_responses
                expire_sent_api_responses(_apigw_wait_timeout)
            except Exception:
                pass

            # Expire API-gateway requests that never got a response (gateway
            # offline or response lost on the lossy link) and tell the waiting
            # user. The pending-request table is global (not per-interface),
            # but the reply must go out on whichever link actually owns that
            # user's network.
            try:
                from utils import expire_api_requests, send_message as _send_user_msg
                for _rid, _uid in expire_api_requests(_apigw_wait_timeout):
                    _reply_link = _link_for_node(links, _uid)
                    _send_user_msg("No gateway response (timed out). Try again later.", _uid, _reply_link.interface)
            except Exception:
                pass

            # Refresh diagnostics snapshot (5 s while syncing, 30 s otherwise)
            # -- also republishes MQTT status telemetry and persists each
            # link's node roster to the DB on the same cadence, see
            # publish_mqtt_status / persist_mesh_clients.
            if now >= next_diagnostics_write:
                publish_mqtt_status(links, write_runtime_diagnostics_snapshot(links, system_config))
                persist_mesh_clients(links)
                sync_progress = get_sync_progress()
                next_diagnostics_write = now + (5 if sync_progress.get('in_progress') else 30)

            # Periodic DB maintenance: keep unbounded tables + WAL bounded so an
            # unattended node never fills the SD card. Only runs when idle so it
            # never competes with an active sync burst. VACUUM on a slower cadence.
            if now >= next_maintenance and not get_sync_progress().get('in_progress'):
                do_vacuum = now >= next_vacuum
                try:
                    _m = run_db_maintenance(do_vacuum=do_vacuum)
                    if any(v for k, v in _m.items() if k.endswith('_deleted')) or _m.get('vacuumed'):
                        logging.info(
                            f"DB maintenance: sync_tx-{_m['sync_transmissions_deleted']} "
                            f"op_log-{_m['op_log_deleted']} sessions-{_m['sync_session_history_deleted']} "
                            f"tombstones-{_m['tombstones_deleted']} vacuum={_m['vacuumed']}"
                        )
                except Exception as exc:
                    logging.warning(f"DB maintenance pass failed: {exc}")
                # Enforce the optional GUI-set DB size cap (0 = disabled), once
                # per active radio so each network's peers get the prune
                # notice. Deletes the oldest content via the tombstoned delete
                # path so the prune propagates to every node identically.
                for link in links:
                    if link.reconnecting:
                        continue
                    try:
                        from db_operations import enforce_db_size_cap
                        _cap = enforce_db_size_cap(link.bbs_nodes, link.interface)
                        if _cap.get('deleted'):
                            logging.info(
                                f"[{link.name}] DB size cap: deleted {_cap['deleted']} oldest record(s); "
                                f"on-disk now {_cap['size_bytes']} bytes"
                            )
                    except Exception as exc:
                        logging.warning(f"[{link.name}] DB size-cap pass failed: {exc}")
                if do_vacuum:
                    next_vacuum = now + max(1, _maint_cfg['vacuum_interval_hours']) * 3600.0
                next_maintenance = now + max(1, _maint_cfg['interval_minutes']) * 60.0

            # --- Trigger files (web admin actions) — read once per tick,
            # shared across every link; each is applied inside
            # _run_link_tick to whichever link(s) it's relevant to. ---
            triggers = {
                'manual': False, 'force_check': False, 'peer_resync_node': None,
                'resolve_zork_save': None, 'resolve_record': None,
            }
            try:
                if os.path.exists(trigger_path):
                    trigger_mtime = os.path.getmtime(trigger_path)
                    if trigger_mtime > last_manual_trigger_mtime:
                        triggers['manual'] = True
                        last_manual_trigger_mtime = trigger_mtime
                        os.remove(trigger_path)
            except Exception as exc:
                logging.debug(f"Unable to process manual sync trigger: {exc}")

            try:
                if os.path.exists(force_check_trigger_path):
                    trigger_mtime = os.path.getmtime(force_check_trigger_path)
                    if trigger_mtime > last_force_check_trigger_mtime:
                        triggers['force_check'] = True
                        last_force_check_trigger_mtime = trigger_mtime
                        os.remove(force_check_trigger_path)
                        logging.info("Force mismatch check requested from web admin")
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
                            triggers['peer_resync_node'] = _peer_id
            except Exception as exc:
                logging.debug(f"Unable to process peer resync trigger: {exc}")

            try:
                if os.path.exists(zork_save_resolve_trigger_path):
                    trigger_mtime = os.path.getmtime(zork_save_resolve_trigger_path)
                    if trigger_mtime > last_zork_save_resolve_trigger_mtime:
                        last_zork_save_resolve_trigger_mtime = trigger_mtime
                        with open(zork_save_resolve_trigger_path, 'r', encoding='utf-8') as _f:
                            _raw = _f.read().strip()
                        os.remove(zork_save_resolve_trigger_path)
                        if _raw:
                            try:
                                payload = json.loads(_raw)
                                user_id = str(payload.get('user_id', '')).strip()
                                game_id = str(payload.get('game_id', '')).strip()
                                if user_id and game_id:
                                    triggers['resolve_zork_save'] = (user_id, game_id)
                            except Exception as exc:
                                logging.warning(f"Unable to parse zork save resolver trigger: {exc}")
            except Exception as exc:
                logging.debug(f"Unable to process zork save resolver trigger: {exc}")

            try:
                if os.path.exists(record_resolve_trigger_path):
                    trigger_mtime = os.path.getmtime(record_resolve_trigger_path)
                    if trigger_mtime > last_record_resolve_trigger_mtime:
                        last_record_resolve_trigger_mtime = trigger_mtime
                        with open(record_resolve_trigger_path, 'r', encoding='utf-8') as _f:
                            _raw = _f.read().strip()
                        os.remove(record_resolve_trigger_path)
                        if _raw:
                            try:
                                payload = json.loads(_raw)
                                scope = str(payload.get('scope', '')).strip().lower()
                                key = str(payload.get('key', '')).strip()
                                if scope and key:
                                    triggers['resolve_record'] = (scope, key)
                            except Exception as exc:
                                logging.warning(f"Unable to parse record resolver trigger: {exc}")
            except Exception as exc:
                logging.debug(f"Unable to process record resolve trigger: {exc}")

            # Operator-requested reconnect of ONE link (or all), from the web
            # admin's Links & Services card. Deliberately just sets the same
            # reconnect_needed flag the automatic liveness check uses, so the
            # existing _reconnect_link path handles it -- close, retry with
            # backoff, rejoin -- without restarting the process or disturbing
            # any other link.
            try:
                if os.path.exists(link_reconnect_trigger_path):
                    trigger_mtime = os.path.getmtime(link_reconnect_trigger_path)
                    if trigger_mtime > last_link_reconnect_trigger_mtime:
                        last_link_reconnect_trigger_mtime = trigger_mtime
                        with open(link_reconnect_trigger_path, 'r', encoding='utf-8') as _f:
                            _target = _f.read().strip()
                        os.remove(link_reconnect_trigger_path)
                        apply_link_reconnect_request(links, _target)
            except Exception as exc:
                logging.debug(f"Unable to process link reconnect trigger: {exc}")

            for link in links:
                _run_link_tick(
                    link, system_config=system_config, config_path=config_path,
                    triggers=triggers, now=now,
                )

            time.sleep(1)

    except KeyboardInterrupt:
        logging.info("Shutting down the server...")
        for link in links:
            try:
                link.interface.close()
            except Exception:
                pass
        if js8call_client.connected:
            js8call_client.close()

if __name__ == "__main__":
    main()
