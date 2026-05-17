import sqlite3
import json

db_path = "bulletins.db"
tables = ["bulletins", "mail", "channels", "channel_comments", "zork_saves", "user_profiles", "game_scores", "connection_events", "peer_sync_state", "deleted_sync_tombstones", "sync_transmissions"]

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 3) Row counts
    for t in tables:
        try:
            cursor.execute(f"SELECT count(*) FROM {t}")
            print(f"{t} row count: {cursor.fetchone()[0]}")
        except sqlite3.OperationalError as e:
            print(f"{t} table error: {e}")
            
    # 4) Recent peer_sync_state
    print("\nRecent 10 peer_sync_state rows:")
    try:
        # Assuming common column names, adjusting based on common mesh patterns if needed
        # Request asks for peer id, counts, reported_at
        # Check columns first
        cursor.execute("PRAGMA table_info(peer_sync_state)")
        cols = [c[1] for c in cursor.fetchall()]
        print(f"Columns: {cols}")
        
        # We'll try to select peer_id and others based on request
        # Common names: peer_id, message_count, bulletins_count, last_seen/reported_at
        cursor.execute("SELECT * FROM peer_sync_state ORDER BY rowid DESC LIMIT 10")
        rows = cursor.fetchall()
        for r in rows:
            print(r)
    except Exception as e:
        print(f"Error reading peer_sync_state: {e}")

    # 5) Recent sync transmissions for keywords
    print("\nKeywords in last 100 sync_transmissions:")
    try:
        cursor.execute("SELECT content FROM sync_transmissions ORDER BY rowid DESC LIMIT 100")
        rows = cursor.fetchall()
        keywords = ["SYNCSTATE", "HASHREQ", "HASHREC", "HASHMISS"]
        found = {k: 0 for k in keywords}
        for r in rows:
            content = str(r[0])
            for k in keywords:
                if k in content:
                    found[k] += 1
        print(found)
    except Exception as e:
        print(f"Error reading sync_transmissions: {e}")

    conn.close()
except Exception as e:
    print(f"General error: {e}")
