import configparser
import base64
import hashlib
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from app_paths import resolve_app_path
from op_log import ensure_op_log_schema, try_dual_write, backfill_op_log as _backfill_op_log

from meshtastic import BROADCAST_NUM

from utils import (
    send_bulletin_to_bbs_nodes,
    send_delete_bulletin_to_bbs_nodes,
    send_delete_channel_comment_to_bbs_nodes,
    send_delete_mail_to_bbs_nodes,
    send_delete_zork_save_to_bbs_nodes,
    send_mail_to_bbs_nodes, send_message, send_channel_to_bbs_nodes,
    send_channel_comment_to_bbs_nodes,
    send_sync_state_to_bbs_nodes,
    send_profile_to_bbs_nodes,
    send_game_score_to_bbs_nodes,
    send_zork_save_to_bbs_nodes,
    get_full_sync_delay_ms,
    get_hash_chunk_pause_seconds,
    is_zork_save_sync_enabled,
    compact_channel_manifest_key,
)


thread_local = threading.local()
_sync_progress_lock = threading.Lock()
_sync_progress = {
    'in_progress': False,
    'progress_percent': 0,
    'completed_items': 0,
    'total_items': 0,
    'remaining_items': 0,
    'current_phase': 'never_run',
    'target_nodes': [],
    'started_at': '',
    'last_updated_at': '',
    'last_result': 'No sync run yet',
}
_connection_log_handler_lock = threading.Lock()
_connection_log_emit_state = threading.local()
_pending_continuation_lock = threading.Lock()
_pending_bulletin_continuations = {}
_pending_mail_continuations = {}
_pending_channel_comment_continuations = {}
_pending_bulletin_expected_lengths = {}
_pending_mail_expected_lengths = {}
_pending_channel_comment_expected_lengths = {}
_PENDING_CONTINUATION_MAX_AGE_SECONDS = 1800

# Node identity — set once at startup by server.py after interface init
_local_node_id: Optional[str] = None


def set_local_node_id(node_id: str) -> None:
    """Store this node's own ID so provenance can be stamped on locally created records."""
    global _local_node_id
    _local_node_id = str(node_id)


def get_local_node_id() -> Optional[str]:
    """Return this node's ID as set at startup, or None if not yet resolved."""
    return _local_node_id


def run_op_log_backfill() -> int:
    """Backfill op_log with locally-originated records that predate Phase 2.

    Must be called after set_local_node_id() has been resolved.  Safe to call
    multiple times — idempotent.  Commits the changes and returns the count of
    new entries created.
    """
    nid = _local_node_id
    if not nid:
        logging.debug('run_op_log_backfill: local_node_id not set yet, skipping')
        return 0
    conn = get_db_connection()
    c = conn.cursor()
    count = _backfill_op_log(c, nid)
    conn.commit()
    return count


class ConnectionEventsLogHandler(logging.Handler):
    def __init__(self, db_path: str):
        super().__init__(level=logging.INFO)
        self.db_path = db_path

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_connection_log_emit_state, 'active', False):
            return
        try:
            _connection_log_emit_state.active = True
            message = self.format(record)
            level_name = str(record.levelname or 'INFO').strip().lower()
            source_name = str(record.name or 'root').strip()[:48]
            _write_connection_event_direct(
                db_path=self.db_path,
                sender_num=None,
                sender_node_id=None,
                sender_short_name=source_name,
                to_id=None,
                message_type=level_name,
                event_text=message,
            )
        except Exception:
            pass
        finally:
            _connection_log_emit_state.active = False


def _update_sync_progress(**kwargs) -> None:
    with _sync_progress_lock:
        _sync_progress.update(kwargs)


def get_sync_progress() -> dict:
    with _sync_progress_lock:
        return dict(_sync_progress)


def _prune_pending_continuations(buffer_store: dict) -> None:
    now = time.time()
    stale_keys = [
        unique_id for unique_id, payload in buffer_store.items()
        if now - float(payload.get('updated_at', now)) > _PENDING_CONTINUATION_MAX_AGE_SECONDS
    ]
    for unique_id in stale_keys:
        buffer_store.pop(unique_id, None)


def _queue_pending_continuation(buffer_store: dict, unique_id: str, char_offset: int, chunk: str) -> None:
    with _pending_continuation_lock:
        _prune_pending_continuations(buffer_store)
        payload = buffer_store.setdefault(str(unique_id), {'updated_at': time.time(), 'chunks': {}})
        payload['updated_at'] = time.time()
        payload['chunks'][int(char_offset)] = str(chunk)


def _queue_pending_expected_length(buffer_store: dict, unique_id: str, expected_length: int) -> None:
    with _pending_continuation_lock:
        _prune_pending_continuations(buffer_store)
        buffer_store[str(unique_id)] = {
            'updated_at': time.time(),
            'expected_length': max(0, int(expected_length)),
        }


def _pop_pending_expected_length(buffer_store: dict, unique_id: str) -> Optional[int]:
    with _pending_continuation_lock:
        _prune_pending_continuations(buffer_store)
        payload = buffer_store.pop(str(unique_id), None)
        if not payload:
            return None
        try:
            return max(0, int(payload.get('expected_length', 0)))
        except Exception:
            return None


def _normalize_expected_content_length(content: str, expected_length: Optional[int]) -> int:
    actual_length = len(str(content or ''))
    if expected_length is None:
        return actual_length
    return max(actual_length, int(expected_length))


def _content_complete_flag(content: str, expected_length: Optional[int]) -> int:
    normalized_expected = _normalize_expected_content_length(content, expected_length)
    return 1 if len(str(content or '')) >= normalized_expected else 0


def _flush_pending_continuations(buffer_store: dict, unique_id: str, current_content: str) -> tuple[str, bool]:
    changed = False
    while True:
        with _pending_continuation_lock:
            _prune_pending_continuations(buffer_store)
            payload = buffer_store.get(str(unique_id))
            if not payload:
                return current_content, changed
            ready_offset = None
            for offset in sorted(payload.get('chunks', {}).keys()):
                if int(offset) <= len(current_content):
                    ready_offset = int(offset)
                    break
            if ready_offset is None:
                return current_content, changed
            chunk = payload['chunks'].pop(ready_offset)
            payload['updated_at'] = time.time()
            if not payload['chunks']:
                buffer_store.pop(str(unique_id), None)

        new_content, status = _merge_continuation_content(current_content, ready_offset, chunk)
        if status == 'applied':
            current_content = new_content
            changed = True


def _merge_continuation_content(current_content: str, char_offset: Optional[int], additional_content: str) -> tuple[str, str]:
    if char_offset is None:
        new_content = current_content + additional_content
        return new_content, 'applied' if new_content != current_content else 'duplicate'
    if char_offset > len(current_content):
        return current_content, 'gap'
    overlap = current_content[char_offset:char_offset + len(additional_content)]
    if overlap == additional_content and len(current_content) >= (char_offset + len(additional_content)):
        return current_content, 'duplicate'
    suffix = current_content[char_offset + len(additional_content):]
    return current_content[:char_offset] + additional_content + suffix, 'applied'


def _apply_continuation_update(table_name: str, unique_id: str, char_offset: Optional[int], additional_content: str,
                               buffer_store: dict, label: str) -> None:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(f"SELECT content, expected_content_length FROM {table_name} WHERE unique_id = ?", (unique_id,))
    row = c.fetchone()
    if row is None:
        if char_offset is not None:
            _queue_pending_continuation(buffer_store, unique_id, char_offset, additional_content)
            logging.info(f"Buffered {label} continuation before base record unique_id={unique_id} offset={char_offset}")
        else:
            logging.warning(f"{label.upper()}CONT received for unknown unique_id={unique_id}; ignored")
        return

    current_content = str(row[0] or '')
    expected_length = row[1] if len(row) > 1 else None
    new_content, status = _merge_continuation_content(current_content, char_offset, additional_content)
    if status == 'gap' and char_offset is not None:
        _queue_pending_continuation(buffer_store, unique_id, char_offset, additional_content)
        logging.info(
            f"Buffered out-of-order {label} continuation unique_id={unique_id}; offset {char_offset}, have {len(current_content)}"
        )
        return

    current_content = new_content
    current_content, flushed = _flush_pending_continuations(buffer_store, unique_id, current_content)
    if status == 'duplicate' and not flushed:
        logging.info(f"Duplicate {label} continuation ignored for unique_id={unique_id} offset={char_offset}")
        return

    normalized_expected_length = _normalize_expected_content_length(current_content, expected_length)
    c.execute(
        f"UPDATE {table_name} SET content = ?, expected_content_length = ?, content_complete = ? WHERE unique_id = ?",
        (
            current_content,
            normalized_expected_length,
            _content_complete_flag(current_content, normalized_expected_length),
            unique_id,
        ),
    )
    conn.commit()
    logging.info(f"Applied continuation content to {label} unique_id={unique_id}")


def flush_pending_bulletin_continuations(unique_id: str) -> None:
    _apply_continuation_update('bulletins', unique_id, None, '', _pending_bulletin_continuations, 'bulletin')


def flush_pending_mail_continuations(unique_id: str) -> None:
    _apply_continuation_update('mail', unique_id, None, '', _pending_mail_continuations, 'mail')


def flush_pending_channel_comment_continuations(unique_id: str) -> None:
    _apply_continuation_update('channel_comments', unique_id, None, '', _pending_channel_comment_continuations, 'channel comment')


def _apply_expected_content_length(table_name: str, unique_id: str, expected_length: int, pending_store: dict, label: str) -> None:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(f"SELECT content FROM {table_name} WHERE unique_id = ?", (unique_id,))
    row = c.fetchone()
    normalized_expected = max(0, int(expected_length))
    if row is None:
        _queue_pending_expected_length(pending_store, unique_id, normalized_expected)
        logging.info(f"Buffered {label} content metadata before base record unique_id={unique_id} expected={normalized_expected}")
        return

    current_content = str(row[0] or '')
    normalized_expected = _normalize_expected_content_length(current_content, normalized_expected)
    c.execute(
        f"UPDATE {table_name} SET expected_content_length = ?, content_complete = ? WHERE unique_id = ?",
        (
            normalized_expected,
            _content_complete_flag(current_content, normalized_expected),
            unique_id,
        ),
    )
    conn.commit()


def apply_bulletin_expected_content_length(unique_id: str, expected_length: int) -> None:
    _apply_expected_content_length('bulletins', unique_id, expected_length, _pending_bulletin_expected_lengths, 'bulletin')


def apply_mail_expected_content_length(unique_id: str, expected_length: int) -> None:
    _apply_expected_content_length('mail', unique_id, expected_length, _pending_mail_expected_lengths, 'mail')


def apply_channel_comment_expected_content_length(unique_id: str, expected_length: int) -> None:
    _apply_expected_content_length('channel_comments', unique_id, expected_length, _pending_channel_comment_expected_lengths, 'channel comment')


def _flush_pending_expected_content_length(table_name: str, unique_id: str, pending_store: dict, label: str) -> None:
    pending_expected = _pop_pending_expected_length(pending_store, unique_id)
    if pending_expected is None:
        return
    _apply_expected_content_length(table_name, unique_id, pending_expected, pending_store, label)


def get_database_path() -> str:
    return resolve_app_path(os.getenv('BBS_DB_PATH'), 'bulletins.db')


def make_channel_manifest_key(name: str, url: str) -> str:
    raw_key = f"{str(name or '')}\x1f{str(url or '')}".encode('utf-8')
    return base64.urlsafe_b64encode(raw_key).decode('ascii').rstrip('=')


def decode_channel_manifest_key(manifest_key: str) -> Optional[tuple[str, str]]:
    normalized = str(manifest_key or '')
    padded = normalized + ('=' * ((4 - len(normalized) % 4) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8')
    except Exception:
        return None
    if '\x1f' not in decoded:
        return None
    name, url = decoded.split('\x1f', 1)
    return name, url


def get_config_path() -> str:
    return os.getenv('BBS_CONFIG_PATH', 'config.ini')


def _write_connection_event_direct(
    db_path: str,
    sender_num: Optional[int],
    sender_node_id: Optional[str],
    sender_short_name: Optional[str],
    to_id: Optional[int],
    message_type: str,
    event_text: str,
) -> None:
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS connection_events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   event_time TEXT NOT NULL,
                   sender_num TEXT,
                   sender_node_id TEXT,
                   sender_short_name TEXT,
                   to_id TEXT,
                   message_type TEXT NOT NULL,
                   event_text TEXT NOT NULL
               );'''
        )
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            '''INSERT INTO connection_events
               (event_time, sender_num, sender_node_id, sender_short_name, to_id, message_type, event_text)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (
                now,
                str(sender_num) if sender_num is not None else None,
                sender_node_id,
                sender_short_name or '',
                str(to_id) if to_id is not None else None,
                message_type,
                event_text,
            )
        )
        _prune_connection_events(conn, _get_max_connection_log_rows())
        conn.commit()
    finally:
        conn.close()


def install_connection_log_handler(db_path: Optional[str] = None) -> None:
    handler_db_path = db_path or get_database_path()
    root_logger = logging.getLogger()
    with _connection_log_handler_lock:
        existing = None
        for handler in list(root_logger.handlers):
            if isinstance(handler, ConnectionEventsLogHandler):
                existing = handler
                break
        if existing is not None and existing.db_path == handler_db_path:
            return
        if existing is not None:
            root_logger.removeHandler(existing)
        new_handler = ConnectionEventsLogHandler(handler_db_path)
        new_handler.setFormatter(logging.Formatter('%(message)s'))
        root_logger.addHandler(new_handler)


def remove_connection_log_handler() -> None:
    root_logger = logging.getLogger()
    with _connection_log_handler_lock:
        for handler in list(root_logger.handlers):
            if isinstance(handler, ConnectionEventsLogHandler):
                root_logger.removeHandler(handler)


def _ensure_zork_saves_table() -> None:
    conn = get_db_connection()
    c = conn.cursor()
    # Check if table already exists
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='zork_saves'")
    table_exists = c.fetchone() is not None
    if table_exists:
        # Migrate from old single-PK schema (no game_id column) if needed
        c.execute("PRAGMA table_info(zork_saves)")
        columns = {row[1] for row in c.fetchall()}
        if 'game_id' not in columns:
            c.execute("ALTER TABLE zork_saves RENAME TO zork_saves_old")
            c.execute('''CREATE TABLE zork_saves (
                            user_id TEXT NOT NULL,
                            game_id TEXT NOT NULL DEFAULT 'zork1',
                            save_data BLOB NOT NULL,
                            updated_at TEXT NOT NULL,
                            PRIMARY KEY (user_id, game_id)
                        );''')
            c.execute('''INSERT INTO zork_saves (user_id, game_id, save_data, updated_at)
                         SELECT user_id, 'zork1', save_data, updated_at FROM zork_saves_old''')
            c.execute("DROP TABLE zork_saves_old")
            conn.commit()
    else:
        c.execute('''CREATE TABLE zork_saves (
                        user_id TEXT NOT NULL,
                        game_id TEXT NOT NULL DEFAULT 'zork1',
                        save_data BLOB NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (user_id, game_id)
                    );''')
        conn.commit()


_cached_max_connection_log_rows: Optional[int] = None


def _get_max_connection_log_rows() -> int:
    """Read connection_log_max_rows from config.ini once and cache it."""
    global _cached_max_connection_log_rows
    if _cached_max_connection_log_rows is None:
        cfg = configparser.ConfigParser()
        cfg.read(get_config_path())
        _cached_max_connection_log_rows = cfg.getint('bbs', 'connection_log_max_rows', fallback=5000)
    return _cached_max_connection_log_rows


def _prune_connection_events(conn, max_rows: int) -> None:
    conn.execute(
        '''DELETE FROM connection_events WHERE id NOT IN (
               SELECT id FROM connection_events ORDER BY id DESC LIMIT ?
           )''',
        (max_rows,),
    )


