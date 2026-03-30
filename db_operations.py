import configparser
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from typing import Optional

from meshtastic import BROADCAST_NUM

from utils import (
    send_bulletin_to_bbs_nodes,
    send_delete_bulletin_to_bbs_nodes,
    send_delete_mail_to_bbs_nodes,
    send_mail_to_bbs_nodes, send_message, send_channel_to_bbs_nodes,
    send_sync_state_to_bbs_nodes,
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


def _update_sync_progress(**kwargs) -> None:
    with _sync_progress_lock:
        _sync_progress.update(kwargs)


def get_sync_progress() -> dict:
    with _sync_progress_lock:
        return dict(_sync_progress)


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
        cfg.read('config.ini')
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
        thread_local.connection = sqlite3.connect('bulletins.db')
    return thread_local.connection

def initialize_database():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS bulletins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    board TEXT NOT NULL,
                    sender_short_name TEXT NOT NULL,
                    date TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    content TEXT NOT NULL,
                    unique_id TEXT NOT NULL
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS mail (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    sender_short_name TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    date TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    content TEXT NOT NULL,
                    unique_id TEXT NOT NULL
                );''')
    c.execute('''CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL
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
                    reported_at TEXT NOT NULL
                );''')
    _dedupe_channels_and_create_unique_index(c)
    conn.commit()
    print("Database schema initialized.")


def get_local_record_counts() -> dict:
    """Return local record counts used by SYNCSTATE comparisons."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM bulletins")
    bulletins = int(c.fetchone()[0])
    c.execute("SELECT COUNT(*) FROM mail")
    mail = int(c.fetchone()[0])
    c.execute("SELECT COUNT(*) FROM channels")
    channels = int(c.fetchone()[0])
    _ensure_zork_saves_table()
    c.execute("SELECT COUNT(*) FROM zork_saves")
    zork_saves = int(c.fetchone()[0])
    return {
        'bulletins': bulletins,
        'mail': mail,
        'channels': channels,
        'zork_saves': zork_saves,
    }


def upsert_peer_sync_state(peer_node_id: str, bulletins: int, mail: int, channels: int, zork_saves: int) -> None:
    """Store the latest advertised SYNCSTATE counts for a peer node."""
    if not peer_node_id:
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        '''INSERT INTO peer_sync_state (peer_node_id, bulletins, mail, channels, zork_saves, reported_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(peer_node_id) DO UPDATE SET
               bulletins=excluded.bulletins,
               mail=excluded.mail,
               channels=excluded.channels,
               zork_saves=excluded.zork_saves,
               reported_at=excluded.reported_at''',
        (
            peer_node_id,
            int(bulletins),
            int(mail),
            int(channels),
            int(zork_saves),
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        ),
    )
    conn.commit()


def get_peer_sync_states() -> list:
    """Return peer-advertised record counts for diagnostics."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT peer_node_id, bulletins, mail, channels, zork_saves, reported_at FROM peer_sync_state ORDER BY peer_node_id"
    )
    return c.fetchall()


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


def add_channel(name, url, bbs_nodes=None, interface=None):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO channels (name, url) VALUES (?, ?)", (name, url))
    conn.commit()

    if c.rowcount == 0:
        logging.info(f"Duplicate channel ignored (name={name}, url={url})")
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



def add_bulletin(board, sender_short_name, subject, content, bbs_nodes, interface, unique_id=None):
    conn = get_db_connection()
    c = conn.cursor()
    date = datetime.now().strftime('%Y-%m-%d %H:%M')
    if not unique_id:
        unique_id = str(uuid.uuid4())
    else:
        # Idempotency for sync replays
        c.execute("SELECT 1 FROM bulletins WHERE unique_id = ? LIMIT 1", (unique_id,))
        if c.fetchone():
            return unique_id
    c.execute(
        "INSERT INTO bulletins (board, sender_short_name, date, subject, content, unique_id) VALUES (?, ?, ?, ?, ?, ?)",
        (board, sender_short_name, date, subject, content, unique_id))
    conn.commit()
    if bbs_nodes and interface:
        send_bulletin_to_bbs_nodes(board, sender_short_name, subject, content, unique_id, bbs_nodes, interface)

    # New logic to send group chat notification for urgent bulletins
    if board.lower() == "urgent":
        notification_message = f"💥NEW URGENT BULLETIN💥\nFrom: {sender_short_name}\nTitle: {subject}\nDM 'CB,,Urgent' to view"
        send_message(notification_message, BROADCAST_NUM, interface)

    return unique_id


def get_bulletins(board):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, subject, sender_short_name, date, unique_id FROM bulletins WHERE board = ? COLLATE NOCASE", (board,))
    return c.fetchall()

def get_bulletin_content(bulletin_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT sender_short_name, date, subject, content, unique_id FROM bulletins WHERE id = ?", (bulletin_id,))
    return c.fetchone()


def delete_bulletin(unique_id, bbs_nodes, interface):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM bulletins WHERE unique_id = ?", (unique_id,))
    conn.commit()
    send_delete_bulletin_to_bbs_nodes(unique_id, bbs_nodes, interface)


def append_bulletin_content(unique_id: str, char_offset: Optional[int], additional_content: str) -> None:
    """Append a continuation chunk to an existing bulletin's content.

    char_offset is the expected current length of the stored content before
    this chunk is applied.  Only appends when length(content) == char_offset,
    making the call idempotent: duplicate or re-synced packets are silent no-ops.
    Pass None to skip the offset guard (legacy/test usage).
    """
    conn = get_db_connection()
    c = conn.cursor()
    if char_offset is not None:
        c.execute(
            "UPDATE bulletins SET content = content || ? WHERE unique_id = ? AND length(content) = ?",
            (additional_content, unique_id, char_offset),
        )
    else:
        c.execute(
            "UPDATE bulletins SET content = content || ? WHERE unique_id = ?",
            (additional_content, unique_id),
        )
    if c.rowcount > 0:
        conn.commit()
        logging.info(f"Appended continuation content to bulletin unique_id={unique_id}")
    else:
        logging.warning(f"BULLETINCONT received for unknown unique_id={unique_id}; ignored")

def add_mail(sender_id, sender_short_name, recipient_id, subject, content, bbs_nodes, interface, unique_id=None):
    conn = get_db_connection()
    c = conn.cursor()
    date = datetime.now().strftime('%Y-%m-%d %H:%M')
    if not unique_id:
        unique_id = str(uuid.uuid4())
    else:
        # Idempotency for sync replays
        c.execute("SELECT 1 FROM mail WHERE unique_id = ? LIMIT 1", (unique_id,))
        if c.fetchone():
            return unique_id
    c.execute("INSERT INTO mail (sender, sender_short_name, recipient, date, subject, content, unique_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (sender_id, sender_short_name, recipient_id, date, subject, content, unique_id))
    conn.commit()
    if bbs_nodes and interface:
        send_mail_to_bbs_nodes(sender_id, sender_short_name, recipient_id, subject, content, unique_id, bbs_nodes, interface)
    return unique_id

def get_mail(recipient_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, sender_short_name, subject, date, unique_id FROM mail WHERE recipient = ?", (recipient_id,))
    return c.fetchall()

def get_mail_content(mail_id, recipient_id):
    # TODO: ensure only recipient can read mail
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT sender_short_name, date, subject, content, unique_id FROM mail WHERE id = ? and recipient = ?", (mail_id, recipient_id,))
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
    conn = get_db_connection()
    c = conn.cursor()
    if char_offset is not None:
        c.execute(
            "UPDATE mail SET content = content || ? WHERE unique_id = ? AND length(content) = ?",
            (additional_content, unique_id, char_offset),
        )
    else:
        c.execute(
            "UPDATE mail SET content = content || ? WHERE unique_id = ?",
            (additional_content, unique_id),
        )
    if c.rowcount > 0:
        conn.commit()
        logging.info(f"Appended continuation content to mail unique_id={unique_id}")
    else:
        logging.warning(f"MAILCONT received for unknown unique_id={unique_id}; ignored")


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
             short_name   = excluded.short_name,
             score        = CASE WHEN excluded.score > score THEN excluded.score ELSE score END,
             max_score    = CASE WHEN excluded.score > score THEN excluded.max_score ELSE max_score END,
             moves        = CASE WHEN excluded.score > score THEN excluded.moves ELSE moves END,
             achieved_at  = CASE WHEN excluded.score > score THEN excluded.achieved_at ELSE achieved_at END''',
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


def sync_full_database_to_nodes(bbs_nodes: list, interface, delay_ms: int = 500) -> dict:
    """
    Sync all existing bulletins, mail, and channels to specified BBS nodes.
    
    This function performs a full database sync to new or rejoining BBS peers without spamming.
    It includes rate limiting between messages to keep network traffic manageable.
    
    Args:
        bbs_nodes: List of target BBS node IDs to sync to
        interface: Meshtastic interface object for sending messages
        delay_ms: Milliseconds to delay between each sync message (default 500ms)
    
    Returns:
        dict: Summary with keys 'bulletins_synced', 'mail_synced', 'channels_synced', 'total_messages'
    
    Example:
        result = sync_full_database_to_nodes([123456, 789012], interface, delay_ms=500)
        print(f"Synced {result['bulletins_synced']} bulletins to {len(bbs_nodes)} nodes")
    """
    if not bbs_nodes or not interface:
        logging.warning("sync_full_database_to_nodes: No bbs_nodes or interface provided")
        _update_sync_progress(
            in_progress=False,
            progress_percent=100,
            completed_items=0,
            total_items=0,
            remaining_items=0,
            current_phase='idle',
            target_nodes=[],
            last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            last_result='No nodes or interface provided',
        )
        return {'bulletins_synced': 0, 'mail_synced': 0, 'channels_synced': 0, 'total_messages': 0}
    
    conn = get_db_connection()
    c = conn.cursor()
    delay_seconds = delay_ms / 1000.0
    total_messages = 0

    c.execute("SELECT COUNT(*) FROM bulletins")
    bulletin_total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM mail")
    mail_total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM channels")
    channel_total = c.fetchone()[0]
    total_items = bulletin_total + mail_total + channel_total

    started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _update_sync_progress(
        in_progress=True,
        progress_percent=0 if total_items > 0 else 100,
        completed_items=0,
        total_items=total_items,
        remaining_items=total_items,
        current_phase='starting',
        target_nodes=[str(node) for node in bbs_nodes],
        started_at=started_at,
        last_updated_at=started_at,
        last_result='Running',
    )

    completed_items = 0

    def _progress_tick(current_phase: str) -> None:
        nonlocal completed_items
        completed_items += 1
        if total_items > 0:
            progress_percent = int((completed_items * 100) / total_items)
        else:
            progress_percent = 100
        _update_sync_progress(
            progress_percent=progress_percent,
            completed_items=completed_items,
            remaining_items=max(total_items - completed_items, 0),
            current_phase=current_phase,
            last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )
    
    try:
        # Sync all bulletins
        _update_sync_progress(current_phase='syncing_bulletins')
        c.execute("SELECT board, sender_short_name, subject, content, unique_id FROM bulletins")
        bulletins = c.fetchall()
        bulletins_synced = 0
        
        for board, sender_short_name, subject, content, unique_id in bulletins:
            send_bulletin_to_bbs_nodes(board, sender_short_name, subject, content, unique_id, bbs_nodes, interface)
            bulletins_synced += 1
            total_messages += 1
            _progress_tick('syncing_bulletins')
            time.sleep(delay_seconds)
        
        logging.info(f"Database sync: Sent {bulletins_synced} bulletins to {len(bbs_nodes)} peer(s)")
        
        # Sync all mail
        _update_sync_progress(current_phase='syncing_mail')
        c.execute("SELECT sender, sender_short_name, recipient, subject, content, unique_id FROM mail")
        mail_messages = c.fetchall()
        mail_synced = 0
        
        for sender_id, sender_short_name, recipient_id, subject, content, unique_id in mail_messages:
            send_mail_to_bbs_nodes(sender_id, sender_short_name, recipient_id, subject, content, unique_id, bbs_nodes, interface)
            mail_synced += 1
            total_messages += 1
            _progress_tick('syncing_mail')
            time.sleep(delay_seconds)
        
        logging.info(f"Database sync: Sent {mail_synced} mail messages to {len(bbs_nodes)} peer(s)")
        
        # Sync all channels
        _update_sync_progress(current_phase='syncing_channels')
        c.execute("SELECT name, url FROM channels")
        channels = c.fetchall()
        channels_synced = 0
        
        for name, url in channels:
            send_channel_to_bbs_nodes(name, url, bbs_nodes, interface)
            channels_synced += 1
            total_messages += 1
            _progress_tick('syncing_channels')
            time.sleep(delay_seconds)
        
        logging.info(f"Database sync: Sent {channels_synced} channels to {len(bbs_nodes)} peer(s)")

        # Send a lightweight consistency check payload so peers can compare
        # their local counts against ours and detect missing records.
        local_counts = get_local_record_counts()
        send_sync_state_to_bbs_nodes(local_counts, bbs_nodes, interface)
        
        result = {
            'bulletins_synced': bulletins_synced,
            'mail_synced': mail_synced,
            'channels_synced': channels_synced,
            'total_messages': total_messages
        }
        logging.info(f"Database sync complete: {total_messages} total messages sent with {delay_ms}ms delays")
        _update_sync_progress(
            in_progress=False,
            progress_percent=100,
            completed_items=total_items,
            total_items=total_items,
            remaining_items=0,
            current_phase='idle',
            last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            last_result=f"Completed: {total_messages} messages sent",
        )
        return result
    
    except Exception as e:
        logging.error(f"Error during full database sync: {e}")
        _update_sync_progress(
            in_progress=False,
            current_phase='error',
            last_updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            last_result=f"Error: {e}",
        )
        raise
