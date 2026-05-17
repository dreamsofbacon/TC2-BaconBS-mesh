import sqlite3

db_path = "bulletins.db"

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check columns for sync_transmissions
    print("Schema for sync_transmissions:")
    cursor.execute("PRAGMA table_info(sync_transmissions)")
    cols = [c[1] for c in cursor.fetchall()]
    print(cols)

    # Keywords in last 100 sync_transmissions
    print("\nKeywords in last 100 sync_transmissions:")
    # Using the first text-like column if 'content' is missing, let's look at schema output first.
    # But I'll try to guess based on 'cols' in the script if I run it again.
    # For now, let's just get the rows and print them if short or search them.
    cursor.execute("SELECT * FROM sync_transmissions ORDER BY rowid DESC LIMIT 100")
    rows = cursor.fetchall()
    keywords = ["SYNCSTATE", "HASHREQ", "HASHREC", "HASHMISS"]
    found = {k: 0 for k in keywords}
    for r in rows:
        row_str = " ".join(map(str, r))
        for k in keywords:
            if k in row_str:
                found[k] += 1
    print(found)

    conn.close()
except Exception as e:
    print(f"Error: {e}")