def _ensure_connection_events_table() -> None:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS connection_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_time TEXT NOT NULL,
                    sender_num TEXT,
                    sender_node_id TEXT,
                    sender_short_name TEXT,
                    to_id TEXT,
                    message_type TEXT NOT NULL,
                    event_text TEXT NOT NULL
                );''')
    conn.commit()

def get_db_connection():
    if not hasattr(thread_local, 'connection'):
        thread_local.connection = sqlite3.connect(get_database_path())
    return thread_local.connection

def initialize_database():
    with _pending_continuation_lock:
        _pending_bulletin_continuations.clear()
        _pending_mail_continuations.clear()
        _pending_channel_comment_continuations.clear()
        _pending_bulletin_expected_lengths.clear()
        _pending_mail_expected_lengths.clear()
        _pending_channel_comment_expected_lengths.clear()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS bulletins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    board TEXT NOT NULL,
                    sender_short_name TEXT NOT NULL,
                    date TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    content TEXT NOT NULL,
                    unique_id TEXT NOT NULL,
                    local_only INTEGER NOT NULL DEFAULT 0,
                    expected_content_length INTEGER,
                    content_complete INTEGER NOT NULL DEFAULT 1,
                    source_node_id TEXT,
                    source_timestamp TEXT,
                    received_at TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS mail (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    sender_short_name TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    date TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    content TEXT NOT NULL,
                    unique_id TEXT NOT NULL,
                    expected_content_length INTEGER,
                    content_complete INTEGER NOT NULL DEFAULT 1,
                    source_node_id TEXT,
                    source_timestamp TEXT,
                    received_at TEXT
                );''')
    c.execute('''CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    local_only INTEGER NOT NULL DEFAULT 0
                );''')
    c.execute('''CREATE TABLE IF NOT EXISTS channel_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL,
                    sender_short_name TEXT NOT NULL,
                    date TEXT NOT NULL,
                    content TEXT NOT NULL,
                    unique_id TEXT NOT NULL DEFAULT '',
                    expected_content_length INTEGER,
                    content_complete INTEGER NOT NULL DEFAULT 1,
                    source_node_id TEXT,
                    source_timestamp TEXT,
                    received_at TEXT,
                    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
                );''')
    c.execute('''CREATE TABLE IF NOT EXISTS zork_saves (
                    user_id TEXT NOT NULL,
                    game_id TEXT NOT NULL DEFAULT 'zork1',
                    save_data BLOB NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, game_id)
                );''')
    # Phase 2 store-and-forward: a gateway persists API responses here keyed by
    # the requester node so an intermittently-connected node (e.g. a sleeping
    # Pico) can retrieve them later via APIPOLL. Not synced between BBS nodes.
    c.execute('''CREATE TABLE IF NOT EXISTS api_mailbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rid TEXT NOT NULL,
                    requester_node_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT '200',
                    body TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    delivered_at TEXT
                );''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_api_mailbox_node
                    ON api_mailbox (requester_node_id, delivered);''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    short_name TEXT NOT NULL DEFAULT '',
                    long_name TEXT NOT NULL DEFAULT '',
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    messages_sent INTEGER NOT NULL DEFAULT 0,
                    bio TEXT NOT NULL DEFAULT ''
                );''')
    c.execute('''CREATE TABLE IF NOT EXISTS game_scores (
                    user_id TEXT NOT NULL,
                    game_id TEXT NOT NULL,
                    short_name TEXT NOT NULL DEFAULT '',
                    score INTEGER NOT NULL DEFAULT 0,
                    max_score INTEGER NOT NULL DEFAULT 0,
                    moves INTEGER NOT NULL DEFAULT 0,
                    achieved_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, game_id)
                );''')
    c.execute('''CREATE TABLE IF NOT EXISTS connection_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_time TEXT NOT NULL,
                    sender_num TEXT,
                    sender_node_id TEXT,
                    sender_short_name TEXT,
                    to_id TEXT,
                    message_type TEXT NOT NULL,
                    event_text TEXT NOT NULL
                );''')
    # Durable "who's in range" roster -- every radio/MQTT link's live
    # interface.nodes dict is transient (rebuilt from scratch on every
    # reconnect), so this is the only place that survives a restart. Keyed
    # per-link (not just per-node) since the same physical node could in
    # principle be seen on more than one active link. 'last_seen' is
    # refreshed on every periodic sweep a node is still present in
    # interface.nodes (server.py's persist_mesh_clients) -- deliberately
    # OUR OWN sweep timestamp rather than the transport's self-reported
    # recency, since only Meshtastic provides that (last_heard_epoch), so
    # this is what makes "still in range" comparable across all transports.
    c.execute('''CREATE TABLE IF NOT EXISTS mesh_clients (
                    link_name TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    node_num TEXT,
                    protocol TEXT NOT NULL DEFAULT '',
                    short_name TEXT NOT NULL DEFAULT '',
                    long_name TEXT NOT NULL DEFAULT '',
                    hw_model TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    battery_level INTEGER,
                    last_heard_epoch INTEGER,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    PRIMARY KEY (link_name, node_id)
                );''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_mesh_clients_last_seen ON mesh_clients (last_seen);')
    c.execute('''CREATE TABLE IF NOT EXISTS peer_sync_state (
                    peer_node_id TEXT PRIMARY KEY,
                    bulletins INTEGER NOT NULL DEFAULT 0,
                    mail INTEGER NOT NULL DEFAULT 0,
                    channels INTEGER NOT NULL DEFAULT 0,
                    zork_saves INTEGER NOT NULL DEFAULT 0,
                    profiles INTEGER NOT NULL DEFAULT 0,
                    game_scores INTEGER NOT NULL DEFAULT 0,
                    bulletins_hash TEXT NOT NULL DEFAULT '',
                    mail_hash TEXT NOT NULL DEFAULT '',
                    channels_hash TEXT NOT NULL DEFAULT '',
                    zork_saves_hash TEXT NOT NULL DEFAULT '',
                    profiles_hash TEXT NOT NULL DEFAULT '',
                    game_scores_hash TEXT NOT NULL DEFAULT '',
                    reported_at TEXT NOT NULL,
                    phases_complete TEXT NOT NULL DEFAULT '',
                    tombstones INTEGER NOT NULL DEFAULT -1,
                    proto_v INTEGER NOT NULL DEFAULT 0,
                    caps TEXT NOT NULL DEFAULT '',
                    caps_observed_at TEXT NOT NULL DEFAULT ''
                );''')
    # Forward-compat migration: ensure newer columns exist on legacy DBs.
    c.execute("PRAGMA table_info(peer_sync_state)")
    _pss_cols = {row[1] for row in c.fetchall()}
    if 'proto_v' not in _pss_cols:
        c.execute("ALTER TABLE peer_sync_state ADD COLUMN proto_v INTEGER NOT NULL DEFAULT 0")
    if 'caps' not in _pss_cols:
        c.execute("ALTER TABLE peer_sync_state ADD COLUMN caps TEXT NOT NULL DEFAULT ''")
    if 'caps_observed_at' not in _pss_cols:
        c.execute("ALTER TABLE peer_sync_state ADD COLUMN caps_observed_at TEXT NOT NULL DEFAULT ''")
    c.execute('''CREATE TABLE IF NOT EXISTS deleted_sync_tombstones (
                    tombstone_key TEXT PRIMARY KEY,
                    deleted_at TEXT NOT NULL
                );''')
    c.execute('''CREATE TABLE IF NOT EXISTS sync_transmissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transmission_time TEXT NOT NULL,
                    frame_type TEXT NOT NULL,
                    destination_node_id TEXT,
                    direction TEXT NOT NULL DEFAULT 'tx',
                    frame_size_bytes INTEGER,
                    is_continuation INTEGER NOT NULL DEFAULT 0
                );''')
    ensure_op_log_schema(c)
    _ensure_content_status_columns(c)
    _ensure_channel_comment_sync_columns(c)
    _ensure_local_only_columns(c)
    _ensure_accounts_tables(c)
    _dedupe_channels_and_create_unique_index(c)
    _dedupe_messages_and_create_unique_indexes(c)
    conn.commit()
    print(f"Database schema initialized at {get_database_path()}.")


def _ensure_accounts_tables(cursor) -> None:
    """Multi-device user accounts: a human can link several physical node
    ids (across protocols) to one account, with a shared display alias.
    Purely additive/opt-in -- user_profiles (numeric-node-id-keyed) is
    untouched and keeps working exactly as before for any node that never
    links to an account. These new tables key on the STABLE STRING node id
    ('!xxxxxxxx' for Meshtastic, the public key/hex prefix for MeshCore),
    never the numeric node number user_profiles uses -- MeshCore's numeric
    id is synthesized from just the first 8 hex chars of its public key
    (meshcore_interface.py's _node_num), which collides with real Meshtastic
    node numbers far too easily to trust for identity/security purposes.
    """
    cursor.execute('''CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    alias TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS linked_nodes (
                    node_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    network TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
                );''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_linked_nodes_account
                    ON linked_nodes (account_id);''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS link_codes (
                    code TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    requested_by_node_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    consumed_by_node_id TEXT
                );''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_link_codes_account
                    ON link_codes (account_id);''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS link_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    success INTEGER NOT NULL DEFAULT 0
                );''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_link_attempts_node_time
                    ON link_attempts (node_id, attempted_at);''')


def _ensure_content_status_columns(cursor) -> None:
    cursor.execute("PRAGMA table_info(bulletins)")
    bulletin_cols = {row[1] for row in cursor.fetchall()}
    if 'expected_content_length' not in bulletin_cols:
        cursor.execute("ALTER TABLE bulletins ADD COLUMN expected_content_length INTEGER")
    if 'content_complete' not in bulletin_cols:
        cursor.execute("ALTER TABLE bulletins ADD COLUMN content_complete INTEGER NOT NULL DEFAULT 1")
    cursor.execute(
        "UPDATE bulletins SET expected_content_length = COALESCE(expected_content_length, LENGTH(content)), content_complete = CASE WHEN LENGTH(content) >= COALESCE(expected_content_length, LENGTH(content)) THEN 1 ELSE 0 END"
    )

    cursor.execute("PRAGMA table_info(mail)")
    mail_cols = {row[1] for row in cursor.fetchall()}
    if 'expected_content_length' not in mail_cols:
        cursor.execute("ALTER TABLE mail ADD COLUMN expected_content_length INTEGER")
    if 'content_complete' not in mail_cols:
        cursor.execute("ALTER TABLE mail ADD COLUMN content_complete INTEGER NOT NULL DEFAULT 1")
    cursor.execute(
        "UPDATE mail SET expected_content_length = COALESCE(expected_content_length, LENGTH(content)), content_complete = CASE WHEN LENGTH(content) >= COALESCE(expected_content_length, LENGTH(content)) THEN 1 ELSE 0 END"
    )

    # Re-fetch bulletin cols (may have changed above) and add provenance columns
    cursor.execute("PRAGMA table_info(bulletins)")
    bulletin_cols = {row[1] for row in cursor.fetchall()}
    if 'source_node_id' not in bulletin_cols:
        cursor.execute("ALTER TABLE bulletins ADD COLUMN source_node_id TEXT")
    if 'source_timestamp' not in bulletin_cols:
        cursor.execute("ALTER TABLE bulletins ADD COLUMN source_timestamp TEXT")
    if 'received_at' not in bulletin_cols:
        cursor.execute("ALTER TABLE bulletins ADD COLUMN received_at TEXT")

    cursor.execute("PRAGMA table_info(mail)")
    mail_cols = {row[1] for row in cursor.fetchall()}
    if 'source_node_id' not in mail_cols:
        cursor.execute("ALTER TABLE mail ADD COLUMN source_node_id TEXT")
    if 'source_timestamp' not in mail_cols:
        cursor.execute("ALTER TABLE mail ADD COLUMN source_timestamp TEXT")
    if 'received_at' not in mail_cols:
        cursor.execute("ALTER TABLE mail ADD COLUMN received_at TEXT")


def _ensure_channel_comment_sync_columns(cursor) -> None:
    cursor.execute("PRAGMA table_info(channel_comments)")
    comment_cols = {row[1] for row in cursor.fetchall()}
    if 'unique_id' not in comment_cols:
        cursor.execute("ALTER TABLE channel_comments ADD COLUMN unique_id TEXT NOT NULL DEFAULT ''")
    if 'expected_content_length' not in comment_cols:
        cursor.execute("ALTER TABLE channel_comments ADD COLUMN expected_content_length INTEGER")
    if 'content_complete' not in comment_cols:
        cursor.execute("ALTER TABLE channel_comments ADD COLUMN content_complete INTEGER NOT NULL DEFAULT 1")
    rows = cursor.execute("SELECT id, channel_id, sender_short_name, date, content, unique_id FROM channel_comments").fetchall()
    for row in rows:
        unique_id = str(row[5] or '').strip()
        if unique_id:
            continue
        seed = f"{row[1]}|{row[2]}|{row[3]}|{row[4]}|{row[0]}"
        generated = str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
        cursor.execute("UPDATE channel_comments SET unique_id = ? WHERE id = ?", (generated, row[0]))
    cursor.execute(
        "UPDATE channel_comments SET expected_content_length = COALESCE(expected_content_length, LENGTH(content)), content_complete = CASE WHEN LENGTH(content) >= COALESCE(expected_content_length, LENGTH(content)) THEN 1 ELSE 0 END"
    )
    cursor.execute("PRAGMA table_info(channel_comments)")
    cc_cols = {row[1] for row in cursor.fetchall()}
    if 'source_node_id' not in cc_cols:
        cursor.execute("ALTER TABLE channel_comments ADD COLUMN source_node_id TEXT")
    if 'source_timestamp' not in cc_cols:
        cursor.execute("ALTER TABLE channel_comments ADD COLUMN source_timestamp TEXT")
    if 'received_at' not in cc_cols:
        cursor.execute("ALTER TABLE channel_comments ADD COLUMN received_at TEXT")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_comments_unique_id_unique ON channel_comments(unique_id)")


def _ensure_deleted_sync_tombstones_table() -> None:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS deleted_sync_tombstones (
                    tombstone_key TEXT PRIMARY KEY,
                    deleted_at TEXT NOT NULL
                );''')
    conn.commit()


def _ensure_sync_transmissions_table() -> None:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sync_transmissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transmission_time TEXT NOT NULL,
                    frame_type TEXT NOT NULL,
                    destination_node_id TEXT,
                    direction TEXT NOT NULL DEFAULT 'tx',
                    frame_size_bytes INTEGER,
                    is_continuation INTEGER NOT NULL DEFAULT 0,
                    frame_text TEXT NOT NULL DEFAULT ''
                );''')
    c.execute("PRAGMA table_info(sync_transmissions)")
    sync_cols = {row[1] for row in c.fetchall()}
    if 'direction' not in sync_cols:
        c.execute("ALTER TABLE sync_transmissions ADD COLUMN direction TEXT NOT NULL DEFAULT 'tx'")
    if 'frame_text' not in sync_cols:
        c.execute("ALTER TABLE sync_transmissions ADD COLUMN frame_text TEXT NOT NULL DEFAULT ''")
    conn.commit()


def log_sync_transmission(
    message: str,
    destination_node_id: Optional[str],
    frame_size_bytes: int,
    is_continuation: bool = False,
    direction: str = 'tx',
) -> None:
    """Log a sync frame send/receive event for monitoring and optimization."""
    _ensure_sync_transmissions_table()
    try:
        # Extract frame type from message (e.g. "SYNCSTATE|..." -> "SYNCSTATE")
        frame_type = message.split('|')[0] if '|' in message else message[:20]
        transmission_time = datetime.utcnow().isoformat() + 'Z'
        direction_value = str(direction or 'tx').strip().lower()
        if direction_value not in ('tx', 'rx'):
            direction_value = 'tx'
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            '''INSERT INTO sync_transmissions
               (transmission_time, frame_type, destination_node_id, direction, frame_size_bytes, is_continuation, frame_text)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (
                transmission_time,
                frame_type,
                destination_node_id,
                direction_value,
                frame_size_bytes,
                1 if is_continuation else 0,
                str(message or ''),
            )
        )
        conn.commit()
    except Exception as e:
        logging.debug(f"Failed to log sync transmission: {e}")


def get_sync_transmission_entries(
    since_id: int = 0,
    limit: int = 200,
    direction: Optional[str] = None,
    frame_type: Optional[str] = None,
    peer_node_id: Optional[str] = None,
    search_query: Optional[str] = None,
) -> list[dict]:
    """Return recent sync transmission rows for live diagnostics views."""
    _ensure_sync_transmissions_table()
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        clauses = ["id > ?"]
        params: list = [max(0, int(since_id))]

        normalized_direction = str(direction or '').strip().lower()
        if normalized_direction in ('tx', 'rx'):
            clauses.append("direction = ?")
            params.append(normalized_direction)

        normalized_frame = str(frame_type or '').strip().upper()
        if normalized_frame:
            clauses.append("frame_type = ?")
            params.append(normalized_frame)

        normalized_peer = str(peer_node_id or '').strip()
        if normalized_peer:
            clauses.append("destination_node_id = ?")
            params.append(normalized_peer)

        normalized_search = str(search_query or '').strip()
        if normalized_search:
            clauses.append("(frame_text LIKE ? OR frame_type LIKE ? OR COALESCE(destination_node_id, '') LIKE ?)")
            like_value = f"%{normalized_search}%"
            params.extend([like_value, like_value, like_value])

        where_clause = " AND ".join(clauses)
        normalized_limit = max(1, int(limit))
        order_clause = "ORDER BY id ASC"
        if max(0, int(since_id)) == 0:
            order_clause = "ORDER BY id DESC"
        params.append(normalized_limit)
        c.execute(
            f"""
            SELECT id, transmission_time, frame_type, destination_node_id, direction,
                   frame_size_bytes, is_continuation, frame_text
            FROM sync_transmissions
            WHERE {where_clause}
            {order_clause}
            LIMIT ?
            """,
            tuple(params),
        )
        rows = [dict(row) for row in c.fetchall()]
        if max(0, int(since_id)) == 0:
            rows.reverse()
        return rows
    except Exception as e:
        logging.debug(f"Failed to get sync transmission entries: {e}")
        return []


