import configparser
import base64
import hashlib
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from meshtastic import BROADCAST_NUM

from utils import (
    send_bulletin_to_bbs_nodes,
    send_delete_bulletin_to_bbs_nodes,
    send_delete_mail_to_bbs_nodes,
    send_mail_to_bbs_nodes, send_message, send_channel_to_bbs_nodes,
    send_sync_state_to_bbs_nodes,
    send_profile_to_bbs_nodes,
    send_game_score_to_bbs_nodes,
    send_zork_save_to_bbs_nodes,
    get_full_sync_delay_ms,
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
_pending_bulletin_expected_lengths = {}
_pending_mail_expected_lengths = {}
_PENDING_CONTINUATION_MAX_AGE_SECONDS = 1800


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


def _flush_pending_expected_content_length(table_name: str, unique_id: str, pending_store: dict, label: str) -> None:
    pending_expected = _pop_pending_expected_length(pending_store, unique_id)
    if pending_expected is None:
        return
    _apply_expected_content_length(table_name, unique_id, pending_expected, pending_store, label)


def get_database_path() -> str:
    return os.getenv('BBS_DB_PATH', 'bulletins.db')


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
        _pending_bulletin_expected_lengths.clear()
        _pending_mail_expected_lengths.clear()
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
                    content_complete INTEGER NOT NULL DEFAULT 1
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
                    content_complete INTEGER NOT NULL DEFAULT 1
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
                    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
                );''')
    c.execute('''CREATE TABLE IF NOT EXISTS zork_saves (
                    user_id TEXT NOT NULL,
                    game_id TEXT NOT NULL DEFAULT 'zork1',
                    save_data BLOB NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, game_id)
                );''')
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
                    reported_at TEXT NOT NULL
                );''')
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
    _ensure_content_status_columns(c)
    _ensure_local_only_columns(c)
    _dedupe_channels_and_create_unique_index(c)
    _dedupe_messages_and_create_unique_indexes(c)
    conn.commit()
    print(f"Database schema initialized at {get_database_path()}.")


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


def _build_tombstone_key(scope: str, record_key: str) -> str:
    return f"{scope}:{record_key}"


def record_sync_tombstone(scope: str, record_key: str) -> None:
    _ensure_deleted_sync_tombstones_table()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        '''INSERT INTO deleted_sync_tombstones (tombstone_key, deleted_at)
           VALUES (?, ?)
           ON CONFLICT(tombstone_key) DO UPDATE SET
             deleted_at = excluded.deleted_at''',
        (_build_tombstone_key(scope, record_key), datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
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


def get_local_record_counts() -> dict:
    """Return local record counts and compact hashes used by SYNCSTATE comparisons."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM bulletins WHERE local_only = 0")
    bulletins = int(c.fetchone()[0])
    c.execute("SELECT COUNT(*) FROM mail")
    mail = int(c.fetchone()[0])
    c.execute("SELECT COUNT(*) FROM channels WHERE local_only = 0")
    channels = int(c.fetchone()[0])
    _ensure_zork_saves_table()
    c.execute("SELECT COUNT(*) FROM zork_saves")
    zork_saves = int(c.fetchone()[0])
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
    channels_hash = _hash_rows(
        "SELECT name, url FROM channels WHERE local_only = 0 ORDER BY name, url"
    )
    zork_saves_hash = _hash_rows(
        "SELECT user_id, game_id, save_data, updated_at FROM zork_saves ORDER BY user_id, game_id"
    )
    profiles_hash = _hash_rows(
        "SELECT user_id, short_name, long_name, first_seen, last_seen, messages_sent, bio FROM user_profiles ORDER BY user_id"
    )
    game_scores_hash = _hash_rows(
        "SELECT user_id, game_id, short_name, score, max_score, moves, achieved_at FROM game_scores ORDER BY user_id, game_id"
    )

    return {
        'bulletins': bulletins,
        'mail': mail,
        'channels': channels,
        'zork_saves': zork_saves,
        'profiles': profiles,
        'game_scores': game_scores,
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
                           zork_saves_hash: str = '', profiles_hash: str = '', game_scores_hash: str = '') -> None:
    """Store the latest advertised SYNCSTATE counts for a peer node."""
    if not peer_node_id:
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        '''INSERT INTO peer_sync_state (
               peer_node_id, bulletins, mail, channels, zork_saves, profiles, game_scores,
               bulletins_hash, mail_hash, channels_hash, zork_saves_hash, profiles_hash, game_scores_hash,
               reported_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
               reported_at=excluded.reported_at''',
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
        ),
    )
    conn.commit()


def get_peer_sync_states() -> list:
    """Return peer-advertised record counts for diagnostics."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT peer_node_id, bulletins, mail, channels, zork_saves, profiles, game_scores, "
        "bulletins_hash, mail_hash, channels_hash, zork_saves_hash, profiles_hash, game_scores_hash, reported_at "
        "FROM peer_sync_state ORDER BY peer_node_id"
    )
    return c.fetchall()


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
            or pz != int(local.get('zork_saves', 0))
            or pp != int(local.get('profiles', 0))
            or ps != int(local.get('game_scores', 0))
            or (phb and phb != str(local.get('bulletins_hash', '')))
            or (phm and phm != str(local.get('mail_hash', '')))
            or (phc and phc != str(local.get('channels_hash', '')))
            or (phz and phz != str(local.get('zork_saves_hash', '')))
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
        if pz != int(local.get('zork_saves', 0)) or (phz and phz != str(local.get('zork_saves_hash', ''))):
            scopes.append('zork_saves')
        if pp != int(local.get('profiles', 0)) or (php and php != str(local.get('profiles_hash', ''))):
            scopes.append('profiles')
        if ps != int(local.get('game_scores', 0)) or (phs and phs != str(local.get('game_scores_hash', ''))):
            scopes.append('game_scores')

        if scopes:
            # Tombstones are only relevant for content scopes where deletion drift exists.
            if 'bulletins' in scopes or 'mail' in scopes:
                scopes.append('tombstones')
            by_peer[peer] = scopes

    return by_peer


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
        return

    if local_only:
        return

    if bbs_nodes and interface:
        send_channel_to_bbs_nodes(name, url, bbs_nodes, interface)


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


