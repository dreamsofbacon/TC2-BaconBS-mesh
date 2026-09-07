import sqlite3

con = sqlite3.connect('bulletins.db')
cur = con.cursor()

tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("Tables:", [t[0] for t in tables])

# Look for account-related tables
for t in tables:
    name = t[0]
    if 'account' in name.lower() or 'user' in name.lower() or 'ssh' in name.lower() or 'auth' in name.lower():
        cols = cur.execute(f"PRAGMA table_info({name})").fetchall()
        print(f"\n{name}: {[c[1] for c in cols]}")
        rows = cur.execute(f"SELECT * FROM {name} LIMIT 10").fetchall()
        for r in rows:
            # Don't print password hashes
            display = []
            for i, val in enumerate(r):
                col_name = cols[i][1]
                if 'hash' in col_name.lower() or 'salt' in col_name.lower() or 'password' in col_name.lower():
                    display.append("***")
                else:
                    display.append(val)
            print(f"  {display}")

con.close()