def get_sync_transmission_stats(since_seconds: int = 3600) -> dict:
    """Get transmission statistics over the past N seconds."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        cutoff_time = (datetime.utcnow() - timedelta(seconds=since_seconds)).isoformat() + 'Z'
        
        # Total transmissions
        c.execute(
            "SELECT COUNT(*) FROM sync_transmissions WHERE transmission_time > ?",
            (cutoff_time,)
        )
        total_transmissions = c.fetchone()[0] or 0
        
        # Breakdown by frame type — count AND bytes
        c.execute(
            """SELECT frame_type, COUNT(*) as count, COALESCE(SUM(frame_size_bytes), 0) as bytes
               FROM sync_transmissions WHERE transmission_time > ?
               GROUP BY frame_type ORDER BY bytes DESC""",
            (cutoff_time,)
        )
        frame_rows = c.fetchall()
        frame_breakdown = {row[0]: row[1] for row in frame_rows}
        frame_bytes    = {row[0]: row[2] for row in frame_rows}

        # Breakdown by destination node
        c.execute(
            "SELECT destination_node_id, COUNT(*) as count FROM sync_transmissions WHERE transmission_time > ? GROUP BY destination_node_id ORDER BY count DESC",
            (cutoff_time,)
        )
        node_breakdown = {row[0]: row[1] for row in c.fetchall()}

        c.execute(
            "SELECT direction, COUNT(*) as count, COALESCE(SUM(frame_size_bytes), 0) as bytes FROM sync_transmissions WHERE transmission_time > ? GROUP BY direction ORDER BY count DESC",
            (cutoff_time,)
        )
        direction_rows = c.fetchall()
        direction_breakdown = {str(row[0] or 'tx'): row[1] for row in direction_rows}
        direction_bytes = {str(row[0] or 'tx'): row[2] for row in direction_rows}
        
        # Total bytes sent
        c.execute(
            "SELECT SUM(frame_size_bytes) FROM sync_transmissions WHERE transmission_time > ?",
            (cutoff_time,)
        )
        total_bytes = c.fetchone()[0] or 0
        
        return {
            'total_transmissions': total_transmissions,
            'total_bytes': total_bytes,
            'frame_breakdown': frame_breakdown,
            'frame_bytes': frame_bytes,
            'node_breakdown': node_breakdown,
            'direction_breakdown': direction_breakdown,
            'direction_bytes': direction_bytes,
            'period_seconds': since_seconds,
        }
    except Exception as e:
        logging.debug(f"Failed to get sync transmission stats: {e}")
        return {}


def prune_old_sync_transmissions(max_rows: int = 10000) -> None:
    """Keep sync_transmissions table bounded in size."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            '''DELETE FROM sync_transmissions WHERE id NOT IN (
               SELECT id FROM sync_transmissions ORDER BY id DESC LIMIT ?)
            ''',
            (max_rows,)
        )
        conn.commit()
    except Exception as e:
        logging.debug(f"Failed to prune sync transmissions: {e}")


def prune_op_log(max_rows: int = 20000) -> int:
    """Keep the op_log bounded by retaining only the newest ``max_rows`` events.

    Safe because the op_log is an *optimization* layer (HAVE/WANT/EVENT discovery)
    layered on top of the authoritative hash-repair sync (SYNCSTATE/HASHZ), which
    reconciles the actual records regardless of op_log contents. Seq allocation
    lives in op_log_state (never pruned), so dropping old op_log rows never
    disturbs monotonic seq assignment. Returns rows deleted."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM op_log")
        total = int(c.fetchone()[0])
        if total <= max_rows:
            return 0
        # rowid is insertion order; keep the newest max_rows.
        c.execute(
            "DELETE FROM op_log WHERE rowid NOT IN ("
            "SELECT rowid FROM op_log ORDER BY rowid DESC LIMIT ?)",
            (max_rows,),
        )
        conn.commit()
        return total - max_rows
    except Exception as e:
        logging.debug(f"Failed to prune op_log: {e}")
        return 0


def prune_sync_session_history(max_rows: int = 2000) -> int:
    """Keep sync_session_history bounded; returns rows deleted."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "DELETE FROM sync_session_history WHERE id NOT IN ("
            "SELECT id FROM sync_session_history ORDER BY id DESC LIMIT ?)",
            (max_rows,),
        )
        deleted = c.rowcount if c.rowcount and c.rowcount > 0 else 0
        conn.commit()
        return deleted
    except Exception as e:
        logging.debug(f"Failed to prune sync_session_history: {e}")
        return 0


def prune_expired_tombstones(max_age_days: int = 30) -> int:
    """Delete tombstones older than ``max_age_days``.

    A floor age (default 30 days) ensures deletes have ample time to propagate to
    slow/offline peers before the tombstone is forgotten — otherwise a deleted
    record could be resurrected by a peer that never saw the delete. Returns rows
    deleted."""
    if max_age_days <= 0:
        return 0
    try:
        cutoff = (datetime.now() - timedelta(days=int(max_age_days))).strftime('%Y-%m-%d %H:%M:%S')
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM deleted_sync_tombstones WHERE deleted_at < ?", (cutoff,))
        deleted = c.rowcount if c.rowcount and c.rowcount > 0 else 0
        conn.commit()
        return deleted
    except Exception as e:
        logging.debug(f"Failed to prune tombstones: {e}")
        return 0


def checkpoint_wal() -> None:
    """Truncate the WAL file so it can't grow unbounded between checkpoints."""
    try:
        conn = get_db_connection()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.commit()
    except Exception as e:
        logging.debug(f"WAL checkpoint failed: {e}")


def vacuum_database() -> None:
    """Reclaim free pages so the DB file shrinks after large prunes. Heavier op —
    callers should run this on a slow cadence (e.g. daily) when idle."""
    try:
        conn = get_db_connection()
        conn.execute("VACUUM;")
        conn.commit()
    except Exception as e:
        logging.debug(f"VACUUM failed: {e}")


def enqueue_api_response(rid: str, requester_node_id: str, status, body) -> int:
    """Gateway side (Phase 2): persist an API response for later retrieval via
    APIPOLL. Returns the new row id (0 on failure)."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "INSERT INTO api_mailbox (rid, requester_node_id, status, body, created_at, delivered) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (str(rid), str(requester_node_id), str(status), str(body or ""),
             datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        )
        conn.commit()
        return int(c.lastrowid or 0)
    except Exception as e:
        logging.debug(f"Failed to enqueue api_mailbox entry: {e}")
        return 0


def fetch_undelivered_api_responses(requester_node_id: str, limit: int = 10) -> list:
    """Return [{'id','rid','status','body'}, ...] of undelivered responses queued
    for *requester_node_id*, oldest first."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "SELECT id, rid, status, body FROM api_mailbox "
            "WHERE requester_node_id = ? AND delivered = 0 ORDER BY id ASC LIMIT ?",
            (str(requester_node_id), int(limit)),
        )
        return [{'id': r[0], 'rid': r[1], 'status': r[2], 'body': r[3]} for r in c.fetchall()]
    except Exception as e:
        logging.debug(f"Failed to fetch api_mailbox entries: {e}")
        return []


def mark_api_responses_delivered(ids) -> int:
    """Mark mailbox rows delivered after a successful flush. Returns rows updated."""
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return 0
    try:
        conn = get_db_connection()
        c = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.executemany(
            "UPDATE api_mailbox SET delivered = 1, delivered_at = ? WHERE id = ?",
            [(now, i) for i in ids],
        )
        conn.commit()
        return c.rowcount if c.rowcount and c.rowcount > 0 else len(ids)
    except Exception as e:
        logging.debug(f"Failed to mark api_mailbox delivered: {e}")
        return 0


def prune_api_mailbox(max_age_days: int = 7, max_rows: int = 5000) -> int:
    """Bound the api_mailbox: drop delivered entries older than max_age_days, and
    cap total rows to max_rows (oldest first). Returns rows deleted."""
    deleted = 0
    try:
        conn = get_db_connection()
        c = conn.cursor()
        if max_age_days and max_age_days > 0:
            cutoff = (datetime.now() - timedelta(days=int(max_age_days))).strftime('%Y-%m-%d %H:%M:%S')
            c.execute(
                "DELETE FROM api_mailbox WHERE delivered = 1 AND COALESCE(delivered_at, created_at) < ?",
                (cutoff,),
            )
            deleted += c.rowcount if c.rowcount and c.rowcount > 0 else 0
        if max_rows and max_rows > 0:
            c.execute(
                "DELETE FROM api_mailbox WHERE id NOT IN ("
                "SELECT id FROM api_mailbox ORDER BY id DESC LIMIT ?)",
                (int(max_rows),),
            )
            deleted += c.rowcount if c.rowcount and c.rowcount > 0 else 0
        conn.commit()
    except Exception as e:
        logging.debug(f"Failed to prune api_mailbox: {e}")
    return deleted


_cached_maintenance_cfg: Optional[dict] = None


def get_maintenance_config() -> dict:
    """Read the [maintenance] config section once, with sensible defaults."""
    global _cached_maintenance_cfg
    if _cached_maintenance_cfg is None:
        cfg = configparser.ConfigParser()
        cfg.read(get_config_path())
        g = lambda opt, d: cfg.getint('maintenance', opt, fallback=d)  # noqa: E731
        _cached_maintenance_cfg = {
            'interval_minutes': g('maintenance_interval_minutes', 60),
            'sync_transmissions_max_rows': g('sync_transmissions_max_rows', 10000),
            'op_log_max_rows': g('op_log_max_rows', 20000),
            'sync_session_history_max_rows': g('sync_session_history_max_rows', 2000),
            'tombstone_max_age_days': g('tombstone_max_age_days', 30),
            'vacuum_interval_hours': g('vacuum_interval_hours', 24),
            'api_mailbox_max_age_days': g('api_mailbox_max_age_days', 7),
            'api_mailbox_max_rows': g('api_mailbox_max_rows', 5000),
        }
    return _cached_maintenance_cfg


def run_db_maintenance(do_vacuum: bool = False) -> dict:
    """Run one periodic maintenance pass: prune unbounded tables, checkpoint WAL,
    optionally VACUUM. Returns a summary dict for logging. Best-effort; each step
    swallows its own errors so a single failure can't abort the rest."""
    cfg = get_maintenance_config()
    summary = {
        'sync_transmissions_deleted': 0,
        'op_log_deleted': 0,
        'sync_session_history_deleted': 0,
        'tombstones_deleted': 0,
        'api_mailbox_deleted': 0,
        'vacuumed': False,
    }
    before = None
    try:
        conn = get_db_connection()
        before = int(conn.execute("SELECT COUNT(*) FROM sync_transmissions").fetchone()[0])
    except Exception:
        before = None
    prune_old_sync_transmissions(cfg['sync_transmissions_max_rows'])
    if before is not None:
        try:
            after = int(get_db_connection().execute("SELECT COUNT(*) FROM sync_transmissions").fetchone()[0])
            summary['sync_transmissions_deleted'] = max(0, before - after)
        except Exception:
            pass
    summary['op_log_deleted'] = prune_op_log(cfg['op_log_max_rows'])
    summary['sync_session_history_deleted'] = prune_sync_session_history(cfg['sync_session_history_max_rows'])
    summary['tombstones_deleted'] = prune_expired_tombstones(cfg['tombstone_max_age_days'])
    summary['api_mailbox_deleted'] = prune_api_mailbox(
        cfg.get('api_mailbox_max_age_days', 7), cfg.get('api_mailbox_max_rows', 5000))
    checkpoint_wal()
    if do_vacuum:
        vacuum_database()
        summary['vacuumed'] = True
    return summary


def _db_total_bytes() -> int:
    """On-disk footprint of the SQLite DB: the file plus its -wal/-shm sidecars."""
    path = get_database_path()
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(path + suffix)
        except OSError:
            pass
    return total


def read_max_db_size_mb() -> int:
    """Read [maintenance] max_db_size_mb fresh so a GUI change hot-reloads.
    0 (default) disables the size cap."""
    try:
        cfg = configparser.ConfigParser()
        cfg.read(get_config_path())
        return max(0, cfg.getint('maintenance', 'max_db_size_mb', fallback=0))
    except Exception:
        return 0


def enforce_db_size_cap(bbs_nodes, interface, max_mb=None, keep_floor=20, max_deletes=100) -> dict:
    """Keep the on-disk database under a configurable megabyte cap by deleting the
    OLDEST content (bulletins/mail/channel_comments) across scopes when over.

    Deletes go through the normal tombstoned delete path, so they propagate to
    peers (and the Pico cache) as DELETE_* frames — meaning every node prunes the
    *same* records the *same* way. Bounded to ``max_deletes`` per pass to avoid a
    DELETE-frame storm; successive maintenance passes converge. ``keep_floor``
    newest records per scope are never deleted, so a board is never emptied.

    A cap of 0/None disables it (the default). Returns a summary dict."""
    if max_mb is None:
        max_mb = read_max_db_size_mb()
    summary = {'enabled': bool(max_mb and max_mb > 0), 'over': False, 'deleted': 0, 'size_bytes': 0}
    if not max_mb or max_mb <= 0:
        return summary
    target = int(max_mb) * 1024 * 1024
    try:
        checkpoint_wal()
    except Exception:
        pass
    size = _db_total_bytes()
    summary['size_bytes'] = size
    if size <= target:
        return summary
    summary['over'] = True
    keep = max(0, int(keep_floor))
    try:
        conn = get_db_connection()
        c = conn.cursor()
        total_rows = 0
        for tbl in ('bulletins', 'mail', 'channel_comments'):
            try:
                total_rows += int(c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0])
            except Exception:
                pass
        avg = max(1, size // max(1, total_rows))
        want = min(int(max_deletes), max(1, int((size - target) * 1.05) // avg + 1))

        def oldest_excluding_newest(table, extra=""):
            rows = c.execute(
                f"SELECT unique_id, date{extra} FROM {table} ORDER BY date ASC, id ASC"
            ).fetchall()
            cut = len(rows) - keep
            return rows[:cut] if cut > 0 else []

        candidates = []  # (date, scope, uid, recipient_or_None)
        for uid, date in oldest_excluding_newest('bulletins'):
            candidates.append((date or '', 'bulletins', uid, None))
        for uid, date, recipient in oldest_excluding_newest('mail', ", recipient"):
            candidates.append((date or '', 'mail', uid, recipient))
        for uid, date in oldest_excluding_newest('channel_comments'):
            candidates.append((date or '', 'channel_comments', uid, None))
        candidates.sort(key=lambda x: (x[0], x[2]))  # oldest first
        to_delete = candidates[:want]

        for date, scope, uid, recipient in to_delete:
            try:
                if scope == 'bulletins':
                    delete_bulletin(uid, bbs_nodes, interface)
                elif scope == 'mail':
                    delete_mail(uid, recipient, bbs_nodes, interface)
                else:
                    delete_channel_comment(uid, bbs_nodes, interface)
                summary['deleted'] += 1
            except Exception as e:
                logging.debug(f"size-cap delete failed for {scope}/{uid}: {e}")
        if summary['deleted']:
            try:
                vacuum_database()
                checkpoint_wal()
            except Exception:
                pass
            summary['size_bytes'] = _db_total_bytes()
    except Exception as exc:
        logging.warning(f"enforce_db_size_cap failed: {exc}")
    return summary


def clear_sync_transmissions() -> None:
    """Clear all rows from sync_transmissions for manual stats reset."""
    try:
        _ensure_sync_transmissions_table()
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM sync_transmissions")
        conn.commit()
    except Exception as e:
        logging.debug(f"Failed to clear sync transmissions: {e}")


def get_sync_sessions(since_seconds: int = 86400, gap_seconds: int = 90, limit: int = 30) -> dict:
    """Infer sync sessions from the transmission log.

    A "session" is a burst of non-SYNCSTATE frames to/from the same peer where
    no two consecutive frames are more than *gap_seconds* apart.

    Returns a dict with two keys:
      'sessions': list of completed session dicts, most-recent first (up to *limit*)
      'active':   list of still-in-progress session dicts (last frame < gap_seconds ago)

    Each session dict contains:
      peer_node_id, started_at, ended_at, duration_seconds,
      bytes_tx, bytes_rx, total_bytes, frame_count, bps (float or None)
    """
    from datetime import timezone as _tz
    try:
        conn = get_db_connection()
        c = conn.cursor()
        since = (datetime.utcnow() - timedelta(seconds=max(60, int(since_seconds)))).isoformat() + 'Z'
        c.execute(
            """SELECT destination_node_id, transmission_time, frame_size_bytes, direction
               FROM sync_transmissions
               WHERE transmission_time >= ?
                 AND frame_type != 'SYNCSTATE'
               ORDER BY destination_node_id, transmission_time ASC""",
            (since,),
        )
        rows = c.fetchall()
    except Exception as e:
        logging.debug(f"get_sync_sessions error: {e}")
        return {'sessions': [], 'active': []}

    def _to_ts(s: str) -> float:
        try:
            return datetime.fromisoformat(s.rstrip('Z').replace(' ', 'T')).replace(tzinfo=_tz.utc).timestamp()
        except Exception:
            return 0.0

    now = datetime.utcnow().replace(tzinfo=_tz.utc).timestamp()
    buckets: dict = {}

    for row in rows:
        peer = str(row[0] or 'broadcast')
        t_str = str(row[1])
        size = int(row[2] or 0)
        direction = str(row[3] or 'tx')
        ts = _to_ts(t_str)
        if ts == 0.0:
            continue
        buckets.setdefault(peer, []).append((ts, size, direction, t_str))

    raw_sessions = []
    for peer, events in buckets.items():
        current = None
        for ts, size, direction, t_str in events:
            if current is None or ts - current['end_ts'] > gap_seconds:
                if current:
                    raw_sessions.append(current)
                current = {
                    'peer': peer, 'start_ts': ts, 'end_ts': ts,
                    'start_str': t_str, 'end_str': t_str,
                    'bytes_tx': 0, 'bytes_rx': 0, 'frames': 0,
                }
            current['end_ts'] = ts
            current['end_str'] = t_str
            current['frames'] += 1
            if direction == 'tx':
                current['bytes_tx'] += size
            else:
                current['bytes_rx'] += size
        if current:
            raw_sessions.append(current)

    def _build(s: dict) -> dict:
        dur = max(s['end_ts'] - s['start_ts'], 0.0)
        total = s['bytes_tx'] + s['bytes_rx']
        return {
            'peer_node_id': s['peer'],
            'started_at': s['start_str'],
            'ended_at': s['end_str'],
            'duration_seconds': round(dur, 1),
            'bytes_tx': s['bytes_tx'],
            'bytes_rx': s['bytes_rx'],
            'total_bytes': total,
            'frame_count': s['frames'],
            'bps': round(total / dur, 1) if dur >= 1.0 else None,
        }

    completed = []
    active = []
    for s in raw_sessions:
        if now - s['end_ts'] < gap_seconds:
            active.append(_build(s))
        else:
            completed.append(_build(s))

    completed.sort(key=lambda x: x['ended_at'], reverse=True)
    active.sort(key=lambda x: x['started_at'])

    # Persist completed sessions to long-term history (idempotent on peer+started_at).
    try:
        _persist_sync_sessions(completed)
    except Exception as e:
        logging.debug(f"_persist_sync_sessions failed: {e}")

    return {'sessions': completed[:max(1, int(limit))], 'active': active}


def _ensure_sync_session_history_table() -> None:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sync_session_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_node_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    bytes_tx INTEGER NOT NULL DEFAULT 0,
                    bytes_rx INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    frame_count INTEGER NOT NULL DEFAULT 0,
                    bps REAL,
                    UNIQUE(peer_node_id, started_at)
                );''')
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_session_history_ended "
        "ON sync_session_history(ended_at DESC)"
    )
    conn.commit()