def add_channel_comment(channel_id, sender_short_name, content):
    conn = get_db_connection()
    c = conn.cursor()
    date = datetime.now().strftime('%Y-%m-%d %H:%M')
    c.execute(
        "INSERT INTO channel_comments (channel_id, sender_short_name, date, content) VALUES (?, ?, ?, ?)",
        (channel_id, sender_short_name, date, content)
    )
    conn.commit()


def get_channel_comments(channel_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT sender_short_name, date, content FROM channel_comments WHERE channel_id = ? ORDER BY id ASC",
        (channel_id,)
    )
    return c.fetchall()



def add_bulletin(board, sender_short_name, subject, content, bbs_nodes, interface, unique_id=None, local_only: bool = False):
    conn = get_db_connection()
    c = conn.cursor()
    date = datetime.now().strftime('%Y-%m-%d %H:%M')
    if not unique_id:
        unique_id = str(uuid.uuid4())
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
            _flush_pending_expected_content_length('bulletins', unique_id, _pending_bulletin_expected_lengths, 'bulletin')
            clear_sync_tombstone('bulletins', str(unique_id))
            return unique_id
    c.execute(
        "INSERT INTO bulletins (board, sender_short_name, date, subject, content, unique_id, local_only, expected_content_length, content_complete) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            board,
            sender_short_name,
            date,
            subject,
            content,
            unique_id,
            1 if local_only else 0,
            len(str(content or '')),
            1,
        ),
    )
    conn.commit()
    _flush_pending_expected_content_length('bulletins', unique_id, _pending_bulletin_expected_lengths, 'bulletin')
    clear_sync_tombstone('bulletins', str(unique_id))
    if (not local_only) and bbs_nodes and interface:
        send_bulletin_to_bbs_nodes(board, sender_short_name, subject, content, unique_id, bbs_nodes, interface)

    # New logic to send group chat notification for urgent bulletins
    if board.lower() == "urgent":
        notification_message = f"💥NEW URGENT BULLETIN💥\nFrom: {sender_short_name}\nTitle: {subject}\nDM 'CB,,Urgent' to view"
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

