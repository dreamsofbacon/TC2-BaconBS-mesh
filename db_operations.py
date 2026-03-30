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
    send_mail_to_bbs_nodes, send_message, send_channel_to_bbs_nodes
)


thread_local = threading.local()


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
    conn.commit()
    print("Database schema initialized.")

def add_channel(name, url, bbs_nodes=None, interface=None):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO channels (name, url) VALUES (?, ?)", (name, url))
    conn.commit()

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
        return {'bulletins_synced': 0, 'mail_synced': 0, 'channels_synced': 0, 'total_messages': 0}
    
    conn = get_db_connection()
    c = conn.cursor()
    delay_seconds = delay_ms / 1000.0
    total_messages = 0
    
    try:
        # Sync all bulletins
        c.execute("SELECT board, sender_short_name, subject, content, unique_id FROM bulletins")
        bulletins = c.fetchall()
        bulletins_synced = 0
        
        for board, sender_short_name, subject, content, unique_id in bulletins:
            send_bulletin_to_bbs_nodes(board, sender_short_name, subject, content, unique_id, bbs_nodes, interface)
            bulletins_synced += 1
            total_messages += 1
            time.sleep(delay_seconds)
        
        logging.info(f"Database sync: Sent {bulletins_synced} bulletins to {len(bbs_nodes)} peer(s)")
        
        # Sync all mail
        c.execute("SELECT sender, sender_short_name, recipient, subject, content, unique_id FROM mail")
        mail_messages = c.fetchall()
        mail_synced = 0
        
        for sender_id, sender_short_name, recipient_id, subject, content, unique_id in mail_messages:
            send_mail_to_bbs_nodes(sender_id, sender_short_name, recipient_id, subject, content, unique_id, bbs_nodes, interface)
            mail_synced += 1
            total_messages += 1
            time.sleep(delay_seconds)
        
        logging.info(f"Database sync: Sent {mail_synced} mail messages to {len(bbs_nodes)} peer(s)")
        
        # Sync all channels
        c.execute("SELECT name, url FROM channels")
        channels = c.fetchall()
        channels_synced = 0
        
        for name, url in channels:
            send_channel_to_bbs_nodes(name, url, bbs_nodes, interface)
            channels_synced += 1
            total_messages += 1
            time.sleep(delay_seconds)
        
        logging.info(f"Database sync: Sent {channels_synced} channels to {len(bbs_nodes)} peer(s)")
        
        result = {
            'bulletins_synced': bulletins_synced,
            'mail_synced': mail_synced,
            'channels_synced': channels_synced,
            'total_messages': total_messages
        }
        logging.info(f"Database sync complete: {total_messages} total messages sent with {delay_ms}ms delays")
        return result
    
    except Exception as e:
        logging.error(f"Error during full database sync: {e}")
        raise