def _persist_sync_sessions(sessions: list) -> None:
    if not sessions:
        return
    _ensure_sync_session_history_table()
    conn = get_db_connection()
    c = conn.cursor()
    for s in sessions:
        try:
            c.execute(
                """INSERT OR IGNORE INTO sync_session_history
                   (peer_node_id, started_at, ended_at, duration_seconds,
                    bytes_tx, bytes_rx, total_bytes, frame_count, bps)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(s.get('peer_node_id') or 'broadcast'),
                    str(s.get('started_at')),
                    str(s.get('ended_at')),
                    float(s.get('duration_seconds') or 0.0),
                    int(s.get('bytes_tx') or 0),
                    int(s.get('bytes_rx') or 0),
                    int(s.get('total_bytes') or 0),
                    int(s.get('frame_count') or 0),
                    float(s['bps']) if s.get('bps') is not None else None,
                ),
            )
        except Exception as e:
            logging.debug(f"persist sync session row failed: {e}")
    conn.commit()


def get_sync_session_history(since_seconds: int = 30 * 24 * 3600, limit: int = 200) -> list:
    """Return persisted completed sync sessions, most-recent first."""
    try:
        _ensure_sync_session_history_table()
        conn = get_db_connection()
        c = conn.cursor()
        since = (datetime.utcnow() - timedelta(seconds=max(60, int(since_seconds)))).isoformat() + 'Z'
        c.execute(
            """SELECT peer_node_id, started_at, ended_at, duration_seconds,
                      bytes_tx, bytes_rx, total_bytes, frame_count, bps
               FROM sync_session_history
               WHERE ended_at >= ?
               ORDER BY ended_at DESC
               LIMIT ?""",
            (since, max(1, int(limit))),
        )
        return [
            {
                'peer_node_id': r[0],
                'started_at': r[1],
                'ended_at': r[2],
                'duration_seconds': r[3],
                'bytes_tx': r[4],
                'bytes_rx': r[5],
                'total_bytes': r[6],
                'frame_count': r[7],
                'bps': r[8],
            }
            for r in c.fetchall()
        ]
    except Exception as e:
        logging.debug(f"get_sync_session_history error: {e}")
        return []


def _build_tombstone_key(scope: str, record_key: str) -> str:
    return f"{scope}:{record_key}"


def record_sync_tombstone(scope: str, record_key: str) -> None:
    record_sync_tombstone_at(scope, record_key, None)


def record_sync_tombstone_at(scope: str, record_key: str, deleted_at: Optional[str]) -> None:
    _ensure_deleted_sync_tombstones_table()
    conn = get_db_connection()
    c = conn.cursor()
    normalized_deleted_at = str(deleted_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    c.execute(
        '''INSERT INTO deleted_sync_tombstones (tombstone_key, deleted_at)
           VALUES (?, ?)
           ON CONFLICT(tombstone_key) DO UPDATE SET
                         deleted_at = CASE
                             WHEN excluded.deleted_at > deleted_sync_tombstones.deleted_at THEN excluded.deleted_at
                             ELSE deleted_sync_tombstones.deleted_at
                         END''',
                (_build_tombstone_key(scope, record_key), normalized_deleted_at),
    )
    conn.commit()


def clear_sync_tombstone(scope: str, record_key: str) -> None:
    _ensure_deleted_sync_tombstones_table()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM deleted_sync_tombstones WHERE tombstone_key = ?", (_build_tombstone_key(scope, record_key),))
    conn.commit()


def has_sync_tombstone(scope: str, record_key: str) -> bool:
    _ensure_deleted_sync_tombstones_table()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM deleted_sync_tombstones WHERE tombstone_key = ? LIMIT 1", (_build_tombstone_key(scope, record_key),))
    return c.fetchone() is not None


def get_sync_tombstone_deleted_at(scope: str, record_key: str) -> Optional[str]:
    _ensure_deleted_sync_tombstones_table()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT deleted_at FROM deleted_sync_tombstones WHERE tombstone_key = ? LIMIT 1",
        (_build_tombstone_key(scope, record_key),),
    )
    row = c.fetchone()
    if not row:
        return None
    return str(row[0] or '') or None


def get_recent_sync_tombstones(scope_prefix: str = '', limit: int = 20) -> list:
    _ensure_deleted_sync_tombstones_table()
    conn = get_db_connection()
    c = conn.cursor()
    normalized_limit = max(1, int(limit))
    if scope_prefix:
        c.execute(
            "SELECT tombstone_key, deleted_at FROM deleted_sync_tombstones WHERE tombstone_key LIKE ? ORDER BY deleted_at DESC, tombstone_key ASC LIMIT ?",
            (f"{scope_prefix}:%", normalized_limit),
        )
    else:
        c.execute(
            "SELECT tombstone_key, deleted_at FROM deleted_sync_tombstones ORDER BY deleted_at DESC, tombstone_key ASC LIMIT ?",
            (normalized_limit,),
        )
    return c.fetchall()


def get_local_record_counts() -> dict:
    """Return local record counts and compact hashes used by SYNCSTATE comparisons."""
    conn = get_db_connection()
    c = conn.cursor()
    zork_save_sync_enabled = is_zork_save_sync_enabled()
    c.execute("SELECT COUNT(*) FROM bulletins WHERE local_only = 0")
    bulletins = int(c.fetchone()[0])
    c.execute("SELECT COUNT(*) FROM mail")
    mail = int(c.fetchone()[0])
    c.execute("SELECT COUNT(*) FROM channels WHERE local_only = 0")
    channels = int(c.fetchone()[0])
    c.execute(
        "SELECT COUNT(*) FROM channel_comments cc JOIN channels ch ON ch.id = cc.channel_id WHERE ch.local_only = 0"
    )
    channels += int(c.fetchone()[0])
    _ensure_zork_saves_table()
    if zork_save_sync_enabled:
        c.execute("SELECT COUNT(*) FROM zork_saves")
        zork_saves = int(c.fetchone()[0])
    else:
        zork_saves = 0
    c.execute("SELECT COUNT(*) FROM user_profiles")
    profiles = int(c.fetchone()[0])
    c.execute("SELECT COUNT(*) FROM game_scores")
    game_scores = int(c.fetchone()[0])

    def _hash_rows(query: str, params: tuple = ()) -> str:
        digest = hashlib.blake2b(digest_size=8)
        row_count = 0
        for row in c.execute(query, params):
            row_count += 1
            for value in row:
                if value is None:
                    blob = b''
                elif isinstance(value, bytes):
                    blob = value
                else:
                    blob = str(value).encode('utf-8')
                digest.update(len(blob).to_bytes(4, 'big'))
                digest.update(blob)
        digest.update(row_count.to_bytes(8, 'big'))
        return base64.urlsafe_b64encode(digest.digest()).decode('ascii').rstrip('=')

    bulletins_hash = _hash_rows(
        "SELECT board, sender_short_name, subject, content, unique_id FROM bulletins WHERE local_only = 0 ORDER BY unique_id"
    )
    mail_hash = _hash_rows(
        "SELECT sender, sender_short_name, recipient, subject, content, unique_id FROM mail ORDER BY unique_id"
    )
    channels_digest = hashlib.blake2b(digest_size=8)
    channels_row_count = 0
    for row in c.execute("SELECT 'channel', name, url FROM channels WHERE local_only = 0 ORDER BY name, url"):
        channels_row_count += 1
        for value in row:
            blob = b'' if value is None else str(value).encode('utf-8')
            channels_digest.update(len(blob).to_bytes(4, 'big'))
            channels_digest.update(blob)
    for row in c.execute(
        "SELECT 'comment', ch.name, ch.url, cc.sender_short_name, cc.date, cc.content, cc.unique_id, "
        "COALESCE(cc.expected_content_length, LENGTH(cc.content)), COALESCE(cc.content_complete, 1) "
        "FROM channel_comments cc JOIN channels ch ON ch.id = cc.channel_id WHERE ch.local_only = 0 ORDER BY cc.unique_id"
    ):
        channels_row_count += 1
        for value in row:
            blob = b'' if value is None else str(value).encode('utf-8')
            channels_digest.update(len(blob).to_bytes(4, 'big'))
            channels_digest.update(blob)
    channels_digest.update(channels_row_count.to_bytes(8, 'big'))
    channels_hash = base64.urlsafe_b64encode(channels_digest.digest()).decode('ascii').rstrip('=')
    if zork_save_sync_enabled:
        zork_saves_hash = _hash_rows(
            "SELECT user_id, game_id, save_data, updated_at FROM zork_saves ORDER BY user_id, game_id"
        )
    else:
        zork_saves_hash = _compact_row_hash(("zork_saves_disabled",))
    profiles_hash = _hash_rows(
        "SELECT user_id, short_name, long_name, bio FROM user_profiles ORDER BY user_id"
    )
    game_scores_hash = _hash_rows(
        "SELECT user_id, game_id, short_name, score, max_score, moves, achieved_at FROM game_scores ORDER BY user_id, game_id"
    )
    c.execute("SELECT COUNT(*) FROM deleted_sync_tombstones")
    tombstones = int(c.fetchone()[0])

    return {
        'bulletins': bulletins,
        'mail': mail,
        'channels': channels,
        'zork_saves': zork_saves,
        'profiles': profiles,
        'game_scores': game_scores,
        'tombstones': tombstones,
        'bulletins_hash': bulletins_hash,
        'mail_hash': mail_hash,
        'channels_hash': channels_hash,
        'zork_saves_hash': zork_saves_hash,
        'profiles_hash': profiles_hash,
        'game_scores_hash': game_scores_hash,
    }


def upsert_peer_sync_state(peer_node_id: str, bulletins: int, mail: int, channels: int, zork_saves: int,
                           profiles: int = 0, game_scores: int = 0,
                           bulletins_hash: str = '', mail_hash: str = '', channels_hash: str = '',
                           zork_saves_hash: str = '', profiles_hash: str = '', game_scores_hash: str = '',
                           tombstones: int = -1,
                           proto_v: int = 0, caps: str = '') -> None:
    """Store the latest advertised SYNCSTATE counts for a peer node.

    ``proto_v`` and ``caps`` carry the peer's wire-protocol version and
    capability list as observed in the most recent SYNCSTATE frame.  ``caps``
    is the comma-separated cap-code list ONLY (no ``vN:`` prefix); the prefix
    is stripped by the caller.  When ``proto_v`` is 0 (legacy peer) the row
    keeps any previously-observed caps untouched.
    """
    if not peer_node_id:
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        '''INSERT INTO peer_sync_state (
               peer_node_id, bulletins, mail, channels, zork_saves, profiles, game_scores,
               bulletins_hash, mail_hash, channels_hash, zork_saves_hash, profiles_hash, game_scores_hash,
               reported_at, tombstones, proto_v, caps, caps_observed_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(peer_node_id) DO UPDATE SET
               bulletins=excluded.bulletins,
               mail=excluded.mail,
               channels=excluded.channels,
               zork_saves=excluded.zork_saves,
               profiles=excluded.profiles,
               game_scores=excluded.game_scores,
               bulletins_hash=excluded.bulletins_hash,
               mail_hash=excluded.mail_hash,
               channels_hash=excluded.channels_hash,
               zork_saves_hash=excluded.zork_saves_hash,
               profiles_hash=excluded.profiles_hash,
               game_scores_hash=excluded.game_scores_hash,
               reported_at=excluded.reported_at,
               tombstones=excluded.tombstones,
               proto_v=CASE WHEN excluded.proto_v > 0 THEN excluded.proto_v ELSE peer_sync_state.proto_v END,
               caps=CASE WHEN excluded.proto_v > 0 THEN excluded.caps ELSE peer_sync_state.caps END,
               caps_observed_at=CASE WHEN excluded.proto_v > 0 THEN excluded.caps_observed_at ELSE peer_sync_state.caps_observed_at END''',
        (
            peer_node_id,
            int(bulletins),
            int(mail),
            int(channels),
            int(zork_saves),
            int(profiles),
            int(game_scores),
            str(bulletins_hash or ''),
            str(mail_hash or ''),
            str(channels_hash or ''),
            str(zork_saves_hash or ''),
            str(profiles_hash or ''),
            str(game_scores_hash or ''),
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            int(tombstones),
            int(proto_v or 0),
            str(caps or ''),
            datetime.now().strftime('%Y-%m-%d %H:%M:%S') if int(proto_v or 0) > 0 else '',
        ),
    )
    conn.commit()
    # Invalidate the in-process cap cache so subsequent peer_supports() calls
    # observe the freshly-stored caps without waiting for TTL.
    _peer_caps_cache.pop(peer_node_id, None)


# ---------------------------------------------------------------------------
# Peer capability lookup (used by senders to gate new wire formats)
# ---------------------------------------------------------------------------

_peer_caps_cache: dict = {}  # peer_node_id -> (expires_at, proto_v, frozenset[str])
_PEER_CAPS_CACHE_TTL_SECONDS = 60.0


def get_peer_caps(peer_node_id: str) -> tuple:
    """Return ``(proto_v, frozenset[caps])`` for the peer.

    Cached for ``_PEER_CAPS_CACHE_TTL_SECONDS`` so the hot send path can call
    this for every frame.  Returns ``(0, frozenset())`` for unknown peers and
    legacy peers that have never sent ``proto_v``.
    """
    if not peer_node_id:
        return (0, frozenset())
    now = time.time()
    cached = _peer_caps_cache.get(peer_node_id)
    if cached is not None and cached[0] > now:
        return (cached[1], cached[2])
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "SELECT proto_v, caps FROM peer_sync_state WHERE peer_node_id = ?",
            (peer_node_id,),
        )
        row = c.fetchone()
    except sqlite3.OperationalError:
        # Table doesn't exist yet on a fresh DB.
        row = None
    if not row:
        result = (0, frozenset())
    else:
        proto_v = int(row[0] or 0)
        cap_csv = str(row[1] or '')
        cap_set = frozenset(tok for tok in (s.strip() for s in cap_csv.split(',')) if tok)
        result = (proto_v, cap_set)
    _peer_caps_cache[peer_node_id] = (now + _PEER_CAPS_CACHE_TTL_SECONDS, result[0], result[1])
    return result


def peer_supports(peer_node_id: str, cap: str) -> bool:
    """Return True iff ``peer_node_id`` has advertised ``cap`` in SYNCSTATE."""
    if not peer_node_id or not cap:
        return False
    _, caps = get_peer_caps(peer_node_id)
    return cap in caps


def _clear_peer_caps_cache() -> None:
    """Test hook: wipe the in-process cap cache."""
    _peer_caps_cache.clear()


def merge_relayed_peer_state(peer_node_id: str, counts: dict, age_seconds: float) -> bool:
    """Merge a gossiped (relayed) peer sync-state into peer_sync_state.

    A neighbour told us what IT last heard about ``peer_node_id`` — counts plus
    how long ago it heard them (``age_seconds``). We translate that age into our
    own clock (observed_at = now - age) to sidestep cross-node clock skew, and
    only adopt it when it is STRICTLY FRESHER than what we already know for that
    peer. This makes mismatch detection mesh-aware (e.g. learning a peer we
    can't directly hear is ahead on zork) without ever clobbering fresher
    first-hand SYNCSTATE knowledge. Returns True if the row was updated.

    proto_v is left at 0 so the existing caps for that peer are preserved — a
    relay carries no authoritative capability information.
    """
    if not peer_node_id:
        return False
    try:
        age = max(0.0, float(age_seconds))
    except (TypeError, ValueError):
        return False
    observed_dt = datetime.now() - timedelta(seconds=age)
    observed_at = observed_dt.strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT reported_at FROM peer_sync_state WHERE peer_node_id = ?", (peer_node_id,))
    row = c.fetchone()
    if row and row[0]:
        try:
            existing_dt = datetime.strptime(str(row[0]), '%Y-%m-%d %H:%M:%S')
            if existing_dt >= observed_dt:
                return False  # we already have equal-or-fresher first-hand/relayed knowledge
        except ValueError:
            pass  # unparseable existing timestamp → treat relay as fresher

    upsert_peer_sync_state(
        peer_node_id,
        int(counts.get('bulletins', 0)), int(counts.get('mail', 0)),
        int(counts.get('channels', 0)), int(counts.get('zork_saves', 0)),
        int(counts.get('profiles', 0)), int(counts.get('game_scores', 0)),
        bulletins_hash='', mail_hash='', channels_hash='',
        zork_saves_hash='', profiles_hash='', game_scores_hash='',
        tombstones=int(counts.get('tombstones', -1)),
        proto_v=0, caps='',
    )
    # upsert_peer_sync_state stamps reported_at=now(); rewrite it to the derived
    # observation time so freshness comparisons stay honest across future relays.
    c.execute("UPDATE peer_sync_state SET reported_at = ? WHERE peer_node_id = ?", (observed_at, peer_node_id))
    conn.commit()
    return True


def get_peer_sync_states() -> list:
    """Return peer-advertised record counts for diagnostics."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT peer_node_id, bulletins, mail, channels, zork_saves, profiles, game_scores, "
        "bulletins_hash, mail_hash, channels_hash, zork_saves_hash, profiles_hash, game_scores_hash, reported_at, tombstones "
        "FROM peer_sync_state ORDER BY peer_node_id"
    )
    return c.fetchall()


def get_sync_progress_data(lookback_seconds: int = 1800) -> dict:
    """Return peer sync gaps and active repair items for the web admin dashboard."""
    local = get_local_record_counts()
    local_counts = {k: local[k] for k in ('bulletins', 'mail', 'channels', 'zork_saves', 'profiles', 'game_scores')}

    _SCOPE_ORDER = ('bulletins', 'mail', 'channels', 'zork_saves', 'profiles', 'game_scores')

    peers = []
    for row in get_peer_sync_states():
        peer_id = str(row[0])
        reported_at = str(row[13]) if len(row) > 13 else ''
        peer_counts = {
            'bulletins':    int(row[1] or 0),
            'mail':         int(row[2] or 0),
            'channels':     int(row[3] or 0),
            'zork_saves':   int(row[4] or 0),
            'profiles':     int(row[5] or 0),
            'game_scores':  int(row[6] or 0),
        }
        gaps = []
        for scope in _SCOPE_ORDER:
            local_val = int(local_counts.get(scope, 0))
            peer_val = int(peer_counts.get(scope, 0))
            if local_val != peer_val:
                gaps.append({
                    'scope': scope,
                    'local': local_val,
                    'peer': peer_val,
                    'delta': local_val - peer_val,
                })
        peers.append({
            'peer_node_id': peer_id,
            'reported_at': reported_at,
            'counts': peer_counts,
            'gaps': gaps,
        })

    # Find active repair items: unique_ids that appeared in recent inbound HASHMISS frames
    since_time = (datetime.utcnow() - timedelta(seconds=max(60, int(lookback_seconds)))).isoformat() + 'Z'

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    try:
        c.execute(
            """SELECT frame_text, destination_node_id, transmission_time
               FROM sync_transmissions
               WHERE frame_type IN ('HASHMISS', 'HASHMISS_TOMB')
                 AND direction = 'rx'
                 AND transmission_time >= ?
               ORDER BY id ASC""",
            (since_time,),
        )
        hashmiss_rows = c.fetchall()
    except Exception:
        hashmiss_rows = []

    # uid → {scope, peer, last_hashmiss_at, request_count}
    uid_info: dict = {}
    for row in hashmiss_rows:
        parts = str(row['frame_text'] or '').split('|')
        if len(parts) >= 3:
            from utils import decode_scope
            scope = decode_scope(parts[1])
            uid = parts[2]
            peer = str(row['destination_node_id'] or '')
            ts = str(row['transmission_time'] or '')
            if uid not in uid_info:
                uid_info[uid] = {'scope': scope, 'peer': peer, 'last_hashmiss_at': ts, 'request_count': 0}
            uid_info[uid]['request_count'] += 1
            if ts > uid_info[uid]['last_hashmiss_at']:
                uid_info[uid]['last_hashmiss_at'] = ts

    active_items = []
    for uid, info in uid_info.items():
        scope = info['scope']
        # Fetch recent outbound frames referencing this uid
        try:
            c.execute(
                """SELECT frame_type, frame_text, transmission_time
                   FROM sync_transmissions
                   WHERE direction = 'tx'
                     AND frame_text LIKE ?
                     AND transmission_time >= ?
                   ORDER BY id ASC""",
                (f'%{uid}%', since_time),
            )
            frame_rows = c.fetchall()
        except Exception:
            frame_rows = []

        sent_frames = []
        for fr in frame_rows:
            ft = str(fr['frame_type'] or '')
            ft_text = str(fr['frame_text'] or '')
            ts = str(fr['transmission_time'] or '')
            offset = None
            if ft == 'BULLETINCONT' or ft == 'MAILCONT' or ft == 'CHANNELCOMMENTCONT':
                fparts = ft_text.split('|')
                if len(fparts) >= 3:
                    try:
                        offset = int(fparts[2])
                    except ValueError:
                        pass
            sent_frames.append({'type': ft, 'offset': offset, 'sent_at': ts})

        # Fetch human-readable label from the appropriate table
        subject = uid[:8]
        try:
            if scope == 'bulletins':
                c.execute("SELECT subject FROM bulletins WHERE unique_id = ? LIMIT 1", (uid,))
                r = c.fetchone()
                if r:
                    subject = str(r[0] or uid[:8])
            elif scope == 'mail':
                c.execute("SELECT subject FROM mail WHERE unique_id = ? LIMIT 1", (uid,))
                r = c.fetchone()
                if r:
                    subject = str(r[0] or uid[:8])
            elif scope == 'channels':
                c.execute("SELECT content FROM channel_comments WHERE unique_id = ? LIMIT 1", (uid,))
                r = c.fetchone()
                if r:
                    subject = (str(r[0] or '')[:40]) or uid[:8]
        except Exception:
            pass

        active_items.append({
            'scope': scope,
            'unique_id': uid,
            'subject': subject,
            'peer': info['peer'],
            'last_hashmiss_at': info['last_hashmiss_at'],
            'request_count': info['request_count'],
            'sent_frames': sent_frames,
        })

    # Sort by last_hashmiss_at descending (most recently active first)
    active_items.sort(key=lambda x: x['last_hashmiss_at'], reverse=True)

    return {
        'local_counts': local_counts,
        'peers': peers,
        'active_items': active_items,
    }


def _get_syncstate_max_age_seconds() -> int:
    raw = str(os.getenv('BBS_SYNCSTATE_MAX_AGE_SECONDS', '1800')).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 1800


def _is_peer_state_fresh(reported_at: str) -> bool:
    max_age_seconds = _get_syncstate_max_age_seconds()
    if max_age_seconds <= 0:
        return True
    try:
        reported_dt = datetime.strptime(str(reported_at or ''), '%Y-%m-%d %H:%M:%S')
    except Exception:
        return True
    age_seconds = (datetime.now() - reported_dt).total_seconds()
    return age_seconds <= float(max_age_seconds)


def get_mismatched_peer_nodes(expected_peer_nodes=None) -> set:
    """Return peer node IDs whose advertised counts differ from local counts.

    expected_peer_nodes: optional iterable used to scope mismatch checks to the
    currently configured sync peers.
    """
    local = get_local_record_counts()
    expected = set(expected_peer_nodes or [])
    mismatched = set()
    zork_save_sync_enabled = is_zork_save_sync_enabled()
    for row in get_peer_sync_states():
        peer = str(row[0])
        if expected and peer not in expected:
            continue
        if not _is_peer_state_fresh(row[13] if len(row) > 13 else ''):
            continue
        pb, pm, pc, pz, pp, ps = (int(row[1]), int(row[2]), int(row[3]), int(row[4]), int(row[5]), int(row[6]))
        phb, phm, phc, phz, php, phs = (
            str(row[7] or ''), str(row[8] or ''), str(row[9] or ''),
            str(row[10] or ''), str(row[11] or ''), str(row[12] or ''),
        )
        if (
            pb != int(local.get('bulletins', 0))
            or pm != int(local.get('mail', 0))
            or pc != int(local.get('channels', 0))
            or (zork_save_sync_enabled and pz != int(local.get('zork_saves', 0)))
            or pp != int(local.get('profiles', 0))
            or ps != int(local.get('game_scores', 0))
            or (phb and phb != str(local.get('bulletins_hash', '')))
            or (phm and phm != str(local.get('mail_hash', '')))
            or (phc and phc != str(local.get('channels_hash', '')))
            or (zork_save_sync_enabled and phz and phz != str(local.get('zork_saves_hash', '')))
            or (php and php != str(local.get('profiles_hash', '')))
            or (phs and phs != str(local.get('game_scores_hash', '')))
        ):
            mismatched.add(peer)
    return mismatched


def get_mismatched_peer_scopes(expected_peer_nodes=None) -> dict:
    """Return mismatched scopes per peer for targeted hash manifest requests."""
    local = get_local_record_counts()
    expected = set(expected_peer_nodes or [])
    by_peer = {}
    zork_save_sync_enabled = is_zork_save_sync_enabled()

    for row in get_peer_sync_states():
        peer = str(row[0])
        if expected and peer not in expected:
            continue
        if not _is_peer_state_fresh(row[13] if len(row) > 13 else ''):
            continue

        scopes = []
        pb, pm, pc, pz, pp, ps = (int(row[1]), int(row[2]), int(row[3]), int(row[4]), int(row[5]), int(row[6]))
        phb, phm, phc, phz, php, phs = (
            str(row[7] or ''), str(row[8] or ''), str(row[9] or ''),
            str(row[10] or ''), str(row[11] or ''), str(row[12] or ''),
        )

        if pb != int(local.get('bulletins', 0)) or (phb and phb != str(local.get('bulletins_hash', ''))):
            scopes.append('bulletins')
        if pm != int(local.get('mail', 0)) or (phm and phm != str(local.get('mail_hash', ''))):
            scopes.append('mail')
        if pc != int(local.get('channels', 0)) or (phc and phc != str(local.get('channels_hash', ''))):
            scopes.append('channels')
        if zork_save_sync_enabled and (pz != int(local.get('zork_saves', 0)) or (phz and phz != str(local.get('zork_saves_hash', '')))):
            scopes.append('zork_saves')
        if pp != int(local.get('profiles', 0)) or (php and php != str(local.get('profiles_hash', ''))):
            scopes.append('profiles')
        if ps != int(local.get('game_scores', 0)) or (phs and phs != str(local.get('game_scores_hash', ''))):
            scopes.append('game_scores')

        if scopes:
            # Tombstones are only relevant for content scopes where deletion drift exists.
            # Skip tombstone reconcile when we know both sides have none (-1 = unknown, include to be safe).
            peer_tombstones = int(row[14]) if len(row) > 14 and row[14] is not None else -1
            local_tombstones = int(local.get('tombstones', 0))
            if ('bulletins' in scopes or 'mail' in scopes or 'channels' in scopes or 'zork_saves' in scopes) and \
               (local_tombstones > 0 or peer_tombstones != 0):
                scopes.append('tombstones')
            by_peer[peer] = scopes

    return by_peer


def get_scopes_to_request_repair(peer_node_id: str, candidate_scopes: list) -> list:
    """Filter *candidate_scopes* to those where we have <= peer's count.

    Sending HASHREQ only makes sense when we have fewer (or equal) records than
    the peer — the peer will send its manifest and we can diff to find what we
    are missing.  When we have *more* records we should be the manifest sender,
    not the requester; the peer will send HASHREQ after it sees our SYNCSTATE.
    Requesting in both directions simultaneously causes bidirectional HASHZ
    storms that saturate the half-duplex LoRa channel.

    Tombstones are always included because deletions can propagate in either
    direction.  Equal-count-but-different-hash scopes are also included; that
    case requires a manifest exchange to find differing records.
    """
    if not peer_node_id or not candidate_scopes:
        return list(candidate_scopes)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT bulletins, mail, channels, zork_saves, profiles, game_scores "
        "FROM peer_sync_state WHERE peer_node_id = ?",
        (peer_node_id,),
    )
    row = c.fetchone()
    if row is None:
        return list(candidate_scopes)

    local = get_local_record_counts()
    peer_counts = {
        'bulletins': int(row[0]),
        'mail':      int(row[1]),
        'channels':  int(row[2]),
        'zork_saves': int(row[3]),
        'profiles':  int(row[4]),
        'game_scores': int(row[5]),
    }
    result = []
    for scope in candidate_scopes:
        if scope == 'tombstones':
            result.append(scope)
            continue
        local_count = int(local.get(scope, 0))
        peer_count = peer_counts.get(scope, 0)
        if local_count <= peer_count:
            result.append(scope)
        elif local_count <= 10:
            # Small scope (≤10 records locally): request peer's manifest even
            # when we have more.  The reconcile will push our extra records
            # directly without relying on the peer receiving our SYNCSTATE
            # (which may be lost on an 80% lossy half-duplex LoRa link).
            # Large manifests (channels etc.) are excluded here to avoid
            # bidirectional HASHZ storms; those peers will request via HASHREQ
            # after observing our SYNCSTATE.
            result.append(scope)
        # else: large scope and local has more — peer will request our manifest
    return result