def add_mail(sender_id, sender_short_name, recipient_id, subject, content, bbs_nodes, interface, unique_id=None):
    conn = get_db_connection()
    c = conn.cursor()
    date = datetime.now().strftime('%Y-%m-%d %H:%M')
    if not unique_id:
        unique_id = str(uuid.uuid4())
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
            _flush_pending_expected_content_length('mail', unique_id, _pending_mail_expected_lengths, 'mail')
            clear_sync_tombstone('mail', str(unique_id))
            return unique_id
    c.execute(
        "INSERT INTO mail (sender, sender_short_name, recipient, date, subject, content, unique_id, expected_content_length, content_complete) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sender_id,
            sender_short_name,
            recipient_id,
            date,
            subject,
            content,
            unique_id,
            len(str(content or '')),
            1,
        ),
    )
    conn.commit()
    _flush_pending_expected_content_length('mail', unique_id, _pending_mail_expected_lengths, 'mail')
    clear_sync_tombstone('mail', str(unique_id))
    if bbs_nodes and interface:
        send_mail_to_bbs_nodes(sender_id, sender_short_name, recipient_id, subject, content, unique_id, bbs_nodes, interface)
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
    # TODO: ensure only recipient can read mail
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


def upsert_synced_zork_save(user_id: str, game_id: str, save_data: bytes, updated_at: str) -> None:
    """Apply a zork save payload received from peer sync.

    Only replaces local save when incoming updated_at is newer.
    """
    _ensure_zork_saves_table()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT updated_at FROM zork_saves WHERE user_id = ? AND game_id = ?",
        (str(user_id), str(game_id)),
    )
    row = c.fetchone()
    if row and row[0] and row[0] >= updated_at:
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


def get_zork_save(user_id: int, game_id: str = 'zork1') -> Optional[bytes]:
    _ensure_zork_saves_table()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT save_data FROM zork_saves WHERE user_id = ? AND game_id = ?", (str(user_id), game_id))
    row = c.fetchone()
    if not row:
        return None
    return row[0]


def delete_zork_save(user_id: int, game_id: str = 'zork1') -> None:
    _ensure_zork_saves_table()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM zork_saves WHERE user_id = ? AND game_id = ?", (str(user_id), game_id))
    conn.commit()


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
            "SELECT board, sender_short_name, subject, content, unique_id FROM bulletins WHERE local_only = 0"
        ):
            key = str(row[4])
            manifest[key] = _compact_row_hash(row)
    elif scope == 'mail':
        for row in c.execute(
            "SELECT sender, sender_short_name, recipient, subject, content, unique_id FROM mail"
        ):
            key = str(row[5])
            manifest[key] = _compact_row_hash(row)
    elif scope == 'channels':
        for row in c.execute(
            "SELECT name, url FROM channels WHERE local_only = 0"
        ):
            raw_key = f"{row[0]}\x1f{row[1]}".encode('utf-8')
            key = base64.urlsafe_b64encode(raw_key).decode('ascii').rstrip('=')
            manifest[key] = _compact_row_hash(row)
    elif scope == 'profiles':
        for row in c.execute(
            "SELECT user_id, short_name, long_name, first_seen, last_seen, messages_sent, bio FROM user_profiles"
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
            key = str(row[0])
            manifest[key] = _compact_row_hash(row)

    return manifest


def get_bulletin_by_unique_id(unique_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT board, sender_short_name, subject, content, unique_id FROM bulletins WHERE unique_id = ? ORDER BY LENGTH(content) DESC, id ASC",
        (unique_id,),
    )
    return c.fetchone()


def get_mail_by_unique_id(unique_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT sender, sender_short_name, recipient, subject, content, unique_id FROM mail WHERE unique_id = ? ORDER BY LENGTH(content) DESC, id ASC",
        (unique_id,),
    )
    return c.fetchone()


def get_channel_by_manifest_key(manifest_key: str):
    padded = manifest_key + ('=' * ((4 - len(manifest_key) % 4) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8')
    except Exception:
        return None
    if '\x1f' not in decoded:
        return None
    name, url = decoded.split('\x1f', 1)
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
        delay_ms = get_full_sync_delay_ms()
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
        c.execute("SELECT sender, sender_short_name, recipient, subject, content, unique_id FROM mail")
        for sender_id, sender_short_name, recipient_id, subject, content, unique_id in c.fetchall():
            send_mail_to_bbs_nodes(sender_id, sender_short_name, recipient_id, subject, content, unique_id,
                                   bbs_nodes, interface)
            mail_synced += 1
            pct = int((mail_synced * 100) / total_items) if total_items else 100
            _update_sync_progress(progress_percent=pct, completed_items=mail_synced,
                                  remaining_items=max(total_items - mail_synced, 0),
                                  current_phase='syncing_mail',
                                  last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            time.sleep(delay_seconds)
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
        delay_ms = get_full_sync_delay_ms()
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
        c.execute("SELECT board, sender_short_name, subject, content, unique_id FROM bulletins WHERE local_only = 0")
        for board, sender_short_name, subject, content, unique_id in c.fetchall():
            send_bulletin_to_bbs_nodes(board, sender_short_name, subject, content, unique_id, bbs_nodes, interface)
            bulletins_synced += 1
            pct = int((bulletins_synced * 100) / total_items) if total_items else 100
            _update_sync_progress(progress_percent=pct, completed_items=bulletins_synced,
                                  remaining_items=max(total_items - bulletins_synced, 0),
                                  current_phase='syncing_bulletins',
                                  last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            time.sleep(delay_seconds)
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
    """P3 — channel directory entries."""
    if not bbs_nodes or not interface:
        return {'channels_synced': 0, 'total_messages': 0}
    conn = get_db_connection()
    c = conn.cursor()
    if delay_ms is None:
        delay_ms = get_full_sync_delay_ms()
    delay_seconds = max(0.0, float(delay_ms) / 1000.0)
    c.execute("SELECT COUNT(*) FROM channels WHERE local_only = 0")
    total_items = c.fetchone()[0]
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _update_sync_progress(in_progress=True, progress_percent=0 if total_items else 100,
                          completed_items=0, total_items=total_items, remaining_items=total_items,
                          current_phase='syncing_channels', target_nodes=[str(n) for n in bbs_nodes],
                          started_at=now_str, last_updated_at=now_str, last_result='Running (P3: channels)')
    channels_synced = 0
    try:
        c.execute("SELECT name, url FROM channels WHERE local_only = 0")
        for name, url in c.fetchall():
            send_channel_to_bbs_nodes(name, url, bbs_nodes, interface)
            channels_synced += 1
            pct = int((channels_synced * 100) / total_items) if total_items else 100
            _update_sync_progress(progress_percent=pct, completed_items=channels_synced,
                                  remaining_items=max(total_items - channels_synced, 0),
                                  current_phase='syncing_channels',
                                  last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            time.sleep(delay_seconds)
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
        delay_ms = get_full_sync_delay_ms()
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
            time.sleep(delay_seconds)
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
        delay_ms = get_full_sync_delay_ms()
    delay_seconds = max(0.0, float(delay_ms) / 1000.0)
    total_messages = 0

    c.execute("SELECT COUNT(*) FROM game_scores")
    game_score_total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM zork_saves")
    zork_total = c.fetchone()[0]
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
        _update_sync_progress(current_phase='syncing_game_scores')
        c.execute("SELECT user_id, game_id, short_name, score, max_score, moves, achieved_at FROM game_scores")
        game_scores_synced = 0
        for user_id, game_id, short_name, score, max_score, moves, achieved_at in c.fetchall():
            send_game_score_to_bbs_nodes(user_id, game_id, short_name, score, max_score, moves, achieved_at,
                                         bbs_nodes, interface)
            game_scores_synced += 1
            total_messages += 1
            _progress_tick('syncing_game_scores')
            time.sleep(delay_seconds)
        logging.info(f"Game sync: Sent {game_scores_synced} game scores to {len(bbs_nodes)} peer(s)")

        _update_sync_progress(current_phase='syncing_zork_saves')
        c.execute("SELECT user_id, game_id, save_data, updated_at FROM zork_saves")
        zork_saves_synced = 0
        for user_id, game_id, save_data, updated_at in c.fetchall():
            send_zork_save_to_bbs_nodes(user_id, game_id, save_data, updated_at, bbs_nodes, interface)
            zork_saves_synced += 1
            total_messages += 1
            _progress_tick('syncing_zork_saves')
            time.sleep(delay_seconds)
        logging.info(f"Game sync: Sent {zork_saves_synced} zork saves to {len(bbs_nodes)} peer(s)")

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