# ---------------------------------------------------------------------------
# Per-peer full-push phase persistence
# ---------------------------------------------------------------------------

def mark_peer_phase_synced(peer_node_id: str, phase: str) -> None:
    """Record that a full push of *phase* has completed for this peer.

    Phase names: 'mail', 'bulletins', 'channels', 'profiles', 'game'.
    """
    if not peer_node_id or not phase:
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT phases_complete FROM peer_sync_state WHERE peer_node_id = ?", (peer_node_id,))
    row = c.fetchone()
    if row is None:
        c.execute(
            "INSERT INTO peer_sync_state (peer_node_id, bulletins, mail, channels, zork_saves, profiles, "
            "game_scores, bulletins_hash, mail_hash, channels_hash, zork_saves_hash, profiles_hash, "
            "game_scores_hash, reported_at, phases_complete) VALUES (?, 0, 0, 0, 0, 0, 0, '', '', '', '', '', '', ?, ?)",
            (peer_node_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), phase),
        )
    else:
        existing = set(str(row[0] or '').split(',')) - {''}
        existing.add(str(phase))
        c.execute(
            "UPDATE peer_sync_state SET phases_complete = ? WHERE peer_node_id = ?",
            (','.join(sorted(existing)), peer_node_id),
        )
    conn.commit()


def clear_peer_phases_complete(peer_node_id: str) -> None:
    """Clear all phase completions for a peer so a full re-push will be triggered."""
    if not peer_node_id:
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE peer_sync_state SET phases_complete = '' WHERE peer_node_id = ?", (peer_node_id,))
    conn.commit()


def clear_all_peer_phases_complete() -> None:
    """Clear phase completions for every peer (called on manual full-resync)."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE peer_sync_state SET phases_complete = ''")
    conn.commit()


def get_peers_with_phase_complete(phase: str) -> set:
    """Return the set of peer node IDs that have completed the given full-push phase."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT peer_node_id, phases_complete FROM peer_sync_state "
        "WHERE phases_complete IS NOT NULL AND phases_complete != ''"
    )
    result = set()
    for peer_id, phases_str in c.fetchall():
        phases = set(str(phases_str or '').split(',')) - {''}
        if str(phase) in phases:
            result.add(str(peer_id))
    return result


def get_incomplete_record_uids() -> dict:
    """Return unique_ids of records with content_complete = 0, grouped by scope.

    Used by the server's periodic repair scan to re-request truncated content
    from peer nodes.
    """
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT unique_id FROM bulletins WHERE content_complete = 0")
    bulletins_uids = [row[0] for row in c.fetchall()]
    c.execute("SELECT unique_id FROM mail WHERE content_complete = 0")
    mail_uids = [row[0] for row in c.fetchall()]
    c.execute("SELECT unique_id FROM channel_comments WHERE content_complete = 0")
    channel_uids = [f"comment:{row[0]}" for row in c.fetchall()]
    return {
        'bulletins': bulletins_uids,
        'mail': mail_uids,
        'channels': channel_uids,
    }


def reset_incomplete_record(scope: str, uid: str) -> bool:
    """Last-resort heal for a record stuck incomplete: clear its partial content
    and any buffered (possibly boundary-misaligned) continuations, so the next
    full resend from a single peer rebuilds it cleanly from offset 0 with
    self-consistent chunk boundaries. expected_content_length is preserved so
    completion is still detectable. Returns True if a row was reset.

    This breaks the deadlock where continuations from senders with different
    chunk boundaries leave a permanent middle gap that no single frame fills."""
    table_map = {'bulletins': 'bulletins', 'mail': 'mail', 'channels': 'channel_comments'}
    buffer_map = {
        'bulletins': _pending_bulletin_continuations,
        'mail': _pending_mail_continuations,
        'channels': _pending_channel_comment_continuations,
    }
    table = table_map.get(scope)
    if not table:
        return False
    real_uid = uid[len('comment:'):] if uid.startswith('comment:') else uid
    buffer_map[scope].pop(str(real_uid), None)  # drop misaligned buffered chunks
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        f"UPDATE {table} SET content = '', content_complete = 0 WHERE unique_id = ? AND COALESCE(content_complete, 1) = 0",
        (str(real_uid),),
    )
    changed = c.rowcount and c.rowcount > 0
    conn.commit()
    if changed:
        logging.info(f"Reset stuck incomplete {scope} record {real_uid} for clean rebuild")
    return bool(changed)


def _ensure_local_only_columns(cursor) -> None:
    """Backfill schema changes on existing deployments."""
    cursor.execute("PRAGMA table_info(bulletins)")
    bulletin_cols = {row[1] for row in cursor.fetchall()}
    if 'local_only' not in bulletin_cols:
        cursor.execute("ALTER TABLE bulletins ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0")

    cursor.execute("PRAGMA table_info(channels)")
    channel_cols = {row[1] for row in cursor.fetchall()}
    if 'local_only' not in channel_cols:
        cursor.execute("ALTER TABLE channels ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0")

    cursor.execute("PRAGMA table_info(peer_sync_state)")
    peer_cols = {row[1] for row in cursor.fetchall()}
    if 'profiles' not in peer_cols:
        cursor.execute("ALTER TABLE peer_sync_state ADD COLUMN profiles INTEGER NOT NULL DEFAULT 0")
    if 'game_scores' not in peer_cols:
        cursor.execute("ALTER TABLE peer_sync_state ADD COLUMN game_scores INTEGER NOT NULL DEFAULT 0")
    if 'bulletins_hash' not in peer_cols:
        cursor.execute("ALTER TABLE peer_sync_state ADD COLUMN bulletins_hash TEXT NOT NULL DEFAULT ''")
    if 'mail_hash' not in peer_cols:
        cursor.execute("ALTER TABLE peer_sync_state ADD COLUMN mail_hash TEXT NOT NULL DEFAULT ''")
    if 'channels_hash' not in peer_cols:
        cursor.execute("ALTER TABLE peer_sync_state ADD COLUMN channels_hash TEXT NOT NULL DEFAULT ''")
    if 'zork_saves_hash' not in peer_cols:
        cursor.execute("ALTER TABLE peer_sync_state ADD COLUMN zork_saves_hash TEXT NOT NULL DEFAULT ''")
    if 'profiles_hash' not in peer_cols:
        cursor.execute("ALTER TABLE peer_sync_state ADD COLUMN profiles_hash TEXT NOT NULL DEFAULT ''")
    if 'game_scores_hash' not in peer_cols:
        cursor.execute("ALTER TABLE peer_sync_state ADD COLUMN game_scores_hash TEXT NOT NULL DEFAULT ''")
    if 'phases_complete' not in peer_cols:
        cursor.execute("ALTER TABLE peer_sync_state ADD COLUMN phases_complete TEXT NOT NULL DEFAULT ''")
    if 'tombstones' not in peer_cols:
        cursor.execute("ALTER TABLE peer_sync_state ADD COLUMN tombstones INTEGER NOT NULL DEFAULT -1")


def _dedupe_channels_and_create_unique_index(cursor) -> None:
    """Deduplicate channels by (name,url) and enforce uniqueness.

    Older databases may already contain duplicates. We keep the lowest id as
    the canonical row, repoint comments to it, delete the extras, then add a
    unique index so future duplicate sync packets are ignored at insert time.
    """
    cursor.execute(
        """
        SELECT name, url, MIN(id) AS keeper_id
        FROM channels
        GROUP BY name, url
        HAVING COUNT(*) > 1
        """
    )
    duplicate_groups = cursor.fetchall()

    for name, url, keeper_id in duplicate_groups:
        cursor.execute(
            "SELECT id FROM channels WHERE name = ? AND url = ? AND id != ?",
            (name, url, keeper_id),
        )
        duplicate_ids = [row[0] for row in cursor.fetchall()]
        for dup_id in duplicate_ids:
            cursor.execute(
                "UPDATE channel_comments SET channel_id = ? WHERE channel_id = ?",
                (keeper_id, dup_id),
            )
            cursor.execute("DELETE FROM channels WHERE id = ?", (dup_id,))

    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_name_url_unique ON channels(name, url)"
    )


def _dedupe_content_records_by_unique_id(cursor, table_name: str, extra_columns: tuple[str, ...] = ()) -> None:
    """Deduplicate bulletin/mail rows keyed by unique_id.

    Older databases can contain replay duplicates for the same synced record.
    Hash manifests key by unique_id, so duplicates keep peers mismatched even if
    the visible content looks similar. Keep the longest content row, prefer a
    syncable bulletin row on ties, then fall back to the lowest id.
    """
    cursor.execute(
        f"""
        SELECT unique_id
        FROM {table_name}
        GROUP BY unique_id
        HAVING COUNT(*) > 1
        """
    )
    duplicate_keys = [row[0] for row in cursor.fetchall()]

    for unique_id in duplicate_keys:
        select_cols = ["id", *extra_columns, "content", "unique_id"]
        cursor.execute(
            f"SELECT {', '.join(select_cols)} FROM {table_name} WHERE unique_id = ?",
            (unique_id,),
        )
        rows = cursor.fetchall()
        if len(rows) < 2:
            continue

        def _sort_key(row: tuple) -> tuple:
            row_id = int(row[0])
            local_only = int(row[1]) if extra_columns == ('local_only',) else 0
            content = str(row[-2] or '')
            return (-len(content), local_only, row_id)

        keeper = min(rows, key=_sort_key)
        keeper_id = int(keeper[0])
        keeper_content = str(keeper[-2] or '')
        if table_name == 'bulletins':
            keeper_local_only = min(int(row[1]) for row in rows)
            cursor.execute(
                "UPDATE bulletins SET content = ?, local_only = ? WHERE id = ?",
                (keeper_content, keeper_local_only, keeper_id),
            )
        else:
            cursor.execute(
                f"UPDATE {table_name} SET content = ? WHERE id = ?",
                (keeper_content, keeper_id),
            )

        duplicate_ids = [int(row[0]) for row in rows if int(row[0]) != keeper_id]
        for dup_id in duplicate_ids:
            cursor.execute(f"DELETE FROM {table_name} WHERE id = ?", (dup_id,))


def _dedupe_messages_and_create_unique_indexes(cursor) -> None:
    _dedupe_content_records_by_unique_id(cursor, 'bulletins', ('local_only',))
    _dedupe_content_records_by_unique_id(cursor, 'mail')
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bulletins_unique_id_unique ON bulletins(unique_id)"
    )
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mail_unique_id_unique ON mail(unique_id)"
    )


def add_channel(name, url, bbs_nodes=None, interface=None, local_only: bool = False):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO channels (name, url, local_only) VALUES (?, ?, ?)", (name, url, 1 if local_only else 0))
    conn.commit()

    if c.rowcount == 0:
        logging.info(f"Duplicate channel ignored (name={name}, url={url})")
        c.execute("SELECT id FROM channels WHERE name = ? AND url = ?", (name, url))
        row = c.fetchone()
        return int(row[0]) if row else None

    channel_id = int(c.lastrowid or 0)
    if channel_id <= 0:
        c.execute("SELECT id FROM channels WHERE name = ? AND url = ?", (name, url))
        row = c.fetchone()
        channel_id = int(row[0]) if row else 0

    if local_only:
        return channel_id

    if bbs_nodes and interface:
        send_channel_to_bbs_nodes(name, url, bbs_nodes, interface)
    return channel_id


def get_channels():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, url FROM channels")
    return c.fetchall()


def get_channel_categories():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, COUNT(*) FROM channels GROUP BY name ORDER BY name COLLATE NOCASE")
    return c.fetchall()


def get_channels_by_name(channel_name):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, name, url FROM channels WHERE name = ? ORDER BY id DESC", (channel_name,))
    return c.fetchall()


def get_channel_by_id(channel_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, name, url FROM channels WHERE id = ?", (channel_id,))
    return c.fetchone()


def get_channel_id_by_name_url(name: str, url: str) -> Optional[int]:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id FROM channels WHERE name = ? AND url = ?", (name, url))
    row = c.fetchone()
    if not row:
        return None
    return int(row[0])


def add_channel_comment(channel_id, sender_short_name, content, bbs_nodes=None, interface=None, unique_id=None, comment_date=None, source_node_id=None, source_timestamp=None):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, name, url FROM channels WHERE id = ?", (channel_id,))
    channel = c.fetchone()
    if channel is None:
        raise ValueError("channel_id not found")

    date = str(comment_date or datetime.now().strftime('%Y-%m-%d %H:%M'))
    now_iso = datetime.now(timezone.utc).isoformat()
    if not unique_id:
        # New locally created record — stamp this node as the source
        unique_id = str(uuid.uuid4())
        if source_node_id is None:
            source_node_id = get_local_node_id()
            source_timestamp = now_iso
    else:
        c.execute(
            "SELECT id, content, expected_content_length FROM channel_comments WHERE unique_id = ? LIMIT 1",
            (unique_id,),
        )
        row = c.fetchone()
        if row:
            existing_id = int(row[0])
            existing_content = str(row[1] or '')
            existing_expected_length = row[2] if len(row) > 2 else None
            merged_content, status = _merge_continuation_content(existing_content, 0, content)
            normalized_expected_length = _normalize_expected_content_length(merged_content, existing_expected_length)
            if status != 'duplicate':
                c.execute(
                    "UPDATE channel_comments SET channel_id = ?, sender_short_name = ?, date = ?, content = ?, expected_content_length = ?, content_complete = ? WHERE id = ?",
                    (
                        int(channel[0]),
                        sender_short_name,
                        date,
                        merged_content,
                        normalized_expected_length,
                        _content_complete_flag(merged_content, normalized_expected_length),
                        existing_id,
                    ),
                )
                conn.commit()
            # Backfill source provenance if the existing row has NULL and we now have a value
            if source_node_id is not None:
                c.execute(
                    "UPDATE channel_comments SET source_node_id = COALESCE(source_node_id, ?), source_timestamp = COALESCE(source_timestamp, ?) WHERE id = ?",
                    (source_node_id, source_timestamp, existing_id),
                )
                conn.commit()
            _flush_pending_expected_content_length('channel_comments', unique_id, _pending_channel_comment_expected_lengths, 'channel comment')
            clear_sync_tombstone('channels', f"comment:{unique_id}")
            return unique_id

    c.execute(
        "INSERT INTO channel_comments (channel_id, sender_short_name, date, content, unique_id, expected_content_length, content_complete, source_node_id, source_timestamp, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (int(channel[0]), sender_short_name, date, content, unique_id, len(str(content or '')), 1, source_node_id, source_timestamp, now_iso)
    )
    conn.commit()
    _local_nid = get_local_node_id()
    if _local_nid and source_node_id == _local_nid:
        try_dual_write(
            c, origin_node_id=_local_nid,
            event_type='upsert', scope='channel_comments', target_uid=unique_id,
            payload={'channel_id': int(channel[0]), 'sender_short_name': sender_short_name, 'date': date},
            created_at=now_iso,
        )
        conn.commit()
    _flush_pending_expected_content_length('channel_comments', unique_id, _pending_channel_comment_expected_lengths, 'channel comment')
    clear_sync_tombstone('channels', f"comment:{unique_id}")
    if bbs_nodes and interface:
        send_channel_comment_to_bbs_nodes(
            make_channel_manifest_key(channel[1], channel[2]),
            sender_short_name,
            date,
            content,
            unique_id,
            bbs_nodes,
            interface,
            source_node_id=source_node_id,
            source_timestamp=source_timestamp,
        )
    return unique_id


def _get_channel_id_by_compact_key(compact_key: str) -> Optional[int]:
    """Look up a channel by its compact '~'-prefixed hash key.

    Used when a CHANNELCOMMENT frame was sent with a compact key because the
    full base64(name+url) manifest key exceeded the Meshtastic packet limit.
    """
    conn = get_db_connection()
    c = conn.cursor()
    for row in c.execute("SELECT id, name, url FROM channels WHERE local_only = 0"):
        full_key = make_channel_manifest_key(row[1], row[2])
        if compact_channel_manifest_key(full_key) == compact_key:
            return int(row[0])
    return None


def add_channel_comment_by_manifest_key(channel_key: str, sender_short_name: str, comment_date: str, content: str, unique_id: str, source_node_id: Optional[str] = None, source_timestamp: Optional[str] = None) -> Optional[str]:
    decoded = decode_channel_manifest_key(channel_key)
    if decoded:
        channel_name, channel_url = decoded
        channel_id = get_channel_id_by_name_url(channel_name, channel_url)
        if channel_id is None:
            channel_id = add_channel(channel_name, channel_url)
    else:
        # Compact key fallback: '~' + 8-char blake2b hash of the full manifest key
        channel_id = _get_channel_id_by_compact_key(channel_key)
        if channel_id is None:
            logging.warning(f"CHANNELCOMMENT with unrecognized channel key ignored: {channel_key!r}")
            return None
    return add_channel_comment(channel_id, sender_short_name, content, unique_id=unique_id, comment_date=comment_date, source_node_id=source_node_id, source_timestamp=source_timestamp)


def get_channel_comments(channel_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, sender_short_name, date, content, unique_id, COALESCE(content_complete, 1) AS content_complete, COALESCE(expected_content_length, LENGTH(content)) AS expected_content_length, source_node_id, source_timestamp, received_at FROM channel_comments WHERE channel_id = ? ORDER BY date DESC, unique_id DESC, id DESC",
        (channel_id,)
    )
    return c.fetchall()


def get_channel_comment_by_unique_id(unique_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT ch.name, ch.url, cc.sender_short_name, cc.date, cc.content, cc.unique_id, COALESCE(cc.expected_content_length, LENGTH(cc.content)), COALESCE(cc.content_complete, 1), cc.source_node_id, cc.source_timestamp "
        "FROM channel_comments cc JOIN channels ch ON ch.id = cc.channel_id WHERE cc.unique_id = ?",
        (str(unique_id),),
    )
    return c.fetchone()


def delete_channel_comment(unique_id, bbs_nodes, interface):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM channel_comments WHERE unique_id = ?", (str(unique_id),))
    conn.commit()
    _local_nid = get_local_node_id()
    if _local_nid:
        try_dual_write(
            c, origin_node_id=_local_nid,
            event_type='delete', scope='channel_comments', target_uid=str(unique_id), payload={},
        )
        conn.commit()
    record_sync_tombstone('channels', f"comment:{unique_id}")
    if bbs_nodes and interface:
        send_delete_channel_comment_to_bbs_nodes(str(unique_id), bbs_nodes, interface)


def append_channel_comment_content(unique_id: str, char_offset: Optional[int], additional_content: str) -> None:
    _apply_continuation_update('channel_comments', unique_id, char_offset, additional_content, _pending_channel_comment_continuations, 'channel comment')



def add_bulletin(board, sender_short_name, subject, content, bbs_nodes, interface, unique_id=None, local_only: bool = False, date=None, source_node_id=None, source_timestamp=None):
    conn = get_db_connection()
    c = conn.cursor()
    original_date = str(date).strip() if date else datetime.now().strftime('%Y-%m-%d %H:%M')
    now_iso = datetime.now(timezone.utc).isoformat()
    if not unique_id:
        # New locally created record — stamp this node as the source
        unique_id = str(uuid.uuid4())
        if source_node_id is None:
            source_node_id = get_local_node_id()
            source_timestamp = now_iso
    else:
        # Idempotency for sync replays
        c.execute(
            "SELECT id, content, local_only, expected_content_length FROM bulletins WHERE unique_id = ? LIMIT 1",
            (unique_id,),
        )
        row = c.fetchone()
        if row:
            existing_id = int(row[0])
            existing_content = str(row[1] or '')
            existing_local_only = int(row[2] or 0)
            existing_expected_length = row[3] if len(row) > 3 else None
            merged_content, status = _merge_continuation_content(existing_content, 0, content)
            merged_local_only = 1 if (local_only and existing_local_only) else 0
            normalized_expected_length = _normalize_expected_content_length(merged_content, existing_expected_length)
            if status != 'duplicate' or merged_local_only != existing_local_only:
                c.execute(
                    "UPDATE bulletins SET board = ?, sender_short_name = ?, subject = ?, content = ?, local_only = ?, expected_content_length = ?, content_complete = ? WHERE id = ?",
                    (
                        board,
                        sender_short_name,
                        subject,
                        merged_content,
                        merged_local_only,
                        normalized_expected_length,
                        _content_complete_flag(merged_content, normalized_expected_length),
                        existing_id,
                    ),
                )
                conn.commit()
            # Backfill source provenance if the existing row has NULL and we now have a value
            if source_node_id is not None:
                c.execute(
                    "UPDATE bulletins SET source_node_id = COALESCE(source_node_id, ?), source_timestamp = COALESCE(source_timestamp, ?) WHERE id = ?",
                    (source_node_id, source_timestamp, existing_id),
                )
                conn.commit()
            _flush_pending_expected_content_length('bulletins', unique_id, _pending_bulletin_expected_lengths, 'bulletin')
            clear_sync_tombstone('bulletins', str(unique_id))
            return unique_id
    c.execute(
        "INSERT INTO bulletins (board, sender_short_name, date, subject, content, unique_id, local_only, expected_content_length, content_complete, source_node_id, source_timestamp, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            board,
            sender_short_name,
            original_date,
            subject,
            content,
            unique_id,
            1 if local_only else 0,
            len(str(content or '')),
            1,
            source_node_id,
            source_timestamp,
            now_iso,
        ),
    )
    conn.commit()
    _local_nid = get_local_node_id()
    if _local_nid and source_node_id == _local_nid:
        try_dual_write(
            c, origin_node_id=_local_nid,
            event_type='upsert', scope='bulletins', target_uid=unique_id,
            payload={'board': board, 'sender_short_name': sender_short_name,
                     'subject': subject, 'date': original_date,
                     'local_only': 1 if local_only else 0},
            created_at=now_iso,
        )
        conn.commit()
    _flush_pending_expected_content_length('bulletins', unique_id, _pending_bulletin_expected_lengths, 'bulletin')
    clear_sync_tombstone('bulletins', str(unique_id))
    if (not local_only) and bbs_nodes and interface:
        send_bulletin_to_bbs_nodes(board, sender_short_name, subject, content, unique_id, bbs_nodes, interface, date=original_date, source_node_id=source_node_id, source_timestamp=source_timestamp)

    # New logic to send group chat notification for urgent bulletins
    if board.lower() == "urgent":
        notification_message = f"💥NEW URGENT BULLETIN💥\nFrom: {sender_short_name}\nTitle: {subject}\nDM 'CB,,Urgent' to view"
        if interface:
            send_message(notification_message, BROADCAST_NUM, interface)

    return unique_id


def upsert_synced_user_profile(user_id: str, short_name: str, long_name: str,
                               first_seen: str, last_seen: str,
                               messages_sent: int, bio: str) -> None:
    """Apply profile metadata learned from a peer sync payload."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        '''INSERT INTO user_profiles (user_id, short_name, long_name, first_seen, last_seen, messages_sent, bio)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             short_name = excluded.short_name,
             long_name = excluded.long_name,
             first_seen = CASE WHEN excluded.first_seen < first_seen THEN excluded.first_seen ELSE first_seen END,
             last_seen = CASE WHEN excluded.last_seen > last_seen THEN excluded.last_seen ELSE last_seen END,
             messages_sent = CASE WHEN excluded.messages_sent > messages_sent THEN excluded.messages_sent ELSE messages_sent END,
             bio = excluded.bio''',
        (str(user_id), short_name, long_name, first_seen, last_seen, int(messages_sent), bio[:100]),
    )
    conn.commit()


def upsert_synced_game_score(user_id: str, game_id: str, short_name: str,
                             score: int, max_score: int, moves: int, achieved_at: str) -> None:
    """Apply game score metadata learned from a peer sync payload."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        '''INSERT INTO game_scores (user_id, game_id, short_name, score, max_score, moves, achieved_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, game_id) DO UPDATE SET
                         -- Deterministic merge for eventual consistency across peers.
                         short_name   = CASE
                                                            WHEN excluded.score > score THEN excluded.short_name
                                                            WHEN excluded.score < score THEN short_name
                                                            WHEN excluded.short_name < short_name THEN excluded.short_name
                                                            ELSE short_name
                                                        END,
                         score        = CASE WHEN excluded.score > score THEN excluded.score ELSE score END,
                         max_score    = CASE
                                                            WHEN excluded.score > score THEN excluded.max_score
                                                            WHEN excluded.score = score AND excluded.max_score > max_score THEN excluded.max_score
                                                            ELSE max_score
                                                        END,
                         moves        = CASE
                                                            WHEN excluded.score > score THEN excluded.moves
                                                            WHEN excluded.score = score AND excluded.moves < moves THEN excluded.moves
                                                            ELSE moves
                                                        END,
                         achieved_at  = CASE
                                                            WHEN excluded.score > score THEN excluded.achieved_at
                                                            WHEN excluded.score = score AND excluded.moves < moves THEN excluded.achieved_at
                                                            WHEN excluded.score = score AND excluded.moves = moves AND excluded.achieved_at < achieved_at THEN excluded.achieved_at
                                                            ELSE achieved_at
                                                        END''',
        (str(user_id), game_id, short_name, int(score), int(max_score), int(moves), achieved_at),
    )
    conn.commit()


def get_bulletins(board):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, CASE WHEN COALESCE(content_complete, 1) = 0 THEN subject || ' [incomplete]' ELSE subject END, sender_short_name, date, unique_id FROM bulletins WHERE board = ? COLLATE NOCASE",
        (board,),
    )
    return c.fetchall()

def get_bulletin_content(bulletin_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT sender_short_name, date, subject, content, unique_id, COALESCE(content_complete, 1), COALESCE(expected_content_length, LENGTH(content)) FROM bulletins WHERE id = ?",
        (bulletin_id,),
    )
    return c.fetchone()


def delete_bulletin(unique_id, bbs_nodes, interface):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM bulletins WHERE unique_id = ?", (unique_id,))
    conn.commit()
    _local_nid = get_local_node_id()
    if _local_nid:
        try_dual_write(
            c, origin_node_id=_local_nid,
            event_type='delete', scope='bulletins', target_uid=str(unique_id), payload={},
        )
        conn.commit()
    record_sync_tombstone('bulletins', str(unique_id))
    send_delete_bulletin_to_bbs_nodes(unique_id, bbs_nodes, interface)


def append_bulletin_content(unique_id: str, char_offset: Optional[int], additional_content: str) -> None:
    """Append a continuation chunk to an existing bulletin's content.

    char_offset is the expected current length of the stored content before
    this chunk is applied. When retransmissions overlap content already stored,
    the overlapping slice is rewritten in place so a replayed repair pass can
    heal truncated records without deleting the row first. Pass None to skip
    the offset guard (legacy/test usage).
    """
    _apply_continuation_update('bulletins', unique_id, char_offset, additional_content, _pending_bulletin_continuations, 'bulletin')

def add_mail(sender_id, sender_short_name, recipient_id, subject, content, bbs_nodes, interface, unique_id=None, date=None, source_node_id=None, source_timestamp=None):
    conn = get_db_connection()
    c = conn.cursor()
    original_date = str(date).strip() if date else datetime.now().strftime('%Y-%m-%d %H:%M')
    now_iso = datetime.now(timezone.utc).isoformat()
    if not unique_id:
        # New locally created record — stamp this node as the source
        unique_id = str(uuid.uuid4())
        if source_node_id is None:
            source_node_id = get_local_node_id()
            source_timestamp = now_iso
    else:
        # Idempotency for sync replays
        c.execute("SELECT id, content, expected_content_length FROM mail WHERE unique_id = ? LIMIT 1", (unique_id,))
        row = c.fetchone()
        if row:
            existing_id = int(row[0])
            existing_content = str(row[1] or '')
            existing_expected_length = row[2] if len(row) > 2 else None
            merged_content, status = _merge_continuation_content(existing_content, 0, content)
            normalized_expected_length = _normalize_expected_content_length(merged_content, existing_expected_length)
            if status != 'duplicate':
                c.execute(
                    "UPDATE mail SET sender = ?, sender_short_name = ?, recipient = ?, subject = ?, content = ?, expected_content_length = ?, content_complete = ? WHERE id = ?",
                    (
                        sender_id,
                        sender_short_name,
                        recipient_id,
                        subject,
                        merged_content,
                        normalized_expected_length,
                        _content_complete_flag(merged_content, normalized_expected_length),
                        existing_id,
                    ),
                )
                conn.commit()
            # Backfill source provenance if the existing row has NULL and we now have a value
            if source_node_id is not None:
                c.execute(
                    "UPDATE mail SET source_node_id = COALESCE(source_node_id, ?), source_timestamp = COALESCE(source_timestamp, ?) WHERE id = ?",
                    (source_node_id, source_timestamp, existing_id),
                )
                conn.commit()
            _flush_pending_expected_content_length('mail', unique_id, _pending_mail_expected_lengths, 'mail')
            clear_sync_tombstone('mail', str(unique_id))
            return unique_id
    c.execute(
        "INSERT INTO mail (sender, sender_short_name, recipient, date, subject, content, unique_id, expected_content_length, content_complete, source_node_id, source_timestamp, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sender_id,
            sender_short_name,
            recipient_id,
            original_date,
            subject,
            content,
            unique_id,
            len(str(content or '')),
            1,
            source_node_id,
            source_timestamp,
            now_iso,
        ),
    )
    conn.commit()
    _local_nid = get_local_node_id()
    if _local_nid and source_node_id == _local_nid:
        try_dual_write(
            c, origin_node_id=_local_nid,
            event_type='upsert', scope='mail', target_uid=unique_id,
            payload={'sender_id': sender_id, 'sender_short_name': sender_short_name,
                     'recipient_id': recipient_id, 'subject': subject, 'date': original_date},
            created_at=now_iso,
        )
        conn.commit()
    _flush_pending_expected_content_length('mail', unique_id, _pending_mail_expected_lengths, 'mail')
    clear_sync_tombstone('mail', str(unique_id))
    if bbs_nodes and interface:
        send_mail_to_bbs_nodes(sender_id, sender_short_name, recipient_id, subject, content, unique_id, bbs_nodes, interface, date=original_date, source_node_id=source_node_id, source_timestamp=source_timestamp)
    return unique_id

def get_mail(recipient_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, sender_short_name, CASE WHEN COALESCE(content_complete, 1) = 0 THEN subject || ' [incomplete]' ELSE subject END, date, unique_id FROM mail WHERE recipient = ?",
        (recipient_id,),
    )
    return c.fetchall()

def get_mail_content(mail_id, recipient_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT sender_short_name, date, subject, content, unique_id, COALESCE(content_complete, 1), COALESCE(expected_content_length, LENGTH(content)) FROM mail WHERE id = ? and recipient = ?",
        (mail_id, recipient_id,),
    )
    return c.fetchone()

def delete_mail(unique_id, recipient_id, bbs_nodes, interface):
    conn = get_db_connection()
    c = conn.cursor()
    try:
        if recipient_id is None:
            c.execute("DELETE FROM mail WHERE unique_id = ?", (unique_id,))
        else:
            c.execute("DELETE FROM mail WHERE unique_id = ? and recipient = ?", (unique_id, recipient_id,))
        conn.commit()
        _local_nid = get_local_node_id()
        if _local_nid:
            try_dual_write(
                c, origin_node_id=_local_nid,
                event_type='delete', scope='mail', target_uid=str(unique_id), payload={},
            )
            conn.commit()
        record_sync_tombstone('mail', str(unique_id))
        if bbs_nodes and interface:
            send_delete_mail_to_bbs_nodes(unique_id, bbs_nodes, interface)
        if c.rowcount == 0:
            logging.info(f"Delete mail noop for unique_id: {unique_id} (already missing).")
        else:
            logging.info(f"Mail with unique_id: {unique_id} deleted.")
    except Exception as e:
        logging.error(f"Error deleting mail with unique_id {unique_id}: {e}")
        raise


def append_mail_content(unique_id: str, char_offset: Optional[int], additional_content: str) -> None:
    """Append a continuation chunk to an existing mail message's content.

    See append_bulletin_content for char_offset semantics.
    """
    _apply_continuation_update('mail', unique_id, char_offset, additional_content, _pending_mail_continuations, 'mail')


def get_sender_id_by_mail_id(mail_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT sender FROM mail WHERE id = ?", (mail_id,))
    result = c.fetchone()
    if result:
        return result[0]
    return None


def upsert_zork_save(user_id: int, save_data: bytes, game_id: str = 'zork1') -> None:
    _ensure_zork_saves_table()
    conn = get_db_connection()
    c = conn.cursor()
    updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute(
        '''INSERT INTO zork_saves (user_id, game_id, save_data, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id, game_id) DO UPDATE SET
             save_data = excluded.save_data,
             updated_at = excluded.updated_at''',
        (str(user_id), game_id, save_data, updated_at)
    )
    conn.commit()
    clear_sync_tombstone('zork_saves', f"{user_id}:{game_id}")


def _should_replace_zork_save(existing_updated_at: str, existing_save_data: bytes, incoming_updated_at: str, incoming_save_data: bytes) -> bool:
    existing_ts = str(existing_updated_at or '')
    incoming_ts = str(incoming_updated_at or '')
    if incoming_ts > existing_ts:
        return True
    if incoming_ts < existing_ts:
        return False

    existing_payload = existing_save_data or b''
    incoming_payload = incoming_save_data or b''
    if len(incoming_payload) != len(existing_payload):
        return len(incoming_payload) > len(existing_payload)

    incoming_hash = _compact_row_hash((incoming_payload,))
    existing_hash = _compact_row_hash((existing_payload,))
    return incoming_hash > existing_hash


def upsert_synced_zork_save(user_id: str, game_id: str, save_data: bytes, updated_at: str) -> None:
    """Apply a zork save payload received from peer sync.

    Only replaces local save when incoming updated_at is newer.
    """
    _ensure_zork_saves_table()
    key = f"{user_id}:{game_id}"
    tombstone_deleted_at = get_sync_tombstone_deleted_at('zork_saves', key)
    if tombstone_deleted_at and tombstone_deleted_at >= str(updated_at or ''):
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT save_data, updated_at FROM zork_saves WHERE user_id = ? AND game_id = ?",
        (str(user_id), str(game_id)),
    )
    row = c.fetchone()
    if row and not _should_replace_zork_save(str(row[1] or ''), row[0], str(updated_at or ''), save_data):
        return

    c.execute(
        '''INSERT INTO zork_saves (user_id, game_id, save_data, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id, game_id) DO UPDATE SET
             save_data = excluded.save_data,
             updated_at = excluded.updated_at''',
        (str(user_id), str(game_id), save_data, updated_at),
    )
    conn.commit()
    clear_sync_tombstone('zork_saves', key)


def get_zork_save(user_id: int, game_id: str = 'zork1') -> Optional[bytes]:
    _ensure_zork_saves_table()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT save_data FROM zork_saves WHERE user_id = ? AND game_id = ?", (str(user_id), game_id))
    row = c.fetchone()
    if not row:
        return None
    return row[0]


def apply_synced_zork_save_delete(user_id: str, game_id: str, deleted_at: str) -> bool:
    _ensure_zork_saves_table()
    key = f"{user_id}:{game_id}"
    normalized_deleted_at = str(deleted_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    record_sync_tombstone_at('zork_saves', key, normalized_deleted_at)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT updated_at FROM zork_saves WHERE user_id = ? AND game_id = ?",
        (str(user_id), str(game_id)),
    )
    row = c.fetchone()
    if row and str(row[0] or '') > normalized_deleted_at:
        return False

    c.execute("DELETE FROM zork_saves WHERE user_id = ? AND game_id = ?", (str(user_id), str(game_id)))
    conn.commit()
    return True


def delete_zork_save(user_id: int, game_id: str = 'zork1', bbs_nodes=None, interface=None, deleted_at: Optional[str] = None) -> None:
    _ensure_zork_saves_table()
    normalized_deleted_at = str(deleted_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    apply_synced_zork_save_delete(str(user_id), str(game_id), normalized_deleted_at)
    if bbs_nodes and interface:
        send_delete_zork_save_to_bbs_nodes(user_id, game_id, normalized_deleted_at, bbs_nodes, interface)


# ---------------------------------------------------------------------------
# User profiles
# ---------------------------------------------------------------------------

def auto_upsert_user_profile(user_id: int, short_name: str, long_name: str) -> None:
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute(
        '''INSERT INTO user_profiles (user_id, short_name, long_name, first_seen, last_seen, messages_sent, bio)
           VALUES (?, ?, ?, ?, ?, 1, '')
           ON CONFLICT(user_id) DO UPDATE SET
             short_name = excluded.short_name,
             long_name = excluded.long_name,
             last_seen = excluded.last_seen,
             messages_sent = messages_sent + 1''',
        (str(user_id), short_name, long_name, now, now)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Mesh client roster ("who's in range") -- see mesh_clients schema comment
# in initialize_database() for why this exists and how 'last_seen' works.
# ---------------------------------------------------------------------------

def upsert_mesh_clients(rows: list[dict]) -> None:
    """Bulk-upsert one sweep's worth of a link's node roster in a single
    transaction. Called once per link per periodic sweep (server.py's
    persist_mesh_clients) with EVERY node currently in that link's
    interface.nodes, not once per node -- a 100+ node mesh must not cost
    100+ separate commits every 5-30s.

    Each row is a dict with keys: link_name, node_id, node_num, protocol,
    short_name, long_name, hw_model, role, battery_level, last_heard_epoch
    (the last two may be None -- only Meshtastic reports them). 'first_seen'
    is preserved across updates via COALESCE-by-conflict (only set on the
    row's first-ever insert); 'last_seen' is always refreshed to now, since
    being in this call's row list already means the node was present in
    interface.nodes this cycle.
    """
    if not rows:
        return
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.executemany(
        '''INSERT INTO mesh_clients
           (link_name, node_id, node_num, protocol, short_name, long_name, hw_model, role,
            battery_level, last_heard_epoch, first_seen, last_seen)
           VALUES (:link_name, :node_id, :node_num, :protocol, :short_name, :long_name, :hw_model, :role,
                   :battery_level, :last_heard_epoch, :now, :now)
           ON CONFLICT(link_name, node_id) DO UPDATE SET
             node_num = excluded.node_num,
             protocol = excluded.protocol,
             short_name = excluded.short_name,
             long_name = excluded.long_name,
             hw_model = excluded.hw_model,
             role = excluded.role,
             battery_level = excluded.battery_level,
             last_heard_epoch = excluded.last_heard_epoch,
             last_seen = excluded.last_seen''',
        [
            {
                'link_name': row.get('link_name', ''),
                'node_id': row.get('node_id', ''),
                'node_num': str(row['node_num']) if row.get('node_num') is not None else None,
                'protocol': row.get('protocol') or '',
                'short_name': row.get('short_name') or '',
                'long_name': row.get('long_name') or '',
                'hw_model': row.get('hw_model') or '',
                'role': row.get('role') or '',
                'battery_level': row.get('battery_level'),
                'last_heard_epoch': row.get('last_heard_epoch'),
                'now': now,
            }
            for row in rows
        ],
    )
    conn.commit()


def get_mesh_clients(
    link_name: Optional[str] = None,
    seen_within_seconds: Optional[int] = None,
) -> list[dict]:
    """Known mesh clients, most-recently-seen first.

    ``link_name`` scopes to one radio/MQTT link; omit for the whole-node
    roster (web_admin.py's Clients page). ``seen_within_seconds`` returns
    only devices seen that recently -- used by MQTT publishing so a metered
    bridge carries currently-present nodes rather than everything ever
    recorded. Filtering here (not in Python) keeps it on the
    idx_mesh_clients_last_seen index.
    """
    conn = get_db_connection()
    c = conn.cursor()
    columns = (
        'link_name, node_id, node_num, protocol, short_name, long_name, '
        'hw_model, role, battery_level, last_heard_epoch, first_seen, last_seen'
    )
    clauses = []
    params: list = []
    if link_name:
        clauses.append('link_name = ?')
        params.append(link_name)
    if seen_within_seconds and seen_within_seconds > 0:
        cutoff = (datetime.now() - timedelta(seconds=int(seen_within_seconds))
                  ).strftime('%Y-%m-%d %H:%M:%S')
        clauses.append('last_seen >= ?')
        params.append(cutoff)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    c.execute(f'SELECT {columns} FROM mesh_clients{where} ORDER BY last_seen DESC', params)
    keys = [
        'link_name', 'node_id', 'node_num', 'protocol', 'short_name', 'long_name',
        'hw_model', 'role', 'battery_level', 'last_heard_epoch', 'first_seen', 'last_seen',
    ]
    return [dict(zip(keys, row)) for row in c.fetchall()]


def get_user_profile(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT user_id, short_name, long_name, first_seen, last_seen, messages_sent, bio "
        "FROM user_profiles WHERE user_id = ?",
        (str(user_id),)
    )
    return c.fetchone()


def update_user_bio(user_id: int, bio: str) -> None:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE user_profiles SET bio = ? WHERE user_id = ?", (bio[:100], str(user_id)))
    conn.commit()


# ---------------------------------------------------------------------------
# Multi-device user accounts
#
# Purely additive on top of user_profiles: linking is opt-in, and a node
# that never links to an account behaves exactly as before this existed.
# Keyed on the STABLE STRING node id (see _ensure_accounts_tables' docstring
# for why -- never the numeric id user_profiles/user_states use).
# ---------------------------------------------------------------------------

_LINK_CODE_ATTEMPT_WINDOW_SECONDS = 3600


def create_account() -> str:
    """Create a new, empty (no linked nodes yet) account and return its id."""
    conn = get_db_connection()
    c = conn.cursor()
    account_id = uuid.uuid4().hex
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute(
        "INSERT INTO accounts (account_id, alias, created_at) VALUES (?, '', ?)",
        (account_id, now)
    )
    conn.commit()
    return account_id


def get_account_id_for_node(node_id: str) -> Optional[str]:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT account_id FROM linked_nodes WHERE node_id = ?", (str(node_id),))
    row = c.fetchone()
    return row[0] if row else None


def get_linked_node_ids(account_id: str) -> list:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT node_id FROM linked_nodes WHERE account_id = ? ORDER BY linked_at",
        (account_id,)
    )
    return [row[0] for row in c.fetchall()]


def get_linked_nodes_detail(account_id: str) -> list:
    """[(node_id, network, linked_at), ...] for display (DM 'list my devices'
    and the web admin account detail page)."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT node_id, network, linked_at FROM linked_nodes WHERE account_id = ? ORDER BY linked_at",
        (account_id,)
    )
    return c.fetchall()


def count_linked_nodes(account_id: str) -> int:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM linked_nodes WHERE account_id = ?", (account_id,))
    return int(c.fetchone()[0])


def link_node_to_account(node_id: str, account_id: str, network: str, now: Optional[str] = None) -> None:
    conn = get_db_connection()
    c = conn.cursor()
    now = now or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute(
        '''INSERT INTO linked_nodes (node_id, account_id, network, linked_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(node_id) DO UPDATE SET
             account_id = excluded.account_id,
             network = excluded.network,
             linked_at = excluded.linked_at''',
        (str(node_id), account_id, network, now)
    )
    conn.commit()


def unlink_node(node_id: str) -> bool:
    """Remove node_id from its account. Refuses (returns False) if this
    would leave the account with zero linked nodes -- an account can never
    reach zero devices this way. Also returns False if node_id isn't linked
    to anything."""
    conn = get_db_connection()
    c = conn.cursor()
    account_id = get_account_id_for_node(node_id)
    if account_id is None:
        return False
    if count_linked_nodes(account_id) <= 1:
        return False
    c.execute("DELETE FROM linked_nodes WHERE node_id = ?", (str(node_id),))
    conn.commit()
    return True


def get_account_alias(account_id: str) -> str:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT alias FROM accounts WHERE account_id = ?", (account_id,))
    row = c.fetchone()
    return row[0] if row else ''


def set_account_alias(account_id: str, alias: str) -> None:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE accounts SET alias = ? WHERE account_id = ?", (str(alias).strip()[:20], account_id))
    conn.commit()


def create_link_code(account_id: str, requested_by_node_id: str, ttl_minutes: int = 10) -> str:
    """Generate a 6-digit, single-use, time-limited code for linking a new
    device to account_id. Uses the `secrets` module (not `random`) for the
    same reason web_admin.py's CSRF tokens do -- this gates account
    membership, so it must be unguessable, not just look random."""
    conn = get_db_connection()
    c = conn.cursor()
    now_dt = datetime.now()
    now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    expires_at = (now_dt + timedelta(minutes=ttl_minutes)).strftime('%Y-%m-%d %H:%M:%S')

    code = f"{secrets.randbelow(1_000_000):06d}"
    # Vanishingly unlikely to collide with another still-live code, but a
    # PRIMARY KEY collision would otherwise raise -- regenerate defensively.
    for _ in range(5):
        c.execute("SELECT 1 FROM link_codes WHERE code = ?", (code,))
        if not c.fetchone():
            break
        code = f"{secrets.randbelow(1_000_000):06d}"

    c.execute(
        '''INSERT INTO link_codes (code, account_id, requested_by_node_id, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?)''',
        (code, account_id, str(requested_by_node_id), now_str, expires_at)
    )
    conn.commit()
    return code


def redeem_link_code(code: str, node_id: str, network: str, max_devices: int = 6) -> tuple:
    """Attempt to link node_id to whichever account owns `code`.

    Returns (ok: bool, message: str). `node_id` MUST be the packet's string
    sender_node_id (fromId) -- never the numeric sender_id/node number --
    since that numeric id can collide between a MeshCore and a Meshtastic
    node in dual-radio bridge mode (see _ensure_accounts_tables). This
    function does not know or care which numeric id was involved; the
    caller is responsible for passing the right (string) identity.
    """
    conn = get_db_connection()
    c = conn.cursor()
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    c.execute(
        "SELECT code, account_id, expires_at FROM link_codes WHERE code = ? AND consumed_at IS NULL",
        (str(code),)
    )
    row = c.fetchone()
    if row is None or not secrets.compare_digest(row[0], str(code)):
        return False, "Invalid or already-used code."
    _code, account_id, expires_at = row
    if expires_at < now_str:
        return False, "That code has expired. Request a new one."

    existing_account = get_account_id_for_node(node_id)
    if existing_account == account_id:
        return False, "This device is already linked to that account."
    if existing_account is not None:
        return False, "This device is already linked to a different account. Unlink it there first."
    if count_linked_nodes(account_id) >= max_devices:
        return False, "That account already has the maximum number of linked devices."

    link_node_to_account(node_id, account_id, network, now=now_str)
    c.execute(
        "UPDATE link_codes SET consumed_at = ?, consumed_by_node_id = ? WHERE code = ?",
        (now_str, str(node_id), _code)
    )
    conn.commit()
    return True, "Device linked successfully."


def record_link_attempt(node_id: str, kind: str, success: bool) -> None:
    """kind is 'request_code' or 'submit_code'. Backed by a DB table
    (link_attempts), not the in-memory deque/dict shape used elsewhere in
    this codebase (e.g. gateway.py's rate limiter) -- deliberately, so
    limits survive a restart and leave an audit trail, per the assumed
    adversarial threat model for account linking."""
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute(
        "INSERT INTO link_attempts (node_id, kind, attempted_at, success) VALUES (?, ?, ?, ?)",
        (str(node_id), kind, now, 1 if success else 0)
    )
    conn.commit()


def count_recent_link_attempts(node_id: str, kind: str, window_seconds: int = _LINK_CODE_ATTEMPT_WINDOW_SECONDS) -> int:
    conn = get_db_connection()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(seconds=window_seconds)).strftime('%Y-%m-%d %H:%M:%S')
    c.execute(
        "SELECT COUNT(*) FROM link_attempts WHERE node_id = ? AND kind = ? AND attempted_at > ?",
        (str(node_id), kind, cutoff)
    )
    return int(c.fetchone()[0])


def link_rate_limit_ok(node_id: str, kind: str, max_per_hour: int) -> bool:
    """True if node_id has made fewer than max_per_hour attempts of `kind`
    ('request_code' or 'submit_code') in the last hour. Callers should check
    this BEFORE attempting the action, and always call record_link_attempt()
    after (success or failure) so the count reflects reality."""
    if max_per_hour <= 0:
        return True
    return count_recent_link_attempts(node_id, kind) < max_per_hour


def account_authorized(node_id: str, configured_allow_lists) -> bool:
    """True if node_id itself is in any of configured_allow_lists, OR
    node_id is linked to an account and any sibling linked node is. Falls
    through to plain membership (today's exact behavior) when node_id has
    no account -- unlinked nodes are completely unaffected by this function.
    configured_allow_lists is a list of node-id lists (e.g. [allow_list,
    allow_list2] for the urgent board's two per-radio lists in dual-radio
    bridge mode) so the union check can genuinely enforce 'any node on any
    configured list authorizes the whole account' without needing this
    function to know about RadioLink/dual-radio internals at all."""
    union = set()
    for lst in configured_allow_lists or []:
        union.update(lst or [])
    if str(node_id) in union:
        return True
    try:
        account_id = get_account_id_for_node(node_id)
    except sqlite3.Error:
        # Accounts schema not present on this connection (e.g. a DB that
        # predates this feature and hasn't gone through
        # initialize_database() yet). Degrade to plain membership -- the
        # check above already covers that -- rather than raising, so
        # callers that don't (yet) have an accounts-aware DB keep working
        # exactly as before this feature existed.
        return False
    if account_id is None:
        return False
    try:
        return any(sibling in union for sibling in get_linked_node_ids(account_id))
    except sqlite3.Error:
        return False


def list_accounts() -> list:
    """[(account_id, alias, created_at, device_count), ...] for the web
    admin's /accounts list page, newest first."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        '''SELECT a.account_id, a.alias, a.created_at, COUNT(l.node_id)
           FROM accounts a
           LEFT JOIN linked_nodes l ON l.account_id = a.account_id
           GROUP BY a.account_id, a.alias, a.created_at
           ORDER BY a.created_at DESC'''
    )
    return c.fetchall()


def get_account(account_id: str):
    """(account_id, alias, created_at) or None."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT account_id, alias, created_at FROM accounts WHERE account_id = ?", (account_id,))
    return c.fetchone()


# ---------------------------------------------------------------------------
# Game scores / scoreboard
# ---------------------------------------------------------------------------

def upsert_game_score(user_id: int, game_id: str, short_name: str,
                      score: int, max_score: int, moves: int) -> None:
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # Only promote the record when the new score is strictly higher
    c.execute(
        '''INSERT INTO game_scores (user_id, game_id, short_name, score, max_score, moves, achieved_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, game_id) DO UPDATE SET
                         short_name   = CASE
                                                            WHEN excluded.score > score THEN excluded.short_name
                                                            WHEN excluded.score < score THEN short_name
                                                            WHEN excluded.short_name < short_name THEN excluded.short_name
                                                            ELSE short_name
                                                        END,
                         score        = CASE WHEN excluded.score > score THEN excluded.score ELSE score END,
                         max_score    = CASE
                                                            WHEN excluded.score > score THEN excluded.max_score
                                                            WHEN excluded.score = score AND excluded.max_score > max_score THEN excluded.max_score
                                                            ELSE max_score
                                                        END,
                         moves        = CASE
                                                            WHEN excluded.score > score THEN excluded.moves
                                                            WHEN excluded.score = score AND excluded.moves < moves THEN excluded.moves
                                                            ELSE moves
                                                        END,
                         achieved_at  = CASE
                                                            WHEN excluded.score > score THEN excluded.achieved_at
                                                            WHEN excluded.score = score AND excluded.moves < moves THEN excluded.achieved_at
                                                            WHEN excluded.score = score AND excluded.moves = moves AND excluded.achieved_at < achieved_at THEN excluded.achieved_at
                                                            ELSE achieved_at
                                                        END''',
        (str(user_id), game_id, short_name, score, max_score, moves, now)
    )
    conn.commit()


def get_game_scoreboard(game_id: str, limit: int = 5) -> list:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        '''SELECT short_name, score, max_score, moves FROM game_scores
           WHERE game_id = ?
           ORDER BY score DESC, moves ASC
           LIMIT ?''',
        (game_id, limit)
    )
    return c.fetchall()


def get_user_game_scores(user_id: int) -> list:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT game_id, score, max_score FROM game_scores WHERE user_id = ? ORDER BY score DESC",
        (str(user_id),)
    )
    return c.fetchall()


def get_hall_of_fame() -> list:
    """Return the top scorer per game (highest score), ordered by game_id."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        '''SELECT gs.game_id, gs.short_name, gs.score, gs.max_score, gs.moves
           FROM game_scores gs
           INNER JOIN (
               SELECT game_id, MAX(score) AS top_score
               FROM game_scores
               GROUP BY game_id
           ) best ON gs.game_id = best.game_id AND gs.score = best.top_score
           ORDER BY gs.game_id ASC'''
    )
    return c.fetchall()


def _compact_row_hash(values: tuple) -> str:
    digest = hashlib.blake2b(digest_size=8)
    for value in values:
        if value is None:
            blob = b''
        elif isinstance(value, bytes):
            blob = value
        else:
            blob = str(value).encode('utf-8')
        digest.update(len(blob).to_bytes(4, 'big'))
        digest.update(blob)
    return base64.urlsafe_b64encode(digest.digest()).decode('ascii').rstrip('=')


def get_record_hash_manifest(scope: str) -> dict:
    """Return a per-record hash map for selective mismatch repair.

    Supported scopes: bulletins, mail, channels, profiles, game_scores, zork_saves, tombstones.
    """
    conn = get_db_connection()
    c = conn.cursor()
    manifest = {}

    if scope == 'bulletins':
        for row in c.execute(
            "SELECT board, sender_short_name, subject, content, unique_id, source_node_id, source_timestamp FROM bulletins WHERE local_only = 0"
        ):
            key = str(row[4])
            manifest[key] = _compact_row_hash(row)
    elif scope == 'mail':
        for row in c.execute(
            "SELECT sender, sender_short_name, recipient, subject, content, unique_id, source_node_id, source_timestamp FROM mail"
        ):
            key = str(row[5])
            manifest[key] = _compact_row_hash(row)
    elif scope == 'channels':
        # Channel records only — comments are a separate sub-scope.
        for row in c.execute(
            "SELECT name, url FROM channels WHERE local_only = 0"
        ):
            key = make_channel_manifest_key(row[0], row[1])
            manifest[key] = _compact_row_hash(row)
    elif scope == 'channel_comments':
        # Comments only — keyed by plain UUID (no 'comment:' prefix needed since
        # the scope already identifies the record type).
        for row in c.execute(
            "SELECT ch.name, ch.url, cc.sender_short_name, cc.date, cc.content, cc.unique_id, "
            "COALESCE(cc.expected_content_length, LENGTH(cc.content)), COALESCE(cc.content_complete, 1), "
            "cc.source_node_id, cc.source_timestamp "
            "FROM channel_comments cc JOIN channels ch ON ch.id = cc.channel_id WHERE ch.local_only = 0"
        ):
            manifest[str(row[5])] = _compact_row_hash(row)
    elif scope == 'profiles':
        for row in c.execute(
            "SELECT user_id, short_name, long_name, bio FROM user_profiles"
        ):
            key = str(row[0])
            manifest[key] = _compact_row_hash(row)
    elif scope == 'game_scores':
        for row in c.execute(
            "SELECT user_id, game_id, short_name, score, max_score, moves, achieved_at FROM game_scores"
        ):
            key = f"{row[0]}:{row[1]}"
            manifest[key] = _compact_row_hash(row)
    elif scope == 'zork_saves':
        if not is_zork_save_sync_enabled():
            return manifest
        _ensure_zork_saves_table()
        for row in c.execute(
            "SELECT user_id, game_id, save_data, updated_at FROM zork_saves"
        ):
            key = f"{row[0]}:{row[1]}"
            manifest[key] = _compact_row_hash(row)
    elif scope == 'tombstones':
        _ensure_deleted_sync_tombstones_table()
        for row in c.execute(
            "SELECT tombstone_key, deleted_at FROM deleted_sync_tombstones"
        ):
            if not is_zork_save_sync_enabled() and str(row[0]).startswith('zork_saves:'):
                continue
            key = str(row[0])
            manifest[key] = _compact_row_hash(row)

    return manifest


def get_bulletin_by_unique_id(unique_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT board, sender_short_name, date, subject, content, unique_id, source_node_id, source_timestamp FROM bulletins WHERE unique_id = ? ORDER BY LENGTH(content) DESC, id ASC",
        (unique_id,),
    )
    return c.fetchone()


def get_mail_by_unique_id(unique_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT sender, sender_short_name, recipient, date, subject, content, unique_id, source_node_id, source_timestamp FROM mail WHERE unique_id = ? ORDER BY LENGTH(content) DESC, id ASC",
        (unique_id,),
    )
    return c.fetchone()


def get_channel_by_manifest_key(manifest_key: str):
    # Compact keys (~XXXXXXXX) are produced when the full base64(name+url) key
    # would overflow a HASHMISS request frame.  Resolve by scanning channels.
    if str(manifest_key).startswith('~'):
        conn = get_db_connection()
        c = conn.cursor()
        for row in c.execute("SELECT name, url FROM channels WHERE local_only = 0"):
            full_key = make_channel_manifest_key(row[0], row[1])
            if compact_channel_manifest_key(full_key) == manifest_key:
                return (row[0], row[1])
        return None
    decoded = decode_channel_manifest_key(manifest_key)
    if not decoded:
        return None
    name, url = decoded
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT name, url FROM channels WHERE name = ? AND url = ? AND local_only = 0",
        (name, url),
    )
    return c.fetchone()


def get_profile_by_user_id(user_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT user_id, short_name, long_name, first_seen, last_seen, messages_sent, bio FROM user_profiles WHERE user_id = ?",
        (str(user_id),),
    )
    return c.fetchone()


def get_game_score_by_user_and_game(user_id: str, game_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT user_id, game_id, short_name, score, max_score, moves, achieved_at FROM game_scores WHERE user_id = ? AND game_id = ?",
        (str(user_id), str(game_id)),
    )
    return c.fetchone()


def get_zork_save_row_by_user_and_game(user_id: str, game_id: str):
    _ensure_zork_saves_table()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT user_id, game_id, save_data, updated_at FROM zork_saves WHERE user_id = ? AND game_id = ?",
        (str(user_id), str(game_id)),
    )
    return c.fetchone()


def log_connection_event(
    sender_num: Optional[int],
    sender_node_id: Optional[str],
    sender_short_name: Optional[str],
    to_id: Optional[int],
    message_type: str,
    event_text: str,
) -> None:
    _ensure_connection_events_table()
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute(
        '''INSERT INTO connection_events
           (event_time, sender_num, sender_node_id, sender_short_name, to_id, message_type, event_text)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (
            now,
            str(sender_num) if sender_num is not None else None,
            sender_node_id,
            sender_short_name or '',
            str(to_id) if to_id is not None else None,
            message_type,
            event_text,
        ),
    )
    _prune_connection_events(conn, _get_max_connection_log_rows())
    conn.commit()


def get_connection_events_since(last_id: int = 0, limit: int = 100) -> list:
    _ensure_connection_events_table()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        '''SELECT id, event_time, sender_num, sender_node_id, sender_short_name, to_id, message_type, event_text
           FROM connection_events
           WHERE id > ?
           ORDER BY id ASC
           LIMIT ?''',
        (last_id, limit),
    )
    return c.fetchall()


def sync_mail_to_nodes(bbs_nodes: list, interface, delay_ms: Optional[int] = None) -> dict:
    """P1 — highest priority: direct mail messages."""
    if not bbs_nodes or not interface:
        return {'mail_synced': 0, 'total_messages': 0}
    conn = get_db_connection()
    c = conn.cursor()
    if delay_ms is None:
        delay_ms = get_full_sync_delay_ms(interface)
    delay_seconds = max(0.0, float(delay_ms) / 1000.0)
    c.execute("SELECT COUNT(*) FROM mail")
    total_items = c.fetchone()[0]
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _update_sync_progress(in_progress=True, progress_percent=0 if total_items else 100,
                          completed_items=0, total_items=total_items, remaining_items=total_items,
                          current_phase='syncing_mail', target_nodes=[str(n) for n in bbs_nodes],
                          started_at=now_str, last_updated_at=now_str, last_result='Running (P1: mail)')
    mail_synced = 0
    try:
        if total_items and delay_seconds > 0:
            time.sleep(delay_seconds)
        c.execute("SELECT sender, sender_short_name, recipient, date, subject, content, unique_id, source_node_id, source_timestamp FROM mail")
        for sender_id, sender_short_name, recipient_id, mail_date, subject, content, unique_id, source_node_id, source_timestamp in c.fetchall():
            send_mail_to_bbs_nodes(sender_id, sender_short_name, recipient_id, subject, content, unique_id,
                                   bbs_nodes, interface, date=mail_date,
                                   source_node_id=source_node_id, source_timestamp=source_timestamp)
            mail_synced += 1
            pct = int((mail_synced * 100) / total_items) if total_items else 100
            _update_sync_progress(progress_percent=pct, completed_items=mail_synced,
                                  remaining_items=max(total_items - mail_synced, 0),
                                  current_phase='syncing_mail',
                                  last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        logging.info(f"P1 mail sync: sent {mail_synced} messages to {len(bbs_nodes)} peer(s)")
        _update_sync_progress(in_progress=False, progress_percent=100, completed_items=total_items,
                              total_items=total_items, remaining_items=0, current_phase='mail_complete',
                              last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                              last_result=f"P1 mail done: {mail_synced} sent")
        return {'mail_synced': mail_synced, 'total_messages': mail_synced}
    except Exception as e:
        logging.error(f"Error during mail sync: {e}")
        _update_sync_progress(in_progress=False, current_phase='error',
                              last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                              last_result=f"Error: {e}")
        raise


def sync_bulletins_to_nodes(bbs_nodes: list, interface, delay_ms: Optional[int] = None) -> dict:
    """P2 — public bulletin board posts."""
    if not bbs_nodes or not interface:
        return {'bulletins_synced': 0, 'total_messages': 0}
    conn = get_db_connection()
    c = conn.cursor()
    if delay_ms is None:
        delay_ms = get_full_sync_delay_ms(interface)
    delay_seconds = max(0.0, float(delay_ms) / 1000.0)
    c.execute("SELECT COUNT(*) FROM bulletins WHERE local_only = 0")
    total_items = c.fetchone()[0]
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _update_sync_progress(in_progress=True, progress_percent=0 if total_items else 100,
                          completed_items=0, total_items=total_items, remaining_items=total_items,
                          current_phase='syncing_bulletins', target_nodes=[str(n) for n in bbs_nodes],
                          started_at=now_str, last_updated_at=now_str, last_result='Running (P2: bulletins)')
    bulletins_synced = 0
    try:
        if total_items and delay_seconds > 0:
            time.sleep(delay_seconds)
        c.execute("SELECT board, sender_short_name, date, subject, content, unique_id, source_node_id, source_timestamp FROM bulletins WHERE local_only = 0")
        for board, sender_short_name, bulletin_date, subject, content, unique_id, source_node_id, source_timestamp in c.fetchall():
            send_bulletin_to_bbs_nodes(board, sender_short_name, subject, content, unique_id, bbs_nodes, interface, date=bulletin_date,
                                       source_node_id=source_node_id, source_timestamp=source_timestamp)
            bulletins_synced += 1
            pct = int((bulletins_synced * 100) / total_items) if total_items else 100
            _update_sync_progress(progress_percent=pct, completed_items=bulletins_synced,
                                  remaining_items=max(total_items - bulletins_synced, 0),
                                  current_phase='syncing_bulletins',
                                  last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        logging.info(f"P2 bulletin sync: sent {bulletins_synced} bulletins to {len(bbs_nodes)} peer(s)")
        _update_sync_progress(in_progress=False, progress_percent=100, completed_items=total_items,
                              total_items=total_items, remaining_items=0, current_phase='bulletins_complete',
                              last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                              last_result=f"P2 bulletins done: {bulletins_synced} sent")
        return {'bulletins_synced': bulletins_synced, 'total_messages': bulletins_synced}
    except Exception as e:
        logging.error(f"Error during bulletin sync: {e}")
        _update_sync_progress(in_progress=False, current_phase='error',
                              last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                              last_result=f"Error: {e}")
        raise


def sync_channels_to_nodes(bbs_nodes: list, interface, delay_ms: Optional[int] = None) -> dict:
    """P3 — channel directory entries plus channel comments."""
    if not bbs_nodes or not interface:
        return {'channels_synced': 0, 'total_messages': 0}
    conn = get_db_connection()
    c = conn.cursor()
    if delay_ms is None:
        delay_ms = get_full_sync_delay_ms(interface)
    delay_seconds = max(0.0, float(delay_ms) / 1000.0)
    c.execute("SELECT COUNT(*) FROM channels WHERE local_only = 0")
    total_items = int(c.fetchone()[0])
    c.execute(
        "SELECT COUNT(*) FROM channel_comments cc JOIN channels ch ON ch.id = cc.channel_id WHERE ch.local_only = 0"
    )
    total_items += int(c.fetchone()[0])
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _update_sync_progress(in_progress=True, progress_percent=0 if total_items else 100,
                          completed_items=0, total_items=total_items, remaining_items=total_items,
                          current_phase='syncing_channels', target_nodes=[str(n) for n in bbs_nodes],
                          started_at=now_str, last_updated_at=now_str, last_result='Running (P3: channels)')
    channels_synced = 0
    try:
        if total_items and delay_seconds > 0:
            time.sleep(delay_seconds)
        c.execute("SELECT name, url FROM channels WHERE local_only = 0")
        for name, url in c.fetchall():
            send_channel_to_bbs_nodes(name, url, bbs_nodes, interface)
            channels_synced += 1
            pct = int((channels_synced * 100) / total_items) if total_items else 100
            _update_sync_progress(progress_percent=pct, completed_items=channels_synced,
                                  remaining_items=max(total_items - channels_synced, 0),
                                  current_phase='syncing_channels',
                                  last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        c.execute(
            "SELECT ch.name, ch.url, cc.sender_short_name, cc.date, cc.content, cc.unique_id, "
            "cc.source_node_id, cc.source_timestamp "
            "FROM channel_comments cc JOIN channels ch ON ch.id = cc.channel_id WHERE ch.local_only = 0 ORDER BY cc.date ASC, cc.unique_id ASC"
        )
        for channel_name, channel_url, sender_short_name, comment_date, content, unique_id, source_node_id, source_timestamp in c.fetchall():
            send_channel_comment_to_bbs_nodes(
                make_channel_manifest_key(channel_name, channel_url),
                sender_short_name,
                comment_date,
                content,
                unique_id,
                bbs_nodes,
                interface,
                source_node_id=source_node_id,
                source_timestamp=source_timestamp,
            )
            channels_synced += 1
            pct = int((channels_synced * 100) / total_items) if total_items else 100
            _update_sync_progress(progress_percent=pct, completed_items=channels_synced,
                                  remaining_items=max(total_items - channels_synced, 0),
                                  current_phase='syncing_channels',
                                  last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        logging.info(f"P3 channel sync: sent {channels_synced} channels to {len(bbs_nodes)} peer(s)")
        _update_sync_progress(in_progress=False, progress_percent=100, completed_items=total_items,
                              total_items=total_items, remaining_items=0, current_phase='channels_complete',
                              last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                              last_result=f"P3 channels done: {channels_synced} sent")
        return {'channels_synced': channels_synced, 'total_messages': channels_synced}
    except Exception as e:
        logging.error(f"Error during channel sync: {e}")
        _update_sync_progress(in_progress=False, current_phase='error',
                              last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                              last_result=f"Error: {e}")
        raise


def sync_profiles_to_nodes(bbs_nodes: list, interface, delay_ms: Optional[int] = None) -> dict:
    """P4 — user profile records."""
    if not bbs_nodes or not interface:
        return {'profiles_synced': 0, 'total_messages': 0}
    conn = get_db_connection()
    c = conn.cursor()
    if delay_ms is None:
        delay_ms = get_full_sync_delay_ms(interface)
    delay_seconds = max(0.0, float(delay_ms) / 1000.0)
    c.execute("SELECT COUNT(*) FROM user_profiles")
    total_items = c.fetchone()[0]
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _update_sync_progress(in_progress=True, progress_percent=0 if total_items else 100,
                          completed_items=0, total_items=total_items, remaining_items=total_items,
                          current_phase='syncing_profiles', target_nodes=[str(n) for n in bbs_nodes],
                          started_at=now_str, last_updated_at=now_str, last_result='Running (P4: profiles)')
    profiles_synced = 0
    try:
        if total_items and delay_seconds > 0:
            time.sleep(delay_seconds)
        c.execute("SELECT user_id, short_name, long_name, first_seen, last_seen, messages_sent, bio FROM user_profiles")
        for user_id, short_name, long_name, first_seen, last_seen, messages_sent, bio in c.fetchall():
            send_profile_to_bbs_nodes(user_id, short_name, long_name, first_seen, last_seen, messages_sent, bio,
                                      bbs_nodes, interface)
            profiles_synced += 1
            pct = int((profiles_synced * 100) / total_items) if total_items else 100
            _update_sync_progress(progress_percent=pct, completed_items=profiles_synced,
                                  remaining_items=max(total_items - profiles_synced, 0),
                                  current_phase='syncing_profiles',
                                  last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        logging.info(f"P4 profile sync: sent {profiles_synced} profiles to {len(bbs_nodes)} peer(s)")
        _update_sync_progress(in_progress=False, progress_percent=100, completed_items=total_items,
                              total_items=total_items, remaining_items=0, current_phase='profiles_complete',
                              last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                              last_result=f"P4 profiles done: {profiles_synced} sent")
        return {'profiles_synced': profiles_synced, 'total_messages': profiles_synced}
    except Exception as e:
        logging.error(f"Error during profile sync: {e}")
        _update_sync_progress(in_progress=False, current_phase='error',
                              last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                              last_result=f"Error: {e}")
        raise


def sync_priority_data_to_nodes(bbs_nodes: list, interface, delay_ms: Optional[int] = None) -> dict:
    """
    Priority sync wrapper: P1 mail → P2 bulletins → P3 channels → P4 profiles.
    Sends SYNCSTATE after completion so peers can compare content counts.
    Prefer calling the individual phase functions from server.py for finer-grained
    phase-completion tracking.
    """
    if not bbs_nodes or not interface:
        logging.warning("sync_priority_data_to_nodes: No bbs_nodes or interface provided")
        return {'bulletins_synced': 0, 'mail_synced': 0, 'channels_synced': 0,
                'profiles_synced': 0, 'total_messages': 0}

    m = sync_mail_to_nodes(bbs_nodes, interface, delay_ms=delay_ms)
    b = sync_bulletins_to_nodes(bbs_nodes, interface, delay_ms=delay_ms)
    ch = sync_channels_to_nodes(bbs_nodes, interface, delay_ms=delay_ms)
    pr = sync_profiles_to_nodes(bbs_nodes, interface, delay_ms=delay_ms)

    # Send SYNCSTATE after all priority phases so peers can immediately compare counts.
    local_counts = get_local_record_counts()
    send_sync_state_to_bbs_nodes(local_counts, bbs_nodes, interface)

    total = (m.get('total_messages', 0) + b.get('total_messages', 0)
             + ch.get('total_messages', 0) + pr.get('total_messages', 0))
    logging.info(f"Priority sync complete: {total} messages sent to {len(bbs_nodes)} peer(s)")
    return {
        'mail_synced': m.get('mail_synced', 0),
        'bulletins_synced': b.get('bulletins_synced', 0),
        'channels_synced': ch.get('channels_synced', 0),
        'profiles_synced': pr.get('profiles_synced', 0),
        'total_messages': total,
    }


def sync_game_data_to_nodes(bbs_nodes: list, interface, delay_ms: Optional[int] = None) -> dict:
    """
    Phase 2 sync: game scores and zork saves.
    Runs after priority content sync so game data never blocks bulletins/mail.
    """
    if not bbs_nodes or not interface:
        logging.warning("sync_game_data_to_nodes: No bbs_nodes or interface provided")
        return {'game_scores_synced': 0, 'zork_saves_synced': 0, 'total_messages': 0}

    conn = get_db_connection()
    c = conn.cursor()
    if delay_ms is None:
        delay_ms = get_full_sync_delay_ms(interface)
    delay_seconds = max(0.0, float(delay_ms) / 1000.0)
    total_messages = 0

    c.execute("SELECT COUNT(*) FROM game_scores")
    game_score_total = c.fetchone()[0]
    if is_zork_save_sync_enabled():
        c.execute("SELECT COUNT(*) FROM zork_saves")
        zork_total = c.fetchone()[0]
    else:
        zork_total = 0
    total_items = game_score_total + zork_total

    completed_items = 0

    def _progress_tick(current_phase: str) -> None:
        nonlocal completed_items
        completed_items += 1
        progress_percent = int((completed_items * 100) / total_items) if total_items > 0 else 100
        _update_sync_progress(
            progress_percent=progress_percent,
            completed_items=completed_items,
            remaining_items=max(total_items - completed_items, 0),
            current_phase=current_phase,
            last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )

    _update_sync_progress(
        in_progress=True,
        progress_percent=0 if total_items > 0 else 100,
        completed_items=0,
        total_items=total_items,
        remaining_items=total_items,
        current_phase='starting_game_sync',
        target_nodes=[str(node) for node in bbs_nodes],
        last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        last_result='Running (game data phase)',
    )

    try:
        if total_items and delay_seconds > 0:
            time.sleep(delay_seconds)
        _update_sync_progress(current_phase='syncing_game_scores')
        c.execute("SELECT user_id, game_id, short_name, score, max_score, moves, achieved_at FROM game_scores")
        game_scores_synced = 0
        for user_id, game_id, short_name, score, max_score, moves, achieved_at in c.fetchall():
            send_game_score_to_bbs_nodes(user_id, game_id, short_name, score, max_score, moves, achieved_at,
                                         bbs_nodes, interface)
            game_scores_synced += 1
            total_messages += 1
            _progress_tick('syncing_game_scores')
        logging.info(f"Game sync: Sent {game_scores_synced} game scores to {len(bbs_nodes)} peer(s)")

        zork_saves_synced = 0
        if is_zork_save_sync_enabled():
            _update_sync_progress(current_phase='syncing_zork_saves')
            c.execute("SELECT user_id, game_id, save_data, updated_at FROM zork_saves")
            # Multi-chunk ZORKSAVE frames need an inter-chunk pause floor or
            # the receiving LoRa radio drops trailing chunks under turbo.
            zork_chunk_pause = get_hash_chunk_pause_seconds(interface)
            for user_id, game_id, save_data, updated_at in c.fetchall():
                send_zork_save_to_bbs_nodes(
                    user_id, game_id, save_data, updated_at, bbs_nodes, interface,
                    pause_seconds=zork_chunk_pause,
                )
                zork_saves_synced += 1
                total_messages += 1
                _progress_tick('syncing_zork_saves')
            logging.info(f"Game sync: Sent {zork_saves_synced} zork saves to {len(bbs_nodes)} peer(s)")
        else:
            logging.info("Game sync: zork save sync disabled by config; skipping zork save phase")

        result = {
            'game_scores_synced': game_scores_synced,
            'zork_saves_synced': zork_saves_synced,
            'total_messages': total_messages,
        }
        logging.info(f"Game data sync complete: {total_messages} messages sent to {len(bbs_nodes)} peer(s)")
        _update_sync_progress(
            in_progress=False,
            progress_percent=100,
            completed_items=total_items,
            total_items=total_items,
            remaining_items=0,
            current_phase='idle',
            last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            last_result=f"Game sync done: {total_messages} messages sent",
        )
        return result

    except Exception as e:
        logging.error(f"Error during game data sync: {e}")
        _update_sync_progress(
            in_progress=False, current_phase='error',
            last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            last_result=f"Error: {e}",
        )
        raise


def sync_full_database_to_nodes(bbs_nodes: list, interface, delay_ms: Optional[int] = None) -> dict:
    """
    Full sync convenience wrapper: runs priority phase then game-data phase sequentially.
    Prefer calling sync_priority_data_to_nodes + sync_game_data_to_nodes separately in
    server.py so the node can be marked content-synced between the two phases.
    """
    if not bbs_nodes or not interface:
        logging.warning("sync_full_database_to_nodes: No bbs_nodes or interface provided")
        _update_sync_progress(
            in_progress=False, progress_percent=100, completed_items=0, total_items=0,
            remaining_items=0, current_phase='idle', target_nodes=[],
            last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            last_result='No nodes or interface provided',
        )
        return {
            'bulletins_synced': 0, 'mail_synced': 0, 'channels_synced': 0,
            'profiles_synced': 0, 'game_scores_synced': 0, 'zork_saves_synced': 0,
            'total_messages': 0,
        }

    p = sync_priority_data_to_nodes(bbs_nodes, interface, delay_ms=delay_ms)
    g = sync_game_data_to_nodes(bbs_nodes, interface, delay_ms=delay_ms)
    return {
        'bulletins_synced':  p.get('bulletins_synced', 0),
        'mail_synced':       p.get('mail_synced', 0),
        'channels_synced':   p.get('channels_synced', 0),
        'profiles_synced':   p.get('profiles_synced', 0),
        'game_scores_synced': g.get('game_scores_synced', 0),
        'zork_saves_synced': g.get('zork_saves_synced', 0),
        'total_messages':    p.get('total_messages', 0) + g.get('total_messages', 0),
    }
